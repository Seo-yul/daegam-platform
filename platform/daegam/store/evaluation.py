"""Agent 의 평가 세트와 사례 (P45 7절, D109). 컬렉션 `evaluation_sets`, `evaluation_cases`.

    evaluation_sets  {_id, tenant, agent_id, name, grader?, repeats, case_count,
                      created_at, created_by, updated_at, updated_by}
    evaluation_cases {_id, tenant, agent_id, set_id, content, digest, origin?, expires_at?,
                      pii_ack?, created_at, created_by, updated_at, updated_by}

세트는 Agent 하나의 것이다. 사례 수와 반복 수를 곱한 값은 `MAX_CASE_RUNS` 이하이고, 세트 문서의
`case_count` 가 그 자리를 지킨다. 사례를 넣기 전에 조건부 `$inc` 로 자리를 잡고, 반복 수를 바꾸는
저장은 지금의 사례 수로 조건을 건다. 그래서 두 저장이 겹쳐도 상한을 넘지 않는다.

사례의 내용(`CaseContent`)은 사람이 쓴 질문, 참조 답, 채점 기준, 확인 항목, 쓰기 Tool 의 가상
결과다. `digest` 는 그 내용의 지문이고(`services/evaluation/cases.py` `case_digest`), 세트의 지문은
읽을 때 설정과 사례의 지문으로 짓는다. 대화에서 복사한 사례는 `origin`(평가 id, 세션 id, 턴 id)을
가지고 사람은 담지 않는다. 그 평가의 `expires_at` 을 자기 TTL 로 가져 이 판의 코드가 돌지 않아도
저장소가 복사본의 수명을 지킨다(8절).

사례를 지우는 모든 길(사례 하나, 세트, 복사본, 정리, Agent)은 그 사례의 실행 결과
(`store/evaluation_runs.py` `EVALUATION_RESULTS`)도 같은 걸음에서 지운다(8절). Agent 를 잊을 때는
그 실행도 지운다.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_partial_index,
    ensure_ttl_index,
    new_id,
    utcnow,
)
from daegam.store.evaluation_runs import EVALUATION_RESULTS, EVALUATION_RUNS, Grader

EVALUATION_SETS = "evaluation_sets"
EVALUATION_CASES = "evaluation_cases"

# 세트 하나의 사례 수 × 반복 수 상한 (7절). 자리를 잡는 조건이 지킨다
MAX_CASE_RUNS = 100
# 다시 세기가 건너뛰는, 방금 바뀐 세트의 나이. 자리를 잡고 사례를 넣는 사이의 세트를 다시 세면
# 하나를 덜 센다
RECOUNT_SETTLE = timedelta(seconds=60)

# 저장 모델은 모르는 칸을 버리고 확인 항목의 종류를 글자로 읽는다. 뒤 판이 칸이나 종류를 더해도 이
# 판이 읽게 하려는 것이다. 상한과 꼴은 요청 본문(`services/evaluation/inputs.py`)이 본다
_READ = ConfigDict(extra="ignore")


class CaseCheck(BaseModel):
    """모델 없이 플랫폼이 보는 확인 하나 (11절)."""

    model_config = _READ

    kind: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None


class CaseReference(BaseModel):
    """참조 답. good 은 이 답이 좋은 답의 예인지다. 복사한 사례는 평가가 아쉬워요이면 거짓이다."""

    model_config = _READ

    text: str
    good: bool = True


class CaseContent(BaseModel):
    """사례의 내용. 질문은 새 대화 하나에서 차례로 보낸다. expect_done 은 턴이 `done` 으로
    끝났는지의 확인이고 기본으로 켜져 있다. tool_results 는 쓰기 Tool 이름마다의 가상
    결과다(10절). 지문이 기본값을 뺀 덤프로 지어지므로 기본값을 바꾸지 않는다."""

    model_config = _READ

    messages: list[str]
    reference: CaseReference | None = None
    criteria: list[str] = Field(default_factory=list)
    checks: list[CaseCheck] = Field(default_factory=list)
    expect_done: bool = True
    tool_results: dict[str, str] = Field(default_factory=dict)


class CaseOrigin(BaseModel):
    """복사한 사례가 온 곳 (8절). 사람은 담지 않는다."""

    model_config = _READ

    rating_id: str
    session_id: str
    turn_id: str


class PiiAck(BaseModel):
    """손으로 쓴 사례의 개인정보 경고를 확인한 사람과 때 (8절)."""

    model_config = ConfigDict(extra="ignore")

    by: str
    at: UtcDatetime


class EvaluationSet(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    agent_id: str
    name: str
    grader: Grader | None = None
    repeats: int = 1
    # 세트에 든 사례 수. 사례를 넣고 지울 때 조건부로 늘고 준다
    case_count: int = 0
    created_at: UtcDatetime = Field(default_factory=utcnow)
    created_by: str
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str


class EvaluationCase(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    agent_id: str
    set_id: str
    content: CaseContent
    digest: str
    origin: CaseOrigin | None = None
    # 복사한 사례의 TTL. 그 평가의 `expires_at` 을 따라 움직인다. 손으로 쓴 사례는 없다
    expires_at: UtcDatetime | None = None
    pii_ack: PiiAck | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    created_by: str
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str


def _fits(repeats_expr: Any, count_expr: Any) -> dict[str, Any]:
    """사례 수 × 반복 수가 상한 안인가의 `$expr`."""
    return {"$expr": {"$lte": [{"$multiply": [count_expr, repeats_expr]}, MAX_CASE_RUNS]}}


class MongoEvaluationStore:
    def __init__(self, db: AsyncDatabase):
        self.sets = db[EVALUATION_SETS]
        self.cases = db[EVALUATION_CASES]
        # 사례와 함께 지우는 실행 결과와, Agent 와 함께 지우는 실행이다
        self.results = db[EVALUATION_RESULTS]
        self.runs = db[EVALUATION_RUNS]

    async def init_schema(self) -> None:
        await self.sets.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("created_at", ASCENDING)],
            name="ix_agent_created",
        )
        await self.cases.create_index(
            [("set_id", ASCENDING), ("created_at", ASCENDING)], name="ix_set_created"
        )
        await self.cases.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING)], name="ix_agent"
        )
        await ensure_partial_index(self.cases, "origin.rating_id", name="ix_origin_rating")
        # 복사한 사례만 칸이 있다. 칸이 없는 문서는 TTL 이 지나치지 않는다
        await ensure_ttl_index(self.cases, "expires_at", 0, "ttl_expires_at")

    # -- 세트 ---------------------------------------------------------------------------

    @staticmethod
    def _set_query(tenant: str, agent_id: str, set_id: str) -> dict[str, Any]:
        return {"_id": set_id, "tenant": tenant, "agent_id": agent_id}

    async def create_set(self, item: EvaluationSet) -> EvaluationSet:
        await self.sets.insert_one(item.to_doc())
        return item

    async def get_set(self, tenant: str, agent_id: str, set_id: str) -> EvaluationSet | None:
        doc = await self.sets.find_one(self._set_query(tenant, agent_id, set_id))
        return EvaluationSet.from_doc(doc) if doc else None

    async def sets_of(self, tenant: str, agent_id: str) -> list[EvaluationSet]:
        cursor = self.sets.find({"tenant": tenant, "agent_id": agent_id})
        cursor = cursor.sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [EvaluationSet.from_doc(d) async for d in cursor]

    async def update_set(self, item: EvaluationSet) -> bool:
        """이름, 채점 모델, 반복 수를 쓴다. 지금의 사례 수에 새 반복 수를 곱해 상한을 넘으면 쓰지
        않고 False 다(세트가 없어도 False 다)."""
        query = {
            **self._set_query(item.tenant, item.agent_id, item.id),
            **_fits(item.repeats, "$case_count"),
        }
        fields = item.to_doc()
        update = {k: fields[k] for k in ("name", "grader", "repeats", "updated_at", "updated_by")}
        res = await self.sets.update_one(query, {"$set": update})
        return res.matched_count == 1

    async def delete_set(self, tenant: str, agent_id: str, set_id: str) -> bool:
        """사례와 그 결과를 먼저 지우고 세트를 지운다. 그 사이에 멈추면 사례 없는 세트가 남을
        뿐이다."""
        await self.cases.delete_many({"tenant": tenant, "agent_id": agent_id, "set_id": set_id})
        await self.results.delete_many({"tenant": tenant, "set_id": set_id})
        res = await self.sets.delete_one(self._set_query(tenant, agent_id, set_id))
        return res.deleted_count == 1

    async def reserve_case(self, item: EvaluationSet, by: str) -> bool:
        """사례 하나의 자리를 잡는다. 사례 수를 하나 늘려도 상한 안일 때만이고, 못 잡으면
        False 다."""
        query = {
            **self._set_query(item.tenant, item.agent_id, item.id),
            **_fits("$repeats", {"$add": ["$case_count", 1]}),
        }
        res = await self.sets.update_one(
            query,
            {"$inc": {"case_count": 1}, "$set": {"updated_at": utcnow(), "updated_by": by}},
        )
        return res.matched_count == 1

    async def release_case(self, set_id: str, count: int = 1) -> None:
        await self.sets.update_one({"_id": set_id}, {"$inc": {"case_count": -count}})

    # -- 사례 ---------------------------------------------------------------------------

    async def insert_case(self, item: EvaluationCase) -> EvaluationCase:
        await self.cases.insert_one(item.to_doc())
        return item

    async def get_case(self, tenant: str, set_id: str, case_id: str) -> EvaluationCase | None:
        doc = await self.cases.find_one({"_id": case_id, "tenant": tenant, "set_id": set_id})
        return EvaluationCase.from_doc(doc) if doc else None

    async def case_digests(self, tenant: str, set_ids: Collection[str]) -> dict[str, list[Any]]:
        """세트 id → 그 사례의 (id, 지문). 세트의 지문을 짓는 데 이 두 칸만 읽는다."""
        found: dict[str, list[Any]] = defaultdict(list)
        query = {"tenant": tenant, "set_id": {"$in": list(set_ids)}}
        async for d in self.cases.find(query, {"set_id": 1, "digest": 1}):
            found[d["set_id"]].append((d["_id"], d["digest"]))
        return found

    async def cases_of(self, tenant: str, set_ids: Collection[str]) -> list[EvaluationCase]:
        """세트들의 사례. 만든 차례다."""
        cursor = self.cases.find({"tenant": tenant, "set_id": {"$in": list(set_ids)}})
        cursor = cursor.sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [EvaluationCase.from_doc(d) async for d in cursor]

    async def update_case(self, item: EvaluationCase) -> bool:
        """내용, 지문, 확인, 고친 사람과 때를 쓴다. 사례가 없으면 False 다."""
        fields = item.to_doc()
        names = ("content", "digest", "pii_ack", "expires_at", "updated_at", "updated_by")
        res = await self.cases.update_one(
            {"_id": item.id, "tenant": item.tenant, "set_id": item.set_id},
            {"$set": {k: fields[k] for k in names}},
        )
        return res.matched_count == 1

    async def delete_case(self, tenant: str, set_id: str, case_id: str) -> bool:
        """사례와 그 결과를 지운다."""
        res = await self.cases.delete_one({"_id": case_id, "tenant": tenant, "set_id": set_id})
        if res.deleted_count != 1:
            return False
        await self.results.delete_many({"case_id": case_id})
        await self.release_case(set_id)
        return True

    # -- 복사본 -------------------------------------------------------------------------

    async def copies_of(self, rating_id: str) -> list[EvaluationCase]:
        """그 평가에서 복사한 사례."""
        return [
            EvaluationCase.from_doc(d)
            async for d in self.cases.find({"origin.rating_id": rating_id})
        ]

    async def move_copies(self, rating_id: str, expires_at: datetime) -> int:
        """그 평가에서 복사한 사례의 TTL 을 평가의 기한으로 옮긴다 (8절)."""
        res = await self.cases.update_many(
            {"origin.rating_id": rating_id}, {"$set": {"expires_at": expires_at}}
        )
        return res.modified_count

    async def delete_copies(self, rating_ids: Collection[str]) -> int:
        """그 평가들에서 복사한 사례를 지우고 세트의 자리를 돌려준다. 지운 수다."""
        if not rating_ids:
            return 0
        query = {"origin.rating_id": {"$in": list(rating_ids)}}
        found = [d async for d in self.cases.find(query, {"set_id": 1})]
        return await self.delete_cases(found)

    async def delete_cases(self, found: list[dict[str, Any]]) -> int:
        """`{_id, set_id}` 문서들의 사례와 그 결과를 세트마다 지우고 그 세트에서 실제로 지운 만큼
        자리를 돌려준다. 그 사이 남이 지운 사례는 두 번 세지 않는다. 지운 수다."""
        by_set: dict[str, list[str]] = defaultdict(list)
        for d in found:
            by_set[d["set_id"]].append(d["_id"])
        deleted = 0
        for set_id, ids in by_set.items():
            res = await self.cases.delete_many({"_id": {"$in": ids}, "set_id": set_id})
            await self.results.delete_many({"case_id": {"$in": ids}})
            if res.deleted_count:
                await self.release_case(set_id, res.deleted_count)
            deleted += res.deleted_count
        return deleted

    async def copied_cases(self) -> list[dict[str, Any]]:
        """복사한 사례 모두의 `{_id, set_id, tenant, origin}`. 정리가 읽는다."""
        cursor = self.cases.find(
            {"origin.rating_id": {"$exists": True}}, {"set_id": 1, "tenant": 1, "origin": 1}
        )
        return [d async for d in cursor]

    async def agent_ids(self) -> set[str]:
        """세트, 사례, 실행이나 결과를 가진 Agent 의 id."""
        return {
            *await self.sets.distinct("agent_id"),
            *await self.cases.distinct("agent_id"),
            *await self.runs.distinct("agent_id"),
            *await self.results.distinct("agent_id"),
        }

    async def orphan_results(self) -> list[str]:
        """사례가 없는 실행 결과를 지운다. 지운 결과의 id 다."""
        case_ids = await self.cases.distinct("_id")
        query = {"case_id": {"$nin": case_ids}}
        ids = [d["_id"] async for d in self.results.find(query, {"_id": 1})]
        if ids:
            await self.results.delete_many({"_id": {"$in": ids}, **query})
        return ids

    async def cases_without_set(self) -> list[dict[str, Any]]:
        """세트가 없는 사례의 `{_id, set_id}`. 세트를 지우는 동안 들어온 사례다."""
        set_ids = await self.sets.distinct("_id")
        cursor = self.cases.find({"set_id": {"$nin": set_ids}}, {"set_id": 1})
        return [d async for d in cursor]

    async def recount(self, now: datetime) -> list[str]:
        """세트의 `case_count` 를 실제 사례 수에 맞춘다. TTL 이 지운 복사본은 자리를 돌려주지 않기
        때문이다. `RECOUNT_SETTLE` 안에 바뀐 세트는 건너뛴다. 자리를 잡고 사례를 넣기 전인 세트를
        세면 하나를 덜 세기 때문이다. 쓰기는 읽은 수와 고친 때가 그대로일 때만이다. 바꾼 세트의
        id 다."""
        pipeline = [{"$group": {"_id": "$set_id", "n": {"$sum": 1}}}]
        actual = {d["_id"]: d["n"] async for d in await self.cases.aggregate(pipeline)}
        changed: list[str] = []
        settled = {"updated_at": {"$lt": now - RECOUNT_SETTLE}}
        async for d in self.sets.find(settled, {"case_count": 1, "updated_at": 1}):
            n = actual.get(d["_id"], 0)
            if d.get("case_count", 0) == n:
                continue
            res = await self.sets.update_one(
                {
                    "_id": d["_id"],
                    "case_count": d.get("case_count", 0),
                    "updated_at": d["updated_at"],
                },
                {"$set": {"case_count": n}},
            )
            if res.modified_count:
                changed.append(d["_id"])
        return changed

    # -- 지우기 ------------------------------------------------------------------------

    async def forget_agent(self, tenant: str, agent_id: str) -> int:
        """그 Agent 의 세트, 사례, 실행과 결과를 지운다. 지운 세트 수다."""
        await self.cases.delete_many({"tenant": tenant, "agent_id": agent_id})
        await self.results.delete_many({"tenant": tenant, "agent_id": agent_id})
        await self.runs.delete_many({"tenant": tenant, "agent_id": agent_id})
        res = await self.sets.delete_many({"tenant": tenant, "agent_id": agent_id})
        return res.deleted_count

    async def forget_agents(self, agent_ids: Collection[str]) -> None:
        """없는 Agent 들의 세트, 사례, 실행과 결과를 지운다. 정리가 부른다."""
        if not agent_ids:
            return
        query = {"agent_id": {"$in": list(agent_ids)}}
        await self.cases.delete_many(query)
        await self.results.delete_many(query)
        await self.runs.delete_many(query)
        await self.sets.delete_many(query)
