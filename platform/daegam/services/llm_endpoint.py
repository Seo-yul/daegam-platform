"""플랫폼 호출자의 LLM 주소와 자격 (P38 6절 "Platform callers").

비서·스펙 생성기·Knowledge 인덱서는 모델이나 임베딩 클라이언트를 만들 때마다 `resolve_endpoint`
하나로 길을 고른다. 게이트웨이를 켰고, 리비전이 한 번 반영됐고, 서명 키가 붙었고, 그 provider 가
`providers_ready` 에 있으면 게이트웨이 경로다. 주소는 `<게이트웨이>/p/<slug>`(뒤에 OpenAI 계열은
`/v1`)이고 SDK 에는 자리표시 키를 주며, 요청마다 5분 토큰을 받는 `bearer` 가 붙는다(5절). 그 밖은
오늘의 길이다. 키는 플랫폼 네임스페이스 Secret(`resolve_api_key`)이고 주소는 provider 의 base URL
이다. 끄면 다음 호출부터 곧바로 직접 경로이고, 닿지 않는 게이트웨이를 직접 경로로 다시 부르지는
않는다. 원장의 `route` 는 고른 주소가 게이트웨이 주소 아래인가로 정한다(P37 18절 `under_base`).

연결 확인(`check_provider`)은 이것을 쓰지 않는다. 키를 저장한 바로 뒤에 부르므로 게이트웨이가 키를
반영하기 전이라도 맞는 키를 맞다고 답해야 하기 때문이다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal

from starlette.concurrency import run_in_threadpool

from daegam.config import Settings
from daegam.k8s.templates.llm_gateway_config import gateway_provider
from daegam.secrets import SecretStore
from daegam.services.llm_providers import resolve_api_key
from daegam.services.llm_readiness import read_readiness
from daegam.services.svc_tokens import SvcTokens
from daegam.store.llm_gateway import MongoLlmGatewayStore
from daegam.store.settings import LLMProvider
from daegam.urls import under_base

Route = Literal["direct", "gateway"]
Purpose = Literal["assistant", "spec_generator", "knowledge_indexer", "evaluator"]
# 평가의 채점 모델 (P45 11절). 그 토큰은 원본 Agent 의 id 를 함께 싣는다
EVALUATOR: Purpose = "evaluator"
# 게이트웨이 경로에서 SDK 가 받는 키다. 토큰 훅이 요청마다 `Authorization` 을 덮고 `x-api-key` 를
# 빼므로 이 값은 어디에도 가지 않는다
PLACEHOLDER_KEY = "daegam-llm-gateway"


@dataclass(frozen=True)
class Endpoint:
    """호출 하나의 길. bearer 는 게이트웨이 경로에서만 있다."""

    base_url: str | None
    api_key: str
    bearer: Callable[[], str] | None
    route: Route

    @property
    def gateway(self) -> bool:
        return self.route == "gateway"


def route_of(base_url: str | None, gateway_url: str | None) -> Route:
    return "gateway" if under_base(base_url, gateway_url) else "direct"


def direct_endpoint(
    cfg: LLMProvider,
    secret_store: SecretStore | None,
    *,
    namespace: str,
    gateway_url: str | None = None,
) -> Endpoint:
    """오늘의 길. 키 조회는 동기(K8s)라 호출자가 스레드풀에서 부른다. stub 은 키가 없다.
    게이트웨이 주소 아래에 적은 base URL 은 경로가 gateway 다 (P37 e2e 6단계)."""
    key = "" if cfg.is_stub else resolve_api_key(cfg, secret_store, namespace=namespace)
    base = cfg.effective_base_url
    return Endpoint(base, key, None, route_of(base, gateway_url))


class LlmEndpoints:
    """`resolve_endpoint` 의 주인. store 나 svc_tokens 가 없으면(테스트) 늘 직접 경로다."""

    def __init__(
        self,
        settings: Settings,
        secret_store: Callable[[], SecretStore | None],
        *,
        store: MongoLlmGatewayStore | None = None,
        svc_tokens: SvcTokens | None = None,
    ):
        self.settings = settings
        self.secret_store = secret_store
        self.store = store
        self.svc_tokens = svc_tokens

    async def resolve_endpoint(
        self, cfg: LLMProvider, *, tenant: str, purpose: Purpose, agent_id: str | None = None
    ) -> Endpoint:
        """호출 하나의 길. 직접 경로의 키가 없으면 `ProviderError`(no_api_key·no_secret·
        no_secret_store) 다. agent_id 는 채점 모델(`evaluator`)이 부르는 원본 Agent 다. 채점은
        반영된 게이트웨이 리비전이 목적 속성을 실을 때만 게이트웨이 길이다. 그 전의 게이트웨이
        기록은 수집이 거절하기 때문이다 (P45 11절)."""
        if await self._through_gateway(cfg, evaluator=purpose == EVALUATOR):
            return self._gateway_endpoint(cfg, tenant=tenant, purpose=purpose, agent_id=agent_id)
        return await self._direct(cfg)

    async def _direct(self, cfg: LLMProvider) -> Endpoint:
        return await run_in_threadpool(
            direct_endpoint,
            cfg,
            self.secret_store(),
            namespace=self.settings.namespace,
            gateway_url=self.settings.llm_gateway_url,
        )

    async def _through_gateway(self, cfg: LLMProvider, *, evaluator: bool) -> bool:
        if cfg.is_stub:
            return False
        readiness = await read_readiness(self.settings, self.store, self.svc_tokens)
        return readiness.opens_evaluator(cfg.name) if evaluator else readiness.opens(cfg.name)

    def _gateway_endpoint(
        self, cfg: LLMProvider, *, tenant: str, purpose: Purpose, agent_id: str | None
    ) -> Endpoint:
        assert self.svc_tokens is not None
        gp = gateway_provider(cfg)
        base = gp.base_url(self.settings.llm_gateway_url)
        bearer = partial(
            self.svc_tokens.platform_llm_bearer, tenant, purpose, gp.slug, agent_id=agent_id
        )
        return Endpoint(
            base, PLACEHOLDER_KEY, bearer, route_of(base, self.settings.llm_gateway_url)
        )
