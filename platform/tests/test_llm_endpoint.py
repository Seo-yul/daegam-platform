"""플랫폼 호출자의 길 (P38 6절 "Platform callers", F6).

`resolve_endpoint` 가 게이트웨이 경로를 고르는 조건, 그 경로의 주소와 요청마다 토큰, 직접 경로의
키, 원장의 경로, 스펙 생성기 provider 가 그 길로 내는 요청과 게이트웨이의 세 코드를 본다. 클러스터와
Mongo 없이 상태 문서는 가짜로, 요청은 목 transport 로 받는다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx2
import openai
import pytest

from daegam import llm_http
from daegam.config import Settings
from daegam.names import provider_slug
from daegam.services.knowledge.embeddings import build_embeddings, classify_embedding_error
from daegam.services.llm_endpoint import (
    PLACEHOLDER_KEY,
    Endpoint,
    LlmEndpoints,
    direct_endpoint,
)
from daegam.services.llm_providers import ProviderError, build_provider
from daegam.store.llm_gateway import LlmGatewayState
from daegam.store.settings import LLMProvider
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.llm_gateway import IMAGES
from tests.helpers.svc_tokens import claims_of, make_svc_tokens

GW = "http://llm-gateway.daegam-llm.svc.cluster.local:8080"
OPENAI = LLMProvider(name="openai", kind="openai", api_key_secret="k-openai")
CLAUDE = LLMProvider(name="Claude.Main", kind="anthropic", api_key_secret="k-claude")
PLAIN = {"content-type": "text/plain"}
READY = LlmGatewayState(ready_at=datetime(2026, 11, 9), providers_ready=["openai", "Claude.Main"])


class _State:
    """상태 문서 자리. 동기화가 쓴 값을 그대로 돌려준다."""

    def __init__(self, state: LlmGatewayState) -> None:
        self.state = state

    async def get(self) -> LlmGatewayState:
        return self.state


def _secrets() -> FakeSecretStore:
    ss = FakeSecretStore()
    ss.put("k-openai", {"api_key": "sk-openai"}, namespace=PLATFORM_NS)
    ss.put("k-claude", {"api_key": "sk-claude"}, namespace=PLATFORM_NS)
    return ss


def _endpoints(
    state: LlmGatewayState = READY, *, on: bool = True, signer: bool = True
) -> LlmEndpoints:
    settings = Settings(
        llm_gateway="agentgateway" if on else "off",
        llm_gateway_images=IMAGES if on else None,
        namespace=PLATFORM_NS,
    )
    svc = make_svc_tokens()
    if not signer:
        svc.issuer._signer = None
    ss = _secrets()
    return LlmEndpoints(settings, lambda: ss, store=_State(state), svc_tokens=svc)  # type: ignore[arg-type]


def _resolve(endpoints: LlmEndpoints, cfg: LLMProvider) -> Endpoint:
    return asyncio.run(endpoints.resolve_endpoint(cfg, tenant="acme", purpose="spec_generator"))


def test_a_ready_provider_goes_through_the_gateway_with_a_per_request_token() -> None:
    """게이트웨이 경로의 주소는 `<게이트웨이>/p/<slug>`(OpenAI 계열은 `/v1` 까지)이고 SDK 는
    자리표시 키를 받는다. 토큰은 bearer 를 부를 때마다 새로 낸 5분 토큰이고 그 provider 하나만
    연다."""
    endpoints = _endpoints()
    openai = _resolve(endpoints, OPENAI)
    assert openai == Endpoint(f"{GW}/p/openai/v1", PLACEHOLDER_KEY, openai.bearer, "gateway")
    claude = _resolve(endpoints, CLAUDE)
    assert claude.base_url == f"{GW}/p/{provider_slug(CLAUDE.name)}" and claude.gateway
    assert openai.bearer is not None
    first, second = openai.bearer(), openai.bearer()
    assert claims_of(first)["jti"] != claims_of(second)["jti"]
    claims = claims_of(first)
    assert (claims["aud"], claims["sub"], claims["tenant"]) == (
        "daegam-llm",
        "svc:platform",
        "acme",
    )
    assert (claims["purpose"], claims["llm_providers"]) == ("spec_generator", ["openai"])


@pytest.mark.parametrize(
    ("state", "on", "signer"),
    [
        (READY, False, True),  # 끄면 다음 호출부터 곧바로 직접 경로다
        (LlmGatewayState(providers_ready=["openai"]), True, True),  # 아직 반영된 리비전이 없다
        (READY, True, False),  # 서명 키가 없다
        # 그 provider 가 반영되지 않았다(SSRF 검사에 걸린 provider 도 그렇다)
        (LlmGatewayState(ready_at=datetime(2026, 11, 9), providers_ready=["other"]), True, True),
    ],
)
def test_otherwise_the_call_goes_direct_with_the_platform_key(state, on, signer) -> None:
    endpoint = _resolve(_endpoints(state, on=on, signer=signer), OPENAI)
    assert endpoint == Endpoint(None, "sk-openai", None, "direct")


def test_the_grader_takes_the_gateway_only_once_the_applied_revision_carries_its_purpose():
    """채점 모델은 반영된 리비전이 목적 속성을 실을 때만 게이트웨이 길이다. 그 전의 기록은 수집이
    거절하기 때문이다. 그 토큰은 목적 `evaluator` 와 원본 Agent 의 id 를 싣는다 (P45 11절)."""

    def graded(state: LlmGatewayState) -> Endpoint:
        return asyncio.run(
            _endpoints(state).resolve_endpoint(
                OPENAI, tenant="acme", purpose="evaluator", agent_id="ag-1"
            )
        )

    assert graded(READY).route == "direct"
    stale = READY.model_copy(update={"applied_rev": "r2", "purpose_rev": "r1"})
    assert graded(stale).route == "direct"
    carried = READY.model_copy(update={"applied_rev": "r2", "purpose_rev": "r2"})
    endpoint = graded(carried)
    assert endpoint.gateway and endpoint.bearer is not None
    claims = claims_of(endpoint.bearer())
    assert (claims["sub"], claims["purpose"], claims["agent_id"]) == (
        "svc:platform",
        "evaluator",
        "ag-1",
    )
    # 다른 플랫폼 호출자는 목적 속성과 상관없이 게이트웨이 길이고 Agent 를 싣지 않는다
    assert "agent_id" not in claims_of(_resolve(_endpoints(READY), OPENAI).bearer())


def test_a_direct_endpoint_has_no_token_and_a_stub_has_no_key() -> None:
    """직접 경로는 provider 의 주소와 키뿐이라 플랫폼 토큰이 provider 로 가지 않는다. 게이트웨이
    주소 아래에 적은 base URL 은 원장에서 게이트웨이 경로다 (P37 18절)."""
    typed = OPENAI.model_copy(update={"base_url": f"{GW}/p/openai/v1"})
    endpoint = direct_endpoint(typed, _secrets(), namespace=PLATFORM_NS, gateway_url=GW)
    assert endpoint.bearer is None and endpoint.route == "gateway"
    stub = direct_endpoint(LLMProvider(name="s", kind="stub"), None, namespace=PLATFORM_NS)
    assert stub == Endpoint(None, "", None, "direct")
    with pytest.raises(ProviderError) as ei:
        direct_endpoint(LLMProvider(name="n", kind="openai"), _secrets(), namespace=PLATFORM_NS)
    assert ei.value.code == "no_api_key"


def test_a_stub_never_goes_through_the_gateway() -> None:
    stub = LLMProvider(name="openai-stub", kind="stub")
    state = LlmGatewayState(ready_at=datetime(2026, 11, 9), providers_ready=["openai-stub"])
    assert _resolve(_endpoints(state), stub).route == "direct"


def _recording(responses: list[httpx2.Response], seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _completion() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "{}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def test_the_spec_provider_calls_the_gateway_with_a_fresh_token_per_request() -> None:
    """스펙 생성기의 provider 는 게이트웨이 경로로만 부르고 요청마다 다른 토큰을 싣는다. 자리표시
    키는 어디에도 가지 않는다. 연결 제한은 5초다."""
    endpoint = _resolve(_endpoints(), OPENAI)
    seen: list[httpx2.Request] = []
    http = _recording([_completion(), _completion()], seen)
    provider = build_provider(OPENAI, endpoint, http_client=http)
    for _ in range(2):
        asyncio.run(provider.complete("sys", [{"role": "user", "content": "hi"}], max_tokens=8))
    assert [str(r.url) for r in seen] == [f"{GW}/p/openai/v1/chat/completions"] * 2
    tokens = [r.headers["authorization"].removeprefix("Bearer ") for r in seen]
    assert len(set(tokens)) == 2 and PLACEHOLDER_KEY not in "".join(tokens)
    assert all(claims_of(t)["aud"] == "daegam-llm" for t in tokens)
    assert provider.chat.request_timeout == llm_http.gateway_timeout(300.0)


def test_a_direct_spec_provider_sends_only_the_provider_key() -> None:
    endpoint = _resolve(_endpoints(on=False), OPENAI)
    seen: list[httpx2.Request] = []
    provider = build_provider(OPENAI, endpoint, http_client=_recording([_completion()], seen))
    asyncio.run(provider.complete("sys", [{"role": "user", "content": "hi"}], max_tokens=8))
    assert seen[0].headers["authorization"] == "Bearer sk-openai"
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (
            httpx2.Response(429, headers={"x-ratelimit-remaining": "0"}),
            llm_http.GATEWAY_RATE_LIMITED,
            False,
        ),
        (
            httpx2.Response(
                403, headers={"content-type": "text/plain"}, text="authorization failed"
            ),
            llm_http.GATEWAY_DENIED,
            False,
        ),
        # provider 자신의 한도는 그대로 provider_rate_limited 이고 다시 보낸다
        (
            httpx2.Response(429, json={"error": {"type": "requests", "message": "slow down"}}),
            "provider_rate_limited",
            True,
        ),
        # 게이트웨이를 지난 provider 의 키 거절은 그날의 auth 다
        (httpx2.Response(401, json={"error": {"message": "bad key"}}), "auth", False),
    ],
)
def test_gateway_answers_become_their_codes(response, code, retryable) -> None:
    endpoint = _resolve(_endpoints(), OPENAI)
    provider = build_provider(OPENAI, endpoint, http_client=_recording([response], []))
    with pytest.raises(ProviderError) as ei:
        asyncio.run(provider.complete("sys", [{"role": "user", "content": "hi"}], max_tokens=8))
    assert (ei.value.code, ei.value.retryable) == (code, retryable)


def test_an_unreachable_gateway_is_its_own_code_and_retried() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    for kind in (OPENAI, CLAUDE):
        provider = build_provider(kind, _resolve(_endpoints(), kind), http_client=http)
        with pytest.raises(ProviderError) as ei:
            asyncio.run(provider.complete("s", [{"role": "user", "content": "x"}], max_tokens=8))
        assert (ei.value.code, ei.value.retryable) == (llm_http.GATEWAY_UNREACHABLE, True)
    direct = build_provider(OPENAI, _resolve(_endpoints(on=False), OPENAI), http_client=http)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(direct.complete("s", [{"role": "user", "content": "x"}], max_tokens=8))
    assert ei.value.code == "connection"


def test_indexer_embeddings_call_the_gateway_with_a_token_and_read_its_codes() -> None:
    """Knowledge 인덱서의 임베딩도 같은 길로 부른다. 게이트웨이의 한도는 문서 오류의 코드가
    된다."""
    endpoint = _resolve(_endpoints(), OPENAI)
    seen: list[httpx2.Request] = []
    vectors = {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}
    body = {"object": "list", "data": [vectors], "model": "m", "usage": {"prompt_tokens": 1}}
    client = build_embeddings(
        "openai",
        api_key=endpoint.api_key,
        model="text-embedding-3-small",
        dimensions=3,
        base_url=endpoint.base_url,
        http_async_client=_recording([httpx2.Response(200, json=body)], seen),
        bearer=endpoint.bearer,
        gateway=endpoint.gateway,
    )
    assert asyncio.run(client.aembed_documents(["a"])) == [[0.1, 0.2, 0.3]]
    assert str(seen[0].url) == f"{GW}/p/openai/v1/embeddings"
    token = seen[0].headers["authorization"].removeprefix("Bearer ")
    assert claims_of(token)["aud"] == "daegam-llm"
    limited = httpx2.Response(
        429, headers={"x-ratelimit-remaining": "0"}, request=httpx2.Request("POST", GW)
    )
    error = openai.RateLimitError("", response=limited, body="")
    assert classify_embedding_error(error, gateway=True).code == llm_http.GATEWAY_RATE_LIMITED
    assert classify_embedding_error(error).code == "embedding_failed"
