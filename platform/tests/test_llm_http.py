"""LLM 호출의 전송 규칙 (P37 2절): 상관 헤더, 훅 단 클라이언트, 사용량 읽기, 다시 보내기."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import anthropic
import httpx2
import openai
import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from daegam import llm_http
from daegam.llm_http import (
    CORRELATION,
    HookedChatAnthropic,
    call_scope,
    estimate_tokens,
    has_correlation_hook,
    hooked_async_client,
    is_retryable,
    retry_delay,
    set_call,
    usage_from_metadata,
)
from daegam.urls import under_base


def test_the_file_imports_neither_package() -> None:
    """런타임과 바이트 그대로 나눠 가지는 파일이라 두 꾸러미 어느 쪽도 import 하지 않는다."""
    tree = ast.parse(Path(llm_http.__file__).read_text(encoding="utf-8"))
    modules = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not {m for m in modules if m and m.split(".")[0] in ("daegam", "runtime")}


def _client(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    return hooked_async_client(httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


def test_one_call_id_is_one_request_and_a_reuse_is_counted() -> None:
    seen: list[httpx2.Request] = []
    client = _client(seen)
    before = CORRELATION.correlation_reuse

    async def go() -> None:
        call = set_call("a" * 32)
        await client.get("http://llm/v1/x")
        await client.get("http://llm/v1/x")  # 같은 id 의 둘째 요청
        with call_scope("b" * 32) as inner:
            await client.get("http://llm/v1/y")
        assert seen[0].headers["traceparent"] == call.traceparent
        assert "traceparent" not in seen[1].headers
        assert seen[2].headers["traceparent"] == f"00-{'b' * 32}-{inner.span_id}-01"

    asyncio.run(go())
    assert CORRELATION.correlation_reuse == before + 1


def test_both_sdk_clients_carry_the_hook() -> None:
    chat = HookedChatAnthropic(model="claude-x", api_key="k", max_retries=0)
    assert has_correlation_hook(chat._async_client._client)
    assert has_correlation_hook(hooked_async_client())


def test_usage_reads_cache_and_reasoning_of_both_providers() -> None:
    anthropic_meta = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "input_token_details": {
            "cache_read": 500,
            "cache_creation": 0,
            "ephemeral_5m_input_tokens": 100,
            "ephemeral_1h_input_tokens": 50,
        },
        "output_token_details": {"reasoning": 120},
    }
    assert usage_from_metadata(anthropic_meta) == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 500,
        "cache_write_tokens": 150,
        "reasoning_tokens": 120,
    }
    openai_meta = {"input_tokens": 10, "output_tokens": 2, "input_token_details": {"cache_read": 4}}
    assert usage_from_metadata(openai_meta)["cache_read_tokens"] == 4
    assert usage_from_metadata(None)["input_tokens"] == 0
    assert estimate_tokens(["abcde", "", "abcd"]) == 2 + 0 + 1


def _status(cls, status: int, body=None, headers=None):
    request = httpx2.Request("POST", "http://llm")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls(message="x", response=response, body=body)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 529])
def test_retries_what_the_sdk_used_to_resend(status) -> None:
    assert is_retryable(_status(openai.APIStatusError, status))


def test_never_retries_a_spent_quota_or_a_bad_request() -> None:
    quota = _status(openai.RateLimitError, 429, {"error": {"code": "insufficient_quota"}})
    assert not is_retryable(quota)
    assert not is_retryable(_status(openai.BadRequestError, 400))
    assert is_retryable(anthropic.APIConnectionError(request=httpx2.Request("GET", "http://x")))


def test_retry_after_is_followed_up_to_ten_seconds() -> None:
    assert retry_delay(_status(openai.RateLimitError, 429, headers={"retry-after": "3"})) == 3.0
    assert retry_delay(_status(openai.RateLimitError, 429, headers={"retry-after": "60"})) == 10.0
    assert 1.0 <= retry_delay(None) <= 1.5


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://gw.example.com:8080/v1", True),
        ("http://gw.example.com:8080/v1/openai", True),
        ("http://gw.example.com:8080/v10", False),
        ("http://gw.example.com.evil.io:8080/v1", False),
        ("https://gw.example.com:8080/v1", False),
        ("http://gw.example.com/v1", False),
        (None, False),
    ],
)
def test_under_base_needs_the_same_origin_and_a_segment_boundary(url, expected) -> None:
    assert under_base(url, "http://gw.example.com:8080/v1") is expected


# -- 게이트웨이 경로 (P38 5절·7절·10절) -------------------------------------------------------


def _gateway_error(status: int, body, headers) -> Exception:
    cls = {429: openai.RateLimitError, 401: openai.AuthenticationError}.get(
        status, openai.APIStatusError
    )
    return _status(cls, status, body, headers)


LIMITED = {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "1000"}
PLAIN = {"content-type": "text/plain"}


@pytest.mark.parametrize(
    ("status", "body", "headers", "code"),
    [
        # 게이트웨이의 한도는 본문이 비고 남은 수가 0 이다
        (429, "", LIMITED, llm_http.GATEWAY_RATE_LIMITED),
        (429, None, LIMITED, llm_http.GATEWAY_RATE_LIMITED),
        # 본문이 있는 429 는 provider 의 것이다. 한도 머리는 제한된 경로의 모든 답에 온다
        (429, {"error": {"type": "rate_limit_exceeded"}}, LIMITED, None),
        (429, "", {}, None),
        # 게이트웨이의 토큰 거절은 text/plain 문구다
        (401, "authentication failure: jwt expired", PLAIN, llm_http.GATEWAY_DENIED),
        (403, "authorization failed", PLAIN, llm_http.GATEWAY_DENIED),
        # provider 가 키를 거절한 답은 JSON 이라 게이트웨이의 것이 아니다
        (401, {"error": {"type": "authentication_error"}}, {}, None),
        # 크레딧 소진이 먼저다
        (429, {"error": {"code": "insufficient_quota"}}, LIMITED, None),
        (402, "", LIMITED, None),
        # 게이트웨이가 넘긴 provider 5xx 와 거절한 요청은 그날의 처리 그대로다
        (503, "upstream call failed: connect error", PLAIN, None),
        (413, "failed to process LLM request: body too large", PLAIN, None),
    ],
)
def test_the_gateway_classifier(status, body, headers, code) -> None:
    error = _gateway_error(status, body, headers)
    assert llm_http.gateway_error(error, gateway=True) == code
    # 직접 경로의 호출에는 게이트웨이 코드가 없다
    assert llm_http.gateway_error(error, gateway=False) is None
    if code is not None:
        assert not is_retryable(error, gateway=True) and llm_http.not_retried(error, gateway=True)
        # 직접 경로 provider 의 같은 꼴(빈 429 등)은 게이트웨이의 것이 아니라 그날처럼 다룬다
        assert not llm_http.not_retried(error, gateway=False)


def test_gateway_refusals_are_not_retried_but_its_5xx_and_absence_are() -> None:
    """한도의 429 는 1분 창이라 1·2초 뒤에도 같다. 게이트웨이를 거친 provider 5xx 와 연결 실패는
    그날처럼 다시 보낸다."""
    assert not is_retryable(_gateway_error(429, "", LIMITED), gateway=True)
    assert is_retryable(_gateway_error(429, "", LIMITED), gateway=False)
    assert is_retryable(_gateway_error(503, "upstream call failed: x", PLAIN), gateway=True)
    refused = openai.APIConnectionError(request=httpx2.Request("POST", "http://llm-gateway"))
    assert is_retryable(refused)
    assert llm_http.gateway_error(refused, gateway=True) == llm_http.GATEWAY_UNREACHABLE
    assert llm_http.gateway_error(refused, gateway=False) is None
    timeout = httpx2.ConnectTimeout("no answer")
    assert llm_http.gateway_error(timeout, gateway=True) == llm_http.GATEWAY_UNREACHABLE


def test_gateway_timeout_limits_only_the_connect() -> None:
    timeout = llm_http.gateway_timeout(300)
    assert timeout.connect == llm_http.GATEWAY_CONNECT_SECONDS == 5.0
    assert timeout.read == 300 and llm_http.gateway_timeout().read is None


def _recording(seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_the_bearer_hook_sets_a_fresh_token_per_request_and_drops_the_key() -> None:
    """플랫폼의 게이트웨이 경로(P38 5절)는 요청마다 토큰을 받는다. SDK 의 자리표시 키와
    `x-api-key` 는 게이트웨이에 가지 않는다."""
    seen: list[httpx2.Request] = []
    minted = iter(["t1", "t2"])
    client = hooked_async_client(
        _recording(seen), base_url="http://llm-gateway/p/a", bearer=lambda: next(minted)
    )

    async def go() -> None:
        for _ in range(2):
            await client.post(
                "http://llm-gateway/p/a/v1/messages",
                headers={"x-api-key": "placeholder", "Authorization": "Bearer placeholder"},
            )

    asyncio.run(go())
    assert [r.headers["authorization"] for r in seen] == ["Bearer t1", "Bearer t2"]
    assert all("x-api-key" not in r.headers for r in seen)
    assert has_correlation_hook(client)


def test_a_bearer_client_shares_one_pool_and_reads_no_proxy_env() -> None:
    first = hooked_async_client(base_url="http://llm-gateway/p/a", bearer=lambda: "a")
    second = hooked_async_client(base_url="http://llm-gateway/p/b", bearer=lambda: "b")
    assert first is not second and first._transport is second._transport
    assert first.trust_env is False and has_correlation_hook(first)
    with pytest.raises(ValueError):
        hooked_async_client(bearer=lambda: "c")


def test_the_gateway_token_goes_only_under_the_gateway_path_and_never_follows_a_redirect() -> None:
    """플랫폼의 토큰은 게이트웨이의 그 provider 경로로 가는 요청에만 실린다. 리디렉트는 따라가지
    않는다. httpx 는 요청 훅을 매 걸음 다시 돌리므로 따라가면 다른 곳에 토큰과 본문이 간다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(307, headers={"location": "http://elsewhere.example/steal"})

    mock = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=True)
    client = hooked_async_client(mock, base_url="http://llm-gateway/p/a", bearer=lambda: "jwt")

    async def go() -> list[int]:
        codes = []
        for url in (
            "http://llm-gateway/p/a/v1/messages",
            "http://llm-gateway/p/ab/v1/messages",
            "http://elsewhere.example/p/a/v1/messages",
        ):
            codes.append((await client.post(url, json={"x": 1})).status_code)
        return codes

    assert asyncio.run(go()) == [307, 307, 307]
    assert [str(r.url) for r in seen] == [
        "http://llm-gateway/p/a/v1/messages",
        "http://llm-gateway/p/ab/v1/messages",
        "http://elsewhere.example/p/a/v1/messages",
    ]
    assert [r.headers.get("authorization") for r in seen] == ["Bearer jwt", None, None]
    own = hooked_async_client(base_url="http://llm-gateway/p/a", bearer=lambda: "jwt")
    assert own.follow_redirects is False


