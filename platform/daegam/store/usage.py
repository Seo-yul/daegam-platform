"""LLM 호출의 원장과 그 곁의 컬렉션 (P37 7절·10절, D62).

- `usage_records`: 호출 하나가 한 행이다. `_id` 는 호출 id(`<trace_id>-<span_id>`)라 두 쓰는 쪽이
  같은 호출에 같은 `_id` 를 쓰고, 먼저 들어간 행이 늘 남는다
- `usage_joins`: 게이트웨이를 거친 호출을 부른 쪽이 남기는 잇기 문서. 게이트웨이 기록이 오면
  그 행에 턴·세션·사람을 채운다. 7일 TTL
- `usage_anomalies`: 원장 건강 사건(`writer_conflicts`·`gateway_extra_records`·`join_mismatch`).
  진 쪽 기록은 남지 않으므로 사건마다 한 번 적는다. 30일 TTL
- `usage_dirty_hours`: 롤업이 다시 셀 시간. 지난 시간의 행이 들어오면 그 시간을 적는다
- `usage_hourly`: 롤업이 시간마다 원장에서 다시 센 묶음. 사용량 화면과 비용 칸은 이것만 읽는다
- `usage_state`: 다시 계산(reprice)의 요청과 커서, 원장 건강의 수, 롤업의 마지막 틱
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_partial_index,
    ensure_ttl_index,
    insert_new,
    utcnow,
)
from daegam.store.hour_marks import HourMarks, bucket_id, day_of, replace_hour

USAGE_RECORDS = "usage_records"
USAGE_JOINS = "usage_joins"
USAGE_ANOMALIES = "usage_anomalies"
USAGE_DIRTY_HOURS = "usage_dirty_hours"
USAGE_HOURLY = "usage_hourly"
USAGE_STATE = "usage_state"
STATE_DOC_ID = "state"
# 롤업이 시간 하나에 쓰는 묶음 열쇠. 원장 행의 칸 이름이다
BUCKET_KEY = {
    "tenant": "$tenant",
    "source_kind": "$source.kind",
    "source_id": "$source.id",
    "user_sub": "$user_sub",
    "provider_kind": "$provider.kind",
    "provider_name": "$provider.name",
    "model": "$model",
    "operation": "$operation",
    "route": "$route",
}

JOIN_TTL = timedelta(days=7)
ANOMALY_TTL = timedelta(days=30)

Writer = Literal["runtime", "platform", "gateway"]
Route = Literal["direct", "gateway"]
SourceKind = Literal[
    "agent", "assistant", "spec_generator", "knowledge_indexer", "gateway_instance"
]
Operation = Literal["chat", "embeddings", "web_search"]
# 원장 건강 사건의 종류. 0 이어야 하는 수다
AnomalyKind = Literal["writer_conflicts", "gateway_extra_records", "join_mismatch"]
ANOMALY_KINDS: tuple[AnomalyKind, ...] = get_args(AnomalyKind)


# 호출 하나의 수 상한. 그보다 큰 수는 틀린 기록이다
MAX_TOKENS = 10**12


class TokenUsage(BaseModel):
    """호출 하나의 토큰. input 은 cache 읽기·쓰기를, output 은 reasoning 을 품는다(langchain 과
    GenAI 의 정의). requests 는 web_search 의 요청 수다."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    output_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    cache_read_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    cache_write_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    reasoning_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    requests: int = Field(default=0, ge=0, le=MAX_TOKENS)


# 호출 하나의 수(`TokenUsage` 의 칸)와 시간 묶음이 더하는 수 전부. 보고도 이 이름들을 더한다
USAGE_FIELDS = tuple(TokenUsage.model_fields)
BUCKET_METRICS = (
    *USAGE_FIELDS,
    "calls",
    "errors",
    "cost_usd",
    "unpriced_calls",
    "unjoined_calls",
)


