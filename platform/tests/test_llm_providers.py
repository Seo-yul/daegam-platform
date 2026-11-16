"""P2 F10(b): LLM provider 계약, 키 조회(Secret 뿐, D25), 연결 확인, 요청 모양(가짜 HTTP),
실제 키가 있을 때만 실호출."""

from __future__ import annotations

import asyncio
import json
import os

import httpx2
import pytest

from daegam.services.llm_endpoint import direct_endpoint
from daegam.services.llm_providers import (
    CHECK_CODES,
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
    SpecProvider,
    build_provider,
    check_provider,
    resolve_api_key,
)
from daegam.store.settings import LLMProvider, ProviderRouting
from tests.helpers.consts import PLATFORM_NS
from tests.helpers.fake_llm import FakeProvider
from tests.helpers.fake_secrets import FakeSecretStore


def secret_store(**secrets: dict[str, str]) -> FakeSecretStore:
    ss = FakeSecretStore()
    for name, data in secrets.items():
        ss.put(name, data, namespace=PLATFORM_NS)
    return ss


# -- 키 조회 ----------------------------------------------------------------------


def test_resolve_key_reads_secret_only(monkeypatch):
    """키는 플랫폼 네임스페이스 Secret 에서만 온다. env 는 있어도 보지 않는다."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    ss = FakeSecretStore()
    cfg = LLMProvider(name="a", kind="openai", api_key_secret="llm-keys")

    # Secret 참조가 없으면 env 와 무관하게 no_api_key
    for kind in ("openai", "anthropic"):
        with pytest.raises(ProviderError) as ei:
            resolve_api_key(LLMProvider(name="b", kind=kind), ss, namespace=PLATFORM_NS)
        assert ei.value.code == "no_api_key" and "'b'" in ei.value.detail
    # 참조는 있는데 Secret 이 없으면 no_secret. 다른 네임스페이스의 Secret 은 보지 않는다
    ss.put("llm-keys", {"api_key": "sk-other-ns"}, namespace="elsewhere")
    with pytest.raises(ProviderError) as ei:
        resolve_api_key(cfg, ss, namespace=PLATFORM_NS)
    assert ei.value.code == "no_secret" and "'llm-keys'" in ei.value.detail
    # Secret 안의 키 이름은 api_key 를 먼저 보고, 없으면 kind 별 옛 이름, 항목이 하나면 그 값이다
    ss.put("llm-keys", {"api_key": "sk-secret"}, namespace=PLATFORM_NS)
    assert resolve_api_key(cfg, ss, namespace=PLATFORM_NS) == "sk-secret"
    ss.put("llm-keys", {"OPENAI_API_KEY": "sk-env-named"}, namespace=PLATFORM_NS)
    assert resolve_api_key(cfg, ss, namespace=PLATFORM_NS) == "sk-env-named"
    ss.put("llm-keys", {"whatever": "sk-single"}, namespace=PLATFORM_NS)
    assert resolve_api_key(cfg, ss, namespace=PLATFORM_NS) == "sk-single"
    # 키를 고를 수 없으면 no_api_key. 메시지에 Secret 의 값은 나오지 않는다
    ss.put("llm-keys", {"a": "sk-leak-1", "b": "sk-leak-2"}, namespace=PLATFORM_NS)
    with pytest.raises(ProviderError) as ei:
        resolve_api_key(cfg, ss, namespace=PLATFORM_NS)
    assert ei.value.code == "no_api_key"
    assert "sk-leak" not in str(ei.value) and "sk-from-env" not in str(ei.value)
    # Secret 참조가 있는데 저장소가 없으면 no_secret_store
    with pytest.raises(ProviderError) as ei:
        resolve_api_key(cfg, None, namespace=PLATFORM_NS)
    assert ei.value.code == "no_secret_store"


def _built(cfg: LLMProvider, ss: FakeSecretStore) -> SpecProvider:
    """직접 경로의 provider. 키는 Secret 에서만 온다 (D25)."""
    return build_provider(cfg, direct_endpoint(cfg, ss, namespace=PLATFORM_NS))


def test_env_alone_is_not_a_key(monkeypatch):
    """env 에 키가 있어도 Secret 이 없으면 provider 를 만들 수 없다 (폴백 없음)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    for kind in ("openai", "anthropic"):
        with pytest.raises(ProviderError) as ei:
            _built(LLMProvider(name="x", kind=kind, api_key_secret="missing"), FakeSecretStore())
        assert ei.value.code == "no_secret", kind
        with pytest.raises(ProviderError) as ei:
            _built(LLMProvider(name="x", kind=kind), FakeSecretStore())
        assert ei.value.code == "no_api_key", kind


