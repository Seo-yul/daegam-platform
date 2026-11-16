"""런타임 이벤트를 수집 경로로 넣는 테스트 도우미 (P37). 한 세션 s1 의 턴 t1 실행 TRACE 가 기본이고
시각은 지금에서 1분 전부터 at 초 뒤다."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daegam.urls import TELEMETRY_EVENTS_PATH

EVENTS = TELEMETRY_EVENTS_PATH
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACE2 = "0af7651916cd43dd8448eb211c80319c"
RUN = "b7ad6b7169203331"
NOW = datetime.now(UTC)


def at_ts(seconds: float) -> str:
    return (NOW - timedelta(seconds=60) + timedelta(seconds=seconds)).isoformat()


def run_event(name: str, at: float, **fields: Any) -> dict[str, Any]:
    return {
        "daegam_event": name,
        "id": uuid.uuid4().hex,
        "ts": at_ts(at),
        "session_id": "s1",
        "user_sub": "alice",
        "turn": 1,
        "turn_id": "t1",
        "trace_id": TRACE,
        **fields,
    }


def turn_end(at: float, status: str, ms: float, **fields: Any) -> dict[str, Any]:
    return run_event(
        "turn_end",
        at,
        span_id=RUN,
        start=at_ts(at - ms / 1000),
        duration_ms=ms,
        status=status,
        usage={"input_tokens": 100, "output_tokens": 10},
        **fields,
    )


def model_call(at: float, span: str, status: str = "ok", **fields: Any) -> dict[str, Any]:
    return run_event(
        "model_call",
        at,
        span_id=span,
        parent_span_id=RUN,
        start=at_ts(at - 0.2),
        duration_ms=200.0,
        operation="chat",
        provider={"name": "p", "kind": "openai"},
        route="direct",
        model="gpt-4o",
        status=status,
        usage={"input_tokens": 1000, "output_tokens": 500} if status == "ok" else {},
        **fields,
    )


def tool_exec(at: float, span: str, status: str = "success", **fields: Any) -> dict[str, Any]:
    return run_event(
        "tool_exec",
        at,
        span_id=span,
        parent_span_id=RUN,
        start=at_ts(at - 0.05),
        duration_ms=50.0,
        tool="Add",
        source={"kind": "mcp_instance", "id": "ms-1"},
        status=status,
        **fields,
    )


def envelope(revision: int, events: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """런타임 shipper 가 보내는 봉투 하나. 파드 agent-faq-x(uid-1)의 부트 b1 이 다른 이벤트 셋을
    버렸다고 알린다. extra 가 칸을 바꾸거나 더한다."""
    return {
        "schema_version": 1,
        "revision": revision,
        "pod": {"name": "agent-faq-x", "uid": "uid-1"},
        "boot_id": "b1",
        "sent_at": NOW.isoformat(),
        "dropped": {"cost": 0, "other": 3},
        "counters": {"correlation_reuse": 0},
        "events": events,
        **extra,
    }


def push_events(client, agent, events: list[dict[str, Any]]) -> None:
    """Agent 의 인스턴스 토큰으로 한 봉투를 보낸다. 거절이 없어야 한다."""
    r = client.post(
        EVENTS, json=envelope(agent.doc["deployed_revision"], events), headers=agent.auth
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
