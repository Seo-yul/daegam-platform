"""P14 F1b: 스텁·루프·세션 문서의 순수 규칙. HTTP 와 Mongo 없이 돈다.

스텁의 steps·`$<n>.<path>` 참조·say 규칙, 전사 투영과 500개 상한, 문맥 자르기(HumanMessage 경계),
직렬화 왕복(thinking 서명·tool_calls·status), 승인 등급표, 재시도 판정, 한 AIMessage 안의
read+deploy 혼합과 ask_user 우선, 401 로 턴 닫기를 가짜 실행기로 든다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from daegam.auth.principal import header_principal
from daegam.llm_http import HookedChatAnthropic
from daegam.services.assistant.catalog import CATALOG, Catalog, ToolSpec
from daegam.services.assistant.loop import (
    KEEP_HUMANS,
    SUMMARY_FLAG,
    SUMMARY_PREFIX,
    Decision,
    Limits,
    TurnContext,
    TurnLoop,
    apply_event,
    is_summary,
    masked_ai_message,
    needs_confirm,
    retryable,
)
from daegam.services.assistant.schema import build_tool_schemas
from daegam.services.assistant.stub import (
    LAST_ERROR_FORMAT,
    NO_TOOL_PREFIX,
    STEPS_DONE_TEXT,
    SUMMARY_MARKER,
    SUMMARY_TEXT,
    AssistantStub,
    current_step,
    json_path,
    parse_steps,
    substitute,
)
from daegam.services.assistant.tools import ToolResult
from daegam.services.llm_providers import retryable_status
from daegam.store.assistant_sessions import (
    TRANSCRIPT_LIMIT,
    AssistantSession,
    FetchedFile,
    Pending,
    PendingCall,
    Usage,
)
from tests.helpers.assistant import steps, tool


def result(payload: Any, call_id: str, name: str = "t", status: str = "success") -> ToolMessage:
    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name=name, status=status)


# -- 스텁 ---------------------------------------------------------------------------------


def test_stub_walks_steps_by_tool_message_count_and_resolves_references():
    stub = AssistantStub()
    stub.bind_tools([{"name": "list_x"}, {"name": "create_y"}])
    assert stub.bound_tools == ["list_x", "create_y"]
    script = steps(
        tool("list_x", q="a"),
        tool("create_y", parent="$0.id", first="$0.items[0].id", label="of-$0.id", n="$0.count"),
        {"say": "made $1.id from $0.id"},
    )
    messages: list[Any] = [SystemMessage("s"), HumanMessage(script)]
    ai = stub.reply(messages)
    (call,) = ai.tool_calls
    assert (
        call["name"] == "list_x" and call["args"] == {"q": "a"} and call["id"].startswith("call_")
    )
    assert ai.usage_metadata["total_tokens"] > 0
    messages += [ai, result({"id": "abc", "items": [{"id": "i0"}], "count": 2}, call["id"])]
    ai = stub.reply(messages)
    (call,) = ai.tool_calls
    assert call["name"] == "create_y"
    # 문자열 전체가 참조면 형을 지키고, 문장 안에 섞이면 문자열로 끼워 넣는다
    assert call["args"] == {"parent": "abc", "first": "i0", "label": "of-abc", "n": 2}
    messages += [ai, result({"id": "y1"}, call["id"])]
    ai = stub.reply(messages)
    assert ai.tool_calls == [] and ai.content == "made y1 from abc"
    # say 뒤에 결과가 더 붙어도 say 는 그대로다. 마지막 결과가 error 면 code 가 뒤에 붙는다
    messages += [ai, result({"code": "forbidden", "status": 403}, "x", status="error")]
    assert stub.reply(messages).content == "made y1 from abc" + LAST_ERROR_FORMAT.format(
        code="forbidden"
    )
    # say 없이 steps 를 다 쓰면 고정 문구
    messages = [HumanMessage(steps(tool("t")))]
    ai = stub.reply(messages)
    messages += [ai, result({"code": "forbidden"}, ai.tool_calls[0]["id"], status="error")]
    assert stub.reply(messages).content == STEPS_DONE_TEXT + LAST_ERROR_FORMAT.format(
        code="forbidden"
    )
    # steps 가 없으면 답만 한다. 요약 프롬프트는 고정 문구
    assert stub.reply([HumanMessage("안녕")]).content == NO_TOOL_PREFIX + "안녕"
    assert stub.reply([HumanMessage(f"{SUMMARY_MARKER}\nuser: x")]).content == SUMMARY_TEXT
    # JSON 이 아닌 도구 결과는 문자열 그대로. 없는 참조는 None
    messages = [HumanMessage(steps(tool("t"), {"say": "$0 / $0.x / $5.y"}))]
    ai = stub.reply(messages)
    messages += [ai, ToolMessage(content="plain", tool_call_id=ai.tool_calls[0]["id"])]
    assert stub.reply(messages).content == "plain / null / null"


def test_stub_multi_call_step_and_pure_helpers():
    """`{"tools": [...]}` 는 한 AIMessage 에 tool_call 여럿이고 그만큼 ToolMessage 를 소비한다."""
    stub = AssistantStub()
    script = steps({"tools": [tool("a"), tool("b", x="$0.v")]}, tool("c", y="$1.w"), {"say": "ok"})
    messages: list[Any] = [HumanMessage(script)]
    ai = stub.reply(messages)
    assert [c["name"] for c in ai.tool_calls] == ["a", "b"]
    assert ai.tool_calls[1]["args"] == {"x": None}
    messages += [
        ai,
        result({"v": 1}, ai.tool_calls[0]["id"]),
        result({"w": 2}, ai.tool_calls[1]["id"]),
    ]
    ai = stub.reply(messages)
    assert [c["name"] for c in ai.tool_calls] == ["c"] and ai.tool_calls[0]["args"] == {"y": 2}
    messages += [ai, result({}, ai.tool_calls[0]["id"])]
    assert stub.reply(messages).content == "ok"

    parsed = parse_steps('앞말 {"steps": [{"tool": "a"}]} 뒷말')
    assert parsed == [{"tool": "a"}]
    assert parse_steps('{"steps": "no"}') is None and parse_steps("x") is None
    assert current_step([{"tool": "a"}, {"say": "s"}], 0) == {"tool": "a"}
    assert current_step([{"tool": "a"}, {"say": "s"}], 1) == {"say": "s"}
    assert current_step([{"tool": "a"}], 1) is None
    assert json_path({"a": [{"b": 3}]}, "a[0].b") == 3 and json_path({"a": 1}, "a.b") is None
    assert substitute({"k": ["$0.a", 5]}, [{"a": "v"}]) == {"k": ["v", 5]}
    assert substitute("$9", []) is None


# -- 전사 ---------------------------------------------------------------------------------


def test_apply_event_projects_events_like_the_ui_and_caps_at_500():
    items: list[dict[str, Any]] = []
    apply_event(items, "token", {"text": "안"})
    apply_event(items, "token", {"text": "녕"})
    assert items == [{"kind": "assistant", "text": "안녕", "streaming": True}]
    apply_event(items, "tool_call", {"id": "c1", "name": "list_x", "args": {}})
    assert items[0]["streaming"] is False and items[1]["result"] is None
    apply_event(
        items, "tool_result", {"id": "c1", "name": "list_x", "status": "success", "content": "[]"}
    )
    assert items[1]["result"]["content"] == "[]"
    # 짝이 없는 tool_result 는 자기 항목을 만든다
    apply_event(items, "tool_result", {"id": "c9", "name": "y", "status": "error", "content": "{}"})
    assert items[2]["call"] == {"id": "c9", "name": "y", "args": {}}
    apply_event(items, "resource", {"kind": "hub", "id": "h", "name": "n", "url": "/hub/h"})
    apply_event(items, "ui", {"type": "navigate", "to": "/x"})
    apply_event(items, "interrupt", {"session_id": "s", "kind": "confirm", "action_requests": []})
    apply_event(
        items,
        "done",
        {"session_id": "s", "turn": 1, "usage": {"total_tokens": 3}, "tool_calls": 1, "text": ""},
    )
    apply_event(items, "error", {"session_id": "s", "code": "tool_limit", "message": "m"})
    assert [it["kind"] for it in items] == [
        "assistant",
        "tool_call",
        "tool_call",
        "resource",
        "interrupt",
        "done",
        "error",
    ]
    assert items[4]["resolved"] is False and items[5]["usage"] == {"total_tokens": 3}
    for i in range(TRANSCRIPT_LIMIT + 20):
        apply_event(items, "error", {"code": "x", "message": str(i)})
    assert len(items) == TRANSCRIPT_LIMIT and items[-1]["message"] == str(TRANSCRIPT_LIMIT + 19)
    # 토큰이 새 말풍선을 열어도 상한을 지킨다
    apply_event(items, "token", {"text": "끝"})
    assert len(items) == TRANSCRIPT_LIMIT and items[-1]["kind"] == "assistant"


def test_apply_event_attaches_result_to_the_first_open_call_like_the_ui():
    """같은 id 의 열린 호출이 둘이면 앞의 것에 붙는다. UI 의 findIndex 와 같은 차례다."""
    items: list[dict[str, Any]] = []
    apply_event(items, "tool_call", {"id": "c1", "name": "a", "args": {}})
    apply_event(items, "tool_call", {"id": "c1", "name": "a", "args": {}})
    apply_event(
        items, "tool_result", {"id": "c1", "name": "a", "status": "success", "content": "1"}
    )
    assert items[0]["result"]["content"] == "1" and items[1]["result"] is None


# -- 직렬화 --------------------------------------------------------------------------------


def test_usage_counts_cache_reads_apart_from_input():
    """캐시에서 읽은 입력은 input_tokens 안의 몫이다. 따로 세야 실제 비용을 셈할 수 있다."""
    usage = Usage()
    usage.add(
        {
            "input_tokens": 10_000,
            "output_tokens": 50,
            "total_tokens": 10_050,
            "input_token_details": {"cache_read": 9_000},
        }
    )
    # 캐시 정보가 없는 provider(stub 등)는 0 을 더한다
    usage.add({"input_tokens": 100, "output_tokens": 5, "total_tokens": 105})
    usage.add({"input_tokens": 1, "output_tokens": 1, "input_token_details": {"cache_read": None}})
    assert usage.model_dump() == {
        "input_tokens": 10_101,
        "output_tokens": 56,
        "total_tokens": 10_157,
        "cached_input_tokens": 9_000,
    }


def test_session_context_round_trips_thinking_blocks_tool_calls_and_status():
    ai = AIMessage(
        content=[
            {"type": "thinking", "thinking": "생각", "signature": "sig-1"},
            {"type": "text", "text": "답"},
        ],
        tool_calls=[{"name": "list_x", "args": {"a": 1}, "id": "c1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    )
    original = [
        HumanMessage(SUMMARY_PREFIX + "요약", additional_kwargs={SUMMARY_FLAG: True}),
        HumanMessage("질문"),
        ai,
        ToolMessage(content='{"ok": true}', tool_call_id="c1", name="list_x", status="error"),
    ]
    session = AssistantSession(
        tenant="t",
        sub="u",
        context=[message_to_dict(m) for m in original],
        files={
            "fetched-1.yaml": FetchedFile(text="openapi: 3.0.0\n", url="https://x/spec.yaml"),
            "fetched-2.txt": FetchedFile(text="옛 세션에서 온 파일"),
        },
        pending=Pending(
            kind="confirm", calls=[PendingCall(id="c2", name="d", args={}, risk="deploy")]
        ),
    )
    doc = session.to_doc()
    # 파일 이름의 점이 Mongo 키가 되지 않게 목록으로 저장한다. url 은 있을 때만 실린다
    assert doc["files"] == [
        {"name": "fetched-1.yaml", "text": "openapi: 3.0.0\n", "url": "https://x/spec.yaml"},
        {"name": "fetched-2.txt", "text": "옛 세션에서 온 파일"},
    ]
    back = AssistantSession.from_doc(json.loads(json.dumps(doc, default=str)))
    restored = messages_from_dict(back.context)
    assert [type(m).__name__ for m in restored] == [
        "HumanMessage",
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
    ]
    assert is_summary(restored[0]) and not is_summary(restored[1])
    assert restored[2].content == ai.content and restored[2].tool_calls == ai.tool_calls
    assert restored[2].usage_metadata == ai.usage_metadata
    assert restored[3].status == "error" and restored[3].tool_call_id == "c1"
    assert back.files == session.files and back.pending == session.pending
    assert back.title == "새 대화"


# -- 루프 순수 규칙 --------------------------------------------------------------------------


def test_needs_confirm_and_retryable():
    assert not needs_confirm("read", True) and not needs_confirm("write", True)
    assert needs_confirm("deploy", True) and not needs_confirm("deploy", False)
    assert needs_confirm("destructive", True) and needs_confirm("destructive", False)
    # 가져온 파일이 얹히면 read 가 아닌 등급은 confirm_deploy 와 무관하게 승인 뒤다
    for risk in ("write", "deploy", "destructive"):
        assert needs_confirm(risk, False, uses_fetched=True), risk
    assert not needs_confirm("read", False, uses_fetched=True)
    # Agent 정의의 제안은 등급과 confirm_deploy 에 상관없이 늘 기다린다 (P48 6.1절)
    assert needs_confirm("write", False, proposal=True)

    class Status(Exception):
        def __init__(self, code: int, error_code: str | None = None) -> None:
            self.status_code = code
            self.code = error_code

    assert retryable(Status(429)) and retryable(Status(503)) and not retryable(Status(400))
    # 크레딧 소진은 429 여도 기다려서 풀리지 않는다(T20)
    assert not retryable(Status(429, "insufficient_quota")) and not retryable(Status(402))
    assert not retryable(Status(401))
    # 5xx 는 목록이 아니라 범위다. Anthropic 의 529(overloaded)도 다시 보낸다. 스펙 생성기와
    # 같은 규칙(`llm_providers.retryable_status`)이다
    assert retryable(Status(529)) and retryable_status(529)
    assert retryable(httpx.ConnectError("x")) and retryable(TimeoutError())
    assert not retryable(ValueError("x"))


class FakeExecutor:
    """도구 이름별로 정해진 결과를 돌려주는 실행기. 호출 순서를 기록한다."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def call(self, name, args, *, ctx, n, files=None):
        self.calls.append((name, dict(args or {}), n))
        return self.results.get(name, ToolResult("success", {"ok": True, "tool": name}))