def test_build_provider_kinds():
    ss = secret_store(**{"oai-keys": {"api_key": "sk-o"}, "ant-keys": {"api_key": "sk-a"}})
    p = _built(LLMProvider(name="o", kind="openai", api_key_secret="oai-keys"), ss)
    assert isinstance(p, OpenAIProvider) and isinstance(p, SpecProvider)
    assert p.model == "gpt-4o" and p.chat.model_name == "gpt-4o"
    assert p.chat.openai_api_key.get_secret_value() == "sk-o"
    # JSON 만 받도록 생성 시점에 고정한다
    assert p.chat.model_kwargs["response_format"] == {"type": "json_object"}
    p = _built(
        LLMProvider(
            name="gw", kind="openai", model="m", base_url="http://gw/v1", api_key_secret="oai-keys"
        ),
        ss,
    )
    assert p.model == "m" and str(p.chat.openai_api_base).startswith("http://gw/v1")
    p = _built(LLMProvider(name="a", kind="anthropic", api_key_secret="ant-keys"), ss)
    assert isinstance(p, AnthropicProvider) and p.model == "claude-opus-5"
    assert p.chat.model == "claude-opus-5"
    assert p.chat.anthropic_api_key.get_secret_value() == "sk-a"


# -- 연결 확인 (models.list 를 가짜 HTTP 로) --------------------------------------------


def test_check_provider_codes():
    """Secret 이 있고 키를 고를 수 있으면 models.list 를 한 번 부른다. 응답 코드는 7종뿐이고
    원문 메시지는 싣지 않는다."""
    seen: list[httpx2.Request] = []
    status = {"code": 200}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if status["code"] == 0:
            raise httpx2.ConnectError("refused")
        if status["code"] == 200:
            return httpx2.Response(200, json={"object": "list", "data": []})
        return httpx2.Response(
            status["code"],
            json={"error": {"message": "Incorrect API key provided: sk-secr***", "type": "x"}},
        )

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    ss = secret_store(
        **{"oai-keys": {"api_key": "sk-secret"}, "ant-keys": {"api_key": "sk-ant-secret"}}
    )
    oai = LLMProvider(name="o", kind="openai", api_key_secret="oai-keys", base_url="http://gw/v1")
    ant = LLMProvider(name="a", kind="anthropic", api_key_secret="ant-keys")

    def check(cfg: LLMProvider):
        return asyncio.run(check_provider(cfg, ss, namespace=PLATFORM_NS, http_client=http))

    # 키 경로 문제는 HTTP 를 부르지 않는다
    assert check(LLMProvider(name="n", kind="openai")).code == "no_api_key"
    assert check(LLMProvider(name="n", kind="openai", api_key_secret="nope")).code == "no_secret"
    ss.put("empty", {"a": "1", "b": "2"}, namespace=PLATFORM_NS)
    assert check(LLMProvider(name="n", kind="openai", api_key_secret="empty")).code == "no_api_key"
    assert seen == []

    # 두 kind 모두 models.list 한 번. Secret 의 키가 요청에 실린다
    r = check(oai)
    assert (r.ok, r.code) == (True, "ok")
    assert str(seen[-1].url) == "http://gw/v1/models" and seen[-1].method == "GET"
    assert seen[-1].headers["authorization"] == "Bearer sk-secret"
    r = check(ant)
    assert (r.ok, r.code) == (True, "ok")
    assert seen[-1].url.path == "/v1/models"
    assert seen[-1].headers["authorization"] == "Bearer sk-ant-secret"
    assert "x-api-key" not in seen[-1].headers

    for code, expected in [(401, "auth"), (403, "auth"), (500, "provider_error")]:
        status["code"] = code
        for cfg in (oai, ant):
            r = check(cfg)
            assert (r.ok, r.code) == (False, expected), (code, cfg.kind)
    status["code"] = 429
    assert check(oai).code == "provider_error"
    status["code"] = 0
    for cfg in (oai, ant):
        assert check(cfg).code == "connection", cfg.kind
    # 재시도 없음: 확인마다 HTTP 호출은 정확히 한 번이다
    assert len(seen) == 2 + 3 * 2 + 1 + 2
    assert {
        "ok",
        "no_secret",
        "no_api_key",
        "auth",
        "connection",
        "quota_exhausted",
        "provider_error",
    } == set(CHECK_CODES)
    asyncio.run(http.aclose())


