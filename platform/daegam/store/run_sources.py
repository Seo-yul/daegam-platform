"""백그라운드 실행의 소스가 함께 쓰는 저장 (P41 3·4·6절). 스케줄과 웹훅 트리거가 이것을 잇는다.

소스는 Agent 하나에 속하고(`tenant`, `agent_id`), 마지막으로 만들거나 바꾸거나 다시 켠 사람에게
묶인다(`acts_as`). 묶일 때의 Agent 리비전(`revision`), 만료(`expires_at`, 고른 길이
`expires_in_days`), 멈춤(`paused`, 까닭 `paused_reason`, 묶인 사람이 접근을 잃었을 때의 원인
`paused_cause`)을 가진다. 멈춤은 모두 `updated_at` 을 바꿔 그사이 워커의 옮기기가 지게 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, cast

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.agent_runs import ActsAs
from daegam.store.base import Document, UtcDatetime


def paused_fields(reason: str, now: datetime, cause: str | None = None) -> dict[str, Any]:
    """멈추는 갱신의 칸. 바꾼 시각을 적어 그사이 워커의 옮기기가 진다."""
    return {"paused": True, "paused_reason": reason, "paused_cause": cause, "updated_at": now}


class RunSource(Document):
    id: str
    tenant: str
    agent_id: str
    acts_as: ActsAs
    revision: int
    expires_at: UtcDatetime
    expires_in_days: int = 90
    paused: bool = False
    paused_reason: str | None = None
    paused_cause: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    def run_message(self) -> str | None:
        """이 소스의 실행이 보내는 메시지. 실행 기록이 메시지를 가지면 None 이다(웹훅)."""
        return None


class MongoRunSourceStore[S: RunSource]:
    """소스 하나의 Mongo 저장. 하위 클래스가 컬렉션 이름과 모델을 정한다. `on_change` 는 사람의
    변경이 함께 적는 칸이다."""

    collection: ClassVar[str]
    model: ClassVar[type[RunSource]]
    on_change: ClassVar[Mapping[str, Any]] = {}

    def __init__(self, db: AsyncDatabase):
        self.coll = db[self.collection]

    def _of(self, doc: dict[str, Any] | None) -> S | None:
        return cast(S, self.model.from_doc(doc)) if doc else None

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING)], name="ix_tenant_agent"
        )
        # 접근을 잃은 사람의 소스를 멈추는 훅과 runs-as-me 가 쓴다
        await self.coll.create_index([("acts_as.sub", ASCENDING)], name="ix_acts_as_sub")

    async def insert(self, source: S) -> None:
        await self.coll.insert_one(source.to_doc())

    async def get(self, tenant: str, agent_id: str, source_id: str) -> S | None:
        return self._of(
            await self.coll.find_one({"_id": source_id, "tenant": tenant, "agent_id": agent_id})
        )

    async def list_for_agent(self, tenant: str, agent_id: str) -> list[S]:
        cursor = self.coll.find({"tenant": tenant, "agent_id": agent_id}).sort(
            "created_at", ASCENDING
        )
        return [cast(S, self.model.from_doc(d)) async for d in cursor]

    async def list_bound(self, sub: str) -> list[S]:
        """sub 에게 묶인 소스를 테넌트와 상관없이, 만든 차례로 (6절의 runs-as-me)."""
        cursor = self.coll.find({"acts_as.sub": sub}).sort("created_at", ASCENDING)
        return [cast(S, self.model.from_doc(d)) async for d in cursor]

    async def get_bound(self, sub: str, source_id: str) -> S | None:
        """sub 에게 묶인 소스 하나. 남의 것이면 None 이다."""
        return self._of(await self.coll.find_one({"_id": source_id, "acts_as.sub": sub}))

    async def count_for_agent(self, tenant: str, agent_id: str) -> int:
        return await self.coll.count_documents({"tenant": tenant, "agent_id": agent_id})

    async def update(
        self, source_id: str, fields: Mapping[str, Any], *, match: Mapping[str, Any] | None = None
    ) -> S | None:
        """사람의 변경. 지워졌거나 match 의 칸이 그사이 바뀌었으면 None 이다."""
        doc = await self.coll.find_one_and_update(
            {**(match or {}), "_id": source_id},
            {"$set": {**fields, **self.on_change}},
            return_document=ReturnDocument.AFTER,
        )
        return self._of(doc)

    async def delete(self, source_id: str) -> bool:
        result = await self.coll.delete_one({"_id": source_id})
        return result.deleted_count == 1

    async def delete_for_agent(self, tenant: str, agent_id: str) -> int:
        result = await self.coll.delete_many({"tenant": tenant, "agent_id": agent_id})
        return result.deleted_count

    async def pause_bound(
        self,
        sub: str,
        *,
        reason: str,
        cause: str,
        now: datetime,
        tenant: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, str]:
        """sub 에게 묶인 켜진 소스를 멈추고, 이번에 멈춘 것만 id → 테넌트로 돌려준다. tenant·
        agent_id 를 주면 그 안의 것만이다. 찾은 뒤에 다시 묶이거나 멈춘 것은 건너뛴다."""
        query: dict[str, Any] = {"acts_as.sub": sub, "paused": False}
        if tenant is not None:
            query["tenant"] = tenant
        if agent_id is not None:
            query["agent_id"] = agent_id
        fields = paused_fields(reason, now, cause)
        paused: dict[str, str] = {}
        async for d in self.coll.find(query, {"tenant": 1}):
            result = await self.coll.update_one(
                {"_id": d["_id"], "acts_as.sub": sub, "paused": False}, {"$set": fields}
            )
            if result.modified_count == 1:
                paused[d["_id"]] = d["tenant"]
        return paused

    async def pause_for_agent(self, tenant: str, agent_id: str, reason: str, now: datetime) -> int:
        """Agent 의 켜진 소스를 모두 멈춘다. 열린 실행은 부른 쪽이 따로 끝낸다. 멈춘 수다."""
        result = await self.coll.update_many(
            {"tenant": tenant, "agent_id": agent_id, "paused": False},
            {"$set": paused_fields(reason, now)},
        )
        return result.modified_count

    async def pause_own(self, sub: str, source_id: str, reason: str, now: datetime) -> S | None:
        """sub 에게 묶인 소스 하나를 멈춘다. 그사이 다른 사람에게 다시 묶였으면 None 이다."""
        doc = await self.coll.find_one_and_update(
            {"_id": source_id, "acts_as.sub": sub},
            {"$set": paused_fields(reason, now)},
            return_document=ReturnDocument.AFTER,
        )
        return self._of(doc)


__all__ = ["MongoRunSourceStore", "RunSource", "paused_fields"]
