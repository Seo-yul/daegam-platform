"""사람이 기억한 승인 선택 (P43 5절, D96).

approval_choices {_id, tenant, sub, agent_id, tool, choice, digest, created_at, expires_at}

세션 소유자가 결정과 함께 고른 "다음부터 묻지 않고 승인" 또는 "거부"다. 사람, Agent, Tool 이름마다
하나이고(유일 인덱스) 다시 고르면 덮어쓴다. `tool` 은 런타임이 부르는 이름이다. `digest` 는 고를 때
그 이름을 주는 tools 노드들의 규칙(결정과 `rememberable`)의 요약이고, 배포가 규칙을 바꾸면 맞지 않아
전달되지 않고 걷힌다(`services/approval_choices.py`). `expires_at` 이 지나면 TTL 인덱스가 지운다.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import UtcDatetime, ensure_ttl_index

APPROVAL_CHOICES = "approval_choices"

Choice = Literal["approve", "reject"]
APPROVE: Choice = "approve"
REJECT: Choice = "reject"


class ApprovalChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="_id")
    tenant: str
    sub: str
    agent_id: str
    tool: str
    choice: Choice
    digest: str
    created_at: UtcDatetime
    expires_at: UtcDatetime


class MongoApprovalChoiceStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[APPROVAL_CHOICES]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("sub", ASCENDING), ("agent_id", ASCENDING), ("tool", ASCENDING)],
            unique=True,
            name="sub_agent_tool",
        )
        await self.coll.create_index([("agent_id", ASCENDING)], name="agent_id")
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def put(self, choice: ApprovalChoice) -> ApprovalChoice:
        """같은 사람, Agent, Tool 의 선택을 이것으로 바꾼다. 처음이면 만든다."""
        key = {"sub": choice.sub, "agent_id": choice.agent_id, "tool": choice.tool}
        fields = choice.model_dump(exclude={"id", "sub", "agent_id", "tool"})
        doc = await self.coll.find_one_and_update(
            key,
            {"$set": fields, "$setOnInsert": {"_id": choice.id}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return ApprovalChoice.model_validate(doc)

    async def live(self, sub: str, agent_id: str, now: datetime) -> list[ApprovalChoice]:
        """그 사람의 그 Agent 에 대한, 아직 끝나지 않은 선택이다. TTL 이 지우기 전의 만료도 뺀다."""
        cursor = self.coll.find({"sub": sub, "agent_id": agent_id, "expires_at": {"$gt": now}})
        return [ApprovalChoice.model_validate(doc) async for doc in cursor]

    async def list_mine(self, sub: str, now: datetime) -> list[ApprovalChoice]:
        """그 사람의 선택 전부다. 모든 테넌트에서 보고 오래된 것부터다."""
        cursor = self.coll.find({"sub": sub, "expires_at": {"$gt": now}}).sort("created_at", 1)
        return [ApprovalChoice.model_validate(doc) async for doc in cursor]

    async def delete_mine(self, sub: str, choice_id: str) -> ApprovalChoice | None:
        """그 사람의 선택 하나를 지우고 지운 것을 돌려준다. 남의 것이거나 없으면 None 이다."""
        doc = await self.coll.find_one_and_delete({"_id": choice_id, "sub": sub})
        return ApprovalChoice.model_validate(doc) if doc is not None else None

    async def delete_for_agent(self, agent_id: str) -> int:
        return (await self.coll.delete_many({"agent_id": agent_id})).deleted_count

    async def delete_for_person(self, sub: str, tenant: str | None = None) -> int:
        """그 사람의 선택을 지운다. tenant 를 주면 그 테넌트의 것만이다(소속을 잃은 테넌트)."""
        query: dict[str, object] = {"sub": sub}
        if tenant is not None:
            query["tenant"] = tenant
        return (await self.coll.delete_many(query)).deleted_count

    async def for_agent(self, agent_id: str) -> list[ApprovalChoice]:
        return [
            ApprovalChoice.model_validate(doc)
            async for doc in self.coll.find({"agent_id": agent_id})
        ]

    async def delete_ids(self, ids: Collection[str]) -> int:
        return (await self.coll.delete_many({"_id": {"$in": list(ids)}})).deleted_count