def test_check_provider_sees_an_exhausted_credit_that_the_model_list_hides():
    """크레딧이 바닥난 키도 모델 목록은 200 이다(T20). OpenAI 자체는 가장 싼 임베딩 한 번으로,
    OpenRouter 는 `/key` 의 남은 한도로 본다. 임베딩의 다른 실패(호환 게이트웨이의 404 등)는 결과를
    바꾸지 않는다. 모델 목록이 402 나 크레딧 소진 429 면 그것도 quota_exhausted 다."""
    answers: dict[str, httpx2.Response] = {}
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        for suffix, response in answers.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx2.Response(200, json={"object": "list", "data": []})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    ss = secret_store(**{"keys": {"api_key": "sk-secret"}})
    openai_cfg = LLMProvider(name="o", kind="openai", api_key_secret="keys")
    router = LLMProvider(
        name="r", kind="openai", base_url="https://openrouter.ai/api/v1", api_key_secret="keys"
    )
    gateway = LLMProvider(name="g", kind="openai", base_url="http://gw/v1", api_key_secret="keys")

    def check(cfg: LLMProvider) -> str:
        return asyncio.run(check_provider(cfg, ss, namespace=PLATFORM_NS, http_client=http)).code

    quota = {"error": {"message": "You exceeded your quota", "code": "insufficient_quota"}}
    answers["/embeddings"] = httpx2.Response(429, json=quota)
    assert check(openai_cfg) == "quota_exhausted"
    assert seen[-2:] == ["/v1/models", "/v1/embeddings"]
    answers["/embeddings"] = httpx2.Response(404, json={"error": {"message": "no model"}})
    assert check(openai_cfg) == "ok"
    seen.clear()
    assert check(gateway) == "ok" and seen == ["/v1/models"]
    answers["/key"] = httpx2.Response(200, json={"data": {"limit": 5, "limit_remaining": 0}})
    assert check(router) == "quota_exhausted"
    answers["/key"] = httpx2.Response(200, json={"data": {"limit": None, "limit_remaining": None}})
    assert check(router) == "ok"
    answers["/models"] = httpx2.Response(402, json={"error": {"message": "Insufficient credits"}})
    assert check(gateway) == "quota_exhausted"
    asyncio.run(http.aclose())


def test_check_provider_confirms_the_key_where_the_model_list_is_open():
    """OpenRouter 는 모델 목록을 키 없이도 200 으로 준다. 목록만 보면 틀린 키도 ok 이므로 `/key` 를
    한 번 더 부르고, 그것이 401 이면 auth 다. 다른 OpenAI 호환 주소는 목록 한 번 그대로다."""
    seen: list[str] = []
    key_status = {"code": 200}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/key"):
            return httpx2.Response(key_status["code"], json={"data": {"label": "sk-or-v1-x"}})
        return httpx2.Response(200, json={"object": "list", "data": []})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    ss = secret_store(**{"or-keys": {"api_key": "sk-or-v1-secret"}})
    router = LLMProvider(
        name="kimi",
        kind="openai",
        model="moonshotai/kimi-k3",
        base_url="https://openrouter.ai/api/v1",
        api_key_secret="or-keys",
    )

    def check(cfg: LLMProvider):
        return asyncio.run(check_provider(cfg, ss, namespace=PLATFORM_NS, http_client=http))

    assert (check(router).ok, seen) == (True, ["/api/v1/models", "/api/v1/key"])
    key_status["code"] = 401
    assert check(router).code == "auth"
    seen.clear()
    other = router.model_copy(update={"base_url": "http://gw/v1"})
    assert check(other).ok and seen == ["/v1/models"]
    asyncio.run(http.aclose())


