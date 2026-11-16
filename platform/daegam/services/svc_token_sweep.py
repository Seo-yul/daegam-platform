"""서비스 토큰 스윕 (D41). 리컨실이 한 시간에 한 번 리스 안에서 부른다.

소비자 셋(Gateway 의 vMCP, 노출한 MCP Server 의 앞단, Agent 의 런타임)을 돌며 Secret 의 토큰을 서명
검증 없이 들여다본다. 토큰은 여는 서버마다 하나다(P50 9절). 토큰이 없거나, JWT 가 아니거나, `kid` 가
지금 서명 키와 다르거나, 만료가 `renew_before`(6개월) 안이거나, audience 가 그 서버에 원하는 것과
다르면(`audiences_due`) 새로 내어 넣고 소비자를 굴린다. 옛 토큰은 자기 `exp` 까지 유효하므로
굴리기가 늦어도 6개월의 겹침 창이 안전망이다. local 에이전트의 gateway_token 은 iss·aud·groups 가
지금 원하는 값과 다를 때도 갈아 끼운다. 인증 재적용(P32 8절)이 같은 비교로 먼저 새로 내므로 이것은
한 시간 안의 두 번째 그물이다.

한 소비자의 실패는 다른 소비자를 막지 않는다. 서명 키가 아직 안 붙었으면(home 클러스터 없음)
아무것도 하지 않는다. 갱신하지 않은 소비자에서는 더는 쓰지 않는 토큰을 거둔다. Gateway 는 지금
렌더가 쓰지 않는 토큰 객체를 vMCP 가 다 굴러간 뒤에 지우고(`GatewayService.settle_token_objects`),
Agent 는 앞 파드 템플릿들의 토큰 키를 그 파드가 다 없어진 뒤에 Secret 에서 뺀다
(`AgentService.prune_token_tables`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import (
    LLM_TOKEN_RENEW_DAYS,
    InternalIssuer,
    claims_current,
    service_sub,
)
from daegam.auth.signer import LLM_AUDIENCE
from daegam.k8s.client import KubeClient, cluster_down
from daegam.k8s.templates.agent import GATEWAY_TOKEN_KEY, LLM_TOKEN_KEY, MCP_TOKEN_KEY, TOKEN_KEY
from daegam.k8s.templates.svc_token import (
    entry_token_objects,
    existing_svc_token,
    expose_svc_name,
    secret_value,
)
from daegam.services.budget import Budget, resumed
from daegam.services.claims import tick_item
from daegam.services.svc_tokens import RENEW_BEFORE, audiences_due, bound_current, token_due
from daegam.store.auth_reapply import LIVE_STATES
from daegam.store.base import utcnow

if TYPE_CHECKING:
    from daegam.services.agents import AgentService
    from daegam.services.gateways import GatewayService
    from daegam.services.mcp_servers import MCPServerService
    from daegam.services.svc_tokens import SvcTokens
    from daegam.store.agents import MongoAgentStore
    from daegam.store.gateways import MongoGatewayStore
    from daegam.store.mcp_servers import MongoMCPServerStore

logger = logging.getLogger("daegam.svc_tokens")

LLM_RENEW_BEFORE = timedelta(days=LLM_TOKEN_RENEW_DAYS)
# 스윕이 한 틱에 쓰는 시간(초). 넘기면 이어 돌 자리를 두고 다음 틱이 잇는다 (P67 2절)
SWEEP_SECONDS_PER_TICK = 20.0


@dataclass(frozen=True)
class HeldToken:
    """소비자 Secret 의 토큰 하나와 그 수명 규칙. kind 가 원하는 클레임을 정한다. service 는 내부
    홉 토큰이고 server_id 는 그 토큰이 여는 서버(모르면 None, Agent 의 옛 단일 토큰)라 audience 를
    견준다. gateway 는 local 에이전트의 gateway_token 이라 iss·aud·groups 만 견주고 만료는 보지
    않는다. llm 은 LLM 게이트웨이 토큰이라 만료 30일 전에 다시 낸다 (P38 5절). bound 는 게시된
    Gateway 에 묶인 소비자 vMCP 의 토큰이라 배포와 같은 검사(`bound_current`)로 본다 (P57 6.1절)."""

    token: str | None
    kind: Literal["service", "gateway", "llm", "bound"] = "service"
    server_id: str | None = None
    renew_before: timedelta = RENEW_BEFORE
    # llm 토큰이 열어야 할 provider slug. 있으면 토큰의 `llm_providers` 와 견준다
    wanted: tuple[str, ...] | None = None
    # bound 토큰의 (소비자 vMCP 의 서비스 신원, 묶인 Gateway 문서의 id, 규칙 지문). id 는
    # `gateway:<id>` 가 아니라 문서 id 그대로다
    bound: tuple[str, str, str | None] | None = None


@dataclass(frozen=True)
class HeldTokens:
    """소비자 Secret 에서 읽은 토큰의 표."""

    tokens: tuple[HeldToken, ...]
    # Agent 의 인스턴스 토큰. 살아 있는 기록과 맞지 않으면 회전이 새로 낸다 (P66 2.7절)
    instance: str | None = None


@dataclass(frozen=True)
class _Kind:
    """스윕이 도는 소비자 한 종류. docs 는 그 종류의 살아 있는 문서를 읽는다."""

    name: str
    docs: Callable[[], Awaitable[list[Any]]] | None
    service: Any
    read_tokens: Callable[[KubeClient, Any], HeldTokens]
    wants: Callable[[Any], bool] | None = None
    settle: Callable[[Any, KubeClient], Awaitable[bool]] | None = None
    instance_current: Callable[[Any, str | None], Awaitable[bool]] | None = None


class SvcTokenSweeper:
    def __init__(
        self,
        svc_tokens: SvcTokens,
        *,
        gateways: tuple[MongoGatewayStore, GatewayService],
        mcp_instances: tuple[MongoMCPServerStore, MCPServerService],
        agents: tuple[MongoAgentStore, AgentService],
        renew_before: timedelta = RENEW_BEFORE,
    ):
        self.svc_tokens = svc_tokens
        self.issuer: InternalIssuer = svc_tokens.issuer
        self.gateways = gateways
        self.mcp_instances = mcp_instances
        self.agents = agents
        self.renew_before = renew_before
        # 한 틱의 시간(`SWEEP_SECONDS_PER_TICK`)에 끝까지 돌지 못하면 끝낸 마지막 소비자의 자리
        # (종류 순번, id)를 둔다. 다음 틱은 그 뒤부터 잇는다. 파드마다의 기억이다 (P67 2절)
        self.resume_after: tuple[int, str] | None = None

    def needs_renewal(
        self, token: str | None, now: datetime, renew_before: timedelta | None = None
    ) -> bool:
        """없음·JWT 아님·서명 키 변경·발급자 주소 변경·만료 임박이면 참이다(`token_due`). 발급자
        주소를 바꿨는데 잡지 않으면 1년짜리 토큰이 만료까지 영영 안 바뀐다."""
        return token_due(self.issuer, token, now, renew_before or self.renew_before)

    async def _due(self, held: HeldTokens, tenant: str, now: datetime) -> bool:
        """표의 토큰 가운데 하나라도 갱신할 때이거나 원하는 클레임과 다르다. 서비스 토큰은
        audience 를, gateway_token 은 iss·aud·groups 를 provision·인증 재적용과 같은 함수로
        견준다."""
        for held_token in held.tokens:
            token = held_token.token
            if held_token.kind == "bound":
                assert held_token.bound is not None
                sub, gateway_id, rules = held_token.bound
                if not bound_current(
                    self.issuer,
                    token,
                    sub=sub,
                    tenant=tenant,
                    gateway_id=gateway_id,
                    rules=rules,
                    now=now,
                ):
                    return True
                continue
            if held_token.kind == "gateway":
                if not await self.svc_tokens.gateway_token_current(token, tenant):
                    return True
                continue
            if self.needs_renewal(token, now, held_token.renew_before):
                return True
            if held_token.kind == "service" and audiences_due(token, tenant, held_token.server_id):
                return True
            if held_token.kind == "llm" and not self._llm_current(held_token):
                return True
        return False

    def _llm_current(self, held: HeldToken) -> bool:
        """llm 토큰이 원하는 provider 를 여는가. 원하는 것을 모르면(F7 전) 맞는 것으로 본다."""
        if held.wanted is None:
            return True
        return claims_current(
            held.token,
            iss=self.issuer.issuer,
            aud=LLM_AUDIENCE,
            groups=[],
            llm_providers=list(held.wanted),
        )

    async def run(self, now: datetime | None = None) -> dict[str, list[str]]:
        """세 종류의 소비자를 (종류 순번, id) 차례로 돈다. 이 틱의 시간(`SWEEP_SECONDS_PER_TICK`)을
        넘기면 끝낸 마지막 소비자의 자리를 `resume_after` 로 두고 결과에도 남긴다. 다음 틱은 그
        뒤부터 잇고, 끝까지 돈 회차가 한 시간을 센다 (P67 2절). 몫은 소비자 사이에서만 본다.
        종류가 바뀌는 자리나 이어 돈 첫 자리도 같아서, 시간이 다했으면 그 자리에서 멈춘다. 이어 돌
        자리가 없는 첫 소비자만은 시간이 다해도 본다. 그래야 몫이 아무리 작아도 나아간다."""
        now = now or utcnow()
        result: dict[str, list[str]] = {"renewed": [], "failed": []}
        if not self.issuer.loaded:
            return result
        # 이번 스윕에서 연결할 수 없다고 답한 클러스터. 그곳의 남은 소비자는 묻지 않는다 (P65)
        down: set[str] = set()
        budget = Budget(SWEEP_SECONDS_PER_TICK)
        resume = self.resume_after
        self.resume_after = None
        kinds = self._kinds()
        entries = [(order, doc) for order, kind in enumerate(kinds) for doc in await kind.docs()]
        last = resume
        for order, doc in resumed(entries, resume, key=lambda e: (e[0], e[1].id), wrap=False):
            await tick_item()
            if budget.out_of_time() and last is not None:
                self.resume_after = last
                result["resume_after"] = [last[1]]
                return result
            last = (order, doc.id)
            await self._sweep_one(kinds[order], doc, now, result, down)
        return result

    def _kinds(self) -> list[_Kind]:
        """스윕이 도는 소비자 종류. 이 차례가 이어 돌 자리의 종류 순번이다."""
        gw_store, gw_service = self.gateways
        mi_store, mi_service = self.mcp_instances
        ai_store, ai_service = self.agents
        return [
            _Kind(
                "gateway",
                gw_store.live,
                gw_service,
                _vmcp_tokens,
                settle=gw_service.settle_token_objects,
            ),
            _Kind(
                "mcp server",
                mi_store.live,
                mi_service,
                _expose_token,
                wants=lambda mi: mi.exposure.enabled,
            ),
            _Kind(
                "agent",
                ai_store.live,
                ai_service,
                lambda client, ai: _agent_tokens(
                    client, ai, ai_service.agent_tokens.wanted_llm(ai)
                ),
                # 미리 보기의 토큰은 그 끝 + 10분까지이고 아무것도 갱신하지 않는다 (P44b 8절).
                # 기본 보기가 이미 빼지만 보기와 상관없이 지킨다
                wants=lambda ai: ai.preview is None,
                settle=ai_service.prune_token_tables,
                instance_current=ai_service.instance_token_current,
            ),
        ]

    async def _sweep(
        self,
        kind: str,
        docs: list[Any],
        service: Any,
        read_tokens: Callable[[KubeClient, Any], HeldTokens],
        now: datetime,
        result: dict[str, list[str]],
        down: set[str],
        *,
        wants: Callable[[Any], bool] | None = None,
        settle: Callable[[Any, KubeClient], Awaitable[bool]] | None = None,
        instance_current: Callable[[Any, str | None], Awaitable[bool]] | None = None,
    ) -> None:
        """한 종류의 소비자를 받은 차례대로 몫 없이 돈다."""
        spec = _Kind(kind, None, service, read_tokens, wants, settle, instance_current)
        for doc in docs:
            await self._sweep_one(spec, doc, now, result, down)

    async def _sweep_one(
        self,
        kind: _Kind,
        doc: Any,
        now: datetime,
        result: dict[str, list[str]],
        down: set[str],
    ) -> None:
        """소비자 하나. 살아 있고 wants 가 참인 문서만 본다. read_tokens 는 Secret 에서 지금 토큰을
        읽는 동기 함수라 스레드풀에서 부른다. settle 은 갱신하지 않은 문서에서 더는 쓰지 않는 토큰을
        거둔다(Gateway 가 뺀 Entry 의 토큰 객체, Agent 의 앞 템플릿 키). instance_current 가
        있으면(Agent) Secret 의 인스턴스 토큰이 살아 있는 기록과 맞지 않는 것도 회전한다. 회전의
        재렌더가 같은 검사로 인스턴스 토큰을 새로 낸다 (P66 2.7절)."""
        if doc.state not in LIVE_STATES or doc.k8s is None:
            return
        if kind.wants is not None and not kind.wants(doc):
            return
        if doc.cluster_id in down:
            result.setdefault("skipped", []).append(doc.id)
            return
        service = kind.service
        try:
            client = await service.client_for_doc(doc)
            tokens = await run_in_threadpool(kind.read_tokens, client, doc)
            stale_instance = kind.instance_current is not None and await self._instance_stale(
                service, doc, tokens.instance, kind.instance_current
            )
            if stale_instance or await self._due(tokens, doc.tenant, now):
                if await service.rotate_svc_token(doc):
                    result["renewed"].append(doc.id)
            elif kind.settle is not None and await kind.settle(doc, client):
                result.setdefault("settled", []).append(doc.id)
        except Exception as e:  # noqa: BLE001 - 한 소비자의 실패가 스윕을 멈추면 안 된다
            logger.warning("svc token sweep %s %s: %s", kind.name, doc.id, e)
            result["failed"].append(doc.id)
            if cluster_down(e):
                down.add(doc.cluster_id)

    @staticmethod
    async def _instance_stale(
        service: Any,
        doc: Any,
        token: str | None,
        instance_current: Callable[[Any, str | None], Awaitable[bool]],
    ) -> bool:
        """Secret 의 인스턴스 토큰이 살아 있는 기록과 맞지 않는가. 견주기 전에 문서를 다시 읽는다.
        목록을 읽은 뒤 끝난 provision 이 새 토큰을 냈으면 옛 사본으로 견주어 괜히 회전하지
        않는다 (P66 2.7절)."""
        fresh = await service.instances.get(doc.id)
        if fresh is None or fresh.state not in LIVE_STATES:
            return False
        return not await instance_current(fresh, token)


def _vmcp_tokens(client: KubeClient, gw: Any) -> HeldTokens:
    """토큰을 받는 Entry 마다의 토큰과 그 Entry 가 부르는 서버 (P50 9절). 게시된 백엔드의 Entry 는
    그 Gateway 에 묶인 토큰이다 (P57 6.1절)."""
    k8s = gw.k8s
    rows = {b.workload: b for b in gw.deployed_backends}
    held: list[HeldToken] = []
    for workload, name in entry_token_objects(k8s.name, gw.deployed_backends).items():
        token = existing_svc_token(client, name, k8s.namespace)
        row = rows[workload]
        if row.bound_gateway_id:
            bound = (service_sub("vmcp", k8s.name), row.bound_gateway_id, row.rules_digest)
            held.append(HeldToken(token, kind="bound", bound=bound))
        else:
            held.append(HeldToken(token, server_id=row.server_id))
    return HeldTokens(tuple(held))


def _expose_token(client: KubeClient, mi: Any) -> HeldTokens:
    token = existing_svc_token(client, expose_svc_name(mi.k8s.name), mi.k8s.namespace)
    return HeldTokens((HeldToken(token, server_id=mi.id),))


def _agent_tokens(client: KubeClient, ai: Any, wanted: list[str] | None = None) -> HeldTokens:
    """옛 단일 토큰(테넌트 audience 하나), 지금 템플릿 표의 MCP Server 마다의 토큰(P50 9절),
    local 의 gateway_token, 게이트웨이 경로의 llm_token(P38 5절)이다. 없는 gateway_token 과
    llm_token 은 표에 없다. 직접 경로(wanted 가 빈 목록)의 llm_token 은 롤아웃 뒤의 정리를 기다리는
    옛 토큰이라 갱신하지 않는다 (P38 6절)."""
    secret = client.get("v1", "Secret", ai.k8s.token_secret, ai.k8s.namespace)
    table = ai.current_token_table
    envs = table.envs if table is not None else {}
    tokens = [HeldToken(secret_value(secret, MCP_TOKEN_KEY))]
    tokens += [
        HeldToken(secret_value(secret, env), server_id=server_id)
        for server_id, env in sorted(envs.items())
    ]
    if gateway := secret_value(secret, GATEWAY_TOKEN_KEY):
        tokens.append(HeldToken(gateway, kind="gateway"))
    if (llm := secret_value(secret, LLM_TOKEN_KEY)) and wanted != []:
        tokens.append(
            HeldToken(
                llm,
                kind="llm",
                renew_before=LLM_RENEW_BEFORE,
                wanted=tuple(wanted) if wanted is not None else None,
            )
        )
    return HeldTokens(tuple(tokens), secret_value(secret, TOKEN_KEY))
