"""턴 위임과 그 토큰 (P53 1·2·4절, D91).

사람이 턴을 시작하거나 이어 가면 채팅·결정·다시 생성 라우트가 런타임을 부르기 전에 위임을 연다
(`open`). 위임은 Agent 의 핀된 리비전이 쓰는 Tool 대상마다, 규칙이 허락하는 대상에만 토큰을 하나씩
만든다. 토큰은 `sub` 가 그 사람, `act` 가 Agent 의 서비스 신원, `bound_to` 가 그 대상 하나인
300초짜리 JWT 이고 `groups`·`cid`·`tv`·`scope` 를 싣지 않는다. 그래서 지금까지의 경계는 모두
거절하고 자기 대상을 이름으로 적은 경계만 받는다.

- **Gateway 대상**은 데이터 플레인이 플랫폼을 믿는 모드(local, platform 인 oidc, P53b 6.1절)에서만
  이다. 사람 발급자와 `aud: daegam-mcp` 로 낸다. Gateway 가
  묶임을 싣고(`carries_binding`), 소유자 Cedar 가 없고, Agent 클러스터의 Gateway 게이트가 열렸을
  때다. 게이트의 판정이 오래됐으면 Agent 이름으로 돌리지 않고 503
  `turn_tokens_unavailable` 이다.
- **MCP Server 대상**은 토큰 인증 모드(local·oidc)에서 그 서버가 자기 audience 로 옮겨 간 뒤
  (`audience_applied`)이다. 서비스 발급자와 그 서버 자신의 audience 하나로 낸다.
- **Agent 대상**(P46 6.4절)은 핀된 리비전이 Tool 로 쓰는 게시한 Agent 다. 토큰 인증 모드에서
  호출이 켜져 있고 그 Agent 가 같은 테넌트에 있을 때다. 서비스 발급자와 그 Agent 의 호출 audience
  (`agent_call_audience`)로 내고 `bound_to` 는 `agent-call:<id>`, 위임의 id 를 함께 싣는다. 플랫폼의
  호출 경로만 받는 토큰이라 데이터 플레인의 발급자, 클러스터의 ToolHive 버전과 상관없다.
- 모든 대상은 설정이 켜졌고, 서명 키가 붙었고, 대상의 클러스터가 ToolHive 0.53 이상이고, Agent 의
  런타임 기록에 `turn_tokens` 가 있고, 사람이 확인을 지날 때다. 그렇지 않은 대상은 Agent 자신의
  자격으로 부른다.

**사람의 확인**은 열 때와 갱신할 때마다 접근 정리와 같은 판정(`AccessRevoker.holder_lost`)으로 한다.
모든 모드에서 Agent 테넌트의 소속 줄을 보고, local 모드는 사용자 문서와 `tv` 를, oidc 모드는 사람
기록(`idp_people`)을 본다. oidc 모드에서는 마지막 IdP 확인(`confirmed_at`)에서 한도까지 토큰 한
수명이 남아야 한다 (P53b 6.3절). 실행은 긴 한도(`idp_confirm_seconds`)이고 넘으면 `unconfirmed` 다.
채팅, 결정, 다시 생성은 대화형 한도(`turn_owner_confirm_seconds`)이고 넘으면 `confirm_bound` 다.
자기 턴은 그 요청이 유효한 IdP 토큰으로 사람이 있음을 보였으므로 열 때는 대화형 한도를 보지 않고,
승인자가 소유자의 이름으로 재개하면 열 때도 소유자의 사람 기록으로 본다. 갱신은 언제나 본다. 화면을
연 사람은 UI 가 IdP 토큰을 갱신할 때마다 확인되므로 긴 턴의 Tool 을 지키고, 떠난 사람은 한도 안에
잃는다. 다른 Agent 가 부른 실행의 턴(계기 `agent` 와 그 실행을 재개하는 결정)은 뿌리 턴의 한도
종류(`long_bound`)를 위임에 적고, 열기와 갱신이 계기 대신 그것으로 본다. 뿌리가 채팅이면 사람이 이
요청에 없으므로 열 때도 대화형 한도를 본다.
대화형 한도를 넘긴 갱신은 410 이라 턴은 이어 가고 Tool 호출만 실패한다. 런타임은 그 위임을
다시 갱신하지 않으므로 사람의 다음 턴까지 그 턴의 Tool 은 꺼진 채다. 접근을 잃은 사람의 갱신은 403
이라 런타임이 턴을 멈춘다. 턴이 연결과 떨어져 돌므로(P78 8절) 마감이 아니라 갱신의 확인이 IdP 에서
빠진 사람의 턴을 묶는다. 마감은 모든 모드에서 턴 시한과 여유다.

**갱신**(`renew`)은 인스턴스 토큰과 위임의 비밀을 보고, 위임이 이 Agent 의 것이고 끝나지 않았고
마감 전인지, 사람이 여전히 확인을 지나는지, 대상이 여전히 같은 규칙을 지나는지를 이 차례로 본다.
클러스터 게이트는 다시 보지 않는다. 같은 클레임에 마감이 자르는 새 `exp` 로 다시 만들고, 대상을
더하거나 마감을 늘리지 않는다. 끝(`end`)은 처음 적힌 것이 남는다.

두 기계 경로는 Agent 마다 따로 센 버킷을 쓴다. 이벤트가 몰려도 갱신을 밀어내지 않는다. 비밀은
요청 본문에만 있고 기록에는 그 해시만 남는다.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

import jwt
from pymongo.errors import PyMongoError

from daegam.auth.cedar import GATEWAY_TARGET, MCP_SERVER_TARGET
from daegam.auth.service_token import (
    AGENT_CALL_TARGET,
    SERVICE_SUB_PREFIX,
    InternalIssuer,
    service_sub,
    turn_token_claims,
)
from daegam.auth.signer import agent_call_audience, server_audience
from daegam.config import Settings
from daegam.config.resolve import EffectiveConfig
from daegam.errors import ServiceError, retry_after_header, unauthorized
from daegam.k8s.templates.agent import agent_config_base
from daegam.services.access_revocation import AccessRevoker, HeldAccess
from daegam.services.agent_features import TURN_TOKENS
from daegam.services.agent_graph import (
    AGENT_SOURCE,
    AgentData,
    EntryData,
    analyze_structure,
    tool_sources,
)
from daegam.services.agent_runtime import serves_turns
from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.services.turn_gates import (
    TurnGates,
    audience_applied,
    carries_binding,
    owner_cedar,
    version_ok,
)
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, pinned_agent
from daegam.store.agents import Agent, AgentRevision, MongoAgentStore
from daegam.store.base import hash_token, utcnow
from daegam.store.clusters import Cluster, MongoClusterStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.mcp_servers import MongoMCPServerStore
from daegam.store.turn_grants import (
    PURGE_AFTER,
    GrantTarget,
    MongoTurnGrantStore,
    Trigger,
    TurnGrant,
)

logger = logging.getLogger("daegam.turn_grants")

# 대상의 종류(`agent.json` 의 `tools[].source.kind`)마다 `bound_to` 의 꼴. vMCP 의 Cedar 가 같은
# 꼴(`auth/cedar.py`)을 글자 그대로 맞댄다 (3절)
BOUND_TO = {
    "gateway_instance": GATEWAY_TARGET,
    "mcp_instance": MCP_SERVER_TARGET,
    AGENT_SOURCE: AGENT_CALL_TARGET,
}
# 마감은 핀된 리비전의 턴 시한에 이만큼을 더한다. 런타임의 `/end` 를 놓친 때의 뒷받침이다
DEADLINE_SLACK = timedelta(seconds=60)
# 위임 id 와 비밀의 무작위 바이트. 128비트다
SECRET_BYTES = 16
# 플랫폼이 스스로 끝내는 까닭 (4절). 런타임이 오류 상태로 답했다, 답 전에 연결이 끊겼다, 답이 위임
# id 를 되돌려주지 않았다, 플랫폼이 런타임을 부르기 전에 턴을 멈췄다
END_REFUSED = "refused"
END_UNREACHABLE = "unreachable"
END_NOT_ACCEPTED = "not_accepted"
END_NOT_SENT = "not_sent"
# oidc 모드의 대화형 턴에서 사람의 마지막 IdP 확인이 대화형 한도보다 멀 때의 거절 까닭 (P53b 6.3절)
CONFIRM_BOUND = "confirm_bound"
# 긴 한도로 사람을 보는 계기. 사람 없이 시작한 실행이다
LONG_BOUND_TRIGGERS: frozenset[Trigger] = frozenset({"run"})
# 사람이 지금 보낸 턴의 계기. 뿌리의 한도 종류(`long_bound`)를 싣지 않으므로 갱신이 그 칸의 참을
# 믿지 않는다 (P46 6.4절, A0)
PRESENT_TRIGGERS: frozenset[Trigger] = frozenset({"chat", "regenerate"})
# 다른 Agent 가 Tool 로 부른 실행의 시작이다. 사람이 이 요청에 없다 (P46 6.4절)
CALL_TRIGGER: Trigger = "agent"
# 그래프를 읽지 못할 때의 턴 시한과 세션 TTL. 런타임 스키마의 기본값과 같다
DEFAULT_TURN_TIMEOUT = AgentData.model_fields["turn_timeout_seconds"].default
DEFAULT_SESSION_TTL = EntryData.model_fields["session_ttl_seconds"].default
# 기계 경로 두 개의 Agent 마다의 버킷. 턴마다 몇 분에 한 번의 갱신과 한 번의 끝이고, 파드가 내려가며
# 열린 턴(세션 동시 상한의 기본 100)을 한꺼번에 끝내는 것까지 받는다. 한 시간 오지 않으면 잊는다
REQUESTS_PER_SECOND = 10.0
REQUEST_BURST = 100.0
RETRY_AFTER = timedelta(seconds=1)
LIMITS_IDLE_SECONDS = 3600.0
LIMITS_ENTRIES = 10_000

TURN_TOKENS_UNAVAILABLE = "turn_tokens_unavailable"


class TurnTokensUnavailable(ServiceError):
    """규칙이 토큰을 주는 대상이 있는데 만들거나 적지 못했다. 턴은 시작하지 않는다 (2절). 앱의 전역
    처리기가 503 으로 답한다."""

    def __init__(self, detail: str):
        super().__init__(503, TURN_TOKENS_UNAVAILABLE, detail)


class TurnGrantError(ServiceError):
    """갱신과 끝내기 경로의 거절이다."""


@dataclass(frozen=True)
class TurnLimits:
    """핀된 리비전의 메인 에이전트 턴 시한과 entry 의 세션 TTL(초)."""

    turn_timeout_seconds: int
    session_ttl_seconds: int


def turn_limits(agent: Agent) -> TurnLimits:
    """위임의 마감과 세션 소유자 기록의 만료가 쓰는 한도다. 핀이 없거나 그래프를 읽지 못하면 런타임
    스키마의 기본값이다."""
    return revision_limits(agent.deployed)


def revision_limits(rev: AgentRevision | None) -> TurnLimits:
    """리비전 하나의 한도. 배포가 앞 리비전과 새 리비전을 견줄 때도 쓴다."""
    if rev is None:
        return TurnLimits(DEFAULT_TURN_TIMEOUT, DEFAULT_SESSION_TTL)
    a = analyze_structure(rev)
    main = a.agent(a.main) if a.main is not None else None
    entry = a.typed(a.entry, EntryData) if a.entry is not None else None
    return TurnLimits(
        main.turn_timeout_seconds if main is not None else DEFAULT_TURN_TIMEOUT,
        entry.session_ttl_seconds if entry is not None else DEFAULT_SESSION_TTL,
    )


def tool_targets(agent: Agent) -> list[GrantTarget]:
    """핀된 리비전이 쓰는 Tool 대상이다. 토큰은 `ref` 가 아니라 대상에 속하므로 롤아웃 중에 앞
    리비전의 파드가 같은 대상을 다른 `ref` 로 불러도 맞는다 (2절)."""
    rev = agent.deployed
    if rev is None:
        return []
    return [GrantTarget(kind=kind, id=source_id) for kind, source_id in tool_sources(rev)]


def call_targets(agent: Agent) -> list[GrantTarget]:
    """핀된 리비전이 Tool 로 쓰는 게시한 Agent 다 (P46 6.4절). 배포가 핀에 박은 호출받는 Agent 의
    id 이고 agent.json 의 Agent 소스가 같은 id 를 싣는다."""
    ids = {pin.call.agent_id for pin in agent.tool_pins.values() if pin.call is not None}
    return [GrantTarget(kind=AGENT_SOURCE, id=agent_id) for agent_id in sorted(ids)]


@dataclass(frozen=True)
class GrantPerson:
    """위임을 받을 사람. tv 는 local 모드에서 맞댈 `token_version`(호출자 토큰의 것, 또는 승인자가
    소유자 대신 재개할 때 소유자 기록의 것)이다."""

    sub: str
    username: str
    tv: int | None = None


@dataclass(frozen=True)
class OpenedGrant:
    id: str
    # 런타임 본문의 `turn_grant` 다. 비밀과 토큰이 들어 있으므로 런타임에 보내는 본문 밖으로 내지
    # 않는다. repr 에서도 뺀다
    turn_grant: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class OpenOutcome:
    """grant 가 None 이면 이 턴은 Agent 의 자격으로 돈다. refused 는 사람이 확인을 지나지 못한
    까닭이다(`lost_access` 의 까닭 또는 `confirm_bound`). no_targets 는 위임을 열 수 있었지만
    규칙이 토큰을 주는 대상이 하나도 없었다는 뜻이다. 다른 Agent 가 부른 실행은 이때만 위임 없이
    돈다 (P46 6.4절)."""

    grant: OpenedGrant | None
    refused: str | None = None
    no_targets: bool = False


class TurnGrantService:
    # 위임이 끝나면 그 턴이 시작한 Agent 호출을 끝낸다 (P46 6.5절). 호출 서비스가 이 서비스를
    # 받으므로 만든 뒤에 붙인다 (wiring.py `_check_wiring`)
    late_hooks: ClassVar[frozenset[str]] = frozenset({"on_end"})

    def __init__(
        self,
        grants: MongoTurnGrantStore,
        *,
        agents: MongoAgentStore,
        instance_tokens: MongoAgentInstanceTokenStore,
        gateways: MongoGatewayStore,
        mcp_servers: MongoMCPServerStore,
        clusters: MongoClusterStore,
        revoker: AccessRevoker,
        gates: TurnGates,
        issuer: InternalIssuer,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.grants = grants
        self.agents = agents
        self.instance_tokens = instance_tokens
        self.gateways = gateways
        self.mcp_servers = mcp_servers
        self.clusters = clusters
        # 사람의 판정은 접근 정리의 것을 쓴다
        self.revoker = revoker
        self.gates = gates
        self.issuer = issuer
        self.settings = settings
        self.on_end: Callable[[str], Awaitable[None]] | None = None
        self._limits = KeyedBuckets(
            REQUESTS_PER_SECOND,
            REQUEST_BURST,
            idle_seconds=LIMITS_IDLE_SECONDS,
            max_entries=LIMITS_ENTRIES,
            clock=clock,
        )

    @property
    def _local(self) -> bool:
        return self.settings.auth_mode == "local"

    # -- 열기 -------------------------------------------------------------------------------------

    async def open(
        self,
        *,
        agent: Agent,
        person: GrantPerson,
        trigger: Trigger,
        session_id: str | None,
        decided_by: str | None = None,
        session_owner: str | None = None,
        long_bound: bool | None = None,
        run_id: str | None = None,
    ) -> OpenOutcome:
        """위임을 열고 런타임에 줄 `turn_grant` 를 만든다. 사람이 확인을 지나지 못하거나 규칙이
        토큰을 주는 대상이 없으면 위임이 없다. 사람을 먼저 본다. 확인을 지나지 못한 사람은 이름이
        대는 까닭이 있어 Agent 의 이름으로 돌고, 게이트의 판정이 오래됐다고 503 을 받지 않는다
        (2절). 토큰을 주어야 하는 대상이 있는데 만들거나 적지 못하면 TurnTokensUnavailable 이다.
        long_bound 는 다른 Agent 가 부른 실행의 턴이 싣는 뿌리 턴의 한도 종류이고 run_id 는 실행의
        턴이 싣는 그 실행이다."""
        if not self._mints_for(agent) or person.sub.startswith(SERVICE_SUB_PREFIX):
            return OpenOutcome(None)
        now = utcnow()
        refused, username = await self._check_person(
            person.sub,
            person.tv,
            agent.tenant,
            now,
            interactive=_opens_interactive(trigger, decided_by, long_bound),
        )
        if refused is not None:
            return OpenOutcome(None, refused=refused)
        targets = await self._open_targets(agent)
        if not targets:
            return OpenOutcome(None, no_targets=True)
        limits = turn_limits(agent)
        deadline = now + timedelta(seconds=limits.turn_timeout_seconds) + DEADLINE_SLACK
        secret = secrets.token_urlsafe(SECRET_BYTES)
        grant = TurnGrant(
            id=secrets.token_urlsafe(SECRET_BYTES),
            secret_sha256=hash_token(secret),
            agent_id=agent.id,
            tenant=agent.tenant,
            sub=person.sub,
            username=(username or person.username) or None,
            tv=person.tv if self._local else None,
            trigger=trigger,
            long_bound=long_bound,
            run_id=run_id,
            decided_by=decided_by,
            session_owner=session_owner,
            session_id=session_id,
            targets=targets,
            opened_at=now,
            deadline=deadline,
            purge_at=deadline + PURGE_AFTER,
        )
        ttl = self._ttl(grant, now, agent)
        tokens = self._mint_all(agent, grant, targets, ttl)
        try:
            await self.grants.insert(grant)
        except PyMongoError as e:
            logger.warning("턴 위임을 적지 못했다 (agent %s): %s", agent.id, type(e).__name__)
            raise TurnTokensUnavailable("the turn grant could not be recorded") from e
        renew_url = (
            f"{agent_config_base(self._platform_url(agent), agent.id)}/turn-grants/{grant.id}"
        )
        return OpenOutcome(
            OpenedGrant(
                grant.id,
                {
                    "id": grant.id,
                    "secret": secret,
                    "sub": grant.sub,
                    "tokens": tokens,
                    "expires_in": ttl,
                    "renew_url": renew_url,
                },
            )
        )

    def _mints_for(self, agent: Agent) -> bool:
        """이 Agent 의 턴에 위임을 열 수 있는가. 설정, 토큰 인증 모드, 서명 키, 프록시가 런타임을
        부르는 상태(`serves_turns`, 재배포에 실패해도 앞 파드가 도는 failed 를 포함한다), 렌더,
        런타임의 `turn_tokens` 다. 서명 키가 없는 플랫폼은 503 이 아니라 지금처럼 돈다 (4절).
        프록시가 거절할 상태의 Agent 에는 위임을 적지 않는다."""
        return (
            self.settings.turn_tokens_enabled
            and self.settings.token_auth
            and self.issuer.loaded
            and serves_turns(agent)
            and agent.k8s is not None
            and agent.effective_config is not None
            and agent.image_reports(TURN_TOKENS)
        )

    async def _open_targets(self, agent: Agent) -> list[GrantTarget]:
        """열 때 토큰을 받는 대상이다. 대상의 문서 규칙과 그 클러스터의 버전을 보고, Gateway 는
        Agent 클러스터의 게이트까지 본다. 게이트의 판정이 오래됐으면 TurnTokensUnavailable 이다."""
        clusters: dict[str, Cluster | None] = {}
        out: list[GrantTarget] = []
        gateways: list[GrantTarget] = []
        for target in tool_targets(agent):
            cluster_id = await self._target_cluster(agent, target)
            if cluster_id is None or not await self._version_ok(cluster_id, clusters):
                continue
            (gateways if target.kind == "gateway_instance" else out).append(target)
        if gateways:
            gates = await self.gates.cluster(agent.cluster_id) if agent.cluster_id else None
            if not self.gates.fresh(gates, utcnow()):
                raise TurnTokensUnavailable("the Gateway gate has no recent verdict")
            if gates is not None and gates.gateway_gate_open:
                out.extend(gateways)
        return out + [t for t in call_targets(agent) if await self._callable(agent, t)]

    async def _callable(self, agent: Agent, target: GrantTarget) -> bool:
        """Agent 대상이 호출 토큰을 받는가. 호출이 켜져 있고 호출받는 Agent 가 같은 테넌트에 있을
        때다. 게시와 그 Agent 의 상태는 호출마다 호출 경로가 본다."""
        if not self.settings.agent_calls_on:
            return False
        return await self.agents.get(target.id, tenant=agent.tenant) is not None

    async def _target_cluster(self, agent: Agent, target: GrantTarget) -> str | None:
        """대상이 규칙을 지나면 그 대상의 클러스터 id 다. 지나지 못하면 None 이다. 열기와 갱신이
        같은 규칙을 쓴다. 클러스터 게이트는 열 때만 따로 본다."""
        if target.kind == "mcp_instance":
            server = await self.mcp_servers.get(target.id, agent.tenant)
            if server is None or not audience_applied(server):
                return None
            return server.cluster_id
        if target.kind == "gateway_instance" and self.settings.data_plane_trusts_platform:
            gateway = await self.gateways.get(target.id, agent.tenant)
            if gateway is None or owner_cedar(gateway) or not carries_binding(gateway):
                return None
            return gateway.cluster_id
        return None

    async def _version_ok(self, cluster_id: str, seen: dict[str, Cluster | None]) -> bool:
        if cluster_id not in seen:
            seen[cluster_id] = await self.clusters.get(cluster_id)
        cluster = seen[cluster_id]
        return cluster is not None and version_ok(cluster)

    def _platform_url(self, agent: Agent) -> str:
        """Agent 의 클러스터가 닿는 플랫폼 주소다. 이벤트 수집 주소와 같은 뿌리라 파드의 env 를
        바꾸지 않는다."""
        return EffectiveConfig.model_validate(agent.effective_config).platform_internal_url

    # -- 사람 -------------------------------------------------------------------------------------

    async def _check_person(
        self, sub: str, tv: int | None, tenant: str, now: datetime, *, interactive: bool
    ) -> tuple[str | None, str | None]:
        """(접근을 잃은 까닭, 사람의 이름). 판정은 접근 정리의 것이고 platform-admin 의 `*` 줄은
        소속이 아니다. 토큰 한 수명 뒤에도 지나야 한다. 그보다 짧게 남은 토큰을 내면 사람이 한도를
        넘어서도 Tool 을 부른다. interactive 면 oidc 모드의 대화형 한도도 본다."""
        holder = await self.revoker.holder(sub, tenant)
        ttl = timedelta(seconds=self.settings.turn_token_ttl_seconds)
        refused = self.revoker.holder_lost(HeldAccess.of(sub, tv, tenant), holder, now + ttl)
        person = holder.person
        if (
            refused is None
            and person is not None
            and interactive
            and not person.confirmed_since(now - self.settings.interactive_bound + ttl)
        ):
            refused = CONFIRM_BOUND
        return refused, holder.username

    # -- 토큰 -------------------------------------------------------------------------------------

    def _ttl(self, grant: TurnGrant, now: datetime, agent: Agent) -> int:
        """토큰 수명(초). 마감을 넘지 않고, 미리 보기면 그 자격의 끝(끝 + 10분)도 넘지 않는다
        (P44b 8절)."""
        left = int((grant.deadline - now).total_seconds())
        if agent.preview is not None:
            left = min(left, int((agent.preview.credentials_end - now).total_seconds()))
        return min(self.settings.turn_token_ttl_seconds, left)

    def _mint_all(
        self, agent: Agent, grant: TurnGrant, targets: list[GrantTarget], ttl: int
    ) -> list[dict[str, str]]:
        try:
            return [
                {"kind": t.kind, "id": t.id, "token": self._mint(agent, grant, t, ttl)}
                for t in targets
            ]
        except (RuntimeError, ValueError, TypeError, jwt.PyJWTError) as e:
            logger.warning(
                "턴 위임 토큰을 서명하지 못했다 (agent %s): %s", agent.id, type(e).__name__
            )
            raise TurnTokensUnavailable("the turn tokens could not be signed") from e

    def _mint(self, agent: Agent, grant: TurnGrant, target: GrantTarget, ttl: int) -> str:
        """대상 하나의 토큰 (1절). 발급자와 키는 지금 설정의 것이라 공개 주소가 바뀐 뒤의 갱신은 새
        발급자로 낸다."""
        calls_agent = target.kind == AGENT_SOURCE
        if target.kind == "gateway_instance":
            iss, aud = self.issuer.human_issuer, self.issuer.mcp_audience
        elif calls_agent:
            iss, aud = self.issuer.issuer, agent_call_audience(grant.tenant, target.id)
        else:
            iss, aud = self.issuer.issuer, server_audience(grant.tenant, target.id)
        # 행위자는 규칙이 보는 주체와 id 다. 미리 보기는 원본의 것이다 (P44b 9절). 위임 자체는
        # 미리 보기의 id 와 인스턴스 토큰에 묶인다(`TurnGrant.agent_id`)
        claims = turn_token_claims(
            issuer=iss,
            audience=aud,
            sub=grant.sub,
            ttl=ttl,
            actor=service_sub("agent", agent.acting_k8s_name),
            bound_to=BOUND_TO[target.kind].format(target.id),
            tenant=grant.tenant,
            username=grant.username,
            agent_id=agent.acting_id,
            # 호출 경로가 이 id 로 위임을 찾아 열려 있는지와 호출 수를 본다
            grant_id=grant.id if calls_agent else None,
        )
        return self.issuer.signer.sign(claims)

    # -- 세션, 갱신, 끝 ----------------------------------------------------------------------------

    async def attach_session(self, grant_id: str, session_id: str) -> None:
        """새 세션을 연 턴의 위임에 런타임이 답한 세션 id 를 적는다."""
        await self.grants.attach_session(grant_id, session_id)

    async def end(self, grant_id: str, reason: str) -> None:
        """플랫폼이 스스로 위임을 끝낸다. 처음 적힌 끝이 남는다."""
        await self._end(grant_id, reason)

    async def _end(self, grant_id: str, reason: str) -> bool:
        """위임을 끝내고, 끝냈으면 그 턴이 시작한 Agent 호출을 끝내는 훅을 부른다. 훅의 실패는
        로그만 남긴다. 지켜보기의 쓸기가 남은 호출을 끝낸다."""
        ended = await self.grants.end(grant_id, reason, utcnow())
        if ended and self.on_end is not None:
            try:
                await self.on_end(grant_id)
            except Exception as e:  # noqa: BLE001 - 끝난 위임의 호출은 쓸기가 끝낸다
                logger.warning("턴 위임 %s 가 시작한 호출을 끝내지 못했다: %s", grant_id, e)
        return ended

    async def renew(
        self, agent_id: str, grant_id: str, *, secret: str, presented: str
    ) -> dict[str, Any]:
        """`{tokens, expires_in}`. 4절의 차례대로 본다. 확인에 실패한 대상은 토큰을 받지 못하고
        위임에서 빠진다. 이 판이 모르는 종류의 대상은 위임에 남고 토큰만 받지 못한다 (P46 A0)."""
        agent, grant = await self._presented_grant(agent_id, grant_id, secret, presented)
        now = utcnow()
        # 설정을 끄거나 토큰 인증이 아닌 모드로 바꾼 플랫폼은 새 토큰을 내지 않는다
        minting = self.settings.turn_tokens_enabled and self.settings.token_auth
        if not minting or grant.ended_at is not None:
            raise _ended("the turn grant has ended")
        ttl = self._ttl(grant, now, agent)
        if ttl <= 0:
            raise _ended("the turn grant is past its deadline")
        refused, _name = await self._check_person(
            grant.sub, grant.tv, grant.tenant, now, interactive=_interactive(grant)
        )
        if refused == CONFIRM_BOUND:
            # 사람은 접근을 잃지 않았고 화면을 떠났을 뿐이다. 토큰만 끝내고 턴은 이어 간다. 런타임은
            # 403 이면 턴을 멈추고 410 이면 그 위임의 Tool 호출만 오류로 답한다
            raise TurnGrantError(
                410,
                "turn_grant_ended",
                "the person's last IdP confirmation is past the interactive bound",
                reason=refused,
            )
        if refused is not None:
            raise TurnGrantError(
                403, "turn_grant_refused", "the person lost access", reason=refused
            )
        targets: list[GrantTarget] = []
        dropped: list[GrantTarget] = []
        for target in grant.targets:
            if target.kind not in BOUND_TO:
                # 뒤 판이 더한 종류의 대상이다 (P46 A0). 롤링 중에 그 판의 갱신이 다시 낼 수 있게
                # 기록에 두고 토큰만 만들지 않는다
                continue
            if target.kind == AGENT_SOURCE:
                passes = await self._callable(agent, target)
            else:
                passes = await self._target_cluster(agent, target) is not None
            (targets if passes else dropped).append(target)
        tokens = self._mint_all(agent, grant, targets, ttl)
        if not await self.grants.record_renewal(grant.id, dropped=dropped, now=now):
            raise _ended("the turn grant has ended")
        return {"tokens": tokens, "expires_in": ttl}

    async def end_by_runtime(
        self, agent_id: str, grant_id: str, *, secret: str, presented: str, reason: str
    ) -> dict[str, bool]:
        """런타임이 턴을 마쳤다고 알린다. 이미 끝난 위임이면 `ended` 가 거짓이다."""
        _agent, grant = await self._presented_grant(agent_id, grant_id, secret, presented)
        return {"ended": await self._end(grant.id, reason)}

    async def _presented_grant(
        self, agent_id: str, grant_id: str, secret: str, presented: str
    ) -> tuple[Agent, TurnGrant]:
        """인스턴스 토큰(401), 버킷(429), 이 Agent 의 위임인지(404), 비밀(401)이다."""
        agent = await pinned_agent(self.agents, self.instance_tokens, agent_id, presented)
        self._admit(agent.id)
        grant = await self.grants.get(grant_id)
        if grant is None or grant.agent_id != agent.id:
            raise TurnGrantError(404, "turn_grant_not_found", "no such turn grant")
        if not secrets.compare_digest(grant.secret_sha256, hash_token(secret)):
            raise unauthorized("invalid turn grant secret")
        return agent, grant

    def _admit(self, agent_id: str) -> None:
        if not self._limits.take(agent_id):
            raise TurnGrantError(
                429,
                "turn_grant_rate_limited",
                "too many turn grant requests for this agent",
                headers=retry_after_header(RETRY_AFTER),
            )


def _ended(detail: str) -> TurnGrantError:
    return TurnGrantError(410, "turn_grant_ended", detail)


def _opens_interactive(trigger: Trigger, decided_by: str | None, long_bound: bool | None) -> bool:
    """열 때 사람을 대화형 한도로 보는가. 뿌리가 실행인 턴(계기 `run`, 또는 적힌 `long_bound` 가
    참)은 긴 한도다. 그 밖에는 사람이 이 요청에 없을 때만 본다. 승인자가 소유자 대신 재개하거나
    다른 Agent 가 부른 실행의 시작이다. 자기 턴(채팅, 다시 생성, 자기 결정)은 이 요청이 유효한 IdP
    토큰으로 사람이 있음을 보였으므로 보지 않는다. 갱신은 언제나 본다(`_interactive`)."""
    long = long_bound if long_bound is not None else trigger in LONG_BOUND_TRIGGERS
    return not long and (decided_by is not None or trigger == CALL_TRIGGER)


def _interactive(grant: TurnGrant) -> bool:
    """갱신이 사람을 대화형 한도로 보는가. 뒤 판(P46)이 적은 뿌리의 한도 종류(`long_bound`)가
    있으면 그것이고, 없으면 계기다 (P46 A0). 실행의 뿌리에서 부른 Agent 의 위임은 계기가 `agent`
    라도 긴 한도다. 거짓은 언제나 대화형 한도이고, 참은 사람이 지금 보낸 턴(`PRESENT_TRIGGERS`)의
    위임에서는 믿지 않는다."""
    if grant.long_bound is False:
        return True
    if grant.long_bound and grant.trigger not in PRESENT_TRIGGERS:
        return False
    return grant.trigger not in LONG_BOUND_TRIGGERS