# kind, Secret 의 키 항목, 기본 모델, 기본 주소, 연결 확인이 부르는 주소
GATEWAY_KINDS = [
    (
        "openrouter",
        "OPENROUTER_API_KEY",
        "moonshotai/kimi-k3",
        "https://openrouter.ai/api/v1",
        ["https://openrouter.ai/api/v1/models", "https://openrouter.ai/api/v1/key"],
    ),
    (
        "zai",
        "api_key",
        "glm-5.3",
        "https://api.z.ai/api/paas/v4",
        ["https://api.z.ai/api/paas/v4/models"],
    ),
]


@pytest.mark.parametrize(("kind", "item", "model", "base", "checked"), GATEWAY_KINDS)
def test_gateway_kind_defaults_to_its_address_and_model(kind, item, model, base, checked):
    """openrouter 와 zai 는 openai SDK 로 부르고, base_url 과 모델을 비우면 kind 의 주소와 모델이다.
    적은 주소는 kind 기본보다 앞선다. 연결 확인은 openrouter 만 그 주소의 `/key` 까지 부른다. 둘 다
    임베딩 provider 로는 쓰지 않는다. openrouter 는 운영자가 만든 Secret 의 `OPENROUTER_API_KEY`
    항목도 키로 읽는다."""
    cfg = LLMProvider(name="gw", kind=kind, api_key_secret="gw-keys")
    assert (cfg.sdk_kind, cfg.effective_model, cfg.effective_base_url) == ("openai", model, base)
    assert not cfg.supports_embedding
    typed = cfg.model_copy(update={"base_url": "http://gw/v1"})
    assert typed.effective_base_url == "http://gw/v1"
    with pytest.raises(ValueError):
        LLMProvider(name="gw", kind=kind, embedding_model="text-embedding-3-small")
    ss = secret_store(**{"gw-keys": {item: "gw-secret"}})
    p = _built(cfg, ss)
    assert isinstance(p, OpenAIProvider) and p.model == model
    assert str(p.chat.openai_api_base) == base
    assert p.chat.openai_api_key.get_secret_value() == "gw-secret"
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"object": "list", "data": []})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    assert asyncio.run(check_provider(cfg, ss, namespace=PLATFORM_NS, http_client=http)).ok
    assert seen == checked
    asyncio.run(http.aclose())


def test_openrouter_routing_rides_in_the_request_body():
    """업체 선호(T19)는 openrouter 만 받고, 기본값인 필드는 빼고 요청 본문 `provider` 로 싣는다.
    다 기본값이면 본문에 아무것도 싣지 않는다. Spec Generator 의 ChatOpenAI 도 같은 값을 받는다."""
    routing = ProviderRouting(order=["moonshotai", "deepinfra/turbo"], require_parameters=True)
    cfg = LLMProvider(name="kimi", kind="openrouter", api_key_secret="or-keys", routing=routing)
    assert cfg.extra_body == {
        "provider": {"order": ["moonshotai", "deepinfra/turbo"], "require_parameters": True}
    }
    full = ProviderRouting(order=["moonshotai"], allow_fallbacks=False, quantizations=["fp8"])
    assert full.body() == {
        "order": ["moonshotai"],
        "allow_fallbacks": False,
        "quantizations": ["fp8"],
    }
    assert cfg.model_copy(update={"routing": ProviderRouting()}).extra_body is None
    assert LLMProvider(name="kimi", kind="openrouter").extra_body is None
    for bad in (
        {"kind": "openai", "routing": {}},
        {"kind": "anthropic", "routing": {"order": ["x"]}},
        {"kind": "openrouter", "routing": {"order": ["Moonshot AI"]}},
        {"kind": "openrouter", "routing": {"quantizations": ["fp5"]}},
        {"kind": "openrouter", "routing": {"sort": "price"}},
    ):
        with pytest.raises(ValueError):
            LLMProvider.model_validate({"name": "p", **bad})
    ss = secret_store(**{"or-keys": {"api_key": "sk-or-v1-secret"}})
    p = _built(cfg, ss)
    assert isinstance(p, OpenAIProvider) and p.chat.extra_body == cfg.extra_body
    plain = _built(cfg.model_copy(update={"routing": None}), ss)
    assert plain.chat.extra_body is None


