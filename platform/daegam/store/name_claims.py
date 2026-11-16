"""이름의 선점 기록이 함께 쓰는 저장 (D82).

읽어서 확인한 뒤 저장하면 동시에 도는 두 요청이 함께 지나가므로, 저장 앞에서 이름을 선점한 기록이
둘 사이를 가른다. 문서 하나가 이름 하나이고 `_id` 가 그 이름이라 Mongo 의 유일한 `_id` 가
인덱스다. 공개 host(`exposure_hosts`)와 Gateway 의 Entry 이름(`gateway_entry_names`)이 이것을 쓰고,
종류마다 기록의 모델과 컬렉션만 다르다.

선점마다 새 nonce 를 쓴다. 지우는 쪽은 읽은 nonce 를 조건으로 지우므로, 그 사이 다시 선점한 배포의
기록을 지우지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, new_id, utcnow

# 선점에서 저장까지의 틈보다 넉넉한 시간. 쓸기는 이보다 젊은 선점을 건드리지 않는다 (P70 2절)
CLAIM_GRACE = timedelta(minutes=5)
# 선점의 넣기·다시 선점·읽기를 되풀이하는 횟수. 다른 요청의 넣기와 지우기가 그 사이 엇갈려도
# 기록을 끝까지 읽어 낸다
CLAIM_ROUNDS = 5


class NameClaim(Document):
    """이름 하나의 선점. `id` 가 그 이름이다."""

    id: str
    resource_id: str
    tenant: str
    nonce: str = Field(default_factory=new_id)
    claimed_at: UtcDatetime = Field(default_factory=utcnow)


class MongoNameClaimStore[C: NameClaim]:
    """선점 기록의 스토어. 하위가 `model`·`collection` 을 채우고, 기록의 그 밖의 칸은 fields 로
    받는다."""

    model: ClassVar[type[Any]]
    collection: ClassVar[str]

    def __init__(self, db: AsyncDatabase):
        self.coll = db[self.collection]

    async def init_schema(self) -> None:
        await self.coll.create_index([("resource_id", ASCENDING)], name="ix_resource")

    async def claim(self, key: str, *, resource_id: str, **fields: Any) -> tuple[C, bool] | None:
        """key 를 선점한다. (기록, 새로 넣었는가) 다. 같은 리소스의 기록이 있으면 새 nonce·시각을
        써서 다시 선점하고(넣지 않았으므로 거짓), 다른 리소스의 기록이 있으면 None 이다. 넣으려다
        부딪친 기록이 그 사이 지워졌으면 다시 넣어 본다. 넣기와 지우기가 엇갈려도 거짓 충돌이 나지
        않도록 몇 번 되풀이한다."""
        for _ in range(CLAIM_ROUNDS):
            claim = self.model(id=key, resource_id=resource_id, **fields)
            try:
                await self.coll.insert_one(claim.to_doc())
                return claim, True
            except DuplicateKeyError:
                pass
            res = await self.coll.update_one(
                {"_id": key, "resource_id": resource_id},
                {"$set": {"nonce": claim.nonce, "claimed_at": claim.claimed_at}},
            )
            if res.matched_count == 1:
                return claim, False
            if await self.coll.find_one({"_id": key}, {"_id": 1}) is not None:
                return None
        return None

    async def get(self, key: str) -> C | None:
        doc = await self.coll.find_one({"_id": key})
        return self.model.from_doc(doc) if doc else None

    async def remove(self, key: str, nonce: str) -> bool:
        """읽은 nonce 의 기록만 지운다. 그 사이 다시 선점된 기록은 남는다."""
        res = await self.coll.delete_one({"_id": key, "nonce": nonce})
        return res.deleted_count == 1

    async def of_resource(self, resource_id: str) -> list[C]:
        cursor = self.coll.find({"resource_id": resource_id})
        return [self.model.from_doc(d) for d in await cursor.to_list()]

    async def remove_resource(self, resource_id: str) -> int:
        """리소스의 기록을 모두 지운다. 문서가 지워진 뒤라 다시 선점할 배포가 없다."""
        res = await self.coll.delete_many({"resource_id": resource_id})
        return res.deleted_count

    async def all(self) -> list[C]:
        return [self.model.from_doc(d) for d in await self.coll.find({}).to_list()]

    async def adopt(self, key: str, *, now: datetime, resource_id: str, **fields: Any) -> bool:
        """기록 없는 살아 있는 리소스에 기록을 준다. 이미 기록이 있으면(같은 리소스든 다른
        리소스든) 건드리지 않고 거짓이다."""
        claim = self.model(id=key, resource_id=resource_id, claimed_at=now, **fields)
        try:
            await self.coll.insert_one(claim.to_doc())
        except DuplicateKeyError:
            return False
        return True


# 해제가 읽어 두었다가 놓는 선점 하나와 그 스토어
HeldClaim = tuple[MongoNameClaimStore[Any], NameClaim]
