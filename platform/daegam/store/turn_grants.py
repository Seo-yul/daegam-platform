"""턴 위임 (P53 4절).

turn_grants {_id, secret_sha256, agent_id, tenant, sub, username, tv?, trigger, long_bound?,
             run_id?, decided_by?, session_owner?, session_id?, targets: [{kind, id}], calls?,
             opened_at, deadline, renewed_at?, renewals, ended_at?, end_reason?, purge_at}
agent_call_turns {_id: "<Agent id>:<세션 id>:<턴 id>", calls, expires_at}

사람이 한 턴을 한 Agent 에 맡겼다는 플랫폼의 기록이다. 토큰은 열 때와 갱신할 때만 만들고 여기에는
두지 않는다. 갱신의 비밀은 sha256 만 둔다(`store/base.py` 의 `hash_token`). `tv` 는 local 모드에서
그 사람을 확인할 때 쓴 `token_version` 이고 갱신마다 사용자 문서와 다시 맞댄다. `decided_by` 는 남의
세션을 그 소유자의 이름으로 재개한 승인자이고, `session_owner` 는 승인자 자신의 입력 때문에 승인자가
위임의 사람이 된 결정에서의 세션 소유자다. `targets` 는 토큰을 받은 대상의 종류와 id 이고 갱신은
대상을 더하지 않는다. 확인에 실패한 대상은 빼고 다시 넣지 않는다. 이 판이 모르는 종류(뒤 판이 더한
것)의 대상은 갱신이 기록에 두고 토큰만 만들지 않는다.

다른 Agent 를 Tool 로 부르는 호출 (P46 6.4·6.5절)이 세 칸을 쓴다. 종류 `agent` 의 대상은 호출받는
Agent 이고 그 토큰은 플랫폼의 호출 경로만 받는다. `long_bound` 는 뿌리 턴의 한도 종류이고(뿌리가
실행이면 참, 채팅이면 거짓) 계기 `agent` 의 위임과 그 실행을 재개하는 결정의 위임만 적는다. 있으면
열기와 갱신이 계기 대신 그것으로 사람을 본다. `run_id` 는 실행의 턴에 연 위임의 그 실행이고, 호출
경로가 부르는 세션의 실행을 이것으로 찾는다. `calls` 는 이 턴이 시작한 호출 수이고 상한까지만 조건부
갱신 하나로 센다(`count_call`). 레플리카가 여럿이어도 한 턴은 상한을 넘지 못한다. 위임 없는 턴
(header 모드)의 호출은 `agent_call_turns` 가 파드가 댄 세션과 턴으로 같은 방식으로 센다.

끝은 처음 적힌 것이 남는다(`end`). 플랫폼이 스트림이 닫힐 때와 런타임이 받지 않았을 때 스스로
끝내고, 런타임이 턴을 마칠 때 `/end` 로 알린다. `deadline` 은 둘 다 놓친 끝의 뒷받침이다. TTL
인덱스가 마감 이레 뒤(`purge_at`)에 지운다.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import Document, UtcDatetime, ensure_ttl_index

TURN_GRANTS = "turn_grants"
# 마감 뒤 기록이 남는 시간. 누가 어느 턴을 어느 Agent 에 맡겼는지를 감사와 맞대 볼 수 있게 둔다
PURGE_AFTER = timedelta(days=7)

AGENT_CALL_TURNS = "agent_call_turns"
# 위임 없는 턴의 호출 수 기록이 남는 시간. 한 턴은 이보다 짧다
CALL_TURN_TTL = timedelta(days=1)

# 위임을 연 턴의 종류. run 은 백그라운드 실행의 시작이고(P41 6절) agent 는 다른 Agent 가 Tool 로
# 부른 실행의 시작이다(P46 6.4절)
Trigger = Literal["chat", "decide", "regenerate", "run", "agent"]
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
    # 뿌리 턴이 실행이면 참, 채팅이면 거짓이다 (P46 6.4절). 다른 Agent 가 부른 실행의 턴만 적고,
    # 없으면 계기가 한도를 정한다
    long_bound: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    # 실행의 턴에 연 위임이면 그 실행의 id 다
    run_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # 이 턴이 시작한 Agent 호출 수 (P46 6.5절)
    calls: int = Field(default=0, exclude_if=lambda v: v == 0)
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
        self.call_turns = db[AGENT_CALL_TURNS]

    async def init_schema(self) -> None:
        await ensure_ttl_index(self.coll, "purge_at", 0, "ttl_purge_at")
        await ensure_ttl_index(self.call_turns, "expires_at", 0, "ttl_expires_at")

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

    # -- Agent 호출 (P46 6.5절) --------------------------------------------------------------

    async def count_call(self, grant_id: str, *, limit: int, now: datetime) -> bool:
        """이 턴의 호출 하나를 센다. 위임이 열려 있고 마감 전이고 limit 에 닿지 않았을 때만 조건부
        갱신 하나로 올린다. 올리지 못했으면 거짓이다."""
        result = await self.coll.update_one(
            {
                "_id": grant_id,
                "ended_at": None,
                "deadline": {"$gt": now},
                "$or": [{"calls": {"$exists": False}}, {"calls": {"$lt": limit}}],
            },
            {"$inc": {"calls": 1}},
        )
        return result.modified_count == 1

    async def count_turn_call(self, key: str, *, limit: int, now: datetime) -> bool:
        """위임 없는 턴의 호출 하나를 key(Agent, 세션, 턴)로 센다. limit 에 닿았으면 거짓이다.
        조건에 맞는 문서가 없을 때의 upsert 는 이미 찬 문서와 `_id` 가 겹쳐 실패한다."""
        try:
            await self.call_turns.update_one(
                {"_id": key, "calls": {"$lt": limit}},
                {"$inc": {"calls": 1}, "$setOnInsert": {"expires_at": now + CALL_TURN_TTL}},
                upsert=True,
            )
        except DuplicateKeyError:
            return False
        return True

    async def closed(self, grant_ids: Collection[str], now: datetime) -> list[str]:
        """grant_ids 가운데 끝났거나 마감이 지났거나 기록이 없는 위임의 id 다. 그 위임이 시작한
        호출을 지켜보기가 끝낸다."""
        ids = list(grant_ids)
        if not ids:
            return []
        cursor = self.coll.find(
            {"_id": {"$in": ids}, "ended_at": None, "deadline": {"$gt": now}}, {"_id": 1}
        )
        open_ids = {d["_id"] async for d in cursor}
        return [i for i in ids if i not in open_ids]
