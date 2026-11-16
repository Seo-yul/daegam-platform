"""서비스 어카운트 (P53b 6.7절).

service_accounts {_id: account_id, tenant, name, role, created_by, created_by_name, created_at,
                  updated_at, updated_by}   (tenant, name) 유니크

CI 처럼 사람 없이 MCP 를 부르는 프로그램의 계정이다. 테넌트 하나에 속하고 그 테넌트의 역할 하나를
갖는다. 토큰은 개인 토큰과 같은 `personal_tokens` 문서이고 `sub` 가 `sa:<account_id>` 다
(`auth/service_token.py` `ACCOUNT_SUB_PREFIX`). 접근 정리는 그 토큰을 사람 대신 계정에 맞댄다
(`role_of`). 계정이 없으면 회수하고(`deleted`), 역할이 토큰보다 낮으면 회수한다(`role_changed`).

`created_by` 는 만든 사람의 sub 이고 `created_by_name` 은 그때의 이름이다. 만든 사람이 테넌트를
떠나도 계정과 토큰은 남는다. 카드가 만든 사람을 보인다.
"""

from __future__ import annotations

from datetime import datetime

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime

SERVICE_ACCOUNTS = "service_accounts"


class ServiceAccount(Document):
    id: str
    tenant: str
    name: str
    role: str
    created_by: str
    created_by_name: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    updated_by: str


class MongoServiceAccountStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SERVICE_ACCOUNTS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )

    async def insert(self, account: ServiceAccount) -> bool:
        """넣는다. 그 테넌트에 같은 이름이 있으면 넣지 않고 거짓이다."""
        try:
            await self.coll.insert_one(account.to_doc())
        except DuplicateKeyError:
            return False
        return True

    async def list(self, tenant: str) -> list[ServiceAccount]:
        cursor = self.coll.find({"tenant": tenant}).sort("name", ASCENDING)
        return [ServiceAccount.from_doc(d) async for d in cursor]

    async def get(self, account_id: str, tenant: str) -> ServiceAccount | None:
        doc = await self.coll.find_one({"_id": account_id, "tenant": tenant})
        return ServiceAccount.from_doc(doc) if doc else None

    async def role_of(self, account_id: str, tenant: str) -> str | None:
        """계정의 지금 역할. 그 테넌트에 계정이 없으면 None 이다. 접근 정리가 서비스 어카운트의
        토큰을 이것에 맞댄다 (`services/access_revocation.py` `ServiceAccountRoles`)."""
        doc = await self.coll.find_one({"_id": account_id, "tenant": tenant}, {"role": 1})
        return doc.get("role") if doc else None

    async def set_role(
        self, account_id: str, tenant: str, role: str, *, by: str, now: datetime
    ) -> ServiceAccount | None:
        """역할을 바꾼 계정. 계정이 그 사이 지워졌으면 None 이다."""
        doc = await self.coll.find_one_and_update(
            {"_id": account_id, "tenant": tenant},
            {"$set": {"role": role, "updated_at": now, "updated_by": by}},
            return_document=ReturnDocument.AFTER,
        )
        return ServiceAccount.from_doc(doc) if doc else None

    async def delete(self, account_id: str, tenant: str) -> bool:
        result = await self.coll.delete_one({"_id": account_id, "tenant": tenant})
        return result.deleted_count == 1