def make_loop(
    executor: Any,
    *,
    confirm_deploy: bool = True,
    tool_defs: Any = None,
    model: Any = None,
    **kw: Any,
) -> tuple[TurnLoop, list]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        events.append((name, data))

    async def renew() -> bool:
        return True

    async def checkpoint() -> None:
        events.append(("checkpoint", {}))

    defs = build_tool_schemas(_OPENAPI, CATALOG) if tool_defs is None else tool_defs
    loop = TurnLoop(
        model=AssistantStub() if model is None else model,
        tool_defs=defs,
        executor=executor,
        emit=emit,
        renew=renew,
        checkpoint=checkpoint,
        confirm_deploy=confirm_deploy,
        **kw,
    )
    return loop, events


def ctx() -> TurnContext:
    return TurnContext(
        principal=header_principal(), tenant="t", session_id="s", request_id="r", turn=1
    )


@pytest.fixture(scope="module", autouse=True)
def _openapi():
    from daegam.main import create_app

    global _OPENAPI
    _OPENAPI = create_app().openapi()


CORE_TOOLS = ["navigate", "read_guide", "ask_user", "use_tools", "fetch_url"]


def test_tools_ride_in_groups_and_use_tools_opens_them_without_ui_events():
    """도구 묶음 (D57). 처음에는 기본 묶음만 싣고, use_tools 가 연 묶음은 다음 호출부터 실린다.
    use_tools 는 실행기로 가지 않고 화면 이벤트도 내지 않는다."""
    ex = FakeExecutor({})
    loop, events = make_loop(ex)
    assert asyncio.run(loop.run_message(ctx(), "안녕")).kind == "done"
    assert loop.model.bound_tools == CORE_TOOLS and loop.tool_groups == set()

    loop, events = make_loop(ex)
    script = steps(
        tool("use_tools", groups=["backend", "cluster"]), tool("list_clusters"), {"say": "끝"}
    )
    assert asyncio.run(loop.run_message(ctx(), script)).kind == "done"
    assert loop.tool_groups == {"backend", "cluster"}
    bound = loop.model.bound_tools
    assert bound[:3] == ["list_backend_apis", "get_backend_api", "create_backend_api"]
    assert "list_clusters" in bound and "use_tools" in bound and "list_mcp_servers" not in bound
    # 화면에는 실제 도구 호출만 보이고, 실행기는 use_tools 를 모른다
    assert [d["name"] for e, d in events if e == "tool_call"] == ["list_clusters"]
    assert [c[0] for c in ex.calls] == ["list_clusters"]
    opened = json.loads(next(m for m in loop.messages if isinstance(m, ToolMessage)).content)
    assert opened["opened"] == ["backend", "cluster"] and "verify_spec" in opened["tools"]


