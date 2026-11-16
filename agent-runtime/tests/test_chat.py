"""/chat 한 턴(스텁 + 가짜 MCP), 세션 색인, 세션 상세, 메트릭, stdout 관측 이벤트, 이어지는 턴의
씨앗(이력의 사용량·도구 이벤트를 다시 내지 않는다)을 본다. 끊긴 턴의 마감은 test_disconnect.py 가,
진행 중 턴을 멈추는 API 는 test_cancel.py 가 본다(D65)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from runtime import boot
from runtime.config import ConfigError, parse_config
from runtime.sessions import open_session_store
from runtime.stream import thread_config
from runtime.turn import SESSION_HEADER
from tests.app_fixtures import (
    STRUCTURED_SCHEMA,
    STRUCTURED_VALUE,
    JsonModel,
    deny_rule,
    guardrail,
    mask_rule,
    resume,
    sid_of,
)
from tests.conftest import RunningApp, chat, event_names, events_named, run_app

DANGLING_CALL_ID = "call_dangling"
DANGLING_USAGE = {"input_tokens": 1000, "output_tokens": 1000, "total_tokens": 2000}
GREETING = "안녕하세요. 무엇을 도와드릴까요?"
"""support-agent.json 입구의 인사말이다."""


async def _plant_dangling_call(running: RunningApp, sid: str) -> None:
    """끊긴 턴이 남기는 꼴이다. 답 없는 도구 호출 하나를 체크포인트 끝에 둔다. 사용량은 눈에 띄게
    크게 잡아 다시 세면 드러나게 한다."""
    orphan = AIMessage(
        content="",
        id="lc_run--dangling",
        tool_calls=[{"name": "Add", "args": {"a": 1, "b": 1}, "id": DANGLING_CALL_ID}],
        usage_metadata=DANGLING_USAGE,
    )
    graph = running.runtime.agent.graph
    await graph.aupdate_state(thread_config(sid), {"messages": [orphan]})


async def test_chat_turn_calls_tool_and_finishes(support_doc, fake_mcp) -> None:
    async with run_app(support_doc) as running:
        health = (await running.client.get("/healthz")).json()
        assert health["status"] == "ok"
        assert health["tools"] == ["Add"]
        assert health["tools_protocols"] == {"tools-calculator": "2026-07-28"}
        assert "no_shared_state" in health["warnings"]

        response, sse = await chat(running, '더해 줘 {"tool": "Add", "args": {"a": 3, "b": 4}}')
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        names = event_names(sse)
        assert names.index("tool_call") < names.index("tool_result") < names.index("done")

        call = events_named(sse, "tool_call")[0]
        assert call["name"] == "Add" and call["args"] == {"a": 3, "b": 4}
        result = events_named(sse, "tool_result")[0]
        assert result["name"] == "Add" and result["content"] == "7" and result["id"] == call["id"]
        assert fake_mcp.calls["Add"] == 1

        tokens = events_named(sse, "token")
        assert tokens and "".join(t["text"] for t in tokens) == "결과: 7"

        done = events_named(sse, "done")[0]
        assert done["text"] == "결과: 7" and done["turn"] == 1 and done["tool_calls"] == 1
        assert done["usage"]["input_tokens"] > 0 and done["usage"]["output_tokens"] > 0
        sid = done["session_id"]
        # 첫 턴은 done 전에는 세션 id 를 모르므로 응답 헤더가 알린다
        assert response.headers[SESSION_HEADER] == sid

        # 색인
        listed = (await running.client.get("/sessions", params={"user_sub": "alice"})).json()
        assert [s["session_id"] for s in listed["sessions"]] == [sid]
        row = listed["sessions"][0]
        assert row["turns"] == 1 and row["status"] == "active" and row["user_sub"] == "alice"
        assert row["usage"]["input_tokens"] == done["usage"]["input_tokens"]
        assert (await running.client.get("/sessions", params={"user_sub": "bob"})).json() == {
            "sessions": []
        }
        assert (await running.client.get("/sessions", params={"status": "pending"})).json() == {
            "sessions": []
        }

        # 상세: 메시지 요약
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["session"]["session_id"] == sid and detail["pending"] is False
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["human", "ai", "tool", "ai"]
        assert detail["messages"][1]["tool_calls"] == ["Add"]
        assert detail["messages"][2]["tool"] == "Add"

        # 두 번째 턴은 같은 세션이다
        response, sse = await chat(running, "다시", session_id=sid)
        assert response.status_code == 200 and response.headers[SESSION_HEADER] == sid
        assert events_named(sse, "done")[0]["turn"] == 2
        assert (await running.client.get(f"/sessions/{sid}")).json()["session"]["turns"] == 2

        metrics = (await running.client.get("/metrics")).json()
        assert metrics["sessions"]["active"] == 1
        assert metrics["totals"]["turns"] == 2
        # 턴·지연·Tool 의 수는 파드가 세지 않는다. 플랫폼이 이벤트로 센다(P37 11절)
        assert "pod" not in metrics and "turns" not in metrics
        assert running.ended("done") == 2 and len(running.observed("tool_exec")) == 2
        assert all(e["duration_ms"] > 0 for e in running.observed("turn_end"))

        # stdout 이벤트: 본문은 record_bodies 가 꺼져 있어 없다
        kinds = [e["daegam_event"] for e in running.events()]
        assert kinds[0] == "runtime_start"
        assert kinds.count("turn_start") == 2 and kinds.count("turn_end") == 2
        assert "model_call" in kinds and "tool_exec" in kinds
        assert "tool_call" not in kinds and "tool_result" not in kinds
        start = running.observed("turn_start")[0]
        assert start["session_id"] == sid and start["user_sub"] == "alice"
        assert (
            start["profile"]["name"] == "support" and start["instance"]["id"] == "ai-support-0001"
        )
        assert "message" not in start
        call_event = running.observed("tool_exec")[0]
        assert (
            call_event["tool"] == "Add" and "args" not in call_event and "content" not in call_event
        )
        end = running.observed("turn_end")[0]
        assert end["status"] == "done" and end["usage"]["total_tokens"] > 0 and "text" not in end


async def test_next_turn_does_not_replay_history_after_dangling_call(support_doc, fake_mcp) -> None:
    """답 없는 호출이 남은 세션의 다음 턴이다. deepagents 가 그 호출을 메우며 이력 전부를 되돌리지만
    새 턴은 제 도구 이벤트만 내고 사용량도 제 몫뿐이다."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 3, "b": 4}}')
        first_call = events_named(sse, "tool_call")[0]["id"]
        sid = sid_of(sse, "done")
        before = (await running.client.get(f"/sessions/{sid}")).json()["session"]["usage"]
        await _plant_dangling_call(running, sid)

        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}', session_id=sid)
        names = event_names(sse)
        assert names == ["tool_call", "tool_result", "token", "done"]
        call = events_named(sse, "tool_call")[0]
        assert call["id"] not in (first_call, DANGLING_CALL_ID) and call["args"] == {"a": 1, "b": 2}
        assert events_named(sse, "tool_result")[0]["id"] == call["id"]
        assert fake_mcp.calls["Add"] == 2
        done = events_named(sse, "done")[0]
        assert done["text"] == "결과: 3" and done["tool_calls"] == 1
        assert 0 < done["usage"]["output_tokens"] < DANGLING_USAGE["output_tokens"]
        after = (await running.client.get(f"/sessions/{sid}")).json()
        assert after["session"]["usage"]["input_tokens"] == (
            before["input_tokens"] + done["usage"]["input_tokens"]
        )
        # 메운 결과는 체크포인트에는 있다(deepagents 가 채우는 오류 결과다). 화면과 전사로 나가지
        # 않았을 뿐이다
        roles = [m["role"] for m in after["messages"]]
        assert roles[4:6] == ["ai", "tool"] and after["messages"][5]["tool"] == "Add"
        patched = after["messages"][5]
        assert patched["status"] == "error" and "did not complete" in patched["text"]
        cards = [i for i in after["transcript"]["items"] if i["kind"] == "tool_call"]
        assert DANGLING_CALL_ID not in [c["call"]["id"] for c in cards]
        # 관측 이벤트도 이번 턴의 호출 하나뿐이다
        calls = [e for e in running.observed("tool_exec") if e["turn"] == 2]
        assert [e["call_id"] for e in calls] == [call["id"]]


