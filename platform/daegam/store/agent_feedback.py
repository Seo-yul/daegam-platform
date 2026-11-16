"""Agent 답변의 피드백 (P36 6절, D61). 컬렉션 `agent_feedback`.

    agent_feedback {_id, tenant, agent_id, session_id, turn_id, turn, user_sub, rating, tags,
                    comment, ephemeral, revision, superseded, evaluation_use, created_at,
                    updated_at, expires_at}

턴마다 한 건인 현재 상태다. 평가는 고치고 거둘 수 있어 이벤트 기록만으로는 목록과 내보내기가
무겁기 때문이다. 바뀔 때마다 남기는 `feedback` 이벤트는 서비스(`services/agent_feedback.py`)가 쓴다.
대화 원문은 담지 않는다. 참조(`session_id`, `turn_id`)와 사람이 쓴 평가만 가진다.
`evaluation_use` 는 그 질문과 답을 평가 사례로 복사해도 된다는 그 사람의 동의다(P45 8절). 칸이 없는
옛 평가는 동의하지 않은 것이다.

색인은 넷이다. 턴 하나에 한 건을 지키는 유일 색인 `uq_turn`, 승인자 목록의 정렬과 내보내기가 읽는
`ix_agent_created_id`(tenant, agent_id, created_at 내림, _id 내림), 보존 기간이 지나면 지우는
`expires_at` TTL, 계정 사건이 그 사람의 동의를 찾는 `ix_consent_user`(동의한 평가만 드는 부분
색인)다. 모든 질의는 테넌트와 `agent_id` 로 거른다. 동의를 찾는 질의만 사람으로 거른다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index, new_id

AGENT_FEEDBACK = "agent_feedback"

Rating = Literal["up", "down"]
FeedbackTag = Literal["inaccurate", "unhelpful", "incomplete", "unsafe", "other"]
RATINGS: tuple[str, ...] = get_args(Rating)
FEEDBACK_TAGS: tuple[str, ...] = get_args(FeedbackTag)
MAX_TAGS = len(FEEDBACK_TAGS)
MAX_COMMENT_CHARS = 2000
# 승인자 목록 쪽 넘김 위치다. 앞 쪽 마지막 행의 (만든 시각, id) 이다
PageCursor = tuple[datetime, str]


class AgentFeedback(Document):
    """턴 하나의 평가. `revision` 은 평가할 때 배포돼 있던 리비전이고 `superseded` 는 다시
    생성으로 그 답이 바뀌었다는 표시다. `evaluation_use` 는 평가 사례로 복사해도 된다는 동의다."""

    id: str = Field(default_factory=new_id)
    tenant: str
    agent_id: str
    session_id: str
    turn_id: str
    turn: int
    user_sub: str
    rating: Rating
    tags: list[FeedbackTag] = Field(default_factory=list)
    comment: str | None = None
    ephemeral: bool = False
    revision: int | None = None
    superseded: bool = False
    evaluation_use: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime
    expires_at: UtcDatetime


def _agent_query(tenant: str, agent_id: str, **extra: Any) -> dict[str, Any]:
    return {"tenant": tenant, "agent_id": agent_id, **extra}


def _turn_query(tenant: str, agent_id: str, session_id: str, turn_id: str) -> dict[str, Any]:
    return _agent_query(tenant, agent_id, session_id=session_id, turn_id=turn_id)


class MongoAgentFeedbackStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_FEEDBACK]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [
                ("tenant", ASCENDING),
                ("agent_id", ASCENDING),
                ("session_id", ASCENDING),
                ("turn_id", ASCENDING),
            ],
            name="uq_turn",
            unique=True,
        )
        await self.coll.create_index(
            [
                ("tenant", ASCENDING),
                ("agent_id", ASCENDING),
                ("created_at", DESCENDING),
                ("_id", DESCENDING),
            ],
            name="ix_agent_created_id",
        )
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")
        # 계정 사건이 그 사람의 동의를 찾는다 (P45 8절). 동의한 평가만 든다
        await self.coll.create_index(
            [("user_sub", ASCENDING)],
            name="ix_consent_user",
            partialFilterExpression={"evaluation_use": True},
        )

    async def upsert(self, fb: AgentFeedback) -> AgentFeedback:
        """턴의 평가를 만들거나 고친다. 처음 만든 id·시각과 `superseded` 는 고쳐도 그대로다."""
        body = fb.to_doc()
        on_insert = {k: body.pop(k) for k in ("_id", "created_at", "superseded")}
        doc = await self.coll.find_one_and_update(
            _turn_query(fb.tenant, fb.agent_id, fb.session_id, fb.turn_id),
            {"$set": body, "$setOnInsert": on_insert},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return AgentFeedback.from_doc(doc)

    async def withdraw(
        self, tenant: str, agent_id: str, session_id: str, turn_id: str, *, user_sub: str
    ) -> str | None:
        """그 사람의 평가를 지우고 그 id 를 돌려준다. 없거나 남의 것이면 None 이다."""
        query = _turn_query(tenant, agent_id, session_id, turn_id)
        doc = await self.coll.find_one_and_delete({**query, "user_sub": user_sub})
        return doc["_id"] if doc else None

    async def for_turn(
        self, tenant: str, agent_id: str, session_id: str, turn_id: str
    ) -> AgentFeedback | None:
        """턴의 평가. 누구의 것이든 턴마다 하나다."""
        doc = await self.coll.find_one(_turn_query(tenant, agent_id, session_id, turn_id))
        return AgentFeedback.from_doc(doc) if doc else None

    async def get_many(self, ids: list[str]) -> dict[str, AgentFeedback]:
        """id → 평가. 없는 id 는 빠진다."""
        if not ids:
            return {}
        cursor = self.coll.find({"_id": {"$in": ids}})
        return {d["_id"]: AgentFeedback.from_doc(d) async for d in cursor}

    async def withdraw_consents(self, user_sub: str, tenant: str | None = None) -> list[str]:
        """그 사람이 평가 사례로 쓰게 한 동의를 거둔다(P45 8절). tenant 를 주면 그 테넌트의
        것만이다. 거둔 평가의 id 다."""
        query: dict[str, Any] = {"user_sub": user_sub, "evaluation_use": True}
        if tenant is not None:
            query["tenant"] = tenant
        ids = [d["_id"] async for d in self.coll.find(query, {"_id": 1})]
        if ids:
            await self.coll.update_many({"_id": {"$in": ids}}, {"$set": {"evaluation_use": False}})
        return ids

    async def for_session(
        self, tenant: str, agent_id: str, session_id: str, *, user_sub: str
    ) -> list[AgentFeedback]:
        query = _agent_query(tenant, agent_id, session_id=session_id, user_sub=user_sub)
        return [AgentFeedback.from_doc(d) async for d in self.coll.find(query)]

    async def page(
        self,
        tenant: str,
        agent_id: str,
        *,
        rating: str | None = None,
        tag: str | None = None,
        before: PageCursor | None = None,
        limit: int,
    ) -> list[AgentFeedback]:
        """새것부터 limit 건. `before` 는 정렬 차례로 그 행 뒤의 것만이다(쪽 넘김). 만든 시각이
        같은 행은 id 로 가른다."""
        query = _agent_query(tenant, agent_id)
        if rating is not None:
            query["rating"] = rating
        if tag is not None:
            query["tags"] = tag
        if before is not None:
            created, feedback_id = before
            query["$or"] = [
                {"created_at": {"$lt": created}},
                {"created_at": created, "_id": {"$lt": feedback_id}},
            ]
        cursor = self.coll.find(query).sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        return [AgentFeedback.from_doc(d) async for d in cursor.limit(limit)]

    async def counts_since(self, tenant: str, agent_id: str, since: datetime) -> dict[str, int]:
        """since 뒤에 만든 평가의 평가별 수다. 없는 평가는 0 이다."""
        pipeline = [
            {"$match": _agent_query(tenant, agent_id, created_at={"$gte": since})},
            {"$group": {"_id": "$rating", "n": {"$sum": 1}}},
        ]
        found = {d["_id"]: d["n"] async for d in await self.coll.aggregate(pipeline)}
        return {r: int(found.get(r, 0)) for r in RATINGS}

    def _range(
        self, tenant: str, agent_id: str, since: datetime, until: datetime, rating: str | None
    ) -> dict[str, Any]:
        query = _agent_query(tenant, agent_id, created_at={"$gte": since, "$lt": until})
        if rating is not None:
            query["rating"] = rating
        return query

    async def count_range(
        self, tenant: str, agent_id: str, *, since: datetime, until: datetime, rating: str | None
    ) -> int:
        return await self.coll.count_documents(self._range(tenant, agent_id, since, until, rating))

    async def iter_range(
        self, tenant: str, agent_id: str, *, since: datetime, until: datetime, rating: str | None
    ) -> AsyncIterator[AgentFeedback]:
        """구간의 평가를 만든 차례로 하나씩 낸다. 내보내기가 다 읽지 않고 흘려 보낸다."""
        query = self._range(tenant, agent_id, since, until, rating)
        async for d in self.coll.find(query).sort([("created_at", ASCENDING), ("_id", ASCENDING)]):
            yield AgentFeedback.from_doc(d)

    async def delete_session(self, tenant: str, agent_id: str, session_id: str) -> int:
        res = await self.coll.delete_many(_agent_query(tenant, agent_id, session_id=session_id))
        return res.deleted_count

    async def delete_agent(self, tenant: str, agent_id: str) -> int:
        res = await self.coll.delete_many(_agent_query(tenant, agent_id))
        return res.deleted_count

    async def mark_superseded(
        self, tenant: str, agent_id: str, session_id: str, turn_id: str
    ) -> int:
        res = await self.coll.update_many(
            _turn_query(tenant, agent_id, session_id, turn_id), {"$set": {"superseded": True}}
        )
        return res.modified_count