class RecordingClaude(HookedChatAnthropic):
    """요청 본문을 기록하고 정해진 청크를 흘리는 Claude. 본문은 실제 클래스의
    `_get_request_payload` 가 짓는다."""

    replies: list[AIMessageChunk] = Field(default_factory=list)
    payloads: list[dict[str, Any]] = Field(default_factory=list)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        kwargs.pop("stream_usage", None)
        self.payloads.append(self._get_request_payload(messages, stop=stop, stream=True, **kwargs))
        yield ChatGenerationChunk(message=self.replies.pop(0))


def test_claude_calls_of_a_turn_read_one_cached_prefix():
    """Claude 의 호출은 고정 프롬프트 블록에 캐시 표지를 달고 요청의 cache_control 로 대화 끝을
    캐시한다. 한 턴의 두 호출은 Tool 정의와 시스템이 바이트 그대로 같고, 뒤 호출의 메시지는 앞
    호출의 메시지로 시작한다 (T80). 문맥 줄은 표지 뒤 블록이다."""
    call = {"name": "read_guide", "args": '{"topic": "agent"}', "id": "c1", "index": 0}
    model = RecordingClaude(
        model="claude-sonnet-5-5",
        api_key="k",
        replies=[AIMessageChunk(content="", tool_call_chunks=[call]), AIMessageChunk(content="끝")],
    )
    loop, _ = make_loop(FakeExecutor({}), model=model)
    turn = loop.run_message(ctx(), "Agent 를 만들어 줘", {"route": "/agents"})
    assert asyncio.run(turn).kind == "done"
    first, second = model.payloads
    mark = {"type": "ephemeral"}
    assert first["cache_control"] == second["cache_control"] == mark
    assert first["system"][0]["cache_control"] == mark and len(first["system"]) == 2
    assert "cache_control" not in first["system"][1]
    assert json.dumps([first["tools"], first["system"]]) == json.dumps(
        [second["tools"], second["system"]]
    )
    assert not any("cache_control" in t for t in first["tools"])
    n = len(first["messages"])
    assert json.dumps(second["messages"][:n]) == json.dumps(first["messages"])
    assert len(second["messages"]) == n + 2


