"""관측 이벤트를 문서로 바꾸는 규칙과 넣는 한 곳 (P37 4절·5절·7절): id, 본문 칸, 거절 코드, 원장,
지운 세션의 표."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from daegam.services.telemetry.normalize import (
    SOURCE_LOG,
    SOURCE_PUSH,
    event_document,
    parse_agent_line,
    rejected_code,
)
from daegam.services.telemetry.writer import TelemetryWriter
from daegam.services.usage.ledger import UsageLedger
from daegam.services.usage.pricing import PriceBook
from daegam.store.agent_events import MongoAgentEventStore
from daegam.store.pricing import MongoPricingStore
from daegam.store.usage import MongoUsageStore
from tests.helpers.aio import run_stores

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
INSTANCE = "ai-faq"


def _event(name: str, **fields: Any) -> dict[str, Any]:
    """런타임이 stdout 에 쓰는 이벤트 한 줄의 객체다."""
    return {
        "daegam_event": name,
        "id": uuid.uuid4().hex,
        "ts": "2026-10-20T01:02:03.456+00:00",
        "n": 7,
        "profile": {"id": "ap-1", "name": "faq", "revision": 3},
        "instance": {"id": INSTANCE, "name": "faq"},
        "session_id": "s1",
        "user_sub": "alice",
        "turn_id": "t1",
        "trace_id": TRACE,
        **fields,
    }


def _model_call(span_id: str, *, route: str = "direct", status: str = "ok") -> dict[str, Any]:
    return _event(
        "model_call",
        span_id=span_id,
        parent_span_id="00f067aa0ba902b7",
        start="2026-10-20T01:02:01.000+00:00",
        duration_ms=2456.0,
        operation="chat",
        provider={"name": "openai-main", "kind": "openai"},
        route=route,
        role="main",
        model="gpt-4o",
        status=status,
        usage={"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 200},
        estimated=False,
    )


def _doc(event: dict[str, Any]) -> dict[str, Any]:
    """수집 경로가 하는 것처럼 문서로 바꾸고 토큰이 정한 테넌트와 인스턴스를 찍는다."""
    doc = event_document(event, source=SOURCE_PUSH)
    assert doc is not None
    return {**doc, "tenant": "default", "instance_id": INSTANCE}


def _run(settings, body):
    async def go(events: MongoAgentEventStore, usage: MongoUsageStore, pricing: MongoPricingStore):
        writer = TelemetryWriter(events, UsageLedger(usage, PriceBook(pricing)))
        return await body(writer)

    return run_stores(settings, (MongoAgentEventStore, MongoUsageStore, MongoPricingStore), go)


def test_an_event_becomes_a_document_with_its_id_and_its_body_apart() -> None:
    event = _event(
        "turn_start",
        message="환불 기한",
        attachments=[{"kind": "text", "bytes": 10, "sha256": "ab", "name": "정책.pdf"}],
    )
    doc = event_document(event, source=SOURCE_PUSH)
    assert doc["_id"] == event["id"] and doc["seq"] == 7 and doc["source"] == "push"
    assert doc["trace_id"] == TRACE and doc["turn_id"] == "t1" and doc["session_id"] == "s1"
    assert doc["body"] == {"message": "환불 기한", "attachment_names": ["정책.pdf"]}
    assert doc["data"]["attachments"] == [{"kind": "text", "bytes": 10, "sha256": "ab"}]
    assert "message" not in doc["data"] and "id" not in doc["data"]
    span = event_document(_model_call("b7ad6b7169203331"), source=SOURCE_PUSH)
    assert span["start"].isoformat() == "2026-10-20T01:02:01" and span["duration_ms"] == 2456.0
    # 줄에 id 가 있으면 수집기도 그것을 쓴다. 두 길로 온 같은 이벤트는 한 번만 들어간다
    line = parse_agent_line(
        '{"daegam_event": "turn_end", "id": "' + event["id"] + '", "ts": "2026-10-20T01:02:03Z"}'
    )
    assert line["_id"] == event["id"] and line["source"] == SOURCE_LOG
    # 플랫폼만 쓰는 이벤트는 런타임의 줄로 들어오지 못한다
    assert parse_agent_line('{"daegam_event": "feedback", "id": "x"}') is None


@pytest.mark.parametrize(
    ("event", "fields", "code"),
    [
        ("rate_limited", {}, "rate_limited"),
        ("budget", {"action": "rejected", "scope": "daily"}, "daily_budget_exceeded"),
        ("budget", {"action": "rejected", "scope": "session"}, "session_budget_exceeded"),
        ("budget", {"action": "turn_ended", "scope": "session"}, None),
        ("guardrail", {"position": "input", "action": "block"}, "guardrail_blocked"),
        ("guardrail", {"position": "output", "action": "block"}, None),
        ("rejected", {"code": "max_turns"}, "max_turns"),
        ("turn_end", {}, None),
    ],
)
def test_entrance_rejections_have_one_code(event, fields, code) -> None:
    assert rejected_code(event, fields) == code


def test_model_calls_reach_the_ledger_once_however_often_they_come(settings, mongo) -> None:
    direct = _doc(_model_call("b7ad6b7169203331"))
    gateway = _doc(_model_call("c8be7c827a314442", route="gateway"))
    cut = _doc(_model_call("d9cf8d938b425553", status="cancelled"))
    tool = _doc(_event("tool_exec", span_id="e0d09ea49c536664", tool="Add", status="success"))

    async def body(writer: TelemetryWriter):
        first = await writer.write([direct, gateway, cut, tool])
        again = await writer.write([dict(d) for d in (direct, gateway, cut, tool)])
        return first, again

    first, again = _run(settings, body)
    assert (first, again) == (4, 0)
    assert mongo["agent_events"].count_documents({}) == 4
    (row,) = list(mongo["usage_records"].find())
    assert row["_id"] == f"{TRACE}-b7ad6b7169203331" and row["writer"] == "runtime"
    # 이벤트 문서의 id 는 인스턴스가 앞에 붙는다
    assert row["route"] == "direct" and row["record_id"] == f"{INSTANCE}:{direct['_id']}"
    assert row["source"] == {"kind": "agent", "id": INSTANCE, "name": "faq"}
    assert (row["ref"], row["turn_id"], row["user_sub"]) == ("s1", "t1", "alice")
    assert row["usage"]["cache_read_tokens"] == 200 and row["cost_usd"] > 0
    (join,) = list(mongo["usage_joins"].find())
    assert join["_id"] == f"{TRACE}-c8be7c827a314442" and join["resolved"] is False


def test_a_session_deleted_while_a_batch_goes_in_loses_the_batch_bodies(settings, mongo):
    """표를 읽은 뒤 넣기 전에 다른 레플리카가 세션을 지웠다. 넣은 뒤 표를 다시 보고 본문을
    지운다."""

    async def body(writer: TelemetryWriter):
        add = writer.events.add

        async def add_after_delete(docs):
            await writer.events.tombstone(INSTANCE, "s1")
            return await add(docs)

        writer.events.add = add_after_delete
        await writer.write([_doc(_event("turn_end", text="지워질 답"))])

    _run(settings, body)
    (row,) = list(mongo["agent_events"].find({"session_id": "s1"}))
    assert "body" not in row