class UsageSource(BaseModel):
    """부른 쪽. agent 는 Agent id, 플랫폼 호출은 그 종류와 일의 id 다. 미리 보기의 호출은 원본
    Agent 의 것이고 preview 가 미리 보기의 id 다 (P44b 9절). 그래서 Agent 마다의 사용량과 일일
    예산이 원본으로 센다."""

    kind: SourceKind
    id: str | None = None
    name: str | None = None
    preview: str | None = None

    @model_serializer(mode="wrap")
    def _without_empty_preview(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """미리 보기가 아닌 행은 `preview` 칸을 두지 않는다. 저장된 모양이 이 칸 전과 같다."""
        data = handler(self)
        if data.get("preview") is None:
            data.pop("preview", None)
        return data


class UsageProvider(BaseModel):
    kind: str  # SDK 계열(openai, anthropic, stub) 또는 web_search
    name: str | None = None  # 등록된 provider 이름


class AppliedPrice(BaseModel):
    """행이 들어올 때 매긴 규칙과 그 값. 규칙이 나중에 바뀌어도 다시 계산(reprice) 전까지
    그대로다."""

    rule_id: str | None = None
    version: int = 0
    input: float = 0
    output: float = 0
    cache_read: float | None = None
    cache_write: float | None = None
    request: float | None = None


class UsageRecord(Document):
    """원장 한 행 = LLM 호출 하나."""

    id: str  # 호출 id. 우리 헤더 없는 게이트웨이 요청은 "gw:" id 다
    ts: UtcDatetime  # 호출이 끝난 때
    writer: Writer
    route: Route
    tenant: str
    source: UsageSource
    ref: str | None = None  # 일의 단위: 세션 id, spec job id, 문서 id
    turn_id: str | None = None
    user_sub: str | None = None
    trace_id: str | None = None
    joined: bool = True  # 게이트웨이 행이 부른 쪽의 잇기 정보를 받았는가
    provider: UsageProvider
    model: str  # 요청한 모델
    response_model: str | None = None
    operation: Operation = "chat"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    estimated: bool = False
    status: Literal["ok", "error"] = "ok"
    latency_ms: float | None = Field(default=None, ge=0)
    cost_usd: float | None = None  # 맞는 규칙이 없으면 None
    price: AppliedPrice | None = None
    # 같은 쓰는 쪽의 다시 보냄과 다른 기록을 가르는 id (이벤트 id, 게이트웨이 기록 id)
    record_id: str | None = None
    gateway: dict[str, Any] | None = None  # 게이트웨이 id, 기록 id


class UsageJoin(Document):
    """게이트웨이를 거친 호출을 부른 쪽이 남기는 잇기 문서 (7절)."""

    id: str  # 호출 id
    tenant: str
    source: UsageSource
    ref: str | None = None
    turn_id: str | None = None
    user_sub: str | None = None
    trace_id: str | None = None
    ts: UtcDatetime = Field(default_factory=utcnow)
    cancelled: bool = False  # 끊긴 호출. 게이트웨이가 끊긴 스트림을 기록하는지는 아직 모른다
    failed: bool = False  # 실패한 호출. 게이트웨이에 닿기 전에 실패했으면 기록이 없다
    resolved: bool = False  # 게이트웨이 행이 이것을 이었는가


class MongoUsageStore:
    def __init__(self, db: AsyncDatabase, *, retention_days: int = 400):
        self.db = db
        self.records = db[USAGE_RECORDS]
        self.joins = db[USAGE_JOINS]
        self.anomalies = db[USAGE_ANOMALIES]
        self.marks = HourMarks(db[USAGE_DIRTY_HOURS])
        self.hourly = db[USAGE_HOURLY]
        self.state = db[USAGE_STATE]
        self.retention = timedelta(days=retention_days)

    async def init_schema(self) -> None:
        r = self.records
        # 롤업의 시간 하나 세기와 다시 계산의 (ts, _id) 커서가 쓴다
        await r.create_index([("ts", ASCENDING), ("_id", ASCENDING)], name="ix_ts_id")
        await r.create_index([("tenant", ASCENDING), ("ts", DESCENDING)], name="ix_tenant_ts")
        await r.create_index([("source.id", ASCENDING), ("ts", DESCENDING)], name="ix_source_ts")
        await r.create_index([("source.id", ASCENDING), ("ref", ASCENDING)], name="ix_source_ref")
        # 같음 조건이 $exists 를 품으므로 쿼리가 이 부분 인덱스를 쓴다($type 조건은 쓰지 못했다)
        await ensure_partial_index(r, "turn_id", name="ix_turn_set")
        await ensure_partial_index(r, "trace_id", name="ix_trace_set")
        await ensure_ttl_index(r, "ts", int(self.retention.total_seconds()), "ttl_ts")
        await self.joins.create_index(
            [("resolved", ASCENDING), ("ts", ASCENDING)],
            name="ix_unresolved",
            partialFilterExpression={"resolved": False},
        )
        await ensure_ttl_index(self.joins, "ts", int(JOIN_TTL.total_seconds()), "ttl_ts")
        await self.anomalies.create_index(
            [("kind", ASCENDING), ("ts", DESCENDING)], name="ix_kind_ts"
        )
        await ensure_ttl_index(self.anomalies, "ts", int(ANOMALY_TTL.total_seconds()), "ttl_ts")
        h = self.hourly
        await h.create_index([("tenant", ASCENDING), ("hour", DESCENDING)], name="ix_tenant_hour")
        await h.create_index(
            [("source_id", ASCENDING), ("hour", DESCENDING)], name="ix_source_hour"
        )
        await ensure_ttl_index(h, "hour", int(self.retention.total_seconds()), "ttl_hour")

    # -- 원장 ----------------------------------------------------------------------------

    async def insert_record(self, row: UsageRecord) -> bool:
        """새 행이면 True. 같은 `_id` 가 이미 있으면 False 이고 먼저 들어간 행이 남는다."""
        return await insert_new(self.records, [row.to_doc()]) == 1

    async def replace_record(self, row: UsageRecord) -> None:
        """같은 `_id` 의 행을 바꾼다. 게이트웨이 행이 먼저 든 런타임 행을 대신할 때만 쓴다."""
        await self.records.replace_one({"_id": row.id}, row.to_doc())

    async def get_record(self, record_id: str) -> UsageRecord | None:
        doc = await self.records.find_one({"_id": record_id})
        return None if doc is None else UsageRecord.from_doc(doc)

    async def join_record(self, call_id: str, join: UsageJoin) -> bool:
        """아직 잇지 못한 게이트웨이 행에 잇기 정보를 채운다. 채웠으면 True 다."""
        res = await self.records.update_one(
            {"_id": call_id, "joined": False, "tenant": join.tenant},
            {
                "$set": {
                    "joined": True,
                    "source": join.source.model_dump(),
                    "ref": join.ref,
                    "turn_id": join.turn_id,
                    "user_sub": join.user_sub,
                    "trace_id": join.trace_id,
                }
            },
        )
        return res.modified_count == 1

    # -- 잇기 문서 ------------------------------------------------------------------------

    async def insert_join(self, join: UsageJoin) -> bool:
        return await insert_new(self.joins, [join.to_doc()]) == 1

    async def get_join(self, call_id: str) -> UsageJoin | None:
        doc = await self.joins.find_one({"_id": call_id})
        return None if doc is None else UsageJoin.from_doc(doc)

    async def resolve_join(self, call_id: str) -> None:
        await self.joins.update_one({"_id": call_id}, {"$set": {"resolved": True}})

    # -- 원장 건강 -------------------------------------------------------------------------

    async def add_anomaly(self, kind: AnomalyKind, call_id: str, record_id: str | None) -> None:
        """사건 하나를 한 번 적는다. 같은 (종류, 호출, 기록)이 다시 오면 무시한다."""
        doc = {
            "_id": f"{kind}:{call_id}:{record_id or ''}",
            "kind": kind,
            "call_id": call_id,
            "record_id": record_id,
            "ts": utcnow(),
        }
        await insert_new(self.anomalies, [doc])

    # -- 시간 묶음 --------------------------------------------------------------------------

    async def hour_buckets(self, hour: datetime) -> list[dict[str, Any]]:
        """시간 하나의 묶음을 원장 행에서 센다. 쓰지는 않는다(`replace_hour`). 같은 입력이면 몇 번을
        세어도 결과가 같다."""
        pipeline: list[dict[str, Any]] = [
            {"$match": {"ts": {"$gte": hour, "$lt": hour + timedelta(hours=1)}}},
            {"$sort": {"ts": ASCENDING}},
            {"$group": _bucket_group()},
        ]
        return [_bucket_doc(hour, g) async for g in await self.records.aggregate(pipeline)]

    async def replace_hour(self, hour: datetime, buckets: list[dict[str, Any]]) -> None:
        """그 시간의 묶음을 buckets 로 바꾸고 사라진 열쇠는 지운다."""
        await replace_hour(self.hourly, hour, buckets)

    async def tokens_used(self, match: dict[str, Any]) -> int:
        """match 가 고른 원장 행의 입력과 출력 토큰 합이다."""
        (total,) = await self.record_sums(match) or [{}]
        return int(total.get("input_tokens") or 0) + int(total.get("output_tokens") or 0)

    async def record_sums(
        self, match: dict[str, Any], key: str | None = None
    ) -> list[dict[str, Any]]:
        """원장 행을 key 칸마다 더한다. 묶음과 같은 수(`BUCKET_METRICS`)이고 `_id` 가 그 칸의
        값이다. 묶음은 시간 단위라 1시간 창의 지표와 세션·턴·trace 의 금액은 행을 읽는다."""
        group = {**_bucket_group(), "_id": None if key is None else f"${key}"}
        pipeline = [{"$match": match}, {"$group": group}]
        return [g async for g in await self.records.aggregate(pipeline)]

    async def trace_records(self, tenant: str, trace_id: str, limit: int) -> list[UsageRecord]:
        """한 trace 의 원장 행. 게이트웨이 행은 게이트웨이의 토큰과 우리 가격이다."""
        cursor = self.records.find({"tenant": tenant, "trace_id": trace_id}).limit(limit)
        return [UsageRecord.from_doc(d) async for d in cursor]

    async def bucket_groups(
        self, match: dict[str, Any], key: dict[str, Any], *, day_tz: str | None
    ) -> list[dict[str, Any]]:
        """묶음을 (날, key)로 더한다. day_tz 가 있으면 그 시간대의 날로 자른다 (10절)."""
        group_id: dict[str, Any] = dict(key)
        if day_tz is not None:
            group_id["day"] = day_of(day_tz)
        sums = {f: {"$sum": f"${f}"} for f in BUCKET_METRICS}
        sums["source_name"] = {"$last": "$source_name"}
        pipeline = [
            {"$match": match},
            {"$sort": {"hour": ASCENDING}},
            {"$group": {"_id": group_id, **sums}},
        ]
        return [g async for g in await self.hourly.aggregate(pipeline)]

    # -- 다시 계산 ---------------------------------------------------------------------------

    async def rows_after(
        self, since: datetime, until: datetime, after: tuple[datetime, str] | None, limit: int
    ) -> list[UsageRecord]:
        """기간 안의 행을 (ts, _id) 순서로 after 다음부터 limit 개."""
        query: dict[str, Any] = {"ts": {"$gte": since, "$lt": until}}
        if after is not None:
            ts, row_id = after
            query = {
                "$and": [
                    query,
                    {"$or": [{"ts": {"$gt": ts}}, {"ts": ts, "_id": {"$gt": row_id}}]},
                ]
            }
        cursor = self.records.find(query).sort([("ts", ASCENDING), ("_id", ASCENDING)]).limit(limit)
        return [UsageRecord.from_doc(d) async for d in cursor]

    async def count_rows(self, since: datetime, until: datetime) -> int:
        return await self.records.count_documents({"ts": {"$gte": since, "$lt": until}})

    async def set_price(self, row_id: str, cost: float | None, price: AppliedPrice | None) -> None:
        await self.records.update_one(
            {"_id": row_id},
            {"$set": {"cost_usd": cost, "price": None if price is None else price.model_dump()}},
        )

    # -- 상태와 건강 -------------------------------------------------------------------------

    async def get_state(self) -> dict[str, Any]:
        return await self.state.find_one({"_id": STATE_DOC_ID}) or {"_id": STATE_DOC_ID}

    async def set_state(self, fields: dict[str, Any]) -> None:
        await self.state.update_one({"_id": STATE_DOC_ID}, {"$set": fields}, upsert=True)

    async def start_reprice(self, reprice: dict[str, Any]) -> bool:
        """다시 계산 요청을 둔다. 끝나지 않은 요청이 있으면 두지 않고 False 다."""
        idle = {"$or": [{"reprice": None}, {"reprice.finished_at": {"$ne": None}}]}
        try:
            await self.state.update_one(
                {"_id": STATE_DOC_ID, **idle}, {"$set": {"reprice": reprice}}, upsert=True
            )
        except DuplicateKeyError:
            # 문서는 있는데 조건이 맞지 않아 upsert 가 같은 _id 로 넣으려 했다
            return False
        return True

    async def save_reprice(self, reprice: dict[str, Any]) -> bool:
        """다시 계산의 진행을 적는다. 같은 요청(id)일 때만 맞는다."""
        res = await self.state.update_one(
            {"_id": STATE_DOC_ID, "reprice.id": reprice["id"]}, {"$set": {"reprice": reprice}}
        )
        return res.matched_count == 1

    async def gateway_missing(self, before: datetime) -> dict[str, int]:
        """게이트웨이 기록이 오지 않은 잇기 문서. 끊긴 호출과 실패한 호출은 따로 센다."""
        base = {"resolved": False, "ts": {"$lt": before}}
        missing = await self.joins.count_documents(
            {**base, "cancelled": False, "failed": {"$ne": True}}
        )
        cut = await self.joins.count_documents({**base, "cancelled": True})
        failed = await self.joins.count_documents({**base, "cancelled": False, "failed": True})
        return {
            "gateway_missing": missing,
            "gateway_missing_cancelled": cut,
            "gateway_missing_failed": failed,
        }

    async def anomaly_counts(self, since: datetime) -> dict[str, int]:
        pipeline = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {"_id": "$kind", "n": {"$sum": 1}}},
        ]
        return {g["_id"]: g["n"] async for g in await self.anomalies.aggregate(pipeline)}

    async def collection_sizes(self) -> dict[str, int]:
        """원장 컬렉션 넷의 저장 크기(바이트). `$collStats` 다 (16절)."""
        out: dict[str, int] = {}
        for coll in (self.records, self.hourly, self.joins, self.anomalies):
            stats = [s async for s in await coll.aggregate([{"$collStats": {"storageStats": {}}}])]
            out[coll.name] = int(stats[0]["storageStats"].get("size", 0)) if stats else 0
        return out


