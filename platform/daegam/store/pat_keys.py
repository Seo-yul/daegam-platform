"""개인 토큰의 서명 키 슬롯 (P32 4절).

pat_keys {_id: kid, master, state, created_at, bound_at, token_id, expires_at}

`kid` 는 `pat-` 뒤에 16자리 hex 다. 슬롯의 키는 저장하지 않는다. 플랫폼 서명 키로 `kid` 에서 다시
만든다(auth/pat_keys.py). `master` 는 슬롯을 만들 때의 플랫폼 `kid` 이고 JWKS 에는 `master` 가 지금
`kid` 인 슬롯만 오른다.

`state` 는 `pool` 이나 `bound` 다. local 모드의 리컨실 틱이 `pool` 을 채워 두고 발급이 하나를
`bound` 로 잡는다. 만든 지 `WARMUP` 이 지난 슬롯이 따뜻한 슬롯이다. 회수는 슬롯 문서를 지우는
것이고, 만료한 `bound` 슬롯은 `expires_at` TTL 인덱스가 지운다.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index

PAT_KEYS = "pat_keys"
KID_PREFIX = "pat-"
POOL = "pool"
BOUND = "bound"
# 슬롯이 JWKS 에 오른 뒤 그 키로 서명한 토큰을 쓸 수 있을 때까지. ToolHive 는 JWKS 를 15분마다 다시
# 받고 모르는 kid 에서는 다시 받지 않으므로 16분 한도에 여유를 더한다 (P32 3절)
WARMUP = timedelta(minutes=20)


def new_kid() -> str:
    return f"{KID_PREFIX}{secrets.token_hex(8)}"


class PatKeySlot(Document):
    id: str = Field(default_factory=new_kid)
    master: str
    state: Literal["pool", "bound"] = POOL
    created_at: UtcDatetime
    bound_at: UtcDatetime | None = None
    token_id: str | None = None
    expires_at: UtcDatetime | None = None

    def ready_at(self, now: datetime) -> datetime | None:
        """이 슬롯의 키로 서명한 토큰을 쓸 수 있는 시각. 이미 따뜻하면 None 이다."""
        warm = self.created_at + WARMUP
        return warm if warm > now else None


class MongoPatKeyStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[PAT_KEYS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("state", ASCENDING), ("master", ASCENDING), ("created_at", ASCENDING)],
            name="ix_state_master_created",
        )
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def kids(self, master: str) -> list[str]:
        """JWKS 에 오르는 슬롯. `master` 의 pool 과 아직 지워지지 않은 bound 전부다."""
        cursor = self.coll.find(
            {"state": {"$in": [POOL, BOUND]}, "master": master}, {"_id": 1}
        ).sort("created_at", ASCENDING)
        return [d["_id"] async for d in cursor]

    async def fill_pool(self, master: str, size: int, now: datetime) -> list[str]:
        """`master` 의 pool 슬롯이 size 보다 적으면 모자란 만큼 만든다. 만든 kid 를 돌려준다."""
        have = await self.coll.count_documents({"state": POOL, "master": master})
        fresh = [PatKeySlot(master=master, created_at=now) for _ in range(size - have)]
        if fresh:
            await self.coll.insert_many([slot.to_doc() for slot in fresh])
        return [slot.id for slot in fresh]

    async def claim(
        self, master: str, *, token_id: str, expires_at: datetime, now: datetime
    ) -> PatKeySlot:
        """가장 오래된 pool 슬롯을 bound 로 잡는다. 따뜻한 슬롯이 있으면 가장 오래된 슬롯이 그중
        하나다. findOneAndUpdate 라 레플리카 둘이 같은 슬롯을 잡지 못한다. pool 이 비었으면 그
        자리에서 만든다. 따뜻하지 않은 슬롯이면 `ready_at` 이 쓸 수 있는 시각을 말한다."""
        bind = {"state": BOUND, "bound_at": now, "token_id": token_id, "expires_at": expires_at}
        doc = await self.coll.find_one_and_update(
            {"state": POOL, "master": master},
            {"$set": bind},
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return PatKeySlot.from_doc(doc)
        slot = PatKeySlot(master=master, created_at=now, **bind)
        await self.coll.insert_one(slot.to_doc())
        return slot

    async def masters(self, kids: list[str]) -> dict[str, str]:
        """있는 슬롯의 kid → master. 없는 kid 는 빠진다."""
        cursor = self.coll.find({"_id": {"$in": kids}}, {"master": 1})
        return {d["_id"]: d["master"] async for d in cursor}

    async def bound(self) -> list[PatKeySlot]:
        return [PatKeySlot.from_doc(d) async for d in self.coll.find({"state": BOUND})]

    async def remove_kids(self, kids: list[str]) -> None:
        """개인 토큰 회수의 둘째 걸음이자 주인 없는 슬롯 정리다. JWKS 에서 그 키가 빠진다."""
        if kids:
            await self.coll.delete_many({"_id": {"$in": kids}})

    async def remove_other_masters(self, master: str) -> list[PatKeySlot]:
        """`master` 가 아닌 서명 키로 만든 슬롯을 지우고 지운 것을 돌려준다."""
        return await self._remove({"master": {"$ne": master}})

    async def remove_pool(self) -> list[PatKeySlot]:
        """pool 슬롯을 모두 지우고 지운 것을 돌려준다."""
        return await self._remove({"state": POOL})

    async def _remove(self, query: dict[str, Any]) -> list[PatKeySlot]:
        slots = [PatKeySlot.from_doc(d) async for d in self.coll.find(query)]
        if slots:
            # 읽은 뒤 발급이 잡은 슬롯은 query 에서 벗어나므로 지우지 않는다
            await self.coll.delete_many({**query, "_id": {"$in": [s.id for s in slots]}})
        return slots