def test_the_next_turn_reads_the_cached_conversation():
    """서비스는 턴마다 세션 문서에서 루프를 다시 짓는다(`messages_from_dict`, 저장한 묶음). 같은
    화면의 다음 턴 첫 호출은 앞 턴 마지막 호출의 Tool 정의, 시스템, 메시지를 바이트 그대로 앞부분에
    둔다. 그래야 다듬기 턴이 앞 대화를 캐시에서 읽는다 (T80). 모든 요청의 캐시 표지는 둘이다
    (허용은 넷, Tool 정의에는 없다)."""
    call = {"name": "use_tools", "args": '{"groups": ["agent"]}', "id": "c1", "index": 0}
    model = RecordingClaude(
        model="claude-sonnet-5-5",
        api_key="k",
        replies=[
            AIMessageChunk(content="", tool_call_chunks=[call]),
            AIMessageChunk(content="첫 턴"),
            AIMessageChunk(content="둘째 턴"),
        ],
    )
    here = {"route": "/agents"}
    first_loop, _ = make_loop(FakeExecutor({}), model=model)
    assert asyncio.run(first_loop.run_message(ctx(), "Agent 를 만들어 줘", here)).kind == "done"
    stored = json.loads(json.dumps([message_to_dict(m) for m in first_loop.messages]))
    next_loop, _ = make_loop(
        FakeExecutor({}),
        model=model,
        messages=messages_from_dict(stored),
        tool_groups=set(first_loop.tool_groups),
    )
    assert asyncio.run(next_loop.run_message(ctx(), "이름을 바꿔 줘", here)).kind == "done"
    last, following = model.payloads[1], model.payloads[2]
    assert json.dumps([last["tools"], last["system"]]) == json.dumps(
        [following["tools"], following["system"]]
    )
    n = len(last["messages"])
    assert json.dumps(following["messages"][:n]) == json.dumps(last["messages"])
    assert all(json.dumps(p).count("cache_control") == 2 for p in model.payloads)


def test_other_models_carry_no_cache_marker(monkeypatch):
    """스텁처럼 Claude 가 아닌 모델에는 표지가 없고 시스템은 지금처럼 문자열 하나다."""
    systems: list[Any] = []
    reply = AssistantStub.reply

    def recording(self, messages):
        systems.append(messages[0].content)
        return reply(self, messages)

    monkeypatch.setattr(AssistantStub, "reply", recording)
    loop, _ = make_loop(FakeExecutor({}))
    assert asyncio.run(loop.run_message(ctx(), "안녕", {"route": "/agents"})).kind == "done"
    assert loop._cache is None
    assert systems and all(isinstance(c, str) and c.endswith("화면을 보고 있다.") for c in systems)


def test_calling_a_tool_of_a_closed_group_opens_it_and_bad_group_names_are_errors():
    ex = FakeExecutor({})
    loop, _ = make_loop(ex)
    # 열지 않은 묶음의 도구를 이름으로 부르면 그 묶음을 열고 그대로 실행한다
    assert (
        asyncio.run(loop.run_message(ctx(), steps(tool("list_mcp_servers"), {"say": "x"}))).kind
        == "done"
    )
    assert loop.tool_groups == {"mcp"} and [c[0] for c in ex.calls] == ["list_mcp_servers"]
    # 모르는 묶음은 결과로 알린다. 열린 묶음은 그대로다
    # 세션이 이미 연 묶음(agent)은 첫 호출부터 실린다
    loop, _ = make_loop(FakeExecutor({}), tool_groups={"agent"})
    names = {d.name for d in loop.active_defs()}
    assert {"create_agent", "use_tools"} <= names and "list_mcp_servers" not in names
    script = steps(tool("use_tools", groups=["nope"]), {"say": "x"})
    assert asyncio.run(loop.run_message(ctx(), script)).kind == "done"
    bad = next(m for m in loop.messages if isinstance(m, ToolMessage))
    assert bad.status == "error" and "unknown_group" in bad.content
    assert loop.tool_groups == {"agent"}


