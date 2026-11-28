"""Agent 의 인스턴스 토큰 (D26).

배포할 때 하나 발급한다. initContainer 가 이 토큰으로 `agent.json` 을 받고, 같은 값이
플랫폼에서 런타임으로 가는 내부 호출의 공유 비밀 헤더다. Mongo 에는 sha256 만 두고 평문은 (F3 이
만드는) 테넌트 네임스페이스 Secret 에만 있다. 범위는 `agent-profile:{id}:rev:{n}` 읽기 하나다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field
from pymongo import ASCENDING
from pymongo.asynchronous.database import AsyncDatabase

from daegam.errors import unauthorized
from daegam.store.base import (
    Document,
    UtcDatetime,
    hash_token,
    match_token,
    new_id,
    revoke_once,
    utcnow,
)

if TYPE_CHECKING:
    from daegam.store.agents import Agent, MongoAgentStore

AGENT_INSTANCE_TOKENS = "agent_instance_tokens"


def scope_for(profile_id: str, revision: int) -> str:
    return f"agent-profile:{profile_id}:rev:{revision}"


class AgentInstanceToken(Document):
    id: str = Field(default_factory=new_id)
    agent_instance_id: str
    # 늘 `agent_instance_id` 와 같고(D42) 읽는 코드는 없다. 2.0.0-rc1 의 모델이 이 칸을 요구하므로
    # 그 판으로 되돌려도 토큰을 읽도록 계속 쓴다
    agent_profile_id: str
    token_sha256: str
    scope: list[str] = Field(default_factory=list)
    created_at: UtcDatetime = Field(default_factory=utcnow)
    revoked_at: UtcDatetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class MongoAgentInstanceTokenStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[AGENT_INSTANCE_TOKENS]

    async def init_schema(self) -> None:
        await self.coll.create_index(
            [("token_sha256", ASCENDING)], unique=True, name="uq_token_sha256"
        )
        await self.coll.create_index([("agent_instance_id", ASCENDING)], name="ix_instance")

    async def create(
        self, *, instance_id: str, profile_id: str, token_sha256: str, scope: list[str]
    ) -> AgentInstanceToken:
        tok = AgentInstanceToken(
            agent_instance_id=instance_id,
            agent_profile_id=profile_id,
            token_sha256=token_sha256,
            scope=list(scope),
        )
        await self.coll.insert_one(tok.to_doc())
        return tok

    async def get(self, token_id: str) -> AgentInstanceToken | None:
        doc = await self.coll.find_one({"_id": token_id})
        return AgentInstanceToken.from_doc(doc) if doc else None

    async def get_by_hash(self, token_sha256: str) -> AgentInstanceToken | None:
        """제시된 토큰의 해시로 찾는다(유일 색인). 런타임 이벤트 수집 경로가 토큰만으로 인스턴스를
        정한다(P37 3절)."""
        doc = await self.coll.find_one({"token_sha256": token_sha256})
        return AgentInstanceToken.from_doc(doc) if doc else None

    async def list(
        self, instance_id: str, *, active_only: bool = False
    ) -> list[AgentInstanceToken]:
        q: dict[str, Any] = {"agent_instance_id": instance_id}
        if active_only:
            q["revoked_at"] = None
        cursor = self.coll.find(q).sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [AgentInstanceToken.from_doc(d) for d in await cursor.to_list()]

    async def revoked_flags(self, token_ids: list[str]) -> dict[str, bool]:
        """id → 회수됐는가. 한 번에 읽는다. 없는 id 는 결과에 없다."""
        if not token_ids:
            return {}
        cursor = self.coll.find({"_id": {"$in": token_ids}}, {"revoked_at": 1})
        return {d["_id"]: d.get("revoked_at") is not None for d in await cursor.to_list()}

    async def revoke(self, token_id: str) -> bool:
        return await revoke_once(self.coll, {"_id": token_id})

    async def add_scope(self, token_id: str, scope: str) -> bool:
        """redeploy 가 핀을 옮길 때 같은 토큰에 새 리비전 읽기를 더한다. 파드 env 와 플랫폼이
        런타임을 부를 때 쓰는 비밀이 같은 값이라 토큰을 바꾸면 롤링 중 옛 파드가 401 을 내기
        때문이다."""
        res = await self.coll.update_one(
            {"_id": token_id, "revoked_at": None}, {"$addToSet": {"scope": scope}}
        )
        return res.matched_count == 1

    async def revoke_for_instance(self, instance_id: str) -> int:
        """인스턴스의 살아 있는 토큰을 전부 회수한다. 멱등이다."""
        res = await self.coll.update_many(
            {"agent_instance_id": instance_id, "revoked_at": None},
            {"$set": {"revoked_at": utcnow()}},
        )
        return res.modified_count


async def authenticate_instance_token(
    store: MongoAgentInstanceTokenStore, instance_id: str, presented: str, *, scope: str
) -> AgentInstanceToken:
    """토큰이 이 인스턴스의 것이고 회수되지 않았고 scope 에 요청 리비전이 있을 때만 통과.
    비교는 `match_token` 이 상수 시간으로 한다."""
    matched = match_token(await store.list(instance_id, active_only=True), presented)
    if matched is None:
        raise unauthorized("invalid or revoked token")
    if scope not in matched.scope:
        raise unauthorized("token scope does not include this revision")
    return matched


async def pinned_agent(
    agents: MongoAgentStore, tokens: MongoAgentInstanceTokenStore, agent_id: str, presented: str
) -> Agent:
    """인스턴스 토큰이 이 Agent 의 것이고 회수되지 않았으며 scope 에 핀된 리비전
    `agent-profile:{문서 id}:rev:{n}` 이 있을 때의 그 문서다. 테넌트 헤더는 보지 않는다. 존재 여부를
    토큰 없이 드러내지 않으므로 못 찾으면 401 이다. initContainer 의 `agent.json`·Skill 읽기와 턴
    위임의 갱신·끝 경로, 샌드박스 열기가 쓴다. 미리 보기의 토큰도 받으므로 미리 보기까지 읽고, 그
    자격이 끝난 미리 보기(끝 + 10분)는 401 이다 (P44b 5절, 8절)."""
    doc = await agents.with_previews().get(agent_id)
    if doc is None or doc.deployed_revision is None:
        raise unauthorized("invalid token")
    if doc.preview is not None and utcnow() >= doc.preview.credentials_end:
        # 인스턴스 토큰에는 만료가 없으므로 미리 보기의 자격이 끝나면 지우기를 기다리지 않고
        # 거절한다 (P44b 8절)
        raise unauthorized("invalid token")
    await authenticate_instance_token(
        tokens, doc.id, presented, scope=scope_for(doc.id, doc.deployed_revision)
    )
    return doc


async def agent_of_token(
    agents: MongoAgentStore, tokens: MongoAgentInstanceTokenStore, presented: str
) -> Agent:
    """인스턴스 토큰만으로 정한 그 Agent 의 문서다. 토큰이 회수되지 않았고 문서가 배포돼 있을
    때이며, 자격이 끝난 미리 보기는 `pinned_agent` 처럼 거절한다. 못 찾으면 401 이다. 경로가 부르는
    Agent 의 id 를 싣지 않는 호출 경로의 header 모드가 쓴다. agents 는 미리 보기도 읽는 보기다."""
    found = await tokens.get_by_hash(hash_token(presented))
    doc = await agents.get(found.agent_instance_id) if found is not None and found.active else None
    if doc is None or doc.deployed_revision is None:
        raise unauthorized("invalid or revoked token")
    if doc.preview is not None and utcnow() >= doc.preview.credentials_end:
        raise unauthorized("invalid or revoked token")
    return doc
