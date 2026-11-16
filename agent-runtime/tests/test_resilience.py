"""재시도·폴백(F2c): 일시 오류 판정, 스텁 실패 훅으로 재시도 뒤 성공, 소진 뒤 실패, 폴백 전환.
크레딧 소진(T20)은 다시 시도하지 않고 턴 오류 quota_exhausted 로 끝난다."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import httpx2
import openai
import pytest

from runtime.llm_http import quota_exhausted
from runtime.resilience import is_transient
from runtime.stream import caused_by_quota
from runtime.stub import FAIL_FIRST_ENV, StubChatModel, StubTransientError
from tests.conftest import chat, event_names, events_named, run_app


class _Status(Exception):
    def __init__(self, status: int, *, code: str | None = None, body: Any = None) -> None:
        super().__init__(str(status))
        self.status_code = status
        self.code = code
        self.body = body


def _openai_quota() -> _Status:
    """OpenAI 의 크레딧 소진이다. 클라이언트는 body 의 error 를 벗겨 code 를 둔다."""
    return _Status(429, code="insufficient_quota", body={"code": "insufficient_quota"})


def _quota_errors() -> list[_Status]:
    """업체마다 크레딧 소진·한도 도달의 꼴이다. OpenAI 클라이언트는 body 의 error 를 벗긴다."""
    return [
        _openai_quota(),
        _Status(429, code="credit_balance_exhausted"),
        # Z.AI 의 잔액 부족이다. 코드가 숫자 문자열이다
        _Status(429, code="1113", body={"code": "1113", "message": "Insufficient balance"}),
        _Status(402, body={"code": 402, "message": "Insufficient credits"}),
        _Status(403, body={"code": 403, "message": "Key limit exceeded (monthly limit)"}),
        _Status(402, body={"type": "error", "error": {"type": "billing_error"}}),
        _Status(
            400,
            body={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the Anthropic API.",
                },
            },
        ),
    ]


def test_is_transient_classifies_connection_429_and_5xx() -> None:
    assert is_transient(StubTransientError("x"))
    assert is_transient(_Status(429)) and is_transient(_Status(503))
    assert not is_transient(_Status(400)) and not is_transient(_Status(401))
    # SDK 가 다시 보내던 408·409 도 다시 보낸다(llm_http 의 규칙, P37 2절)
    assert is_transient(_Status(408)) and is_transient(_Status(409))
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(TimeoutError())
    assert not is_transient(ValueError("bad"))


def test_an_exhausted_quota_is_not_transient_even_as_429() -> None:
    """크레딧 소진은 기다려도 풀리지 않는다. 같은 429 라도 rate limit 만 다시 시도한다."""
    for error in _quota_errors():
        assert quota_exhausted(error), error.body
        assert not is_transient(error)
    rate_limited = _Status(429, code="rate_limit_exceeded", body={"code": "rate_limit_exceeded"})
    assert not quota_exhausted(rate_limited) and is_transient(rate_limited)
    assert not quota_exhausted(_Status(400, body={"error": {"type": "invalid_request_error"}}))
    group = ExceptionGroup("tools", [ValueError("x"), RuntimeError("y")])
    assert not caused_by_quota(group)
    wrapped = RuntimeError("fallback failed")
    wrapped.__cause__ = _openai_quota()
    assert caused_by_quota(wrapped)
    assert caused_by_quota(ExceptionGroup("tools", [ValueError("x"), _openai_quota()]))


@pytest.fixture
def fast_retry(monkeypatch) -> None:
    monkeypatch.setenv("DAEGAM_RETRY_INITIAL_DELAY_SECONDS", "0.01")


async def test_retry_succeeds_after_transient_failure(support_doc, monkeypatch, fast_retry) -> None:
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    support_doc["main"]["limits"]["retries"] = 1
    async with run_app(support_doc) as running:
        response, sse = await chat(running, '{"tool": "Add", "args": {"a": 2, "b": 3}}')
        assert response.status_code == 200
        assert events_named(sse, "done")[0]["text"] == "결과: 5"
        assert not events_named(sse, "model_fallback")
        assert running.runtime.agent.model.failures == 1
        retries = running.observed("model_retry")
        assert len(retries) == 1
        assert retries[0]["attempt"] == 1 and retries[0]["model"] == "stub"
        assert retries[0]["turn"] == 1 and retries[0]["session_id"]


async def test_retries_exhausted_fails_turn(support_doc, monkeypatch, fast_retry) -> None:
    monkeypatch.setenv(FAIL_FIRST_ENV, "3")
    support_doc["main"]["limits"]["retries"] = 1
    async with run_app(support_doc) as running:
        response, sse = await chat(running, "hi")
        assert response.status_code == 200
        assert event_names(sse) == ["error"]
        error = sse[0][1]
        assert error["code"] == "turn_failed" and "StubTransientError" in error["message"]
        assert running.runtime.agent.model.failures == 2
        assert running.ended("turn_failed") == 1


class _BadRequestModel(StubChatModel):
    """400 처럼 다시 시도해도 소용없는 오류를 낸다."""

    calls: int = 0

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.calls += 1
        raise _Status(400)


class _QuotaModel(StubChatModel):
    """크레딧이 바닥난 provider 처럼 429 insufficient_quota 를 낸다."""

    calls: int = 0

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.calls += 1
        raise _openai_quota()


async def test_an_exhausted_quota_ends_the_turn_without_retrying(support_doc, fast_retry) -> None:
    """크레딧 소진은 다시 시도하지 않고 turn_failed 와 다른 quota_exhausted 로 끝난다. 화면은 결제와
    한도를 확인하라고 안내한다."""
    support_doc["main"]["limits"]["retries"] = 3
    model = _QuotaModel(model_name="stub")
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, "hi")
        assert sse[0][0] == "error" and sse[0][1]["code"] == "quota_exhausted"
        assert model.calls == 1
        assert not running.observed("model_retry")


async def test_an_exhausted_main_model_goes_straight_to_the_fallback(
    support_doc, fast_retry
) -> None:
    """메인 모델의 크레딧이 바닥나면 다시 시도하지 않고 곧바로 폴백 모델로 간다. 폴백이 답하면 턴은
    성공이다."""
    support_doc["main"]["tools"] = []
    support_doc["main"]["limits"]["retries"] = 3
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    support_doc["main"]["fallback"] = fallback
    model = _QuotaModel(model_name="stub")
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, "hi")
        assert events_named(sse, "done") and not events_named(sse, "error")
        assert events_named(sse, "model_fallback")
        assert model.calls == 1
        assert not running.observed("model_retry")


async def test_non_transient_error_is_not_retried(support_doc, fast_retry) -> None:
    support_doc["main"]["limits"]["retries"] = 3
    model = _BadRequestModel(model_name="stub")
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, "hi")
        assert sse[0][0] == "error" and sse[0][1]["code"] == "turn_failed"
        assert model.calls == 1
        assert not running.observed("model_retry")


async def test_fallback_model_takes_over(support_doc, monkeypatch, fast_retry, fake_mcp) -> None:
    monkeypatch.setenv(FAIL_FIRST_ENV, "5")
    support_doc["main"]["limits"]["retries"] = 0
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    support_doc["main"]["fallback"] = fallback
    async with run_app(support_doc) as running:
        response, sse = await chat(running, '{"tool": "Add", "args": {"a": 4, "b": 5}}')
        assert response.status_code == 200
        assert events_named(sse, "done")[0]["text"] == "결과: 9"
        assert fake_mcp.calls["Add"] == 1
        # 모델 호출 두 번(툴 호출, 결과 요약) 모두 폴백으로 갔다
        fallbacks = events_named(sse, "model_fallback")
        assert fallbacks == [{"from": "stub", "to": "stub-fallback"}] * 2
        events = running.observed("model_fallback")
        assert len(events) == 2
        assert events[0]["from"] == "stub" and events[0]["to"] == "stub-fallback"
        assert running.runtime.agent.model.failures == 2
        assert running.runtime.agent.fallback_model.model_name == "stub-fallback"


async def test_fallback_after_retries(support_doc, monkeypatch, fast_retry) -> None:
    """재시도를 먼저 소진하고 그다음 폴백이다. 폴백 모델은 자기 재시도를 따로 거친다."""
    monkeypatch.setenv(FAIL_FIRST_ENV, "10")
    support_doc["main"]["tools"] = []
    support_doc["main"]["limits"]["retries"] = 2
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    support_doc["main"]["fallback"] = fallback
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        assert events_named(sse, "done")[0]["text"] == "Tool 이 없어 답만 합니다: hi"
        # 주 모델 1 + 재시도 2 = 3번 실패한 뒤 폴백으로 넘어간다
        assert running.runtime.agent.model.failures == 3
        retries = running.observed("model_retry")
        assert [e["attempt"] for e in retries] == [1, 2]
        assert events_named(sse, "model_fallback") == [{"from": "stub", "to": "stub-fallback"}]


async def test_fail_first_env_is_ignored_without_stub(support_doc, monkeypatch) -> None:
    """훅은 메인 모델에만 걸린다. 폴백 스텁은 실패하지 않는다."""
    monkeypatch.setenv(FAIL_FIRST_ENV, "2")
    support_doc["main"]["tools"] = []
    support_doc["main"]["limits"]["retries"] = 0
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    support_doc["main"]["fallback"] = fallback
    async with run_app(support_doc) as running:
        assert running.runtime.agent.model.fail_first == 2
        assert running.runtime.agent.fallback_model.fail_first == 0


GATEWAY_REQUEST = httpx2.Request("POST", "http://llm-gateway/p/main/v1/chat/completions")


def _gateway_limited() -> Exception:
    """게이트웨이의 한도 429 다(P38 7절). 본문이 비고 `x-ratelimit-remaining: 0` 이다. langchain 은
    다시 보내도 되는 ModelError 로 감싼다."""
    from langchain_openai.chat_models.base import OpenAIRateLimitError

    response = httpx2.Response(429, request=GATEWAY_REQUEST, headers={"x-ratelimit-remaining": "0"})
    return OpenAIRateLimitError(message="", response=response, body="")


def _gateway_denied() -> Exception:
    response = httpx2.Response(403, request=GATEWAY_REQUEST, headers={"content-type": "text/plain"})
    return openai.PermissionDeniedError(
        message="authorization failed", response=response, body="authorization failed"
    )


def test_gateway_refusals_are_never_transient() -> None:
    """게이트웨이의 한도와 토큰 거절은 다시 보내지 않는다. langchain 이 감싼 429 도 그렇다. 본문이
    있는 provider 의 429 와 연결 오류는 그대로 다시 보낸다."""
    limited = _gateway_limited()
    assert limited.is_retryable is True  # langchain 의 판정을 llm_http 가 먼저 막는다
    assert not is_transient(limited, gateway=True)
    assert not is_transient(_gateway_denied(), gateway=True)
    # 직접 경로 provider 의 빈 429 는 게이트웨이의 표시와 꼴이 같아도 다시 보낸다
    assert is_transient(limited, gateway=False)
    provider = openai.RateLimitError(
        "x",
        response=httpx2.Response(429, request=GATEWAY_REQUEST),
        body={"error": {"type": "rate_limit_exceeded"}},
    )
    assert is_transient(provider, gateway=True)
    assert is_transient(openai.APIConnectionError(request=GATEWAY_REQUEST), gateway=True)


class _RaisingModel(StubChatModel):
    calls: int = 0
    error: Any = None

    async def _agenerate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    ("error", "calls", "code"),
    [
        (_gateway_limited, 1, "llm_gateway_rate_limited"),
        (_gateway_denied, 1, "llm_gateway_denied"),
        (lambda: openai.APIConnectionError(request=GATEWAY_REQUEST), 3, "llm_gateway_unreachable"),
    ],
)
async def test_a_gateway_turn_ends_with_the_gateway_code(
    support_doc, fast_retry, error, calls, code
) -> None:
    """게이트웨이 경로의 턴 오류는 세 코드 가운데 하나다(P38 F5, R4 의 덧붙임). 한도와 거절은 다시
    시도하지 않고, 연결할 수 없는 게이트웨이는 재시도를 다 쓴 뒤다."""
    support_doc["main"]["limits"]["retries"] = 2
    support_doc["main"]["model"]["provider"]["gateway"] = True
    model = _RaisingModel(model_name="stub", error=error())
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, "hi")
        assert sse[0][0] == "error" and sse[0][1]["code"] == code
        assert model.calls == calls


async def test_a_direct_connection_error_stays_turn_failed(support_doc, fast_retry) -> None:
    """직접 경로의 연결 오류는 게이트웨이의 코드가 아니다."""
    support_doc["main"]["limits"]["retries"] = 0
    refused = openai.APIConnectionError(request=GATEWAY_REQUEST)
    model = _RaisingModel(model_name="stub", error=refused)
    async with run_app(support_doc, model=model) as running:
        _, sse = await chat(running, "hi")
        assert sse[0][1]["code"] == "turn_failed"


async def test_a_gateway_fallback_alone_makes_the_agent_a_gateway_agent(support_doc) -> None:
    """경로 표식은 메인 모델만이 아니라 폴백과 서브 모델의 provider 도 본다(P38 6절). 그래야 폴백의
    게이트웨이 거절이 게이트웨이의 코드로 끝나고 다시 보내지 않는다."""
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    fallback["provider"]["gateway"] = True
    support_doc["main"]["fallback"] = fallback
    async with run_app(support_doc) as running:
        assert running.runtime.agent.gateway is True
    support_doc["main"]["fallback"] = None
    async with run_app(support_doc) as running:
        assert running.runtime.agent.gateway is False