def test_check_provider_requires_secret_store():
    with pytest.raises(ProviderError) as ei:
        asyncio.run(
            check_provider(
                LLMProvider(name="o", kind="openai", api_key_secret="k"),
                None,
                namespace=PLATFORM_NS,
            )
        )
    assert ei.value.code == "no_secret_store"


# -- 요청 모양 (SDK 를 가짜 HTTP 로) ---------------------------------------------------


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _chat_response(content: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }


def test_openai_request_shape_and_usage():
    """레거시 모델: max_tokens 는 max_completion_tokens 로 나가고 temperature 0 은 살아 있다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_chat_response('{"openapi": "3.0.3"}'))

    p = OpenAIProvider(
        "o", "gpt-4o", "sk-test", base_url="http://gw/v1", http_client=_client(handler)
    )
    text, usage = asyncio.run(
        p.complete("SYS", [{"role": "user", "content": "make a spec"}], max_tokens=1234)
    )
    assert text == '{"openapi": "3.0.3"}'
    assert usage == {
        "input_tokens": 12,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    req = seen[0]
    assert str(req.url) == "http://gw/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4o"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 1234 and "max_tokens" not in body
    assert body["temperature"] == 0
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "make a spec"},
    ]


def test_openai_reasoning_model_drops_unsupported_params():
    """gpt-5 계열은 max_tokens 와 기본값 아닌 temperature 를 400 으로 거부한다. 우리가 모델
    이름을 분기하지 않고 langchain-openai 가 떼도록 맡긴 것을 요청 본문으로 확인한다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_chat_response("{}"))

    p = OpenAIProvider("o", "gpt-5-mini", "sk-test", http_client=_client(handler))
    text, _ = asyncio.run(p.complete("SYS", [{"role": "user", "content": "x"}], max_tokens=1234))
    assert text == "{}"
    body = json.loads(seen[0].content)
    assert body["max_completion_tokens"] == 1234 and "max_tokens" not in body
    assert "temperature" not in body
    assert body["response_format"] == {"type": "json_object"}


def test_openai_errors_map_to_provider_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        what = body["messages"][-1]["content"]
        if what == "quota":
            error = {"message": "You exceeded your current quota", "code": "insufficient_quota"}
            return httpx2.Response(429, json={"error": error})
        if what == "key-limit":
            error = {"message": "Key limit exceeded (monthly limit)", "code": 403}
            return httpx2.Response(403, json={"error": error})
        code = int(what)
        return httpx2.Response(code, json={"error": {"message": f"boom {code}"}})

    async def call(status: int | str):
        p = OpenAIProvider("o", "m", "k", http_client=_client(handler), max_retries=0)
        return await p.complete("s", [{"role": "user", "content": str(status)}], max_tokens=10)

    for status, code, retryable in [
        (429, "provider_rate_limited", True),
        ("quota", "quota_exhausted", False),
        ("key-limit", "quota_exhausted", False),
        (402, "quota_exhausted", False),
        (500, "provider_error", True),
        (401, "auth", False),
        (400, "bad_request", False),
    ]:
        with pytest.raises(ProviderError) as ei:
            asyncio.run(call(status))
        assert (ei.value.code, ei.value.retryable) == (code, retryable), status


def test_openai_connection_error_is_retryable():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    p = OpenAIProvider("o", "m", "k", http_client=_client(handler), max_retries=0)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(p.complete("s", [{"role": "user", "content": "x"}], max_tokens=10))
    assert (ei.value.code, ei.value.retryable) == ("connection", True)