def test_read_and_deploy_in_one_ai_message_runs_read_and_interrupts_on_deploy():
    ex = FakeExecutor({})
    loop, events = make_loop(ex)
    script = steps(
        {
            "tools": [
                tool("list_clusters"),
                tool("deploy_mcp_server", server_id="p"),
            ]
        },
        {"say": "끝 $1.ok"},
    )
    outcome = asyncio.run(loop.run_message(ctx(), script))
    assert outcome.kind == "interrupt"
    names = [e for e, _ in events]
    assert names == ["tool_call", "tool_result", "checkpoint", "interrupt"]
    assert [c[0] for c in ex.calls] == ["list_clusters"]
    interrupt = events[-1][1]
    assert interrupt["kind"] == "confirm" and interrupt["session_id"] == "s"
    assert interrupt["action_requests"] == [
        {
            "name": "deploy_mcp_server",
            "args": {"server_id": "p"},
            # 모델에게 주는 뒤 문장(exposure 규칙)은 빼고 첫 문장만 사람에게 보인다
            "description": "draft 인 MCP Server 를 클러스터에 배포한다.",
        }
    ]
    assert interrupt["review_configs"] == [
        {"action_name": "deploy_mcp_server", "allowed_decisions": ["approve", "reject"]}
    ]
    assert loop.pending is not None and loop.pending.kind == "confirm"
    (pending_call,) = loop.pending.calls
    assert pending_call.name == "deploy_mcp_server" and pending_call.risk == "deploy"
    # 문맥: Human, AI(둘), Tool(list_clusters) 하나. 승인 대기 id 는 아직 답이 없다
    kinds = [type(m).__name__ for m in loop.messages]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage"]
    assert loop.transcript[-1]["kind"] == "interrupt" and loop.transcript[-1]["resolved"] is False

    # 승인 → 실행 → say. 결정 뒤 그 호출의 tool_call·tool_result 가 난다
    events.clear()
    outcome = asyncio.run(loop.run_decisions(ctx(), [Decision(id=pending_call.id, type="approve")]))
    assert outcome.kind == "done"
    assert [e for e, _ in events] == ["tool_call", "tool_result", "token", "done"]
    assert ex.calls[-1][0] == "deploy_mcp_server" and ex.calls[-1][2] == 2
    assert events[-1][1]["text"] == "끝 true" and events[-1][1]["tool_calls"] == 2
    assert loop.pending is None and loop.transcript[2]["resolved"] is True
    assert (
        isinstance(loop.messages[3], ToolMessage)
        and loop.messages[3].tool_call_id == pending_call.id
    )

    # confirm_deploy=false 면 deploy 는 바로 실행되고 destructive 는 여전히 승인 뒤다
    loop2, events2 = make_loop(FakeExecutor({}), confirm_deploy=False)
    script = steps(
        {
            "tools": [
                tool("deploy_mcp_server", server_id="p"),
                tool("delete_mcp_server", server_id="i"),
            ]
        },
        {"say": "x"},
    )
    assert asyncio.run(loop2.run_message(ctx(), script)).kind == "interrupt"
    assert [e for e, _ in events2] == ["tool_call", "tool_result", "checkpoint", "interrupt"]
    assert loop2.pending.calls[0].name == "delete_mcp_server"
    # 한 문장뿐인 설명은 그대로 보인다
    (request,) = events2[-1][1]["action_requests"]
    assert request["description"] == "MCP Server 의 정의와 클러스터의 것을 모두 지운다."


def test_a_fetched_file_reference_forces_approval_even_when_confirm_deploy_is_off():
    """가져온 데이터가 얹힌 쓰기는 confirm_deploy=false 에서도 승인 카드를 거친다(D31).

    read 등급(`verify_spec`)은 부작용이 없어 그대로 돈다. 카드에는 그 호출이 심는 파일 이름과
    출처 URL 이 실린다."""
    ex = FakeExecutor({})
    loop, events = make_loop(ex, confirm_deploy=False)
    loop.files = {
        "fetched-1.wsdl": FetchedFile(text="<definitions/>", url="https://api.example.com/c.wsdl"),
        "fetched-2.yaml": FetchedFile(text="openapi: 3.0.0\n"),  # 옛 세션이라 URL 이 없다
    }
    script = steps(
        {
            "tools": [
                tool("verify_spec", text="$file:fetched-1.wsdl", filename="c.wsdl"),
                tool(
                    "create_backend_api",
                    name="calc",
                    spec_text="$file:fetched-1.wsdl",
                    filename="c.wsdl",
                ),
                tool(
                    "create_backend_api",
                    name="orders",
                    spec_text="$file:fetched-2.yaml",
                    filename="orders.yaml",
                ),
            ]
        },
        {"say": "끝"},
    )
    assert asyncio.run(loop.run_message(ctx(), script)).kind == "interrupt"
    # verify_spec 만 바로 돌고 나머지 둘은 승인 대기다
    assert [c[0] for c in ex.calls] == ["verify_spec"]
    interrupt = events[-1][1]
    assert [r["name"] for r in interrupt["action_requests"]] == [
        "create_backend_api",
        "create_backend_api",
    ]
    # args 의 참조는 그대로 남아 있고(실행기가 실행 직전에 되살린다) 카드가 출처를 안다
    assert interrupt["action_requests"][0]["args"]["spec_text"] == "$file:fetched-1.wsdl"
    assert interrupt["action_requests"][0]["files"] == [
        {"name": "fetched-1.wsdl", "url": "https://api.example.com/c.wsdl"}
    ]
    assert interrupt["action_requests"][1]["files"] == [{"name": "fetched-2.yaml"}]
    # 등급 자체는 표의 값 그대로다. 승인을 부른 것은 참조이지 등급이 아니다
    assert [c.risk for c in loop.pending.calls] == ["write", "write"]
    # 승인하면 그대로 실행된다
    ids = [c.id for c in loop.pending.calls]
    decisions = [Decision(id=i, type="approve") for i in ids]
    assert asyncio.run(loop.run_decisions(ctx(), decisions)).kind == "done"
    assert [c[0] for c in ex.calls] == [
        "verify_spec",
        "create_backend_api",
        "create_backend_api",
    ]
    # 참조가 없는 write 는 confirm_deploy=false 에서 예전대로 바로 돈다
    loop2, events2 = make_loop(FakeExecutor({}), confirm_deploy=False)
    script2 = steps(tool("create_mcp_server", name="p"), {"say": "x"})
    assert asyncio.run(loop2.run_message(ctx(), script2)).kind == "done"
    assert [e for e, _ in events2] == ["tool_call", "tool_result", "token", "done"]