async def test_output_pii_mask_does_not_replay_tool_events(support_doc) -> None:
    """출력 PII 가리기는 가린 뒤 목록 전체를 되돌린다. 같은 턴의 tool_result 는 한 번만 나가고,
    다음 턴은 이력의 도구 이벤트를 내지 않으며 사용량도 제 몫뿐이다."""
    support_doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 100, "b": 23}}')
        names = event_names(sse)
        assert names.count("tool_call") == 1 and names.count("tool_result") == 1
        assert events_named(sse, "guardrail")[0]["action"] == "mask"
        first_call = events_named(sse, "tool_call")[0]["id"]
        done = events_named(sse, "done")[0]
        assert done["tool_calls"] == 1
        sid = done["session_id"]
        first_usage = done["usage"]
        # 가린 목록이 체크포인트에 있다. 미들웨어가 목록 전체를 되돌렸다는 뜻이다
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["messages"][-1]["text"] == "결과: ****"

        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 400, "b": 56}}', session_id=sid)
        names = event_names(sse)
        assert names.count("tool_call") == 1 and names.count("tool_result") == 1
        call = events_named(sse, "tool_call")[0]
        assert call["id"] != first_call and call["args"] == {"a": 400, "b": 56}
        assert events_named(sse, "tool_result")[0]["id"] == call["id"]
        # 첫 턴의 글은 다시 흐르지 않는다. 이번 턴의 글은 출력 필터를 지나 가려진 채 하나로 나간다
        assert [t["text"] for t in events_named(sse, "token")] == ["결과: ****"]
        done = events_named(sse, "done")[0]
        assert done["tool_calls"] == 1
        # 둘째 턴은 이력이 더 길어 입력이 조금 크지만 첫 턴을 통째로 다시 센 크기는 아니다
        assert done["usage"]["input_tokens"] < 2 * first_usage["input_tokens"]
        row = (await running.client.get(f"/sessions/{sid}")).json()["session"]
        assert row["usage"]["input_tokens"] == (
            first_usage["input_tokens"] + done["usage"]["input_tokens"]
        )


