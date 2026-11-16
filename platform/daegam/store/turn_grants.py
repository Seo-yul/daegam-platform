"""턴 위임 (P53 4절).

turn_grants {_id, secret_sha256, agent_id, tenant, sub, username, tv?, trigger, long_bound?,
             decided_by?, session_owner?, session_id?, targets: [{kind, id}], opened_at, deadline,
             renewed_at?, renewals, ended_at?, end_reason?, purge_at}

사람이 한 턴을 한 Agent 에 맡겼다는 플랫폼의 기록이다. 토큰은 열 때와 갱신할 때만 만들고 여기에는
두지 않는다. 갱신의 비밀은 sha256 만 둔다(`store/base.py` 의 `hash_token`). `tv` 는 local 모드에서
그 사람을 확인할 때 쓴 `token_version` 이고 갱신마다 사용자 문서와 다시 맞댄다. `decided_by` 는 남의
세션을 그 소유자의 이름으로 재개한 승인자이고, `session_owner` 는 승인자 자신의 입력 때문에 승인자가
위임의 사람이 된 결정에서의 세션 소유자다. `targets` 는 토큰을 받은 대상의 종류와 id 이고 갱신은
대상을 더하지 않는다. 확인에 실패한 대상은 빼고 다시 넣지 않는다. 이 판이 모르는 종류(뒤 판이 더한
것, P46 의 `agent`)의 대상은 갱신이 기록에 두고 토큰만 만들지 않는다. `long_bound` 는 뒤 판(P46)이
적는 뿌리 턴의 한도 종류이고, 있으면 갱신이 계기 대신 그것으로 사람을 본다 (P46 A0).

끝은 처음 적힌 것이 남는다(`end`). 플랫폼이 스트림이 닫힐 때와 런타임이 받지 않았을 때 스스로
끝내고, 런타임이 턴을 마칠 때 `/end` 로 알린다. `deadline` 은 둘 다 놓친 끝의 뒷받침이다. TTL
인덱스가 마감 이레 뒤(`purge_at`)에 지운다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index

TURN_GRANTS = "turn_grants"
# 마감 뒤 기록이 남는 시간. 누가 어느 턴을 어느 Agent 에 맡겼는지를 감사와 맞대 볼 수 있게 둔다
PURGE_AFTER = timedelta(days=7)

# 위임을 연 턴의 종류. run 은 백그라운드 실행의 시작이다(P41 6절)
Trigger = Literal["chat", "decide", "regenerate", "run"]
# 런타임이 `/end` 에 싣는 끝난 까닭 (6절). 런타임의 `runtime/turn_grants.py` 의 `END_*` 와 같다
RuntimeEndReason = Literal["done", "error", "interrupt", "cancelled", "shutdown"]


class GrantTarget(BaseModel):
    """토큰을 받은 대상 하나. `agent.json` 의 `tools[].source` 와 같은 종류와 id 다."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str
    id: str


class TurnGrant(Document):
    id: str
    secret_sha256: str
    agent_id: str
    tenant: str
    sub: str
    username: str | None = None
    tv: int | None = None
    # 읽을 때는 모르는 종류도 받는다. 롤링 중에 새 판이 연 위임을 옛 판이 갱신할 수 있다
    trigger: str
    # 뿌리 턴이 실행이면 참, 채팅이면 거짓이다 (P46 6.4절). 이 판은 적지 않고 읽기만 한다. 없으면
    # 계기가 한도를 정한다
    long_bound: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    decided_by: str | None = None
    session_owner: str | None = None
    session_id: str | None = None
    targets: list[GrantTarget]
    opened_at: UtcDatetime
    deadline: UtcDatetime
    renewed_at: UtcDatetime | None = None
    renewals: int = 0
    ended_at: UtcDatetime | None = None
    end_reason: str | None = None
    purge_at: UtcDatetime


class MongoTurnGrantStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[TURN_GRANTS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "purge_at", 0, "ttl_purge_at")

    async def insert(self, grant: TurnGrant) -> None:
        await self.coll.insert_one(grant.to_doc())

    async def get(self, grant_id: str) -> TurnGrant | None:
        doc = await self.coll.find_one({"_id": grant_id})
        return TurnGrant.from_doc(doc) if doc else None

    async def attach_session(self, grant_id: str, session_id: str) -> None:
        """새 세션의 위임에 런타임이 답한 세션 id 를 적는다. 이미 적힌 세션은 바꾸지 않는다."""
        await self.coll.update_one(
            {"_id": grant_id, "session_id": None}, {"$set": {"session_id": session_id}}
        )

    async def record_renewal(
        self, grant_id: str, *, dropped: list[GrantTarget], now: datetime
    ) -> bool:
        """끝나지 않은 위임의 갱신을 적는다. dropped 는 이번에 확인에 실패한 대상이고 기록에서 빼며
        다시 넣지 않는다. 두 갱신이 겹쳐도 한쪽이 뺀 대상을 다른 쪽이 되살리지 않는다. 그사이
        끝났으면 False 다."""
        update: dict[str, Any] = {"$set": {"renewed_at": now}, "$inc": {"renewals": 1}}
        if dropped:
            update["$pull"] = {"targets": {"$in": [t.model_dump() for t in dropped]}}
        result = await self.coll.update_one({"_id": grant_id, "ended_at": None}, update)
        return result.matched_count == 1

    async def end(self, grant_id: str, reason: str, now: datetime) -> bool:
        """위임을 끝낸다. 처음 적힌 끝이 남으므로 이미 끝난 위임은 바꾸지 않고 거짓이다."""
        result = await self.coll.update_one(
            {"_id": grant_id, "ended_at": None},
            {"$set": {"ended_at": now, "end_reason": reason}},
        )
        return result.modified_count == 1
