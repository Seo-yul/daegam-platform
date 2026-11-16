"""입구 한도·일당 예산·관측(F2d): 분당 429 + Retry-After, 일당 예산 429, 이벤트 스키마, metrics."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from runtime import routes, turn, turn_gate
from runtime.stream import describe_error, error_detail
from runtime.stub import FAIL_FIRST_ENV
from tests.app_fixtures import deny_rule, guardrail, resume, sid_of
from tests.conftest import chat, events_named, run_app
from tests.store_fixtures import pod_store

NOON = datetime(2026, 9, 6, 12, 34, 56, tzinfo=UTC)
LATE = datetime(2026, 9, 6, 23, 59, 30, tzinfo=UTC)


@pytest.fixture
def frozen_clock(monkeypatch):
    """입구 검사(turn_gate), 턴 마감(turn), 지표(routes)가 보는 utcnow 를 고정해 창 경계를 넘지
    않게 한다. 마감이 더한 토큰을 입구와 지표가 같은 날의 창에서 읽어야 한다."""

    def freeze(at: datetime) -> None:
        for module in (turn_gate, routes, turn):
            monkeypatch.setattr(module, "utcnow", lambda: at)

    return freeze


async def test_per_user_per_minute_returns_429_with_retry_after(support_doc, frozen_clock) -> None:
    frozen_clock(NOON)
    support_doc["entry"]["rate_limit"] = {"per_user_per_minute": 2}
    async with run_app(support_doc) as running:
        for _ in range(2):
            response, sse = await chat(running, "hi")
            assert response.status_code == 200 and events_named(sse, "done")
        response, _ = await chat(running, "hi")
        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"
        assert response.headers["Retry-After"] == "4"
        # 다른 사용자는 자기 창을 쓴다
        response, _ = await chat(running, "hi", user_sub="bob")
        assert response.status_code == 200
        # 거절된 요청도 창 안에 센다
        response, _ = await chat(running, "hi")
        assert response.status_code == 429

        events = running.observed("rate_limited")
        assert len(events) == 2
        assert events[0]["user_sub"] == "alice" and events[0]["limit"] == 2
        assert events[0]["count"] == 3 and events[0]["retry_after"] == 4
        assert events[0]["window"] == "20260906T1234"
        assert running.rejections() == ["rate_limited", "rate_limited"]
        metrics = (await running.client.get("/metrics")).json()
        # 세션은 3개(alice 2, bob 1)이고 거절은 세션을 만들지 않았다
        assert metrics["totals"]["sessions"] == 3


async def test_rate_limit_is_checked_before_other_entry_rules(support_doc, frozen_clock) -> None:
    frozen_clock(NOON)
    support_doc["entry"]["rate_limit"] = {"per_user_per_minute": 1}
    support_doc["entry"]["max_input_chars"] = 3
    async with run_app(support_doc) as running:
        response, _ = await chat(running, "123456")
        assert response.status_code == 413
        response, _ = await chat(running, "123456")
        assert response.status_code == 429 and response.json()["code"] == "rate_limited"


async def test_no_rate_limit_when_null(support_doc) -> None:
    support_doc["entry"]["rate_limit"] = None
    async with run_app(support_doc) as running:
        for _ in range(5):
            response, _ = await chat(running, "hi")
            assert response.status_code == 200
        assert running.rejections() == []


async def test_daily_budget_returns_429_for_everyone(support_doc, frozen_clock) -> None:
    frozen_clock(LATE)
    support_doc["entry"]["rate_limit"] = None
    support_doc["service"]["budget"]["daily_tokens"] = 1
    async with run_app(support_doc) as running:
        metrics = (await running.client.get("/metrics")).json()
        assert metrics["daily_tokens"] == {
            "used": 0,
            "limit": 1,
            "window": "20260906",
        }
        # 첫 턴은 아직 쓴 것이 없어 지나간다
        response, sse = await chat(running, "hi")
        assert response.status_code == 200
        done = events_named(sse, "done")[0]
        spent = done["usage"]["input_tokens"] + done["usage"]["output_tokens"]
        assert spent > 1

        # 그다음은 누구든 자정까지 429 다
        for user in ("alice", "bob"):
            response, _ = await chat(running, "hi", user_sub=user)
            assert response.status_code == 429, user
            assert response.json()["code"] == "daily_budget_exceeded"
            assert response.headers["Retry-After"] == "30"
        # 기존 세션의 다음 턴도 막힌다
        response, _ = await chat(running, "hi", session_id=done["session_id"])
        assert response.status_code == 429

        budget = running.observed("budget")
        assert len(budget) == 3
        assert budget[0]["scope"] == "daily" and budget[0]["action"] == "rejected"
        assert budget[0]["used"] == spent and budget[0]["limit"] == 1
        assert budget[0]["retry_after"] == 30 and budget[0]["user_sub"] == "alice"
        assert budget[1]["user_sub"] == "bob"
        metrics = (await running.client.get("/metrics")).json()
        assert metrics["daily_tokens"]["used"] == spent
        assert running.rejections() == ["daily_budget_exceeded"] * 3


async def test_daily_budget_charges_failed_turns_too(
    support_doc, frozen_clock, monkeypatch
) -> None:
    frozen_clock(LATE)
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    support_doc["entry"]["rate_limit"] = None
    support_doc["main"]["limits"]["retries"] = 0
    support_doc["service"]["budget"]["daily_tokens"] = 1
    support_doc["service"]["budget"]["session_tokens"] = 1
    async with run_app(support_doc) as running:
        # 첫 턴: 모델 호출이 실패해 usage 가 없다. 아무것도 더하지 않는다
        _, sse = await chat(running, "hi")
        assert sse[0][0] == "error" and sse[0][1]["code"] == "turn_failed"
        assert (await running.client.get("/metrics")).json()["daily_tokens"]["used"] == 0
        # 둘째 턴: 세션 예산으로 끊기지만 쓴 토큰은 일당 카운터에 들어간다
        _, sse = await chat(running, "hi")
        assert sse[-1][0] == "error" and sse[-1][1]["code"] == "budget_exceeded"
        assert (await running.client.get("/metrics")).json()["daily_tokens"]["used"] > 0
        response, _ = await chat(running, "hi")
        assert response.status_code == 429
        assert response.json()["code"] == "daily_budget_exceeded"


async def test_daily_budget_is_shared_across_apps(support_doc, frozen_clock) -> None:
    frozen_clock(LATE)
    support_doc["entry"]["rate_limit"] = None
    support_doc["service"]["budget"]["daily_tokens"] = 1
    async with (
        pod_store(support_doc) as store,
        run_app(support_doc, store=store) as pod_a,
        run_app(support_doc, store=store) as pod_b,
    ):
        response, _ = await chat(pod_a, "hi")
        assert response.status_code == 200
        response, _ = await chat(pod_b, "hi")
        assert response.status_code == 429
        assert response.json()["code"] == "daily_budget_exceeded"


async def test_metrics_carry_only_shared_values(support_doc, monkeypatch) -> None:
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    support_doc["main"]["limits"]["retries"] = 0
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        assert sse[0][0] == "error"
        _, sse = await chat(running, "hi", user_sub="bob")
        assert sse[-1][0] == "interrupt"
        metrics = (await running.client.get("/metrics")).json()
        # 모든 레플리카에 같은 값만 싣는다. 턴·지연·Tool·거절의 수는 플랫폼이 이벤트로 센다
        assert set(metrics) == {
            "sessions",
            "totals",
            "daily_tokens",
            "tools",
            "warnings",
            "runtime",
            "chat",
            "telemetry",
        }
        assert metrics["sessions"] == {"active": 1, "pending": 1, "closed": 0}
        assert metrics["totals"]["turns"] == 1 and metrics["totals"]["input_tokens"] > 0
        assert running.ended("pending") == 1 and len(running.observed("turn_end")) == 2
        assert metrics["daily_tokens"] is None
        assert metrics["tools"]["protocols"] == {"tools-calculator": "2026-07-28"}


BASE_KEYS = {"daegam_event", "id", "ts", "n", "profile", "instance"}
# 실행 안의 점 이벤트가 드는 트레이스 칸이다(P37)
IN_RUN = {"turn_id", "trace_id", "parent_span_id"}
SPAN = {"trace_id", "span_id", "parent_span_id", "start", "duration_ms", "status"}
REQUIRED_KEYS: dict[str, set[str]] = {
    "runtime_start": {
        "warnings",
        "tools",
        "subagents",
        "harness_key",
        "shared_state",
        "memory_kind",
    },
    "turn_start": {"session_id", "user_sub", "turn", "turn_id", "trace_id", "span_id"},
    "turn_end": {
        "session_id",
        "user_sub",
        "turn",
        "turn_id",
        "trace_id",
        "span_id",
        "start",
        "duration_ms",
        "status",
        "latency_ms",
        "usage",
        "tool_calls",
    },
    "model_call": {
        *SPAN,
        "session_id",
        "turn_id",
        "operation",
        "provider",
        "route",
        "role",
        "purpose",
        "attempt",
        "fallback",
        "usage",
        "estimated",
    },
    "tool_exec": {*SPAN, "session_id", "turn_id", "tool", "call_id", "source"},
    "interrupt": {"session_id", "user_sub", "turn", "tools", *IN_RUN},
    "decision": {
        "session_id",
        "user_sub",
        "owner_sub",
        "approver",
        "turn",
        "decisions",
        "tools",
        *IN_RUN,
    },
    "guardrail": {"user_sub", "position", "rule", "action"},
    "budget": {"user_sub", "scope", "action", "used", "limit"},
    "rate_limited": {"user_sub", "limit", "count", "retry_after", "window"},
    "model_fallback": {"session_id", "user_sub", "turn", "from", "to", *IN_RUN},
    "model_retry": {"session_id", "user_sub", "turn", "attempt", "model", *IN_RUN},
    "rejected": {"user_sub", "code"},
}
BODY_KEYS = {"message", "args", "content", "text"}


def _check_schema(events: list[dict[str, Any]], *, bodies: bool) -> set[str]:
    seen: set[str] = set()
    for event in events:
        kind = event["daegam_event"]
        seen.add(kind)
        assert BASE_KEYS <= set(event), event
        assert set(event["profile"]) == {"id", "name", "revision"}
        assert set(event["instance"]) == {"id", "name"}
        assert REQUIRED_KEYS[kind] <= set(event), (kind, sorted(event))
        if not bodies:
            assert not (BODY_KEYS & set(event)), (kind, sorted(event))
    return seen


async def test_stdout_event_schema_covers_every_kind(
    support_doc, frozen_clock, monkeypatch
) -> None:
    frozen_clock(LATE)
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    monkeypatch.setenv("DAEGAM_RETRY_INITIAL_DELAY_SECONDS", "0.01")
    # /chat 세 번 중 셋째가 분당 한도(2)를 넘는다. /decisions 는 요청 수에 들지 않는다
    support_doc["entry"]["rate_limit"] = {"per_user_per_minute": 2}
    support_doc["service"]["budget"]["daily_tokens"] = 1
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    support_doc["guardrails"] = [guardrail("input", [deny_rule("더해")], "flag")]
    support_doc["main"]["limits"]["retries"] = 1
    async with run_app(support_doc) as running:
        # 턴 1: 가드레일 flag, 재시도 뒤 성공, 승인 대기
        _, sse = await chat(running, '더해 {"tool": "Add", "args": {"a": 1, "b": 2}}')
        sid = sid_of(sse, "interrupt")
        response, resumed = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200
        assert resumed[-1][0] == "done"
        # 일당 예산 초과 → budget, 분당 한도 초과 → rate_limited
        response, _ = await chat(running, "hi")
        assert response.status_code == 429 and response.json()["code"] == "daily_budget_exceeded"
        response, _ = await chat(running, "hi")
        assert response.status_code == 429 and response.json()["code"] == "rate_limited"
        seen = _check_schema(running.events(), bodies=False)

    # 폴백은 따로 낸다
    fallback = copy.deepcopy(support_doc["main"]["model"])
    fallback["name"] = "stub-fallback"
    support_doc["main"]["fallback"] = fallback
    support_doc["main"]["limits"]["retries"] = 0
    support_doc["main"]["tools"] = []
    support_doc["service"]["budget"]["daily_tokens"] = None
    support_doc["service"]["observability"]["record_bodies"] = True
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        assert events_named(sse, "model_fallback") and events_named(sse, "done")
        # 입구의 길이 거절은 rejected 를 남긴다(P37)
        limit = support_doc["entry"]["max_input_chars"]
        response, _ = await chat(running, "x" * (limit + 1))
        assert response.status_code == 413
        assert running.observed("rejected")[-1]["code"] == "max_input_chars"
        seen |= _check_schema(running.events(), bodies=True)
        end = running.observed("turn_end")[0]
        assert end["text"] == "Tool 이 없어 답만 합니다: hi"

    assert seen == set(REQUIRED_KEYS)


def test_describe_error_unwraps_exception_groups_without_messages() -> None:
    """MCP 세션의 anyio TaskGroup 이 낸 ExceptionGroup 은 잎 예외의 종류까지 펼쳐 원인을 보인다.
    문장은 싣지 않는다(D39). provider 의 인증 오류 문장에는 키 조각이 들어 있을 수 있다."""
    leaf = TimeoutError("proxy did not answer")
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [leaf])])
    assert describe_error(grouped) == "ExceptionGroup: ExceptionGroup: TimeoutError"
    assert describe_error(ValueError("plain")) == "ValueError"
    leaked = RuntimeError("Incorrect API key provided: sk-proj-abcd****wxyz")
    assert "sk-" not in describe_error(leaked) and describe_error(leaked) == "RuntimeError"
    # 로그용 꼴만 문장을 잇는다
    assert error_detail(grouped) == (
        "ExceptionGroup: ExceptionGroup: TimeoutError: proxy did not answer"
    )