def test_ask_user_takes_precedence_and_defers_approvals_then_answer_becomes_tool_message():
    ex = FakeExecutor({})
    loop, events = make_loop(ex)
    script = steps(
        {
            "tools": [
                tool(
                    "ask_user",
                    question="어느 클러스터?",
                    options=["edge", "home"],
                    field="cluster_id",
                ),
                tool("deploy_mcp_server", server_id="p"),
                tool("list_clusters"),
            ]
        },
        # $n 은 ToolMessage 순서다. 바로 실행(목록), deferred(배포), 사용자의 답 순으로 붙는다
        {"say": "답 $2.answer, 배포 $1.code, 목록 $0.ok"},
    )
    outcome = asyncio.run(loop.run_message(ctx(), script))
    assert outcome.kind == "interrupt"
    names = [e for e, _ in events]
    # 읽기는 바로, 승인 대상은 deferred 로 닫고, 질문만 낸다
    assert names == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "checkpoint",
        "interrupt",
    ]
    assert events[0][1]["name"] == "list_clusters"
    assert events[2][1]["name"] == "deploy_mcp_server"
    assert events[3][1]["status"] == "error" and "deferred" in events[3][1]["content"]
    interrupt = events[-1][1]
    assert interrupt["kind"] == "question" and interrupt["action_requests"] == []
    assert interrupt["question"] == {
        "text": "어느 클러스터?",
        "options": ["edge", "home"],
        "field": "cluster_id",
    }
    assert loop.pending.kind == "question" and loop.pending.calls[0].name == "ask_user"
    # 결정 대신 새 메시지가 답이다. HumanMessage 가 늘지 않고 ToolMessage 가 붙는다
    events.clear()
    outcome = asyncio.run(loop.run_message(ctx(), "edge"))
    assert outcome.kind == "done"
    humans = [m for m in loop.messages if isinstance(m, HumanMessage)]
    assert len(humans) == 1
    answer = next(m for m in loop.messages if isinstance(m, ToolMessage) and m.name == "ask_user")
    assert json.loads(answer.content) == {"answer": "edge"}
    assert events[-1][1]["text"] == "답 edge, 배포 deferred, 목록 true"
    assert loop.transcript[0] == {"kind": "user", "text": script}
    assert {"kind": "user", "text": "edge"} in loop.transcript
    # 다음 모델 호출 전에는 그 AIMessage 의 모든 tool_call id 에 ToolMessage 가 있었다
    ai = next(m for m in loop.messages if isinstance(m, AIMessage) and m.tool_calls)
    answered = {m.tool_call_id for m in loop.messages if isinstance(m, ToolMessage)}
    assert {c["id"] for c in ai.tool_calls} <= answered


def test_superseded_reject_and_401_close_the_turn_correctly():
    ex = FakeExecutor({})
    loop, events = make_loop(ex)
    script = steps(tool("deploy_mcp_server", server_id="p"), {"say": "s $0.code"})
    assert asyncio.run(loop.run_message(ctx(), script)).kind == "interrupt"
    waiting = loop.pending.calls[0].id
    # 승인 없이 새 메시지: 대기 전부를 rejected(superseded) 로 닫은 뒤 새 HumanMessage
    events.clear()
    assert asyncio.run(loop.run_message(ctx(), steps({"say": "새 말"}))).kind == "done"
    assert [e for e, _ in events][:2] == ["tool_call", "tool_result"]
    superseded = json.loads(events[1][1]["content"])
    assert superseded["code"] == "rejected" and "superseded" in superseded["detail"]
    closing = next(
        m for m in loop.messages if isinstance(m, ToolMessage) and m.tool_call_id == waiting
    )
    assert closing.status == "error"
    assert [m for m in loop.messages if isinstance(m, HumanMessage)][-1].content == steps(
        {"say": "새 말"}
    )
    assert loop.pending is None
    # reject 는 사유가 ToolMessage 가 되고 모델이 그것을 본다
    loop, events = make_loop(ex)
    assert asyncio.run(loop.run_message(ctx(), script)).kind == "interrupt"
    call_id = loop.pending.calls[0].id
    events.clear()
    outcome = asyncio.run(
        loop.run_decisions(ctx(), [Decision(id=call_id, type="reject", message="아직은 아니다")])
    )
    assert outcome.kind == "done"
    assert events[-1][1]["text"] == "s rejected" + LAST_ERROR_FORMAT.format(code="rejected")
    assert "아직은 아니다" in events[1][1]["content"]
    assert [c[0] for c in ex.calls] == []
    # 401 은 되먹이지 않고 턴을 닫는다. 남은 호출도 ToolMessage 로 닫힌다
    ex = FakeExecutor(
        {
            "list_clusters": ToolResult(
                "error", {"status": 401, "code": "unauthorized", "detail": "x"}, http_status=401
            )
        }
    )
    loop, events = make_loop(ex)
    script = steps({"tools": [tool("list_clusters"), tool("list_hub_items")]}, {"say": "never"})
    outcome = asyncio.run(loop.run_message(ctx(), script))
    assert outcome.kind == "error" and outcome.code == "unauthorized"
    assert [e for e, _ in events] == ["tool_call", "tool_result", "error"]
    assert [c[0] for c in ex.calls] == ["list_clusters"]
    ai = loop.messages[1]
    answered = {m.tool_call_id for m in loop.messages if isinstance(m, ToolMessage)}
    assert {c["id"] for c in ai.tool_calls} == answered