def agent_records(tenant: str, agent_id: str) -> dict[str, Any]:
    """한 Agent 의 원장 행을 고르는 조건이다. 시간 묶음은 `agent_buckets` 다."""
    return {"tenant": tenant, "source.kind": "agent", "source.id": agent_id}


def agent_buckets(tenant: str, agent_id: str) -> dict[str, Any]:
    """한 Agent 의 시간 묶음을 고르는 조건이다."""
    return {"tenant": tenant, "source_kind": "agent", "source_id": agent_id}


def _bucket_group() -> dict[str, Any]:
    sums = {f: {"$sum": f"$usage.{f}"} for f in USAGE_FIELDS}
    return {
        "_id": BUCKET_KEY,
        "calls": {"$sum": 1},
        "errors": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}},
        **sums,
        "cost_usd": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
        "unpriced_calls": {"$sum": {"$cond": [{"$eq": [{"$type": "$cost_usd"}, "null"]}, 1, 0]}},
        "unjoined_calls": {"$sum": {"$cond": ["$joined", 0, 1]}},
        "source_name": {"$last": "$source.name"},
    }


def _bucket_doc(hour: datetime, group: dict[str, Any]) -> dict[str, Any]:
    key = group["_id"]
    doc = {k: v for k, v in group.items() if k != "_id"}
    return {"_id": bucket_id(hour, (key.get(k) for k in BUCKET_KEY)), "hour": hour, **key, **doc}
