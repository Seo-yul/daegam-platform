"""로그인 실패 잠금 (D40).

login_failures {_id: username, count, first_failure_at, locked_until, expires_at}

사용자 이름마다 15분 안에 5번 틀리면 15분 잠긴다. 문서는 TTL 인덱스로 창이 끝나면 사라지므로
성공하지 않아도 스스로 풀린다. 레플리카가 여럿이라 메모리가 아니라 Mongo 다.

한계를 안다. 사용자 이름별 잠금은 남의 계정을 15분 잠글 수 있는 길이다. IP 별 잠금은 Ingress
뒤에서 X-Forwarded-For 를 믿어야 해 두지 않았다. 잠긴 동안에도 응답은 429 하나라 비밀번호를 맞춰
볼 수는 없다.

Teams 의 연결 코드(P47 6.3절)도 같은 꼴을 쓴다. 열쇠는 `teams-code:` 로 시작해 사용자 이름(`users`
의 `USERNAME_RE`, 콜론이 없다)과 겹치지 않고, 창과 잠금은 한 시간이다(`max_failures`·`window`·
`lock`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import utcnow

LOGIN_FAILURES = "login_failures"
MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
LOCK = timedelta(minutes=15)


class MongoLoginThrottle:
    def __init__(
        self,
        db: AsyncDatabase,
        *,
        max_failures: int = MAX_FAILURES,
        window: timedelta = WINDOW,
        lock: timedelta = LOCK,
    ):
        self.coll = db[LOGIN_FAILURES]
        self.max_failures = max_failures
        self.window = window
        self.lock = lock

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_expires_at"
        )

    async def locked_until(self, username: str) -> datetime | None:
        doc = await self.coll.find_one({"_id": username})
        until = (doc or {}).get("locked_until")
        if until and until > utcnow():
            return until
        return None

    async def record_failure(self, username: str) -> datetime | None:
        """실패 하나를 더한다. 이번 실패로 잠기면 그 시각을 돌려준다.

        창은 첫 실패에서 시작해 고정이다. 실패마다 창을 밀면 "15분 안에 5번" 이 아니라 "간격이
        15분을 넘지 않은 5번" 이 되어 한 시간에 걸친 다섯 번도 잠긴다.

        창이나 잠금이 끝난 문서는 먼저 지운다. TTL 인덱스는 1분쯤 늦게 지우므로, 그 사이의 실패가
        지난 잠금 시각을 돌려받아 429 가 되거나 끝난 창의 실패 수를 이어받으면 안 된다."""
        now = utcnow()
        await self.coll.delete_one({"_id": username, "expires_at": {"$lte": now}})
        doc = await self.coll.find_one_and_update(
            {"_id": username},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {"first_failure_at": now, "expires_at": now + self.window},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if doc["count"] >= self.max_failures and not doc.get("locked_until"):
            until = now + self.lock
            await self.coll.update_one(
                {"_id": username}, {"$set": {"locked_until": until, "expires_at": until}}
            )
            return until
        return doc.get("locked_until")

    async def clear(self, username: str) -> None:
        await self.coll.delete_one({"_id": username})
