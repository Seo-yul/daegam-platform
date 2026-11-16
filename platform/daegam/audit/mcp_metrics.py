"""Gateway·MCP Server 메트릭의 시간 묶음 (P51 3절, D90).

- `mcp_hourly`: 롤업이 시간마다 `mcp_audit_events` 의 호출 이벤트에서 다시 센 묶음. 열쇠는 (시간,
  테넌트, 문서, 프록시 종류, 유형, Tool, 백엔드)이고, 결과별 수와 고정 구간마다의 걸린 시간 수를
  담는다. 메트릭 API 는 이것만 읽는다
- `mcp_metrics_state`: 롤업의 마지막 틱, 묶음 판, backfill 커서, 바로 센 시간의 호출 이벤트 수

다시 셀 시간의 표시(`mcp_dirty_hours`)는 감사 저장소가 이벤트를 넣으며 적는다
(`MongoAuditStore.add_mcp_events`).

Tool 이름은 클라이언트가 보낸 것이라 아무 문자열이나 온다. (시간, 문서, 유형, 백엔드)마다 호출이
많은 200개만 이름을 남기고 나머지와 이름 없는 호출은 `tool: null`·`folded: true` 묶음 하나로 접는다.
접기는 집계 안에서 두 번 묶어 한다. 이름은 묶기 전에 128자로 자르고, 순위는 호출 수와 이름 순이라
같은 이벤트면 언제 다시 세도 같은 Tool 을 접는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.audit.store import MCP_AUDIT_EVENTS, MCP_CALL_TYPES, MCP_DIRTY_HOURS, MCP_TOOL_CALL
from daegam.store.base import ensure_ttl_index, facets
from daegam.store.hour_marks import HOUR, HourMarks, bucket_id, day_of, replace_hour

MCP_HOURLY = "mcp_hourly"
MCP_METRICS_STATE = "mcp_metrics_state"
STATE_DOC_ID = "state"
# 열쇠·칸·구간·접기 규칙이 바뀌면 올린다. 롤업은 새 판으로 묶음을 다시 만들고 API 는 지금 판만
# 읽는다
BUCKET_VERSION = 1
VMCP_KIND = "virtualmcpserver"
# ToolHive 의 결과 다섯과 그 밖의 값(없거나 새 값)
OUTCOMES: tuple[str, ...] = ("success", "application_error", "denied", "failure", "error")
UNKNOWN_OUTCOME = "unknown"
OUTCOME_CLASSES: tuple[str, ...] = (*OUTCOMES, UNKNOWN_OUTCOME)
# 걸린 시간 구간의 위 끝(ms, 포함). 백엔드 타임아웃은 300초까지다(`TIMEOUT_MAX_SECONDS`)
RANGE_EDGES: tuple[int, ...] = (
    5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 300000,
)  # fmt: skip
RANGE_FIELDS: tuple[str, ...] = (*(f"le_{e}" for e in RANGE_EDGES), f"gt_{RANGE_EDGES[-1]}")
KEY_FIELDS: tuple[str, ...] = ("tenant", "instance_id", "proxy_kind", "type", "tool", "backend")
KEPT_TOOLS = 200
NAME_CHARS = 128
DURATION = "$metadata.extra.duration_ms"


class MongoMcpMetricsStore:
    def __init__(self, db: AsyncDatabase, *, ttl_days: int = 90):
        self.db = db
        self.events = db[MCP_AUDIT_EVENTS]
        self.hourly = db[MCP_HOURLY]
        self.state = db[MCP_METRICS_STATE]
        self.marks = HourMarks(db[MCP_DIRTY_HOURS])
        # 묶음은 이벤트만큼 남는다. 롤업은 그 경계보다 오래된 시간을 다시 세지 않는다
        self.retention = timedelta(days=ttl_days)

    async def init_schema(self) -> None:
        h = self.hourly
        await h.create_index(
            [("instance_id", ASCENDING), ("hour", ASCENDING)], name="ix_instance_hour"
        )
        await ensure_ttl_index(h, "hour", int(self.retention.total_seconds()), "ttl_hour")

    # -- 롤업 --------------------------------------------------------------------------------

    async def count_calls(self, hour: datetime) -> int:
        """그 시간의 호출 이벤트 수. (type, ts) 인덱스만으로 센다. 문서 없는 이벤트도 든다.
        롤업이 바로 세는 시간이 바뀌었는지 보는 데만 쓴다."""
        return await self.events.count_documents(
            {"type": {"$in": list(MCP_CALL_TYPES)}, "ts": {"$gte": hour, "$lt": hour + HOUR}}
        )

    async def hour_buckets(self, hour: datetime) -> list[dict[str, Any]]:
        """시간 하나의 묶음을 이벤트에서 센다. 쓰지는 않는다(`replace_hour`)."""
        cursor = await self.events.aggregate(_hour_pipeline(hour), allowDiskUse=True)
        return [_bucket_doc(hour, g) async for g in cursor]

    async def replace_hour(self, hour: datetime, buckets: list[dict[str, Any]]) -> None:
        await replace_hour(self.hourly, hour, buckets)

    async def get_state(self) -> dict[str, Any]:
        return await self.state.find_one({"_id": STATE_DOC_ID}) or {"_id": STATE_DOC_ID}

    async def set_state(self, fields: dict[str, Any]) -> None:
        await self.state.update_one({"_id": STATE_DOC_ID}, {"$set": fields}, upsert=True)

    # -- 읽기 --------------------------------------------------------------------------------

    async def window_sums(
        self,
        instance_id: str,
        since: datetime,
        until: datetime,
        *,
        names: list[str],
        day_tz: str | None,
    ) -> dict[str, list[dict[str, Any]]]:
        """문서 하나의 묶음을 창 안에서 더한다. 유형별(`types`), Tool 호출의 Tool 별(`tools`)·
        백엔드별(`backends`)·단계별(`series`) 합이다. Tool 별은 names(문서가 광고하는 이름)만
        이름으로 묶고 나머지는 이름 없음(null) 한 행이다. 접기는 시간마다 이름 수를 막을 뿐이라
        긴 창에서는 다른 이름이 시간마다 쌓이므로, 묶음을 DB 안에서 줄여 넘긴다. day_tz 가 있으면
        단계는 그 시간대의 날이고 없으면 시간이다."""
        step: Any = "$hour" if day_tz is None else day_of(day_tz)
        sums = _bucket_sums()
        calls_only = {"$match": {"type": MCP_TOOL_CALL}}
        # 묶음의 이름은 128자로 잘려 있다. 파이썬의 자르기는 $substrCP 처럼 코드 포인트로 센다
        cut = [n[:NAME_CHARS] for n in names]
        named = {"$cond": [{"$in": ["$tool", cut]}, "$tool", None]}
        match = {
            "instance_id": instance_id,
            "v": BUCKET_VERSION,
            "hour": {"$gte": since, "$lt": until},
        }
        return await facets(
            self.hourly,
            match,
            {
                "types": [{"$group": {"_id": "$type", **sums}}],
                "tools": [calls_only, {"$group": {"_id": named, **sums}}],
                "backends": [calls_only, {"$group": {"_id": "$backend", **sums}}],
                "series": [
                    calls_only,
                    {"$group": {"_id": step, **sums}},
                    {"$sort": {"_id": ASCENDING}},
                ],
            },
        )


def _hour_pipeline(hour: datetime) -> list[dict[str, Any]]:
    """시간 하나의 묶음. 이벤트를 열쇠로 묶고, (시간, 문서, 유형, 백엔드)마다 Tool 순위를 매긴 뒤,
    200위 밖과 이름 없는 호출을 rest 묶음으로 다시 묶는다."""
    partition = {k: f"$_id.{k}" for k in KEY_FIELDS if k != "tool"}
    rest = {"$or": [{"$eq": ["$_id.tool", None]}, {"$gt": ["$rank", KEPT_TOOLS]}]}
    return [
        {
            "$match": {
                "ts": {"$gte": hour, "$lt": hour + HOUR},
                "type": {"$in": list(MCP_CALL_TYPES)},
                "instance_id": {"$ne": None},
            }
        },
        {"$project": _event_fields()},
        {"$group": {"_id": {k: f"${k}" for k in KEY_FIELDS}, **_event_sums()}},
        # 이름 없는 묶음은 순위의 맨 뒤라 이름 있는 Tool 이 200자리를 다 쓴다
        {"$addFields": {"unnamed": {"$cond": [{"$eq": ["$_id.tool", None]}, 1, 0]}}},
        {
            "$setWindowFields": {
                "partitionBy": partition,
                "sortBy": {"unnamed": 1, "calls": -1, "_id.tool": 1},
                # $documentNumber 는 정렬 칸 하나만 받아, 앞에서부터 센 수를 순위로 쓴다
                "output": {"rank": {"$sum": 1, "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$addFields": {"rest": rest}},
        {
            "$group": {
                "_id": {
                    **partition,
                    "tool": {"$cond": ["$rest", None, "$_id.tool"]},
                    "folded": {"$and": ["$rest", {"$eq": ["$_id.type", MCP_TOOL_CALL]}]},
                },
                **{f: {"$sum": f"${f}"} for f in _summed_fields()},
                "max_ms": {"$max": "$max_ms"},
            }
        },
    ]


def _event_fields() -> dict[str, Any]:
    """이벤트에서 열쇠와 값을 꺼낸다. Tool 이름은 Tool 호출에만 있고 128자로 자르며, 빈 이름은
    이름 없음이다. 백엔드는 vMCP 이벤트의 `backend_name` 뿐이다."""
    named = {
        "$and": [
            {"$eq": ["$type", MCP_TOOL_CALL]},
            {"$eq": [{"$type": "$tool"}, "string"]},
            {"$ne": ["$tool", ""]},
        ]
    }
    backend = "$metadata.extra.backend_name"
    from_vmcp = {
        "$and": [{"$eq": ["$proxy_kind", VMCP_KIND]}, {"$eq": [{"$type": backend}, "string"]}]
    }
    return {
        "tenant": 1,
        "instance_id": 1,
        "proxy_kind": 1,
        "type": 1,
        "tool": {"$cond": [named, {"$substrCP": ["$tool", 0, NAME_CHARS]}, None]},
        "backend": {"$cond": [from_vmcp, backend, None]},
        "outcome": {"$cond": [{"$in": ["$outcome", list(OUTCOMES)]}, "$outcome", UNKNOWN_OUTCOME]},
        "d": {"$cond": [{"$isNumber": DURATION}, DURATION, None]},
    }


def _event_sums() -> dict[str, Any]:
    """이벤트 묶음의 수. 구간 칸은 (앞 끝, 위 끝]에 든 걸린 시간 수다."""
    has_d = {"$ne": ["$d", None]}
    out: dict[str, Any] = {"calls": {"$sum": 1}}
    for c in OUTCOME_CLASSES:
        out[f"o_{c}"] = {"$sum": {"$cond": [{"$eq": ["$outcome", c]}, 1, 0]}}
    out["d_count"] = {"$sum": {"$cond": [has_d, 1, 0]}}
    out["d_sum"] = {"$sum": {"$ifNull": ["$d", 0]}}
    out["max_ms"] = {"$max": "$d"}
    low: int | None = None
    for edge, field in zip(RANGE_EDGES, RANGE_FIELDS, strict=False):
        cond: list[Any] = [has_d, {"$lte": ["$d", edge]}]
        if low is not None:
            cond.append({"$gt": ["$d", low]})
        out[field] = {"$sum": {"$cond": [{"$and": cond}, 1, 0]}}
        low = edge
    out[RANGE_FIELDS[-1]] = {"$sum": {"$cond": [{"$and": [has_d, {"$gt": ["$d", low]}]}, 1, 0]}}
    return out


def _summed_fields() -> tuple[str, ...]:
    return ("calls", *(f"o_{c}" for c in OUTCOME_CLASSES), "d_count", "d_sum", *RANGE_FIELDS)


def _bucket_doc(hour: datetime, group: dict[str, Any]) -> dict[str, Any]:
    key = group["_id"]
    return {
        "_id": bucket_id(hour, (key.get(k) for k in KEY_FIELDS)),
        "v": BUCKET_VERSION,
        "hour": hour,
        **{k: key.get(k) for k in KEY_FIELDS},
        "folded": bool(key.get("folded")),
        "calls": group["calls"],
        "outcomes": {c: group[f"o_{c}"] for c in OUTCOME_CLASSES},
        "duration": {
            "count": group["d_count"],
            "sum_ms": group["d_sum"],
            "max_ms": group.get("max_ms"),
            **{f: group[f] for f in RANGE_FIELDS},
        },
    }


def _bucket_sums() -> dict[str, Any]:
    """묶음을 더하는 칸. 이름은 묶음의 칸 이름에서 점을 밑줄로 바꾼 것이다."""
    out: dict[str, Any] = {"calls": {"$sum": "$calls"}}
    for c in OUTCOME_CLASSES:
        out[f"outcomes_{c}"] = {"$sum": f"$outcomes.{c}"}
    for f in ("count", "sum_ms", *RANGE_FIELDS):
        out[f"duration_{f}"] = {"$sum": f"$duration.{f}"}
    out["duration_max_ms"] = {"$max": "$duration.max_ms"}
    return out
