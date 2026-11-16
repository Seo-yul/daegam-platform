"""웹훅 트리거의 Secret 을 가진 Agent 의 장부 (P41 4·12절).

agent_hook_secrets {_id: Agent id, written_at, swept_at?}

플랫폼은 플랫폼 네임스페이스의 Secret 을 이름으로만 읽고 목록은 보지 않는다
(`charts/daegam/templates/rbac.yaml`). 그래서 리컨실의 쓸기가 남은 키와 Agent 가 사라진 Secret 을
찾도록, 트리거의 Secret 에 쓰기 전에 그 Agent 를 여기 적는다(`TriggerSecrets.put`). `written_at` 은
마지막으로 쓰기를 시작한 시각이고, 쓸기는 그 뒤 한동안 손대지 않으며 행을 지울 때도 그 값이 그대로일
때만 지운다.
`swept_at` 은 마지막으로 쓴 시각이다.
"""

from __future__ import annotations

from datetime import datetime

from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime

AGENT_HOOK_SECRETS = "agent_hook_secrets"


class HookSecret(Document):
    id: str
    written_at: UtcDatetime
    swept_at: UtcDatetime | None = None


class MongoHookSecretStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_HOOK_SECRETS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("swept_at", ASCENDING), ("written_at", ASCENDING)], name="ix_swept_written"
        )

    async def note_write(self, agent_id: str, now: datetime) -> None:
        """Agent 의 Secret 에 쓰기 전에 부른다."""
        await self.coll.update_one(
            {"_id": agent_id},
            {"$set": {"written_at": now}, "$setOnInsert": {"swept_at": None}},
            upsert=True,
        )

    async def get(self, agent_id: str) -> HookSecret | None:
        doc = await self.coll.find_one({"_id": agent_id})
        return HookSecret.from_doc(doc) if doc else None

    async def due(
        self, swept_before: datetime, written_before: datetime, limit: int
    ) -> list[HookSecret]:
        """쓸 때가 된 행. 마지막 쓰기가 written_before 보다 이른 것만, 아직 쓴 적이 없거나 오래전에
        쓴 것부터다. 막 쓴 행이 자리를 다 차지하지 못한다."""
        query = {
            "$or": [{"swept_at": None}, {"swept_at": {"$lt": swept_before}}],
            "written_at": {"$lte": written_before},
        }
        cursor = self.coll.find(query).sort("swept_at", ASCENDING).limit(limit)
        return [HookSecret.from_doc(d) async for d in cursor]

    async def mark_swept(self, agent_id: str, now: datetime) -> None:
        await self.coll.update_one({"_id": agent_id}, {"$set": {"swept_at": now}})

    async def forget(self, agent_id: str, written_at: datetime) -> bool:
        """행을 지운다. 그사이 누가 쓰기를 시작했으면(`written_at` 이 다르면) 두고 거짓이다."""
        result = await self.coll.delete_one({"_id": agent_id, "written_at": written_at})
        return result.deleted_count == 1


__all__ = ["AGENT_HOOK_SECRETS", "HookSecret", "MongoHookSecretStore"]