def test_tool_limit_closes_calls_and_timeout_closes_open_calls():
    ex = FakeExecutor({})
    loop, events = make_loop(ex, limits=Limits(max_tool_calls=2, timeout=5))
    script = steps(
        tool("list_clusters"), tool("list_clusters"), tool("list_clusters"), {"say": "x"}
    )
    outcome = asyncio.run(loop.run_message(ctx(), script))
    assert outcome.kind == "error" and outcome.code == "tool_limit"
    assert [e for e, _ in events] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "error",
    ]
    assert len(ex.calls) == 2
    ai = [m for m in loop.messages if isinstance(m, AIMessage)][-1]
    answered = {m.tool_call_id for m in loop.messages if isinstance(m, ToolMessage)}
    assert {c["id"] for c in ai.tool_calls} <= answered

    class Slow(FakeExecutor):
        async def call(self, *a, **kw):
            await asyncio.sleep(5)
            return await super().call(*a, **kw)

    loop, events = make_loop(Slow({}), limits=Limits(timeout=0.2))
    outcome = asyncio.run(loop.run_message(ctx(), steps(tool("list_clusters"), {"say": "x"})))
    assert outcome.kind == "error" and outcome.code == "turn_timeout"
    assert [e for e, _ in events] == ["tool_call", "error"]
    # 끊긴 호출은 turn_timeout ToolMessage 로 닫혀 다음 턴의 문맥이 provider 계약을 지킨다
    last = loop.messages[-1]
    assert isinstance(last, ToolMessage) and json.loads(last.content)["code"] == "turn_timeout"


def test_context_trimming_cuts_only_before_a_human_message():
    loop, _ = make_loop(FakeExecutor({}))
    turns = KEEP_HUMANS + 2
    for i in range(turns):
        loop.messages.append(HumanMessage(f"질문 {i}"))
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "list_x", "args": {}, "id": f"c{i}", "type": "tool_call"}],
        )
        loop.messages += [ai, result({"i": i}, f"c{i}", "list_x"), AIMessage(f"답 {i}")]
    before = len(loop.messages)
    asyncio.run(loop._trim())
    assert is_summary(loop.messages[0])
    assert (
        loop.messages[0].content.startswith(SUMMARY_PREFIX)
        and SUMMARY_TEXT in loop.messages[0].content
    )
    # 남은 구간은 최근 HumanMessage 12개부터이고 첫 메시지가 HumanMessage 다
    assert isinstance(loop.messages[1], HumanMessage) and loop.messages[1].content == "질문 2"
    humans = [m for m in loop.messages if isinstance(m, HumanMessage) and not is_summary(m)]
    assert len(humans) == KEEP_HUMANS
    assert len(loop.messages) == before - 2 * 4 + 1
    # AIMessage 와 ToolMessage 사이는 잘리지 않았다
    for i, m in enumerate(loop.messages):
        if isinstance(m, AIMessage) and m.tool_calls:
            nxt = loop.messages[i + 1]
            assert isinstance(nxt, ToolMessage) and nxt.tool_call_id == m.tool_calls[0]["id"]
    # 요약 호출도 usage 에 센다. 다시 잘라도 그대로다(요약 HumanMessage 는 세지 않는다)
    assert loop.usage.total_tokens > 0
    n = len(loop.messages)
    asyncio.run(loop._trim())
    assert len(loop.messages) == n
    # 12개 이하면 자르지 않는다
    loop2, _ = make_loop(FakeExecutor({}))
    loop2.messages = [HumanMessage(str(i)) for i in range(KEEP_HUMANS)]
    asyncio.run(loop2._trim())
    assert len(loop2.messages) == KEEP_HUMANS and not any(is_summary(m) for m in loop2.messages)


# -- 비밀 마스킹 ------------------------------------------------------------------------------


def _secret_tool_defs() -> list[Any]:
    """비밀 필드를 가진 write 도구 하나와 deploy 도구 하나 (가짜 OpenAPI)."""
    body = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "auth": {"type": "object", "properties": {"password": {"type": "string"}}},
        },
    }
    openapi = {
        "paths": {
            "/api/things": {
                "post": {"requestBody": {"content": {"application/json": {"schema": body}}}}
            },
            "/api/things/deploy": {
                "post": {"requestBody": {"content": {"application/json": {"schema": body}}}}
            },
        },
        "components": {"schemas": {}},
    }
    catalog = Catalog(
        tools=(
            ToolSpec(
                "create_thing",
                "POST",
                "/api/things",
                "write",
                "x",
                secret_fields=("/auth/password",),
            ),
            ToolSpec(
                "deploy_thing",
                "POST",
                "/api/things/deploy",
                "deploy",
                "x",
                secret_fields=("/auth/password",),
            ),
        ),
        excluded={},
    )
    return build_tool_schemas(openapi, catalog)


