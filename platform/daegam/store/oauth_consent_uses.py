"""OAuth 요청 핸들의 사용 기록 (P33 3절).

oauth_consent_uses {_id: jti, sub, decision, created_at, expires_at}

인가 요청은 서버에 두지 않고 서명한 핸들로 동의 화면에 간다. 처음 저장되는 것이 로그인한 사람이
그 핸들을 허용하거나 거절한 기록이다. `_id` 가 핸들의 `jti` 라 같은 핸들을 두 번 쓰면 겹쳐 409
`request_used` 가 된다. 기록은 핸들보다 오래 남아야 한다. 먼저 지워지면 아직 살아 있는 핸들을
다시 쓸 수 있기 때문이다. 그래서 `expires_at` 은 핸들의 `exp` 에 `KEEP_AFTER_HANDLE` 를 더한
값이고 TTL 인덱스가 그때 지운다. 한 사람의 허용 상한은 `ix_sub_created` 로 최근 허용을 센다.
허용은 기록을 넣은 뒤 세고, 상한에 걸리면 그 기록을 지운다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import ensure_ttl_index

OAUTH_CONSENT_USES = "oauth_consent_uses"
# 핸들이 만료한 뒤 기록을 더 두는 시간 (17절)
KEEP_AFTER_HANDLE = timedelta(minutes=10)

Decision = Literal["approve", "deny"]
APPROVE: Decision = "approve"
DENY: Decision = "deny"


class MongoOAuthConsentUseStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[OAUTH_CONSENT_USES]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("sub", ASCENDING), ("created_at", DESCENDING)], name="ix_sub_created"
        )
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def used(self, jti: str) -> bool:
        return await self.coll.find_one({"_id": jti}, {"_id": 1}) is not None

    async def record(
        self, jti: str, *, sub: str, decision: Decision, now: datetime, handle_exp: datetime
    ) -> bool:
        """핸들 사용을 적는다. 이미 쓰인 핸들이면 False 다."""
        doc = {
            "_id": jti,
            "sub": sub,
            "decision": decision,
            "created_at": now,
            "expires_at": handle_exp + KEEP_AFTER_HANDLE,
        }
        try:
            await self.coll.insert_one(doc)
        except DuplicateKeyError:
            return False
        return True

    async def forget(self, jti: str) -> None:
        """허용 상한에 걸린 기록을 지운다. 걸린 허용은 핸들을 쓰지 않는다."""
        await self.coll.delete_one({"_id": jti})

    async def approvals_since(self, sub: str, since: datetime) -> int:
        return await self.coll.count_documents(
            {"sub": sub, "created_at": {"$gte": since}, "decision": APPROVE}
        )
