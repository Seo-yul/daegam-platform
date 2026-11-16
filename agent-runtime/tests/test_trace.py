"""실행 트레이서(P37 1·2절): 스팬의 나무, 턴 사용량의 출처, 끊긴 호출 닫기, 타임아웃 신호, 입구의
rejected, 이벤트 기록의 id·본문 가리기·크기 상한."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from runtime.compile import compile_agent
from runtime.config import parse_config
from runtime.events import MAX_EVENT_BYTES, EventLog
from runtime.guards import build_guardrails
from runtime.mcp import ToolSources
from runtime.stream import TurnState, stream_turn
from runtime.stub import FAIL_FIRST_ENV
from runtime.trace import RunTracer
from tests.app_fixtures import (
    TOOLS,
    guardrail,
    mask_rule,
    resume,
    running_sid,
    sid_of,
    sleeping_turn,
    tool_message,
    transcript_items,
    with_subagent,
)
from tests.conftest import chat, events_named, offline_doc, run_app

USAGE = {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}


def _log(doc: dict[str, Any], *, bodies: bool = False) -> tuple[EventLog, io.StringIO]:
    doc["service"]["observability"]["record_bodies"] = bodies
    out = io.StringIO()
    return EventLog(parse_config(doc), out=out), out


def _lines(out: io.StringIO, *names: str) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return [e for e in events if not names or e["daegam_event"] in names]


async def test_a_turn_leaves_one_trace_with_model_and_tool_spans(support_doc) -> None:
    async with run_app(support_doc) as running:
        _, sse = await chat(running, tool_message("Add", a=3, b=4))
        done = events_named(sse, "done")[0]
        (start,) = running.observed("turn_start")
        (end,) = running.observed("turn_end")
        models = running.observed("model_call")
        (call,) = running.observed("tool_exec")
        items = await transcript_items(running, done["session_id"])

    trace = done["trace_id"]
    assert len(trace) == 32 and items[-1]["trace_id"] == trace
    assert start["span_id"] == end["span_id"] and end["duration_ms"] == end["latency_ms"]
    assert "start" in end and end["status"] == "done"
    for span in (*models, call):
        assert span["trace_id"] == trace and span["parent_span_id"] == end["span_id"]
        assert span["turn_id"] == done["turn_id"] and span["session_id"] == done["session_id"]
    assert [m["status"] for m in models] == ["ok", "ok"]
    first = models[0]
    assert first["tool_calls"] == [{"name": "Add", "id": call["call_id"]}]
    assert first["provider"]["kind"] == "stub" and first["model"] == "stub"
    assert (first["operation"], first["route"], first["role"], first["purpose"]) == (
        "chat",
        "direct",
        "main",
        "agent",
    )
    assert first["attempt"] == 1 and first["fallback"] is False and first["estimated"] is False
    assert call["tool"] == "Add" and call["status"] == "success"
    assert call["source"] == support_doc["tools"][0]["source"]
    # 턴 사용량은 이 실행의 모델 호출의 합이다
    for key in ("input_tokens", "output_tokens"):
        assert done["usage"][key] == sum(m["usage"][key] for m in models) == end["usage"][key]
    span_ids = [s["span_id"] for s in (end, *models, call)]
    assert len(set(span_ids)) == len(span_ids)
    events = running.events()
    assert len({e["id"] for e in events}) == len(events)


async def test_subagent_spans_hang_under_the_task_span(support_doc, fake_mcp) -> None:
    with_subagent(support_doc)
    task = tool_message("task", description=tool_message("Add", a=1, b=2), subagent_type="calc")
    async with run_app(support_doc) as running:
        _, sse = await chat(running, task)
        done = events_named(sse, "done")[0]
        models = running.observed("model_call")
        calls = {e["tool"]: e for e in running.observed("tool_exec")}

    delegate, add = calls["task"], calls["Add"]
    assert delegate["subagent"] == "calc" and delegate["source"] == {"kind": "subagent"}
    assert add["parent_span_id"] == delegate["span_id"] and add["agent"] == "calc"
    inner = [m for m in models if m.get("agent") == "calc"]
    assert len(inner) == 2 and len(models) == 4
    assert all(m["parent_span_id"] == delegate["span_id"] for m in inner)
    assert {m["role"] for m in inner} == {"subagent"}
    assert done["usage"]["input_tokens"] == sum(m["usage"]["input_tokens"] for m in models)


async def test_a_retried_model_call_is_a_second_attempt(support_doc, monkeypatch) -> None:
    monkeypatch.setenv(FAIL_FIRST_ENV, "1")
    monkeypatch.setenv("DAEGAM_RETRY_INITIAL_DELAY_SECONDS", "0.01")
    support_doc["main"]["limits"]["retries"] = 1
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        await chat(running, "hi")
        models = running.observed("model_call")
        (retry,) = running.observed("model_retry")
    assert [(m["status"], m["attempt"]) for m in models] == [("error", 1), ("ok", 2)]
    assert (
        models[0]["error_type"] == "StubTransientError" and models[0]["usage"]["input_tokens"] == 0
    )
    assert retry["trace_id"] == models[0]["trace_id"] and retry["parent_span_id"]


async def test_a_cancelled_turn_closes_its_open_model_call(support_doc) -> None:
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        task = await sleeping_turn(running, 5)
        sid = running_sid(running)
        await running.client.post(f"/sessions/{sid}/cancel", json={"user_sub": "alice"})
        response = await task
        (end,) = running.observed("turn_end")
        (model,) = running.observed("model_call")
    error = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    assert error[-1]["trace_id"] == end["trace_id"]
    assert end["status"] == "cancelled" and end["span_id"] and end["duration_ms"] >= 0
    assert model["status"] == "cancelled" and model["error_type"] == "turn_cancelled"
    assert model["usage"]["input_tokens"] == 0 and model["parent_span_id"] == end["span_id"]


async def test_a_resumed_turn_is_a_second_trace_and_the_decision_belongs_to_it(
    support_doc,
) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        _, sse = await chat(running, tool_message("Add", a=1, b=2))
        sid = sid_of(sse, "interrupt")
        _, resumed = await resume(running, sid, [{"type": "approve"}])
        done = events_named(resumed, "done")[0]
        starts = running.observed("turn_start")
        (decision,) = running.observed("decision")
        (interrupt,) = running.observed("interrupt")
    assert starts[0]["trace_id"] != starts[1]["trace_id"] == done["trace_id"]
    assert interrupt["trace_id"] == starts[0]["trace_id"]
    assert decision["trace_id"] == done["trace_id"] and decision["turn_id"] == done["turn_id"]
    assert decision["parent_span_id"] == starts[1]["span_id"]


async def test_entrance_limits_leave_a_rejected_event(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["entry"]["max_turns"] = 1
    support_doc["entry"]["max_concurrent_sessions"] = 1
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        over_turns, _ = await chat(running, "again", session_id=sid)
        over_sessions, _ = await chat(running, "new")
        rejected = running.observed("rejected")
    assert (over_turns.status_code, over_sessions.status_code) == (429, 429)
    assert [(e["code"], e.get("session_id")) for e in rejected] == [
        ("max_turns", sid),
        ("max_concurrent_sessions", None),
    ]
    assert {e["user_sub"] for e in rejected} == {"alice"}


async def test_bodies_pass_every_pii_mask_rule_whatever_its_position(support_doc) -> None:
    """도구 위치의 가리기 규칙도 관측 이벤트의 본문에 걸린다. 사용자의 말(turn_start.message)은
    원래 도구 규칙의 대상이 아니다."""
    support_doc["service"]["observability"]["record_bodies"] = True
    support_doc["guardrails"] = [guardrail("tool", [mask_rule(r"\d{3}")], "flag")]
    async with run_app(support_doc) as running:
        await chat(running, tool_message("Add", a=100, b=23))
        (start,) = running.observed("turn_start")
        (call,) = running.observed("tool_exec")
    assert "100" not in start["message"] and "a" in start["message"]
    assert "100" not in call["args"] and "123" not in call["content"]


async def test_subagent_timeout_closes_the_task_span_and_what_ran_under_it() -> None:
    doc = offline_doc()
    with_subagent(doc)
    doc["subagents"][0]["limits"]["turn_timeout_seconds"] = 1
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    log, out = _log(doc)
    state = TurnState()
    state.trace.begin(log, where={"session_id": "s1"}, sources=agent.tool_sources)
    task = tool_message("task", description='{"sleep": 5}', subagent_type="calc")
    async for _ in stream_turn(
        agent, session_id="s1", turn_input=task, state=state, owner_sub="alice"
    ):
        pass
    state.trace.close(None)
    (delegate,) = _lines(out, "tool_exec")
    inner = [m for m in _lines(out, "model_call") if m.get("agent") == "calc"]
    assert delegate["status"] == "error" and delegate["error_type"] == "timeout"
    assert [(m["status"], m["error_type"]) for m in inner] == [("cancelled", "timeout")]
    assert inner[0]["parent_span_id"] == delegate["span_id"]


async def test_tool_timeout_is_marked_on_the_span(support_doc) -> None:
    sources = ToolSources(parse_config(support_doc), default_timeout=0.0)
    await sources.load()
    add = sources.tools_by_ref["tools-calculator"][0]
    log, out = _log(support_doc)
    tracer = RunTracer()
    tracer.begin(log, where={}, sources={})
    call = {"type": "tool_call", "id": "c1", "name": "Add", "args": {"a": 1, "b": 2}}
    message = await add.ainvoke(call, config={"callbacks": [tracer]})
    (span,) = _lines(out, "tool_exec")
    assert message.status == "error"
    assert (span["status"], span["error_type"], span["call_id"]) == ("error", "timeout", "c1")


def _chunk(usage: dict[str, int] | None) -> ChatGenerationChunk:
    return ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage))


async def test_close_keeps_a_finished_stream_and_drops_late_end_hooks() -> None:
    """사용량 조각까지 받고 끊긴 호출은 그 사용량으로 ok 이고 턴 사용량에 든다. 조각 없이 끊긴
    호출은 토큰 없이 cancelled 다. close 뒤의 끝 훅은 버려 스팬이 두 번 나가지 않는다."""
    log, out = _log(offline_doc())
    tracer = RunTracer()
    tracer.begin(log, where={}, sources={})
    finished, cut = uuid4(), uuid4()
    for run in (finished, cut):
        await tracer.on_chat_model_start({}, [], run_id=run, metadata={"ls_provider": "openai"})
    await tracer.on_llm_new_token("안녕", chunk=_chunk(None), run_id=finished)
    await tracer.on_llm_new_token("", chunk=_chunk(USAGE), run_id=finished)
    # 취소는 끝 훅이 아니다. 턴의 코드로 닫는 것은 close 다
    await tracer.on_llm_error(asyncio.CancelledError(), run_id=cut)
    tracer.close("turn_cancelled")
    late = LLMResult(
        generations=[[ChatGeneration(message=AIMessage("늦음", usage_metadata=USAGE))]]
    )
    await tracer.on_llm_end(late, run_id=finished)
    spans = {s["status"]: s for s in _lines(out, "model_call")}
    assert len(_lines(out, "model_call")) == 2
    assert spans["ok"]["usage"]["input_tokens"] == 30 and spans["ok"]["ttft_ms"] is not None
    assert spans["cancelled"]["error_type"] == "turn_cancelled"
    assert spans["cancelled"]["usage"]["output_tokens"] == 0
    assert tracer.usage.input_tokens == 30 and tracer.usage.output_tokens == 5


async def test_close_closes_a_cancelled_tool_and_a_failed_tool_stays_an_error() -> None:
    """langchain-core 1.7 부터 취소된 도구도 오류 훅(CancelledError)을 부른다. 트레이서는 그것을
    버리므로 턴 마감의 close 가 턴의 코드로 닫는다. 보통 예외로 실패한 도구는 그대로 error 다."""
    log, out = _log(offline_doc())
    tracer = RunTracer()
    tracer.begin(log, where={}, sources={})
    started = asyncio.Event()

    @tool
    async def wait_forever() -> str:
        """끝나지 않는 도구다."""
        started.set()
        await asyncio.sleep(60)
        return "never"

    @tool
    async def broken() -> str:
        """실패하는 도구다."""
        raise ValueError("boom")

    running = asyncio.create_task(wait_forever.ainvoke({}, config={"callbacks": [tracer]}))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with pytest.raises(ValueError):
        await broken.ainvoke({}, config={"callbacks": [tracer]})
    tracer.close("turn_cancelled")
    spans = {s["tool"]: s for s in _lines(out, "tool_exec")}
    assert len(_lines(out, "tool_exec")) == 2
    assert (spans["wait_forever"]["status"], spans["wait_forever"]["error_type"]) == (
        "cancelled",
        "turn_cancelled",
    )
    assert (spans["broken"]["status"], spans["broken"]["error_type"]) == ("error", "ValueError")


def test_event_log_ids_masks_and_cuts_oversized_events() -> None:
    doc = offline_doc()
    doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    log, out = _log(doc, bodies=True)
    log.masks = build_guardrails(parse_config(doc)).masks
    log.emit("turn_start", message=log.body("번호 123"))
    big = log.body("가" * 9_000)
    log.emit("turn_end", text=big, structured_response=big, message=big)
    first, second = _lines(out)
    assert first["id"] != second["id"] and first["message"] == "번호 ****"
    raw = out.getvalue().splitlines()[1]
    assert second["truncated"] is True and "text" not in second
    assert len(raw.encode()) <= MAX_EVENT_BYTES


async def test_a_call_without_usage_is_estimated_and_counted() -> None:
    """스트림 사용량을 끈 호환 서버의 호출은 글자 수로 어림해 `estimated` 를 달고 턴 사용량에
    센다."""
    log, out = _log(offline_doc())
    tracer = RunTracer()
    tracer.begin(log, where={}, sources={})
    run = uuid4()
    prompt = [[HumanMessage("12345678")]]
    await tracer.on_chat_model_start({}, prompt, run_id=run, metadata={"ls_provider": "openai"})
    done = LLMResult(generations=[[ChatGeneration(message=AIMessage("abcd"))]])
    await tracer.on_llm_end(done, run_id=run)
    (span,) = _lines(out, "model_call")
    assert span["estimated"] is True and span["status"] == "ok"
    assert (span["usage"]["input_tokens"], span["usage"]["output_tokens"]) == (2, 1)
    assert (tracer.usage.input_tokens, tracer.usage.output_tokens) == (2, 1)
