"""LLM 게이트웨이의 회수 목록 `llm_revocations` (P38 5절).

`_id` 가 jti 이고 토큰의 `iat` 과 만료(`expires_at`, TTL)를 든다. 게이트웨이를 꺼 둔 동안에도 쓴다.
그동안 해제한 Agent 의 토큰도 다시 켰을 때 목록에 있다. 렌더는 켠 시각(`activated_at`) 이후에 낸
토큰만 싣는다. 그 앞의 토큰은 리스너의 `jwt.iat` 규칙이 이미 막는다.
"""

from __future__ import annotations

from datetime import datetime

from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import ensure_ttl_index, utcnow

LLM_REVOCATIONS = "llm_revocations"


class MongoLlmRevocationStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[LLM_REVOCATIONS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "expires_at", 0, "llm_revocations_ttl")

    async def add(self, jti: str, *, iat: datetime, exp: datetime) -> None:
        """같은 jti 를 두 번 넣어도 하나다. at 은 처음 넣은 때다."""
        await self.coll.update_one(
            {"_id": jti},
            {"$setOnInsert": {"iat": iat, "expires_at": exp, "at": utcnow()}},
            upsert=True,
        )

    async def listed(self, since: datetime) -> list[str]:
        """since 이후에 내고 아직 만료되지 않은 jti(정렬). TTL 은 1분마다 돌므로 만료를 다시
        본다."""
        query = {"iat": {"$gte": since}, "expires_at": {"$gt": utcnow()}}
        return sorted([d["_id"] async for d in self.coll.find(query, {"_id": 1})])
