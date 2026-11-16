"""Agent 의 LLM 경로 (P38 6절 "Routes and migration", F7).

`decide` 는 틱마다 한 번 읽은 게이트웨이 상태(`GatewayView`)로 직접 경로와 게이트웨이 경로 가운데
하나와 그 까닭을 고르는 순수 함수다. 표의 첫 행이 이긴다. provision 과 재적용이 결정을
`Agent.llm_route` 에 적고, 렌더와 파드 없는 컴파일은 그것만 읽는다.

- 묶기(`bind`): 결정을 문서에 적고 그 경로의 입력을 둔다. 직접 경로는 키 복사본을 만들고(그 키의
  지문을 설정 지문에 넣는다), 게이트웨이 경로는 아무것도 복사하지 않는다. 토큰은 토큰 Secret 을 쓰는
  자리(`AgentTokens.write_secret`)가 `wanted` 의 provider 로 낸다
- 정리(`after_apply`): 옛 템플릿의 파드가 다시 떠도 옛 env 가 가리키는 Secret 을 읽으므로,
  게이트웨이 경로로 옮긴 뒤의 복사본 지우기와 직접 경로로 옮긴 뒤의 jti 회수와 `llm_token` 빼기는
  롤아웃이 끝난 뒤 `Agent.llm_cleanup` 이 한다(`LlmTokens.settle`)
- 재적용의 몫(`rev_inputs`): 결정이 게이트웨이인 Agent 만 판에 표시를 얹는다. 그래서 init 이 기능을
  알리거나 provider 가 반영되면 그 Agent 만 다시 렌더되고, 끄면 표시가 사라져 다시 렌더되며, 직접
  경로인 Agent 는 끈 채로 다시 렌더되지 않는다
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from daegam.auth.signer import LLM_AUDIENCE
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.templates.agent import LLM_TOKEN_KEY
from daegam.names import provider_slug
from daegam.services import agent_graph
from daegam.services.agent_features import LLM_GATEWAY
from daegam.services.agent_pod_inputs import AgentPodInputs
from daegam.services.agent_tokens import llm_revoking
from daegam.services.llm_providers import ProviderError, resolve_api_key
from daegam.services.llm_readiness import GatewayReadiness, read_readiness
from daegam.services.llm_tokens import MINT_STATES
from daegam.services.redis_tls_consumers import RecentReads
from daegam.services.svc_tokens import SvcTokens
from daegam.store.agents import Agent, AgentRevision, LlmCleanup, LlmRoute, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.knowledge import KnowledgeBase
from daegam.store.llm_gateway import MongoLlmGatewayStore
from daegam.store.settings import LLMProvider, MongoPlatformSettingsStore

# 직접 경로의 까닭 (6절 표). 앞 세 행(gateway_off, gateway_not_ready, no_signer)은 플랫폼 호출자와
# 같은 규칙이라 llm_readiness 에 있다. 이 차례가 그 뒤의 차례다
REMOTE_CLUSTER = "remote_cluster"
STUB_ONLY = "stub_only"
PROVIDER_NOT_READY = "provider_not_ready"
RUNTIME_FEATURES_UNKNOWN = "runtime_features_unknown"
RUNTIME_IMAGE_TOO_OLD = "runtime_image_too_old"
# 상태 API 의 수. 게이트웨이로 결정됐지만 아직 다시 렌더되지 않은 Agent 다
MOVING = "moving"
GATEWAY_VIEW_KEY = "llm_gateway_view"


@dataclass(frozen=True)
class LlmBefore:
    """묶기 앞의 경로와 그때 문서에 있던 토큰 jti. 적용 뒤에 새로 낸 토큰을 가른다."""

    route: LlmRoute
    jtis: frozenset[str]


@dataclass(frozen=True)
class GatewayView(GatewayReadiness):
    """틱마다 한 번 읽는 게이트웨이 상태. 플랫폼 호출자와 같은 준비에 home 클러스터를 더했다."""

    home_id: str | None


def decide(doc: Agent, providers: Sequence[LLMProvider], gw: GatewayView) -> LlmRoute:
    """경로와 까닭. providers 는 그래프가 쓰는 provider(메인, 폴백, 서브, 요약, Knowledge 임베딩)다.
    게이트웨이 경로의 토큰은 그 가운데 stub 이 아닌 provider 의 slug 를 연다. 기록은 이 결정만
    읽는다(D65)."""
    keyed = [p for p in providers if not p.is_stub]
    reason = _direct_reason(doc, keyed, gw)
    if reason is not None:
        return LlmRoute(route="direct", reason=reason)
    return LlmRoute(route="gateway", providers=sorted({provider_slug(p.name) for p in keyed}))


def _direct_reason(doc: Agent, keyed: Sequence[LLMProvider], gw: GatewayView) -> str | None:
    if (closed := gw.closed()) is not None:
        return closed
    if doc.cluster_id is None or doc.cluster_id != gw.home_id:
        return REMOTE_CLUSTER
    if not keyed:
        return STUB_ONLY
    if any(p.name not in gw.providers_ready for p in keyed):
        return PROVIDER_NOT_READY
    record = doc.runtime_for_image
    if record is None:
        return RUNTIME_FEATURES_UNKNOWN
    if LLM_GATEWAY not in record.features:
        return RUNTIME_IMAGE_TOO_OLD
    return None


class AgentLlm:
    """Agent 의 provider 해소, 경로 결정과 묶기, 롤아웃 뒤의 정리 예약. 게이트웨이 상태 문서는
    배선이 붙인다(`state`). 없으면 늘 끈 것으로 본다."""

    def __init__(
        self,
        *,
        settings: Settings,
        platform_settings: MongoPlatformSettingsStore,
        agents: MongoAgentStore,
        cluster_service: Any,
        svc_tokens: SvcTokens,
        pod_inputs: AgentPodInputs,
        knowledge_bases: Callable[..., Awaitable[dict[str, KnowledgeBase]]],
        error_cls: type[ServiceError],
    ):
        self.settings = settings
        self.platform_settings = platform_settings
        self.agents = agents
        self.cluster_service = cluster_service
        self.svc_tokens = svc_tokens
        self.pod_inputs = pod_inputs
        self.knowledge_bases = knowledge_bases
        self.error_cls = error_cls
        self.state: MongoLlmGatewayStore | None = None
        # 재적용 틱은 Agent 마다 결정을 보므로 상태와 설정을 잠깐 다시 쓴다
        self._recent = RecentReads()

    # -- provider ------------------------------------------------------------------------------

    async def providers(
        self, tenant: str, rev: AgentRevision
    ) -> tuple[list[LLMProvider], list[tuple[int, LLMProvider]]]:
        """(그래프가 쓰는 provider 전부, knowledge[] 항목별 (n, 임베딩 provider)). 앞은 복사본과
        채팅 키 env, 뒤는 임베딩 키 env 의 재료다. 지워진 provider 는 422 unknown_provider 다."""
        by_name = (await self.platform_settings.get()).providers_by_name

        def resolve(name: str) -> LLMProvider:
            p = by_name.get(name)
            if p is None:
                raise self.error_cls(
                    422, "unknown_provider", f"provider {name!r} is no longer registered"
                )
            return p

        kbs = await self.knowledge_bases(tenant, status=False)
        providers = [resolve(name) for name in agent_graph.used_providers(rev, kbs)]
        knowledge = [(n, resolve(name)) for n, name in agent_graph.knowledge_providers(rev, kbs)]
        return providers, knowledge

    def has_key_checker(self) -> Callable[[LLMProvider], bool]:
        """검증 문맥의 키 유무. 부를 때 플랫폼 네임스페이스 Secret 을 읽는다(동기 K8s 호출이라
        검증·컴파일은 스레드풀에서 돈다). 게이트웨이도 같은 원본을 복사하므로 경로와 상관없다."""
        secret_store = self.cluster_service.secret_store()

        def has_key(provider: LLMProvider) -> bool:
            try:
                resolve_api_key(provider, secret_store, namespace=self.settings.namespace)
            except ProviderError:
                return False
            return True

        return has_key

    # -- 결정 ----------------------------------------------------------------------------------

    async def view(self) -> GatewayView:
        return await self._recent.get(GATEWAY_VIEW_KEY, self._read_view)

    async def _read_view(self) -> GatewayView:
        r = await read_readiness(self.settings, self.state, self.svc_tokens)
        home = await self.cluster_service.home()
        return GatewayView(
            on=r.on,
            ready=r.ready,
            signer=r.signer,
            providers_ready=r.providers_ready,
            carries_purpose=r.carries_purpose,
            home_id=home.id if home is not None else None,
        )

    async def decision(
        self, doc: Agent, providers: Sequence[LLMProvider] | None = None
    ) -> LlmRoute:
        """지금 게이트웨이 상태로 본 결정. providers 를 주지 않으면 핀한 리비전에서 해소하고, 지워진
        provider 가 있으면 직접 경로(`provider_not_ready`)다. 끈 게이트웨이는 표의 첫 행이라
        provider 를 읽지 않는다. 재적용 틱이 Agent 마다 부르기 때문이다."""
        gw = await self.view()
        if not gw.on:
            return decide(doc, [], gw)
        if providers is None:
            rev = doc.deployed
            if rev is None:
                # 핀한 리비전이 사라진 문서다. provider 를 알 수 없으니 반영되지 않은 것으로 본다
                return LlmRoute(route="direct", reason=PROVIDER_NOT_READY)
            try:
                providers, _ = await self._recent.get(
                    ("llm_providers", doc.id, doc.deployed_revision),
                    lambda: self.providers(doc.tenant, rev),
                )
            except ServiceError:
                return LlmRoute(route="direct", reason=PROVIDER_NOT_READY)
        return decide(doc, providers, gw)

    def wanted(self, doc: Agent) -> list[str]:
        """토큰이 열어야 할 provider slug (`AgentTokens.llm_wanted`). 직접 경로는 비어 있어 토큰을
        내지 않고, 스윕도 남은 토큰을 갱신하지 않는다."""
        return list(doc.llm_route.providers) if doc.llm_route.gateway else []

    async def rev_inputs(self, doc: Agent) -> dict[str, Any] | None:
        """재적용의 llm 몫. 결정이 게이트웨이일 때만 그 provider, 스냅숏 이미지와 그 기능,
        게이트웨이 주소, audience, 발급자를 판에 넣는다. 아니면 None 이라 판이 그대로다."""
        decided = await self.decision(doc)
        if not decided.gateway:
            return None
        return {
            "providers": decided.providers,
            "image": doc.snapshot_image,
            "llm_gateway": doc.image_reports(LLM_GATEWAY),
            "url": self.settings.llm_gateway_url,
            "audience": LLM_AUDIENCE,
            "issuer": self.settings.platform_issuer_url,
        }

    async def counts(self) -> dict[str, Any]:
        """상태 API 의 Agent 수 (11절). 저장된 경로가 게이트웨이인 수, 나머지의 지금 까닭마다의 수,
        게이트웨이로 결정됐지만 아직 옮기지 않은 수, 정리가 남은 수다."""
        out: dict[str, Any] = {"gateway": 0, "direct": {}, MOVING: 0, "cleanups_pending": 0}
        for doc in await self.agents.live():
            out["cleanups_pending"] += 1 if doc.llm_cleanup else 0
            if doc.llm_route.gateway:
                out["gateway"] += 1
                continue
            reason = (await self.decision(doc)).reason
            if reason is None:
                out[MOVING] += 1
            else:
                out["direct"][reason] = out["direct"].get(reason, 0) + 1
        return out

    # -- 묶기와 정리 ----------------------------------------------------------------------------

    async def bind(
        self,
        doc: Agent,
        ctx: Any,
        client: KubeClient,
        *,
        provision: bool = True,
        expect_state: frozenset[str] = frozenset({"deploying"}),
    ) -> dict[str, str]:
        """결정을 문서에 적고 그 경로의 입력을 둔다. 직접 경로의 키 복사본은 provision 이 쓰고,
        재적용과 회전(provision 이 아니다)은 게이트웨이 경로에서 옮겨 올 때와 문서의 복사본 목록이
        모자랄 때만 쓴다. 그 밖에 다시 쓰면 바꾼 플랫폼 키가 설정 지문의 롤 없이 복사본에 닿아 한
        템플릿의 레플리카가 서로 다른 키로 돈다(D56). 돌려주는 것은 설정 지문에 드는 {복사본 이름:
        키 sha256} 이고, 쓰지 않았거나 게이트웨이 경로면 빈 표다."""
        before = doc.llm_route
        doc.llm_route = await self.decision(doc, ctx.providers)
        if doc.llm_route.gateway:
            return {}
        if not provision and not before.gateway:
            if self.pod_inputs.has_llm_copies(doc, ctx.providers):
                return {}
        return await self.pod_inputs.copy_llm_secrets(doc, ctx, client, expect_state=expect_state)

    def before(self, doc: Agent) -> LlmBefore:
        """묶기 앞의 경로와 토큰 jti. provision 과 재적용이 `after_apply` 에 넘긴다."""
        return LlmBefore(route=doc.llm_route, jtis=frozenset(r.jti for r in doc.llm_tokens))

    async def after_apply(self, doc: Agent, before: LlmBefore, applied: dict[str, Any]) -> None:
        """그 롤아웃이 끝난 뒤의 정리를 남긴다. 게이트웨이 경로로 옮기면 복사본을 지우고, 직접
        경로로 옮기면 살아 있는 jti 를 회수하고 `llm_token` 을 뺀다. 게이트웨이 경로에 머물며 새
        토큰을 냈으면(교체, 갱신, provider 를 바꾼 재배포) 그 앞의 살아 있는 토큰을 회수한다. 뺀
        provider 를 연 옛 토큰이 만료까지 남지 않게 한다. 롤아웃이 멈추면 기다린다. 멈춘 롤아웃을
        끊지 않고, 게이트웨이를 다시 켤 때 남은 토큰이 열리지 않게 한다."""
        now = doc.llm_route
        generation = ((applied or {}).get("metadata") or {}).get("generation")
        live = [r.jti for r in doc.llm_tokens if r.exp > utcnow()]
        if now.gateway != before.route.gateway:
            if now.gateway and not doc.llm_secrets:
                return
            cleanup = (
                LlmCleanup(drop_secrets=list(doc.llm_secrets), generation=generation)
                if now.gateway
                else LlmCleanup(revoke=live, drop_keys=[LLM_TOKEN_KEY], generation=generation)
            )
        else:
            minted = {r.jti for r in doc.llm_tokens} - before.jtis
            pending = llm_revoking(doc)
            replaced = [j for j in live if j not in minted and j not in pending]
            if not now.gateway or not minted or not replaced:
                return
            cleanup = LlmCleanup(revoke=replaced, generation=generation)
        if await self.agents.add_llm_cleanup(doc.id, cleanup, states=MINT_STATES):
            doc.llm_cleanup = [*doc.llm_cleanup, cleanup]