async def test_record_bodies_puts_bodies_in_events(support_doc) -> None:
    support_doc["service"]["observability"]["record_bodies"] = True
    async with run_app(support_doc) as running:
        await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 2}}')
        events = {e["daegam_event"]: e for e in running.events()}
        assert events["turn_start"]["message"].startswith('{"tool": "Add"')
        assert events["tool_exec"]["args"] == '{"a": 1, "b": 2}'
        assert events["tool_exec"]["content"] == "3"
        assert events["turn_end"]["text"] == "결과: 3"


@pytest.mark.parametrize("record_bodies", [False, True])
async def test_done_carries_the_structured_result(support_doc, record_bodies) -> None:
    """응답 형식이 JSON 스키마면 done 과 전사의 done 항목이 structured_response 를 싣고, text
    형식이면 그 칸이 없다. turn_end 에는 record_bodies 일 때만 실린다(D65)."""
    support_doc["main"]["tools"] = []
    support_doc["service"]["observability"]["record_bodies"] = record_bodies
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        done = events_named(sse, "done")[0]
        saved = (await running.runtime.store.transcripts.read(done["session_id"]))[-1]
        end = running.observed("turn_end")[0]
        assert saved["kind"] == "done"
        assert all("structured_response" not in d for d in (done, saved, end))

    support_doc["main"]["response_format"] = {"kind": "json_schema", "schema": STRUCTURED_SCHEMA}
    async with run_app(support_doc, model=JsonModel()) as running:
        _, sse = await chat(running, "hi")
        done = events_named(sse, "done")[0]
        assert done["structured_response"] == STRUCTURED_VALUE
        saved = (await running.runtime.store.transcripts.read(done["session_id"]))[-1]
        assert saved["kind"] == "done" and saved["structured_response"] == STRUCTURED_VALUE
        end = running.observed("turn_end")[0]
        expected = json.dumps(STRUCTURED_VALUE, ensure_ascii=False) if record_bodies else None
        assert end.get("structured_response") == expected


