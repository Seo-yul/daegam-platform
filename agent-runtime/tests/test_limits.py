"""입구 한도(max_turns 429, max_input_chars 413, 동시 세션 429), 인스턴스 토큰 401, 툴 한도,
시간이다."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from runtime import compile as compile_module
from runtime.state import Settings
from runtime.stub import StubChatModel
from runtime.tool_limit import LIMIT_NOTICE, LIMIT_STOP, SUBAGENT_STOP, TurnToolLimit, hidden
from runtime.tool_names import STRUCTURED_RESPONSE_TOOL
from runtime.trace import ROLE_SUBAGENT
from runtime.turn_gate import MESSAGE_MAX_CHARS
from tests.app_fixtures import (
    INSTANCE_TOKEN,
    bearer,
    folded,
    sid_of,
    transcript_items,
    with_subagent,
)
from tests.conftest import chat, event_names, events_named, run_app


async def test_max_turns_returns_429(support_doc) -> None:
    support_doc["entry"]["max_turns"] = 1
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        response, _ = await chat(running, "again", session_id=sid)
        assert response.status_code == 429
        assert response.json()["code"] == "max_turns"
        assert running.rejections() == ["max_turns"]


async def test_max_input_chars_returns_413(support_doc) -> None:
    support_doc["entry"]["max_input_chars"] = 5
    async with run_app(support_doc) as running:
        response, _ = await chat(running, "123456")
        assert response.status_code == 413
        assert response.json()["code"] == "max_input_chars"


async def test_a_message_above_the_ceiling_is_max_input_chars_and_counted(support_doc) -> None:
    """요청 모델에는 길이 상한이 없고 입력 길이 검사가 천장(MESSAGE_MAX_CHARS)까지 본다 (P76 F2).
    max_input_chars 가 천장보다 커도 천장을 넘는 글은 413 이고 거절로 센다. 다시 생성도 같다."""
    support_doc["entry"]["max_input_chars"] = 1_000_000
    long = "x" * (MESSAGE_MAX_CHARS + 1)
    async with run_app(support_doc) as running:
        response, _ = await chat(running, long)
        assert response.status_code == 413 and response.json()["code"] == "max_input_chars"
        response = await running.client.post(
            "/sessions/s1/regenerate", json={"user_sub": "alice", "message": long}
        )
        assert response.status_code == 413 and response.json()["code"] == "max_input_chars"
        assert running.rejections() == ["max_input_chars", "max_input_chars"]


async def test_max_concurrent_sessions_returns_429(support_doc) -> None:
    support_doc["entry"]["max_concurrent_sessions"] = 1
    async with run_app(support_doc) as running:
        response, _ = await chat(running, "hi")
        assert response.status_code == 200
        response, _ = await chat(running, "hi", user_sub="bob")
        assert response.status_code == 429
        assert response.json()["code"] == "max_concurrent_sessions"
        assert response.headers["Retry-After"] == "60"


async def test_instance_token_guards_everything_but_healthz(support_doc) -> None:
    settings = Settings(instance_token=INSTANCE_TOKEN)
    async with run_app(support_doc, settings=settings) as running:
        assert (await running.client.get("/healthz")).status_code == 200
        for path in ("/sessions", "/metrics", "/sessions/x"):
            response = await running.client.get(path)
            assert response.status_code == 401, path
            assert response.json()["code"] == "unauthorized"
        response, _ = await chat(running, "hi")
        assert response.status_code == 401
        response, _ = await chat(running, "hi", headers=bearer("wrong"))
        assert response.status_code == 401
        response, sse = await chat(running, "hi", headers=bearer(INSTANCE_TOKEN))
        assert response.status_code == 200 and events_named(sse, "done")
        ok = await running.client.get("/metrics", headers=bearer(INSTANCE_TOKEN))
        assert ok.status_code == 200


class LoopingModel(StubChatModel):
    """결과를 받아도 계속 Add 를 부른다. 도구 호출 한도가 턴을 끊는지 보려는 모델이다.

    `bind_tools` 로 받은 목록의 첫 도구를 고르지 않고 Add 를 이름으로 부른다. 파일 도구가 그
    목록에 드는지는 같은 프로세스의 앞 테스트가 같은 모델 키에 건 harness profile 에 따라
    달라지므로(등록은 합집합이다), 첫 도구를 고르면 결과가 테스트 실행 순서에 따라 바뀐다."""

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "Add",
                    "args": {"a": 1, "b": 1},
                    "id": f"call_{len(messages)}",
                    "type": "tool_call",
                }
            ],
        )


class AnsweringModel(LoopingModel):
    """한도 알림을 받을 때까지 Add 를 부르고 받으면 글로 답한다. 알림을 따르는 모델의 꼴이다."""

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.status == "error":
            return AIMessage(content="지금까지 얻은 결과로 답합니다.")
        return super().reply(messages)


async def test_the_first_call_past_the_limit_lets_the_model_answer(support_doc, fake_mcp) -> None:
    """한도를 처음 넘는 호출은 돌지 않고 알림 결과를 받는다. 모델이 한 번 더 불려 자기 말로 답하고
    영어 안내문은 답이 되지 않는다. tool_limit 이 한 번 나간다(T82)."""
    support_doc["main"]["limits"]["max_tool_calls_per_turn"] = 2
    async with run_app(support_doc, model=AnsweringModel(model_name="stub")) as running:
        response, sse = await chat(running, "loop")
        assert response.status_code == 200
        assert fake_mcp.calls["Add"] == 2
        results = events_named(sse, "tool_result")
        assert [r["content"] for r in results[:2]] == ["2", "2"]
        assert len(results) == 3 and results[2]["status"] == "error"
        assert results[2]["content"] == LIMIT_NOTICE.format(limit=2)
        assert events_named(sse, "tool_limit") == [{"limit": 2}]
        done = events_named(sse, "done")[0]
        assert done["text"] == "지금까지 얻은 결과로 답합니다."
        assert "Tool call limit reached" not in done["text"]
        names = event_names(sse)
        assert names.index("tool_limit") < names.index("done") and names[-1] == "done"
        # 다시 읽은 전사도 알림 하나와 모델의 답을 든다
        items = await transcript_items(running, sid_of(sse, "done"))
        assert items == folded(("loop", sse))
        assert [i for i in items if i["kind"] == "tool_limit"] == [
            {"kind": "tool_limit", "limit": 2}
        ]
        assert items[-2] == {
            "kind": "assistant",
            "text": "지금까지 얻은 결과로 답합니다.",
            "streaming": False,
        }


async def test_a_model_that_keeps_calling_after_the_notice_ends_the_turn(
    support_doc, fake_mcp
) -> None:
    """알림 뒤에도 Tool 을 부르면 그 호출은 돌지 않고 턴이 닫힌다. 답으로 붙는 글은 없고 턴은
    타임아웃까지 돌지 않는다. 알림은 한 번이다."""
    support_doc["main"]["limits"]["max_tool_calls_per_turn"] = 2
    async with run_app(support_doc, model=LoopingModel(model_name="stub")) as running:
        response, sse = await chat(running, "loop")
        assert response.status_code == 200
        names = event_names(sse)
        assert names[-1] == "done"
        assert fake_mcp.calls["Add"] == 2
        results = events_named(sse, "tool_result")
        assert len(results) == 4
        assert results[2]["content"] == LIMIT_NOTICE.format(limit=2)
        assert results[3]["content"] == LIMIT_STOP.format(limit=2)
        assert events_named(sse, "tool_limit") == [{"limit": 2}]
        done = events_named(sse, "done")[0]
        assert done["text"] == "" and done["tool_calls"] == 4


async def test_turn_timeout_emits_error(support_doc) -> None:
    """스텁이 턴 제한(1초)보다 오래(5초) 자면 턴이 turn_timeout 으로 끝난다."""
    support_doc["main"]["limits"]["turn_timeout_seconds"] = 1
    async with run_app(support_doc) as running:
        started = asyncio.get_running_loop().time()
        response, sse = await chat(running, '{"sleep": 5}')
        assert response.status_code == 200
        assert asyncio.get_running_loop().time() - started < 4
        assert event_names(sse) == ["error"]
        error = sse[0][1]
        assert error["code"] == "turn_timeout"
        sid = error["session_id"]
        row = (await running.client.get(f"/sessions/{sid}")).json()["session"]
        # 끊긴 턴은 턴 수에 들어가지 않고 세션은 계속 쓸 수 있다
        assert row["turns"] == 0 and row["status"] == "active"
        end = running.observed("turn_end")[0]
        assert end["status"] == "turn_timeout"


def _calls(*ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "Add", "args": {}, "id": i, "type": "tool_call"} for i in ids],
    )


def test_a_batch_that_crosses_the_limit_runs_its_allowed_calls() -> None:
    """한 묶음이 한도를 넘으면 한도 안의 호출은 돌고 넘는 호출만 알림 결과를 받는다. 다음 묶음은
    턴을 닫고 그 호출 모두에 결과를 채운다. 닫는 묶음은 스레드 셈에 들지 않는다."""
    limit = TurnToolLimit(2)
    first = limit.after_model({"messages": [_calls("a", "b", "c")]}, None)
    assert first is not None and "jump_to" not in first
    assert [m.tool_call_id for m in first["messages"]] == ["c"]
    assert first["messages"][0].content == LIMIT_NOTICE.format(limit=2)
    assert first["run_tool_call_count"] == {"__all__": 3}
    assert first["thread_tool_call_count"] == {"__all__": 2}
    state = {**first, "messages": [_calls("d", "e")]}
    second = limit.after_model(state, None)
    assert second is not None and second["jump_to"] == "end"
    assert [m.tool_call_id for m in second["messages"]] == ["d", "e"]
    assert {m.content for m in second["messages"]} == {LIMIT_STOP.format(limit=2)}
    assert second["thread_tool_call_count"] == {"__all__": 2}
    assert limit.after_model({"messages": [AIMessage(content="답")]}, None) is None


def test_the_structured_answer_is_neither_counted_nor_refused() -> None:
    """ToolStrategy 의 구조화 결과 호출은 답이다. 모델 노드가 결과를 이미 붙였으므로 한도는 그
    호출을 세지 않고, 알린 뒤에도 막거나 결과를 또 붙이지 않는다(같은 tool_call_id 가 둘이 된다)."""
    limit = TurnToolLimit(2)
    answer = AIMessage(
        content="",
        tool_calls=[
            {"name": STRUCTURED_RESPONSE_TOOL, "args": {}, "id": "s1", "type": "tool_call"}
        ],
    )
    own = ToolMessage(content="ok", tool_call_id="s1", name=STRUCTURED_RESPONSE_TOOL)
    for count in (2, 3):
        state = {"messages": [answer, own], "run_tool_call_count": {"__all__": count}}
        assert limit.after_model(state, None) is None
    # 알린 뒤 같은 묶음에 다른 호출이 섞이면 그 호출에만 결과를 붙인다
    mixed = AIMessage(
        content="",
        tool_calls=[
            {"name": STRUCTURED_RESPONSE_TOOL, "args": {}, "id": "s2", "type": "tool_call"},
            {"name": "Add", "args": {}, "id": "a9", "type": "tool_call"},
        ],
    )
    own2 = ToolMessage(content="ok", tool_call_id="s2", name=STRUCTURED_RESPONSE_TOOL)
    state = {"messages": [mixed, own2], "run_tool_call_count": {"__all__": 3}}
    stopped = limit.after_model(state, None)
    assert stopped is not None and stopped["jump_to"] == "end"
    assert [m.tool_call_id for m in stopped["messages"]] == ["a9"]


class DelegatingModel(StubChatModel):
    """메인 모델이다. calc 에게 한 번 맡기고, task 결과가 오면 그 글을 그대로 옮겨 답한다."""

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == "task":
            return AIMessage(content=f"서브: {last.text}")
        call = {"subagent_type": "calc", "description": "더해 줘"}
        return AIMessage(
            content="",
            tool_calls=[{"name": "task", "args": call, "id": "t1", "type": "tool_call"}],
        )


def _sub_model(monkeypatch, model: StubChatModel) -> None:
    """서브에이전트의 모델만 바꾼다. 메인 모델은 run_app 이 끼운다."""
    build = compile_module.build_chat_model

    def fake(spec, *, role="main", **kwargs):  # type: ignore[no-untyped-def]
        return model if role == ROLE_SUBAGENT else build(spec, role=role, **kwargs)

    monkeypatch.setattr(compile_module, "build_chat_model", fake)


async def test_a_subagent_that_crosses_its_limit_answers_the_main_agent(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """서브에이전트가 자기 한도(2)를 넘으면 알림을 받고 답한다. tool_limit 은 그 자리의 이름표를
    달고 메인의 한도는 울리지 않는다. 메인 모델은 서브의 답을 task 결과로 읽는다."""
    with_subagent(support_doc)
    _sub_model(monkeypatch, AnsweringModel(model_name="stub-sub"))
    async with run_app(support_doc, model=DelegatingModel(model_name="stub")) as running:
        _, sse = await chat(running, "맡겨")
        assert fake_mcp.calls["Add"] == 2
        [event] = events_named(sse, "tool_limit")
        assert event["limit"] == 2 and event.get("agent")
        [task] = [r for r in events_named(sse, "tool_result") if r["name"] == "task"]
        assert task["content"] == "지금까지 얻은 결과로 답합니다."
        assert events_named(sse, "done")[0]["text"] == "서브: 지금까지 얻은 결과로 답합니다."


async def test_a_subagent_that_keeps_calling_tells_the_main_agent_it_stopped(
    support_doc, fake_mcp, monkeypatch
) -> None:
    """알린 뒤에도 부르는 서브에이전트는 닫히고, 메인 모델은 한도 때문에 멈췄다는 글을 task 결과로
    읽는다. 그 글은 숨긴 메시지라 서브에이전트의 말풍선으로 나가지 않는다."""
    with_subagent(support_doc)
    _sub_model(monkeypatch, LoopingModel(model_name="stub-sub"))
    async with run_app(support_doc, model=DelegatingModel(model_name="stub")) as running:
        _, sse = await chat(running, "맡겨")
        assert fake_mcp.calls["Add"] == 2
        assert len(events_named(sse, "tool_limit")) == 1
        stop = SUBAGENT_STOP.format(limit=2)
        [task] = [r for r in events_named(sse, "tool_result") if r["name"] == "task"]
        assert task["content"] == stop
        assert not [t for t in events_named(sse, "token") if t.get("agent")]
        assert events_named(sse, "done")[0]["text"] == f"서브: {stop}"


def test_hidden_marks_only_the_limits_own_message() -> None:
    assert hidden(AIMessage(content="x", additional_kwargs={"daegam_hidden": True}))
    assert not hidden(AIMessage(content="x"))
