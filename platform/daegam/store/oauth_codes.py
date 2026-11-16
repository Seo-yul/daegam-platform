"""OAuth 인가 코드 (P33 8절).

oauth_codes {_id: sha256, client_id, redirect_uri, code_challenge, sub, tv, tenant, resource, scope,
             created_at, expires_at, used_at, grant_id, reused_at, purge_at}

코드 평문은 허용 응답의 `redirect_to` 에만 있고 여기에는 sha256 만 둔다(`store/base.py` 의
`issue_token`). 코드는 `CODE_TTL` 뒤 만료하고 만료는 교환이 직접 본다. 쓰인 코드가 조금 더 남아야
재사용을 알아보므로 `purge_at` 은 만료에 `PURGE_AFTER` 를 더한 값이고 TTL 인덱스가 그때 지운다.
`tv` 는 허용 때 읽은 사용자 문서의 `token_version` 이다. `used_at` 과 `grant_id` 는 교환이 한 번에
찍는다. 쓰인 코드가 다시 오면 `reused_at` 을 찍는다. 첫 교환은 연결을 넣은 뒤 이 값을 다시 읽어,
연결을 넣기 전에 재사용이 왔으면 제 연결을 스스로 끊는다 (8절).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index

OAUTH_CODES = "oauth_codes"
# 코드의 수명과 만료 뒤 보관 (17절)
CODE_TTL = timedelta(seconds=120)
PURGE_AFTER = timedelta(minutes=10)


class OAuthCode(Document):
    id: str  # 코드 평문의 sha256
    client_id: str
    redirect_uri: str
    code_challenge: str
    sub: str
    tv: int
    tenant: str
    resource: str
    scope: str
    created_at: UtcDatetime
    expires_at: UtcDatetime
    used_at: UtcDatetime | None = None
    grant_id: str | None = None
    reused_at: UtcDatetime | None = None
    purge_at: UtcDatetime


class MongoOAuthCodeStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[OAUTH_CODES]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "purge_at", 0, "ttl_purge_at")

    async def insert(self, code: OAuthCode) -> None:
        await self.coll.insert_one(code.to_doc())

    async def get(self, digest: str) -> OAuthCode | None:
        doc = await self.coll.find_one({"_id": digest})
        return OAuthCode.from_doc(doc) if doc else None

    async def consume(self, digest: str, *, grant_id: str, now: datetime) -> OAuthCode | None:
        """교환. 쓰이지 않았고 만료하지 않은 코드에 `used_at` 과 새 연결 id 를 한 번에 찍고 찍은
        문서를 돌려준다. 그런 코드가 없으면 None 이다. 재사용이 끊을 연결 id 가 교환 순간에 정해진다
        (8절)."""
        doc = await self.coll.find_one_and_update(
            {"_id": digest, "used_at": None, "expires_at": {"$gt": now}},
            {"$set": {"used_at": now, "grant_id": grant_id}},
            return_document=ReturnDocument.AFTER,
        )
        return OAuthCode.from_doc(doc) if doc else None

    async def mark_reused(self, digest: str, now: datetime) -> OAuthCode | None:
        """이미 쓰인 코드에 `reused_at` 을 찍고 찍은 문서를 돌려준다. 쓰이지 않았거나 없는 코드면
        None 이다."""
        doc = await self.coll.find_one_and_update(
            {"_id": digest, "grant_id": {"$ne": None}},
            {"$set": {"reused_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        return OAuthCode.from_doc(doc) if doc else None

    async def reused(self, digest: str) -> bool:
        query = {"_id": digest, "reused_at": {"$ne": None}}
        return await self.coll.find_one(query, {"_id": 1}) is not None