async def test_greeting_is_the_first_event_of_a_new_session(support_doc) -> None:
    """새 세션의 첫 이벤트는 입구의 인사말이다(D65). 입력 가드레일 flag 보다 앞이고, 전사에서는
    사용자 항목 앞이며, 모델 메시지에는 없다. 재개 턴과 이어지는 턴에는 없고, 인사말이 비어
    있으면 이벤트도 없다."""
    support_doc["entry"]["greeting"] = GREETING
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "reject"]}
    support_doc["guardrails"] = [guardrail("input", [deny_rule("Add")], "flag")]
    async with run_app(support_doc) as running:
        assert (await running.client.get("/healthz")).json()["greeting"] == GREETING
        response, sse = await chat(running, '{"tool": "Add", "args": {"a": 3, "b": 4}}')
        sid = response.headers[SESSION_HEADER]
        names = event_names(sse)
        assert names[:3] == ["greeting", "guardrail", "tool_call"] and names[-1] == "interrupt"
        assert sse[0][1] == {"session_id": sid, "text": GREETING}
        saved = await running.runtime.store.transcripts.read(sid)
        assert saved[0] == {
            "kind": "assistant",
            "text": GREETING,
            "streaming": False,
            "greeting": True,
        }
        assert saved[1]["kind"] == "user"
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["messages"][0]["role"] == "human"
        assert all(m.get("text") != GREETING for m in detail["messages"])

        response, resumed = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200
        assert "greeting" not in event_names(resumed)
        _, sse = await chat(running, "다시", session_id=sid)
        assert sse and "greeting" not in event_names(sse)

    for empty in (None, ""):
        support_doc["entry"]["greeting"] = empty
        async with run_app(support_doc) as running:
            response, sse = await chat(running, "hi")
            assert "greeting" not in event_names(sse)
            saved = await running.runtime.store.transcripts.read(response.headers[SESSION_HEADER])
            assert saved[0]["kind"] == "user"


async def test_session_ownership_and_unknown_session(support_doc) -> None:
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        response, _ = await chat(running, "hi", session_id=sid, user_sub="bob")
        assert response.status_code == 403 and response.json()["code"] == "not_session_owner"
        response, _ = await chat(running, "hi", session_id="nope")
        assert response.status_code == 404 and response.json()["code"] == "session_not_found"
        assert (await running.client.get("/sessions/nope")).status_code == 404
        response = await running.client.get("/sessions", params={"status": "weird"})
        assert response.status_code == 422 and response.json()["code"] == "invalid_status"


@pytest.mark.parametrize("injected", [False, True])
async def test_failed_startup_closes_only_what_the_app_opened(
    support_doc, monkeypatch, injected
) -> None:
    """기동 도중에 실패하면 앱이 연 세션 저장소는 닫고, 주입받은 것은 준 쪽에 맡긴다."""
    closed: list[str] = []
    store = await open_session_store(parse_config(copy.deepcopy(support_doc)))

    async def close() -> None:
        closed.append("store")

    store.close = close  # type: ignore[method-assign]

    async def open_spy(cfg: Any) -> Any:
        return store

    def broken_compile(*args: Any, **kwargs: Any) -> Any:
        raise ConfigError("boom", "컴파일이 실패했다")

    monkeypatch.setattr(boot, "open_session_store", open_spy)
    monkeypatch.setattr(boot, "compile_agent", broken_compile)
    with pytest.raises(ConfigError):
        async with run_app(support_doc, store=store if injected else None):
            pass
    assert closed == ([] if injected else ["store"])