def test_anthropic_request_shape_and_usage():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {"type": "text", "text": '{"openapi": '},
                    {"type": "text", "text": '"3.0.3"}'},
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 30, "output_tokens": 9},
            },
        )

    p = AnthropicProvider(
        "a", "claude-opus-5", "sk-ant-test", http_client=_client(handler), max_retries=0
    )
    text, usage = asyncio.run(
        p.complete(
            "SYS",
            [
                {"role": "user", "content": "make a spec"},
                {"role": "assistant", "content": "{}"},
                {"role": "user", "content": "fix it"},
            ],
            max_tokens=4321,
        )
    )
    assert text == '{"openapi": "3.0.3"}'
    # 다섯 수다. 표는 llm_http.usage_from_metadata 가 읽는다 (P37 7절)
    assert usage == {
        "input_tokens": 30,
        "output_tokens": 9,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    req = seen[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["authorization"] == "Bearer sk-ant-test"
    assert "x-api-key" not in req.headers
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5" and body["max_tokens"] == 4321
    assert body["system"] == "SYS"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert "temperature" not in body


def test_anthropic_refusal_and_errors():
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        what = body["messages"][-1]["content"]
        if what == "refuse":
            return httpx2.Response(
                200,
                json={
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [],
                    "stop_reason": "refusal",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )
        if what == "credit":
            low = "Your credit balance is too low to access the Anthropic API."
            error = {"type": "invalid_request_error", "message": low}
            return httpx2.Response(400, json={"type": "error", "error": error})
        return httpx2.Response(
            int(what), json={"type": "error", "error": {"type": "x", "message": f"boom {what}"}}
        )

    async def call(what: str):
        p = AnthropicProvider(
            "a", "claude-opus-5", "k", http_client=_client(handler), max_retries=0
        )
        return await p.complete("s", [{"role": "user", "content": what}], max_tokens=10)

    with pytest.raises(ProviderError) as ei:
        asyncio.run(call("refuse"))
    assert ei.value.code == "refusal"
    for status, code, retryable in [
        ("429", "provider_rate_limited", True),
        ("credit", "quota_exhausted", False),
        ("529", "provider_error", True),
        ("403", "auth", False),
    ]:
        with pytest.raises(ProviderError) as ei:
            asyncio.run(call(status))
        assert (ei.value.code, ei.value.retryable) == (code, retryable), status


# -- 가짜 ------------------------------------------------------------------------


def test_fake_provider_sequence():
    fake = FakeProvider(["a", ProviderError("provider_rate_limited", "slow", retryable=True), "c"])
    assert isinstance(fake, SpecProvider)
    text, usage = asyncio.run(fake.complete("s", [{"role": "user", "content": "1"}], max_tokens=5))
    assert text == "a" and usage == {"input_tokens": 1000, "output_tokens": 500}
    with pytest.raises(ProviderError):
        asyncio.run(fake.complete("s", [], max_tokens=5))
    assert asyncio.run(fake.complete("s", [], max_tokens=5))[0] == "c"
    assert asyncio.run(fake.complete("s", [], max_tokens=5))[0] == "c"  # 마지막 응답 반복
    assert len(fake.calls) == 4 and fake.calls[0]["max_tokens"] == 5


# -- 실제 provider: 셸 env 에 키가 있을 때만. 플랫폼 경로대로 Secret 에 넣어 부른다 ------------

SYSTEM = "Reply with a JSON object only. No prose, no code fences."
ASK = [{"role": "user", "content": 'Return exactly {"ok": true}.'}]


def live_provider(kind: str, env_name: str) -> SpecProvider:
    """env 값을 FakeSecretStore 에 넣고 직접 경로로 만든다. 폴백이 없으므로 이 경로뿐이다."""
    ss = secret_store(**{"live-keys": {"api_key": os.environ[env_name]}})
    return _built(LLMProvider(name=kind, kind=kind, api_key_secret="live-keys"), ss)


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_openai_live():
    p = live_provider("openai", "OPENAI_API_KEY")
    text, usage = asyncio.run(p.complete(SYSTEM, ASK, max_tokens=64))
    assert json.loads(text)["ok"] is True
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
def test_anthropic_live():
    p = live_provider("anthropic", "ANTHROPIC_API_KEY")
    text, usage = asyncio.run(p.complete(SYSTEM, ASK, max_tokens=256))
    assert json.loads(text)["ok"] is True
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