def test_secrets_are_masked_in_events_context_and_pending_but_executor_sees_originals():
    ex = FakeExecutor({})
    loop, events = make_loop(ex, tool_defs=_secret_tool_defs())
    script = steps(
        tool("create_thing", name="a", auth={"password": "pw-immediate"}),
        tool("deploy_thing", name="b", auth={"password": "pw-pending"}),
        {"say": "끝"},
    )
    outcome = asyncio.run(loop.run_message(ctx(), script))
    assert outcome.kind == "interrupt"
    # 바로 실행: 실행기는 원본, 이벤트·문맥은 마스킹
    assert ex.calls[0] == ("create_thing", {"name": "a", "auth": {"password": "pw-immediate"}}, 1)
    assert events[0][0] == "tool_call"
    assert events[0][1]["args"] == {"name": "a", "auth": {"password": "***"}}
    first_ai = loop.messages[1]
    assert isinstance(first_ai, AIMessage)
    assert first_ai.tool_calls[0]["args"] == {"name": "a", "auth": {"password": "***"}}
    # 승인 대기: pending·interrupt·전사 어디에도 원문이 없다
    (call,) = loop.pending.calls
    assert call.args == {"name": "b", "auth": {"password": "***"}}
    interrupt = events[-1][1]
    assert interrupt["action_requests"][0]["args"] == {"name": "b", "auth": {"password": "***"}}
    # 사용자 메시지 자체(user 항목·HumanMessage)는 그대로 남는 한계가 있어 그것만 뺀다
    dumped = json.dumps(
        [it for it in loop.transcript if it["kind"] != "user"], ensure_ascii=False
    ) + json.dumps(
        [m.model_dump() for m in loop.messages if not isinstance(m, HumanMessage)],
        ensure_ascii=False,
        default=str,
    )
    assert "pw-immediate" not in dumped and "pw-pending" not in dumped
    # 승인하면 실행기는 마스킹된 값을 받는다. 원문은 문서에 없으므로 되살릴 수 없고, 실제 실행기는
    # *** 도 비밀로 판정해 secret_in_chat 으로 거부한다 (test_assistant_tools)
    events.clear()
    outcome = asyncio.run(loop.run_decisions(ctx(), [Decision(id=call.id, type="approve")]))
    assert outcome.kind == "done"
    assert ex.calls[-1][1] == {"name": "b", "auth": {"password": "***"}}


def test_masked_ai_message_covers_tool_calls_tool_use_blocks_and_openai_kwargs():
    def mask(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {**args, "password": "***"} if "password" in args else args

    ai = AIMessage(
        content=[
            {"type": "text", "text": "만든다"},
            {"type": "tool_use", "id": "c1", "name": "t", "input": {"password": "pw", "n": 1}},
        ],
        tool_calls=[
            {"name": "t", "args": {"password": "pw", "n": 1}, "id": "c1", "type": "tool_call"}
        ],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "t", "arguments": '{"password": "pw", "n": 1}'},
                }
            ]
        },
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    out = masked_ai_message(ai, mask)
    assert out.tool_calls[0]["args"] == {"password": "***", "n": 1}
    assert (
        out.content[1]["input"] == {"password": "***", "n": 1} and out.content[0] == ai.content[0]
    )
    assert json.loads(out.additional_kwargs["tool_calls"][0]["function"]["arguments"]) == {
        "password": "***",
        "n": 1,
    }
    assert out.usage_metadata == ai.usage_metadata
    # 깨진 arguments 원문은 비밀 자리를 골라낼 수 없어 통째로 가린다
    broken = ai.model_copy(
        update={
            "additional_kwargs": {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": '{"password": "pw'},
                    }
                ]
            }
        }
    )
    assert (
        masked_ai_message(broken, mask).additional_kwargs["tool_calls"][0]["function"]["arguments"]
        == "***"
    )
    assert "pw" not in json.dumps(out.model_dump(), default=str)
    # 원본은 그대로다. 도구 호출이 없으면 같은 객체다
    assert ai.tool_calls[0]["args"]["password"] == "pw"
    plain = AIMessage("답")
    assert masked_ai_message(plain, mask) is plain


def test_invalid_tool_call_without_id_gets_no_tool_message():
    loop, _ = make_loop(FakeExecutor({}))
    loop._close_invalid({"name": "x", "args": "{", "error": "bad json", "id": None})
    assert loop.messages == []
    loop._close_invalid({"name": "x", "args": "{", "error": "bad json", "id": "c9"})
    assert isinstance(loop.messages[-1], ToolMessage) and loop.messages[-1].tool_call_id == "c9"


# -- 배선 자기 점검 ----------------------------------------------------------------------------


def test_masking_covers_streamed_partial_json_and_invalid_calls() -> None:
    """Anthropic 스트리밍은 tool_use 블록의 인자를 `partial_json` 문자열로 모은다. 깨진 인자는
    invalid_tool_calls 에 원문이 남는다. 둘 다 저장 전에 가려져야 한다."""
    from langchain_core.messages import AIMessage

    from daegam.services.assistant.loop import masked_ai_message

    def mask(name: str, args: dict) -> dict:
        return {k: ("***" if k == "password" else v) for k, v in args.items()}

    ai = AIMessage(
        content=[
            {
                "type": "tool_use",
                "id": "t1",
                "name": "create_data_service",
                "input": {},
                "partial_json": '{"name": "db", "password": "hunter2"}',
            },
            {"type": "tool_use", "id": "t2", "name": "x", "input": {}, "partial_json": "{broken"},
        ],
        tool_calls=[
            {
                "id": "t1",
                "name": "create_data_service",
                "args": {"name": "db", "password": "hunter2"},
            }
        ],
        invalid_tool_calls=[
            {"id": "t2", "name": "x", "args": '{"password": "hunter2"', "error": "e"}
        ],
    )
    out = masked_ai_message(ai, mask)
    dumped = json.dumps(out.model_dump(), ensure_ascii=False)
    assert "hunter2" not in dumped
    assert out.content[0]["input"] == {"name": "db", "password": "***"}
    assert "partial_json" not in out.content[0] and out.content[1]["input"] == {"_": "***"}
    assert out.invalid_tool_calls[0]["args"] == "***"
