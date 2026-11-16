"""Agent 세션의 소유자 기록 (P53 5.1절).

agent_session_owners {_id: "<agent id>/<session id>", agent_id, session_id, sub, username, tv?,
confirmed_at, last_turn_at, expires_at}

승인자가 남의 세션을 승인만 해서 재개하면 그 턴은 소유자의 이름으로 돈다. 플랫폼은 그 소유자를
런타임에 묻지 않고 소유자 자신의 요청이 받아들여졌을 때 적어 둔 이 기록에서 읽는다. 런타임에 물으면
런타임이 대는 사람의 이름으로 토큰을 만들게 되기 때문이다. `tv`(local 모드)와 `confirmed_at` 은
소유자가 그 세션에 낸 마지막 자기 요청(채팅, 다시 생성, 자기 결정)의 것이고, `confirmed_at` 은
플랫폼이 그 요청을 인증한 때다. `username` 은 소유자 대신 만드는 토큰의 `preferred_username` 이다.

기록은 주인이 바뀌지 않는다. `_id` 가 (Agent, 세션) 이고 쓰기는 같은 `sub` 의 문서만 고치므로 다른
사람의 기록이 있는 자리에 쓰면 겹쳐서 아무것도 바뀌지 않는다. 승인자의 결정은 `expires_at` 만
늘린다(`extend`). `expires_at` 은 마지막 턴에 핀된 리비전의 세션 TTL 과 하루를 더한 때이고 TTL
인덱스가 그때 지운다. 그때는 런타임이 세션을 이미 닫았다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import UtcDatetime, ensure_ttl_index

AGENT_SESSION_OWNERS = "agent_session_owners"


def owner_key(agent_id: str, session_id: str) -> str:
    """기록의 `_id` 다. Agent id 에는 `/` 가 없어 어떤 세션 id 와도 겹치지 않는다."""
    return f"{agent_id}/{session_id}"


class SessionOwner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str
    session_id: str
    sub: str
    username: str = ""
    tv: int | None = None
    confirmed_at: UtcDatetime
    last_turn_at: UtcDatetime
    expires_at: UtcDatetime


class MongoAgentSessionOwnerStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_SESSION_OWNERS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "expires_at", 0, "ttl_expires_at")

    async def get(self, agent_id: str, session_id: str) -> SessionOwner | None:
        doc = await self.coll.find_one({"_id": owner_key(agent_id, session_id)})
        return SessionOwner.model_validate(doc) if doc is not None else None

    async def confirm(self, owner: SessionOwner) -> bool:
        """소유자 자신의 요청이 받아들여졌다. 기록이 없으면 만들고 같은 사람의 기록이면 `tv`,
        `confirmed_at`, `last_turn_at`, `expires_at` 을 고친다. 다른 사람의 기록이 있으면 아무것도
        바꾸지 않고 False 다."""
        key = owner_key(owner.agent_id, owner.session_id)
        try:
            # 새로 만들 때는 조건의 `_id`·`sub` 가 문서에 들어간다
            await self.coll.update_one(
                {"_id": key, "sub": owner.sub},
                {"$set": owner.model_dump(exclude={"sub"})},
                upsert=True,
            )
        except DuplicateKeyError:
            return False
        return True

    async def extend(
        self, agent_id: str, session_id: str, *, sub: str, expires_at: datetime
    ) -> None:
        """승인자가 소유자의 이름으로 재개했다. 그 사람의 기록이면 `expires_at` 만 늘린다. 승인이
        이어지는 동안 세션이 열려 있어도 기록이 먼저 사라지지 않게 하되 소유자 자신의 요청을
        대신하지는 않는다."""
        await self.coll.update_one(
            {"_id": owner_key(agent_id, session_id), "sub": sub},
            {"$max": {"expires_at": expires_at}},
        )