def test_a_token_that_cannot_be_minted_is_no_signer_and_not_retried() -> None:
    """토큰 훅이 토큰을 받지 못하면 SDK 가 연결 실패로 감싸도 코드는 no_signer 이고 다시 보내지
    않는다. 게이트웨이가 없는 것(llm_gateway_unreachable)과 다르다."""

    def broken() -> str:
        raise RuntimeError("no signing key")

    client = hooked_async_client(_recording([]), base_url="http://llm-gateway/p/a", bearer=broken)
    sdk = openai.AsyncOpenAI(
        api_key="placeholder",
        base_url="http://llm-gateway/p/a/v1",
        http_client=client,
        max_retries=0,
    )

    async def go() -> Exception:
        try:
            await sdk.embeddings.create(model="m", input=["x"])
        except Exception as e:  # noqa: BLE001
            return e
        raise AssertionError("the call should fail")

    error = asyncio.run(go())
    assert isinstance(error, openai.APIConnectionError)
    assert llm_http.gateway_error(error, gateway=True) == llm_http.NO_SIGNER
    assert llm_http.not_retried(error, gateway=True) and not is_retryable(error, gateway=True)


MESSAGE = {
    "id": "m",
    "type": "message",
    "role": "assistant",
    "model": "claude-x",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
# anthropic SDK 2.x 가 보내는 API 판이다. 판을 올린 SDK 는 여기서 걸려 그 판의 변경을 다시 본다
API_VERSION = "2026-10-21"
GATEWAY_BASE = "http://llm-gateway/p/a"


def _anthropic_requests(chat_kwargs: dict, calls: int = 1) -> list[httpx2.Request]:
    """HookedChatAnthropic 을 목 transport 로 calls 번 불러 나간 요청을 돌려준다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=MESSAGE)

    chat = HookedChatAnthropic(
        model="claude-x",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        **chat_kwargs,
    )

    async def go() -> None:
        for _ in range(calls):
            await chat.ainvoke("hi")

    asyncio.run(go())
    return seen


@pytest.mark.parametrize(
    ("chat_kwargs", "authorization"),
    [
        # 직접 경로. Claude API 와 base_url 뒤의 Anthropic 호환 서버가 같은 헤더를 받는다
        ({"api_key": "sk-ant"}, "Bearer sk-ant"),
        ({"api_key": "sk-ant", "base_url": "http://llm-proxy/anthropic"}, "Bearer sk-ant"),
        # 런타임의 게이트웨이 경로. env 의 90일 토큰이 키 자리에 온다
        ({"api_key": "llm-token", "base_url": GATEWAY_BASE}, "Bearer llm-token"),
        # 플랫폼의 게이트웨이 경로. 훅이 자리표시 키를 요청마다 받는 토큰으로 덮는다
        (
            {"api_key": "placeholder", "base_url": GATEWAY_BASE, "bearer": lambda: "t1"},
            "Bearer t1",
        ),
    ],
)
def test_anthropic_sends_one_bearer_on_api_version_2026_10_21(chat_kwargs, authorization) -> None:
    """anthropic SDK 2.x 의 요청이다. 키는 `Authorization: Bearer` 하나이고 `x-api-key` 는 없다.
    자리표시 키는 어느 헤더에도 남지 않는다."""
    (request,) = _anthropic_requests(chat_kwargs)
    assert request.headers["authorization"] == authorization
    assert "x-api-key" not in request.headers
    assert request.headers["anthropic-version"] == API_VERSION
    assert not any("placeholder" in v for v in request.headers.values())


def test_the_gateway_client_has_the_connect_limit() -> None:
    chat = HookedChatAnthropic(
        model="claude-x",
        api_key="llm-token",
        base_url=GATEWAY_BASE,
        connect_timeout=llm_http.GATEWAY_CONNECT_SECONDS,
    )
    assert chat._async_client.timeout.connect == llm_http.GATEWAY_CONNECT_SECONDS


def test_the_sdk_adds_no_trace_context_of_its_own() -> None:
    """`traceparent` 는 상관 헤더 훅만 싣는다. 지금 문맥에 유효한 OpenTelemetry span 이 있어도
    같은 id 의 둘째 요청은 헤더 없이 간다. SDK 의 OpenTelemetry 가 켜져 있으면 그 span 의 id 가
    실린다."""
    parent = SpanContext(
        trace_id=0x1234, span_id=0x5678, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED)
    )
    with trace.use_span(NonRecordingSpan(parent)), call_scope("c" * 32) as call:
        first, second = _anthropic_requests({"api_key": "sk-ant"}, calls=2)
    assert first.headers["traceparent"] == call.traceparent
    assert "traceparent" not in second.headers
