"""컴파일: 압축 정책 교체, 툴 한도, HITL, general-purpose 없음, 파일 툴 숨김, 서브에이전트와 그 턴
제한, 응답 형식의 title, 내장 도구와 이름이 겹친 소스 도구 빼기."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import deepagents.graph as deep_graph
import deepagents.middleware.subagents as deep_subagents
import pytest
from deepagents.middleware import UnsupportedContentMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY
from langchain.agents.middleware import HumanInTheLoopMiddleware, ToolCallLimitMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

import runtime.compile as compile_module
from runtime.compile import (
    BUILTIN_TOOL_NAME,
    SUMMARIZATION_NAME,
    TOOL_EXCLUSION_NAME,
    CompiledAgent,
    builtin_names,
    compile_agent,
    wrapped_middleware_names,
)
from runtime.config import ConfigError, parse_config
from runtime.models import BLOCK_BINDING, THINKING_BINDING_BETA, build_chat_model
from runtime.resilience import SUBAGENT_TIMEOUT_TEXT, SubagentTimeout
from runtime.stream import (
    INTERRUPT,
    TOOL_RESULT,
    TURN_FAILED,
    TurnState,
    snapshot_messages,
    stream_turn,
    thread_config,
)
from runtime.stub import SUMMARY_TEXT, StubChatModel
from runtime.tool_limit import TurnToolLimit
from runtime.tool_names import (
    BUILTIN_TOOLS,
    CODEX_MODELS,
    FILESYSTEM_TOOLS,
    STRUCTURED_RESPONSE_TOOL,
    builtin_tools_for,
)
from tests.app_fixtures import TOOLS, tool_message, with_subagent
from tests.conftest import events_named, load_fixture, offline_doc, run_app


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """deepagents 가 create_agent 에 넘기는 미들웨어 스택을 잡는다."""
    box: dict[str, Any] = {}
    original = deep_graph.create_agent

    def spy(model, **kwargs):
        box["model"] = model
        box["middleware"] = list(kwargs.get("middleware") or [])
        box["tools"] = list(kwargs.get("tools") or [])
        box["system_prompt"] = kwargs.get("system_prompt")
        box["response_format"] = kwargs.get("response_format")
        return original(model, **kwargs)

    monkeypatch.setattr(deep_graph, "create_agent", spy)

    class SpySubAgentMiddleware(SubAgentMiddleware):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            box["subagent_specs"] = list(kwargs.get("subagents") or [])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(deep_graph, "SubAgentMiddleware", SpySubAgentMiddleware)
    return box


def _by_name(middleware: list[Any], name: str) -> list[Any]:
    return [m for m in middleware if m.name == name]


def _policy(summary: Any) -> tuple[Any, Any]:
    """deepagents 요약 미들웨어는 langchain 미들웨어를 _lc_helper 로 감싼다. 정책은 거기 있다."""
    helper = getattr(summary, "_lc_helper", summary)
    return helper.trigger, helper.keep


def test_support_agent_compiles_with_policy(captured) -> None:
    doc = offline_doc()
    # 파일 도구는 기본이 노출이다. 아래의 숨김 미들웨어 단정을 보려면 여기서 꺼야 한다
    doc["main"]["builtin_tools"]["filesystem"] = False
    config = parse_config(doc)
    agent = compile_agent(config, tools_by_ref=TOOLS, checkpointer=InMemorySaver())

    assert agent.harness_key == "stub:stub"
    assert agent.turn_timeout == 120.0
    assert agent.interrupt_on == {"Divide": {"allowed_decisions": ["approve", "reject"]}}
    assert [t.name for t in captured["tools"]] == ["Add", "Divide"]
    assert "Always respond in the language whose code is 'ko'" in captured["system_prompt"]

    stack = captured["middleware"]
    summaries = _by_name(stack, SUMMARIZATION_NAME)
    assert len(summaries) == 1, [m.name for m in stack]
    assert _policy(summaries[0]) == (("fraction", 0.85), ("fraction", 0.1))
    # 요약 입력을 자르지 않는다. 생성자 기본 4000 은 요약 대상의 끝만 요약한다
    assert summaries[0]._lc_helper.trim_tokens_to_summarize is None

    limits = [m for m in stack if isinstance(m, ToolCallLimitMiddleware)]
    assert len(limits) == 1 and limits[0].run_limit == 8
    # 처음 넘으면 모델이 답하게 하는 한도다(T82). 그래프의 노드 이름은 langchain 의 것 그대로다
    assert isinstance(limits[0], TurnToolLimit) and limits[0].name == "ToolCallLimitMiddleware"

    names = [m.name for m in stack]
    hitl = names.index(HumanInTheLoopMiddleware.__name__)
    # 모델 프로필이 거부하는 미디어 블록을 바꾸는 미들웨어는 하나이고 HITL 뒤다. 우리 미들웨어
    # (가드레일·예산·폴백·재시도)는 여전히 HITL 앞이라 wrap 훅에서 바깥이다
    assert names.count(UnsupportedContentMiddleware.__name__) == 1
    assert names.index(UnsupportedContentMiddleware.__name__) > hitl
    ours = [i for i, name in enumerate(names) if name.startswith("Daegam")]
    assert ours and all(i < hitl for i in ours), names
    # 서브에이전트가 없고 general-purpose 를 껐으므로 task 도구도, 서브 턴 제한도 없다
    assert not any(isinstance(m, SubAgentMiddleware | SubagentTimeout) for m in stack)
    # 파일 도구를 숨기는 미들웨어가 스택과 컴파일 결과 그래프 양쪽에 있다(기동 시 자기 점검이
    # 보는 것이다). 실제로 모델에 안 보이는지는 test_stub_turn_sees_only_mcp_tools 가 본다
    assert [m.name for m in stack if m.name == TOOL_EXCLUSION_NAME] == [TOOL_EXCLUSION_NAME]
    assert TOOL_EXCLUSION_NAME in wrapped_middleware_names(agent.graph)


def test_harness_profile_mismatch_blocks_startup(captured, monkeypatch) -> None:
    """프로필 키가 deepagents 의 키 유도와 어긋나면 파일 도구가 노출되므로 ConfigError 다."""
    doc = offline_doc()
    # 자기 점검은 파일 도구를 숨겨야 할 때만 돈다. 기본은 노출이므로 여기서 끈다
    doc["main"]["builtin_tools"]["filesystem"] = False
    # 같은 프로세스의 다른 테스트가 stub:stub 에 이미 걸어 둔 프로필을 타지 않게 이름을 따로 쓴다
    doc["main"]["model"]["name"] = "stub-mismatch"
    monkeypatch.setattr(compile_module, "harness_key", lambda model: "stub:wrong-key")
    with pytest.raises(ConfigError) as ei:
        compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    assert ei.value.code == "harness_profile_not_applied"
    assert "filesystem tools would be exposed" in str(ei.value)
    assert not [m for m in captured["middleware"] if m.name == TOOL_EXCLUSION_NAME]


def test_minimal_compiles_with_anthropic_model(captured, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config = parse_config(load_fixture("minimal.json"))
    agent = compile_agent(config, tools_by_ref={}, checkpointer=None)
    assert agent.harness_key == "anthropic:claude-opus-5"
    assert isinstance(captured["model"], ChatAnthropic)
    assert captured["system_prompt"] == "짧고 정확하게 답한다."
    assert not any(isinstance(m, HumanInTheLoopMiddleware) for m in captured["middleware"])
    assert [m for m in captured["middleware"] if isinstance(m, ToolCallLimitMiddleware)][
        0
    ].run_limit == 1


class _NoNetwork(Exception):
    """요청 본문을 잡은 뒤 실제 호출 대신 낸다. 4xx 가 아니라 재시도도 폴백도 없다."""


async def test_sonnet_55_json_schema_first_request_has_no_forced_tool_choice(monkeypatch) -> None:
    """Sonnet 5.5 는 langchain-anthropic 의 프로필이 구조화 출력을 밝혀 JSON 스키마 응답
    형식이 provider 의 output_config.format 으로 간다(P35 7절). 첫 모델 요청에 강제 tool_choice 가
    없고, 추론 인자는 adaptive 에 block_binding 과 그 베타다. 요청 본문은 잡기만 하고 안 보낸다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    doc = load_fixture("minimal.json")
    doc["main"]["model"]["name"] = "claude-sonnet-5-5"
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": schema}
    agent = compile_agent(parse_config(doc), tools_by_ref={}, checkpointer=InMemorySaver())
    payloads: list[dict[str, Any]] = []
    original = ChatAnthropic._get_request_payload

    def capture(self: ChatAnthropic, input_: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(original(self, input_, **kwargs))
        raise _NoNetwork("captured")

    monkeypatch.setattr(ChatAnthropic, "_get_request_payload", capture)
    _, state = await _turn(agent, "안녕")
    assert state.error is not None and state.error[0] == TURN_FAILED
    (first,) = payloads
    assert "tool_choice" not in first
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["output_config"]["effort"] == "low"
    assert first["thinking"] == {"type": "adaptive", "block_binding": BLOCK_BINDING}
    assert first["betas"] == [THINKING_BINDING_BETA]


def test_compaction_policy_variants(captured) -> None:
    doc = offline_doc()
    doc["main"]["compaction"] = {
        "trigger": {"kind": "tokens", "value": 50000},
        "keep": {"kind": "messages", "value": 12},
    }
    compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    summary = _by_name(captured["middleware"], SUMMARIZATION_NAME)[0]
    assert _policy(summary) == (("tokens", 50000), ("messages", 12))


def test_model_name_with_colons_compiles_and_hides_filesystem(captured) -> None:
    """파인튜닝 모델 이름(`ft:x:y`)처럼 `:` 가 든 이름도 컴파일된다. 프로필 키는 첫 `:` 만 구분자라
    deepagents 가 글자 그대로 맞추고, 파일 도구 숨김 검사가 지난다."""
    doc = offline_doc()
    doc["main"]["builtin_tools"]["filesystem"] = False
    doc["main"]["model"]["name"] = "ft:x:y"
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    assert agent.harness_key == "stub:ft:x:y"
    assert [m.name for m in captured["middleware"] if m.name == TOOL_EXCLUSION_NAME]
    assert TOOL_EXCLUSION_NAME in wrapped_middleware_names(agent.graph)


def test_filesystem_tools_visible_when_enabled(captured) -> None:
    doc = offline_doc()
    doc["main"]["builtin_tools"]["filesystem"] = True
    # harness profile 등록은 키별 합집합이라 다른 모델 이름으로 따로 건다
    doc["main"]["model"]["name"] = "stub-fs"
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    assert agent.harness_key == "stub:stub-fs"
    assert not [m for m in captured["middleware"] if m.name == "_ToolExclusionMiddleware"]


@pytest.mark.parametrize("title", [None, "Add"])
def test_response_format_json_schema_title_is_the_reserved_tool_name(captured, title) -> None:
    """스키마의 title 은 사용자가 무엇을 적었든 구조화 출력 도구의 예약 이름이다. 도구 전략에서
    langchain 이 그 이름으로 도구를 만들고 번역이 그 이름의 이벤트를 버리므로, 실제 도구 이름(Add)을
    적어도 그 도구의 이벤트를 버리는 일이 없다."""
    doc = offline_doc()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    if title is not None:
        schema["title"] = title
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": schema}
    compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    assert captured["response_format"] == {**schema, "title": STRUCTURED_RESPONSE_TOOL}
    assert STRUCTURED_RESPONSE_TOOL in BUILTIN_TOOLS


def test_subagent_is_isolated_and_only_ours(captured) -> None:
    doc = offline_doc()
    with_subagent(doc)
    doc["subagents"][0]["interrupt_on"] = {"Divide": ["approve"]}
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    assert agent.subagent_names == ["calc"]
    subs = [m for m in captured["middleware"] if isinstance(m, SubAgentMiddleware)]
    assert len(subs) == 1
    # 서브의 턴 제한은 메인 스택의 SubagentTimeout 이 이름마다 든다. 서브 스택에는 없다
    timeouts = [m for m in captured["middleware"] if isinstance(m, SubagentTimeout)]
    assert len(timeouts) == 1 and timeouts[0].timeouts == {"calc": 30.0}
    specs = captured["subagent_specs"]
    # general-purpose 는 꺼져 있어 우리 서브에이전트만 있다
    assert [spec["name"] for spec in specs] == ["calc"]
    spec = specs[0]
    assert spec["interrupt_on"] == {"Divide": {"allowed_decisions": ["approve"]}}
    assert spec.get("mode") == "isolated"
    assert [t.name for t in spec["tools"]] == ["Add", "Divide"]
    sub_summary = _by_name(list(spec["middleware"]), SUMMARIZATION_NAME)
    assert len(sub_summary) == 1 and _policy(sub_summary[0])[0] == ("fraction", 0.85)
    sub_limits = [m for m in spec["middleware"] if isinstance(m, ToolCallLimitMiddleware)]
    assert len(sub_limits) == 1 and sub_limits[0].run_limit == 2
    assert not any(isinstance(m, SubagentTimeout) for m in spec["middleware"])


async def test_subagent_turn_timeout_returns_an_error_tool_message() -> None:
    """서브의 turn_timeout_seconds 를 넘기면 서브 실행이 끊기고 메인은 status error 인 task 결과를
    받아 턴을 이어 간다. 메인의 턴 제한(120초)은 바깥 한도 그대로라 턴은 오류가 아니다."""
    doc = offline_doc()
    with_subagent(doc)
    doc["subagents"][0]["limits"]["turn_timeout_seconds"] = 1
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    # 서브의 스텁 모델이 5초를 기다린다(description 이 서브의 첫 사용자 말이다)
    task = {"tool": "task", "args": {"description": '{"sleep": 5}', "subagent_type": "calc"}}
    started = asyncio.get_running_loop().time()
    events, state = await _turn(agent, json.dumps(task))
    assert asyncio.get_running_loop().time() - started < 4
    expected = SUBAGENT_TIMEOUT_TEXT.format(name="calc", seconds=1)
    result = next(d for d in events_named(events, TOOL_RESULT) if d["name"] == "task")
    assert result["status"] == "error" and result["content"] == expected
    assert state.error is None and state.text == "결과: " + expected


async def test_stub_turn_sees_only_mcp_tools() -> None:
    doc = offline_doc()
    # 파일 도구는 기본이 노출이다. 숨기는 쪽(excluded_tools)을 보려면 여기서 꺼야 한다
    doc["main"]["builtin_tools"]["filesystem"] = False
    agent = compile_agent(parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver())
    result = await agent.graph.ainvoke(
        {"messages": [HumanMessage('{"tool": "Add", "args": {"a": 3, "b": 4}}')]},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert agent.model.bound_tools == ["Add", "Divide"]
    # 파일 도구 7종은 모델에 보이지 않는다(HarnessProfile 의 excluded_tools 가 걸린 결과다)
    assert set(agent.model.bound_tools).isdisjoint(FILESYSTEM_TOOLS)
    assert result["messages"][-1].content == "결과: 7"


def _calculator_with(*extra: BaseTool) -> dict[str, list[BaseTool]]:
    """계산기 소스가 내장 이름 넷(write_file, execute, task, write_todos)과 extra 를 더 주는
    `tools_by_ref` 다. 소스의 write_file 은 불리면 "written remotely" 를 돌려준다."""

    @tool("write_file")
    def remote_write_file(file_path: str, content: str) -> str:
        """원격 서버에 파일을 쓴다."""
        return "written remotely"

    @tool("execute")
    def remote_execute(command: str) -> str:
        """원격 DB 에 SQL 을 돌린다."""
        return "1 row affected"

    @tool("task")
    def remote_task(description: str) -> str:
        """원격 작업을 만든다."""
        return "created"

    @tool("write_todos")
    def remote_todos(items: str) -> str:
        """원격 할 일 목록에 적는다."""
        return "saved"

    named = [remote_write_file, remote_execute, remote_task, remote_todos]
    return {"tools-calculator": [*TOOLS["tools-calculator"], *named, *extra]}


def _codex(name: str = "gpt-5.2-codex") -> ChatOpenAI:
    """Codex 모델이다. deepagents 0.7.20 의 harness profile 이 할 일 목록 미들웨어를 붙인다."""
    model = ChatOpenAI(model=name, api_key="sk-test")
    model.profile = {**(model.profile or {}), "max_input_tokens": 200000}
    return model


def _builtin_warnings(agent: CompiledAgent) -> list[str]:
    return [w for w in agent.warnings if w.startswith(f"{BUILTIN_TOOL_NAME}: ")]


async def test_source_tools_named_like_built_ins_are_dropped() -> None:
    """서브에이전트가 없는 메인은 파일 도구와 execute 를 지키고, 내장이 아닌 task·write_todos 는
    소스의 것을 쓴다. 모델이 write_file 을 부르면 내장 도구가 세션 state 에 쓴다."""
    doc = offline_doc()
    # harness profile 등록은 키별 합집합이라 파일 도구가 보이는 모델 이름을 따로 쓴다
    doc["main"]["model"]["name"] = "stub-builtin-names"
    agent = compile_agent(
        parse_config(doc), tools_by_ref=_calculator_with(), checkpointer=InMemorySaver()
    )
    assert [t.name for t in agent.tools] == ["Add", "Divide", "task", "write_todos"]
    assert agent.warnings == [
        "builtin_tool_name: tool sources offer tools named like built-in tools (main: write_file "
        "from calc, execute from calc). The Agent keeps its built-in tools and does not offer "
        "these. Rename them at their source, or connect that source through a Gateway in prefix "
        "mode."
    ]
    assert "write_file" not in agent.tool_sources and "task" in agent.tool_sources
    events, state = await _turn(agent, tool_message("write_file", file_path="/a.txt", content="x"))
    assert state.error is None
    result = next(d for d in events_named(events, TOOL_RESULT) if d["name"] == "write_file")
    assert result["content"] != "written remotely"
    files = (await agent.graph.aget_state(thread_config("s1"))).values["files"]
    assert "/a.txt" in files


async def test_an_approval_on_a_dropped_name_holds_the_built_in() -> None:
    """뺀 이름에 둔 승인은 interrupt_on 에 남아 같은 이름의 내장 도구를 멈춘다. 승인이 느슨해지지
    않는다."""
    doc = offline_doc()
    doc["main"]["model"]["name"] = "stub-builtin-names"
    doc["tools"][0]["approval"]["write_file"] = ["approve", "reject"]
    agent = compile_agent(
        parse_config(doc), tools_by_ref=_calculator_with(), checkpointer=InMemorySaver()
    )
    assert agent.interrupt_on["write_file"] == {"allowed_decisions": ["approve", "reject"]}
    events, _ = await _turn(agent, tool_message("write_file", file_path="/a.txt", content="x"))
    [pending] = events_named(events, INTERRUPT)
    assert [a["name"] for a in pending["action_requests"]] == ["write_file"]
    files = (await agent.graph.aget_state(thread_config("s1"))).values.get("files") or {}
    assert "/a.txt" not in files


def test_task_is_built_in_only_where_subagents_are(captured) -> None:
    """서브에이전트가 있는 메인은 task 도 지킨다. 서브에이전트에는 task 가 내장이 아니라 소스의
    것이 남는다. 응답 형식이 있는 메인은 구조화 출력 도구 이름도 지킨다."""
    doc = offline_doc()
    with_subagent(doc)
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": {"type": "object"}}

    @tool(STRUCTURED_RESPONSE_TOOL)
    def remote_structured(answer: str) -> str:
        """원격 응답을 적는다."""
        return "ok"

    agent = compile_agent(
        parse_config(doc),
        tools_by_ref=_calculator_with(remote_structured),
        checkpointer=InMemorySaver(),
    )
    assert [t.name for t in agent.tools] == ["Add", "Divide", "write_todos"]
    sub_tools = [t.name for t in captured["subagent_specs"][0]["tools"]]
    assert sub_tools == ["Add", "Divide", "task", "write_todos", STRUCTURED_RESPONSE_TOOL]
    [warning] = _builtin_warnings(agent)
    assert warning.startswith(
        "builtin_tool_name: tool sources offer tools named like built-in tools (main: write_file "
        "from calc, execute from calc, task from calc, structured_response from calc; "
        "subagents[calc]: write_file from calc, execute from calc)."
    )


def test_codex_models_keep_their_write_todos() -> None:
    """Codex 모델은 harness profile 로 write_todos 를 내장으로 받으므로 소스의 write_todos 를 뺀다.
    같은 프로필이 없는 모델은 소스의 것을 쓴다."""
    codex = compile_agent(
        parse_config(offline_doc()),
        tools_by_ref=_calculator_with(),
        checkpointer=InMemorySaver(),
        model=_codex(),
    )
    assert [t.name for t in codex.tools] == ["Add", "Divide", "task"]
    [warning] = _builtin_warnings(codex)
    assert "write_todos from calc" in warning
    plain = compile_agent(
        parse_config(offline_doc()),
        tools_by_ref=_calculator_with(),
        checkpointer=InMemorySaver(),
        model=_codex("gpt-4o"),
    )
    assert "write_todos" in [t.name for t in plain.tools]


@pytest.fixture
def registered(monkeypatch) -> dict[str | None, set[str]]:
    """에이전트 이름(메인은 프로필 이름)마다 create_agent 가 받은 도구와 미들웨어 도구의 이름이다.
    deepagents 는 메인을 graph.py 에서, 서브에이전트를 subagents.py 에서 만든다."""
    box: dict[str | None, set[str]] = {}

    def spy(original: Any) -> Any:
        def create(model: Any, **kwargs: Any) -> Any:
            names = {t.name for t in kwargs.get("tools") or [] if isinstance(t, BaseTool)}
            for middleware in kwargs.get("middleware") or []:
                names.update(t.name for t in getattr(middleware, "tools", ()))
            box[kwargs.get("name")] = names
            return original(model, **kwargs)

        return create

    monkeypatch.setattr(deep_graph, "create_agent", spy(deep_graph.create_agent))
    monkeypatch.setattr(deep_subagents, "create_agent", spy(deep_subagents.create_agent))
    return box


@pytest.mark.parametrize("subagent", [False, True])
@pytest.mark.parametrize("codex", [False, True])
def test_builtin_names_match_what_deepagents_registers(registered, subagent, codex) -> None:
    """소스가 없을 때 각 에이전트가 실제로 받는 도구가 `builtin_names` 와 같다. deepagents 가 도구를
    붙이는 미들웨어를 더하거나 이름을 바꾸면 여기서 깨진다."""
    doc = offline_doc()
    if subagent:
        with_subagent(doc)
    config = parse_config(doc)
    model = _codex() if codex else None
    agent = compile_agent(config, tools_by_ref={}, checkpointer=InMemorySaver(), model=model)
    main = builtin_names(agent.model, subagents=subagent, structured=False)
    assert registered[config.profile["name"]] == main
    assert ("write_todos" in main) is codex
    if subagent:
        # 서브에이전트의 모델은 문서의 스텁이다(model 인자는 메인만 바꾼다)
        stub = build_chat_model(doc["subagents"][0]["model"])
        assert registered["calc"] == builtin_names(stub, subagents=False, structured=False)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("subagents", [False, True])
@pytest.mark.parametrize("codex", [False, True])
def test_builtin_names_match_the_shared_name_list(subagents, structured, codex) -> None:
    """`tool_names.builtin_tools_for` 는 플랫폼이 저장 앞에서 경고할 때 쓰는 이름의 정본이다
    (플랫폼 `toolnames.builtin_tool_names` 가 같은 결과를 내는지는 플랫폼 테스트가 본다). 실제
    deepagents 스택에서 읽는 `builtin_names` 가 모든 조합에서 같아야 한다."""
    model = _codex() if codex else _codex("gpt-4o")
    assert builtin_names(model, subagents=subagents, structured=structured) == builtin_tools_for(
        subagents=subagents, structured=structured, todos=codex
    )


def test_only_the_codex_profiles_add_middleware_tools() -> None:
    """deepagents 에 등록된 harness profile 가운데 미들웨어 도구를 더하는 것은 Codex 이름뿐이다.
    플랫폼이 write_todos 를 내장으로 세는 모델이 `CODEX_MODELS` 이므로, 도구를 더하는 새 profile
    이 생기면 여기서 깨지고 플랫폼 목록도 고쳐야 한다."""
    from deepagents.profiles.harness.harness_profiles import (
        _HARNESS_PROFILES,
        _ensure_harness_profiles_loaded,
    )

    _ensure_harness_profiles_loaded()
    with_tools = {
        key: sorted(
            tool.name
            for middleware in profile.materialize_extra_middleware()
            for tool in getattr(middleware, "tools", ())
        )
        for key, profile in _HARNESS_PROFILES.items()
    }
    with_tools = {key: names for key, names in with_tools.items() if names}
    assert with_tools == {f"openai:{name}": ["write_todos"] for name in CODEX_MODELS}
    for name in CODEX_MODELS:
        assert "write_todos" in builtin_names(_codex(name), subagents=False, structured=False)


class FakeKnowledge:
    """KnowledgeTools 의 tools_for 만 흉내 낸다. 검색 도구는 아무 BaseTool 이나 된다."""

    def __init__(self, by_ref: dict[str, Any]) -> None:
        self._by_ref = by_ref

    def tools_for(self, refs: list[str]) -> list[Any]:
        return [self._by_ref[ref] for ref in refs]


def test_knowledge_search_tool_joins_main_tools(captured) -> None:
    @tool("search_faq")
    def search_faq(query: str) -> str:
        """FAQ 를 찾는다."""
        return "{}"

    config = parse_config(offline_doc("knowledge-agent.json"))
    agent = compile_agent(
        config,
        tools_by_ref={},
        knowledge=FakeKnowledge({"kb-faq": search_faq}),
        checkpointer=InMemorySaver(),
    )
    assert [t.name for t in captured["tools"]] == ["search_faq"]
    assert [t.name for t in agent.tools] == ["search_faq"]
    # summarizer 는 knowledge 가 비어 있어 검색 도구가 없다
    assert [t.name for t in captured["subagent_specs"][0]["tools"]] == []


def test_knowledge_tool_name_conflict_with_mcp_tool() -> None:
    @tool("Add")
    def search_add(query: str) -> str:
        """MCP 의 Add 와 같은 이름이다."""
        return "{}"

    doc = offline_doc("knowledge-agent.json")
    support = load_fixture("support-agent.json")
    doc["tools"] = support["tools"]
    doc["main"]["tools"] = ["tools-calculator"]
    with pytest.raises(ConfigError) as info:
        compile_agent(
            parse_config(doc),
            tools_by_ref=TOOLS,
            knowledge=FakeKnowledge({"kb-faq": search_add}),
            checkpointer=InMemorySaver(),
        )
    assert info.value.code == "tool_name_conflict" and "'Add'" in info.value.message


def test_knowledge_refs_without_tools_is_config_error() -> None:
    with pytest.raises(ConfigError) as info:
        compile_agent(
            parse_config(offline_doc("knowledge-agent.json")), tools_by_ref={}, checkpointer=None
        )
    assert info.value.code == "knowledge_missing"


async def test_recompile_keeps_knowledge_tools() -> None:
    @tool("search_faq")
    def search_faq(query: str) -> str:
        """FAQ 를 찾는다."""
        return "{}"

    knowledge = FakeKnowledge({"kb-faq": search_faq})
    async with run_app(offline_doc("knowledge-agent.json"), knowledge=knowledge) as running:
        start = running.observed("runtime_start")[0]
        assert start["tools"] == ["search_faq"]
        assert running.runtime.knowledge is knowledge
        running.runtime.recompile()
        assert [t.name for t in running.runtime.agent.tools] == ["search_faq"]
        health = (await running.client.get("/healthz")).json()
        assert health["tools"] == ["search_faq"]


class _ContextOverflow(Exception):
    """provider 의 컨텍스트 초과 400 이다. deepagents 는 status_code 와 문구로 알아본다."""

    status_code = 400


class _OverflowOnceModel(StubChatModel):
    """arm 한 다음 호출 하나에 컨텍스트 초과 400 을 낸다. 그 뒤는 스텁 그대로다."""

    _armed: bool = PrivateAttr(default=False)
    _overflows: int = PrivateAttr(default=0)

    def arm(self) -> None:
        self._armed = True

    @property
    def overflows(self) -> int:
        return self._overflows

    def _maybe_fail(self) -> None:
        if self._armed:
            self._armed = False
            self._overflows += 1
            raise _ContextOverflow("prompt is too long: 250000 tokens > 200000 maximum")


async def _turn(
    agent: CompiledAgent, text: str
) -> tuple[list[tuple[str, dict[str, Any]]], TurnState]:
    """세션 s1 의 턴 하나다. routes 처럼 이어지는 세션의 체크포인트로 씨앗을 만든다."""
    snapshot = await agent.graph.aget_state(thread_config("s1"))
    state = TurnState.seeded(snapshot_messages(snapshot), resumed=False)
    events = [
        event
        async for event in stream_turn(
            agent, session_id="s1", turn_input=text, state=state, owner_sub="alice"
        )
    ]
    return events, state


def _overflow_doc(*, fallback: bool) -> dict[str, Any]:
    """보관량이 메시지 2 라 셋째 턴에는 요약할 이력이 있다."""
    doc = offline_doc()
    doc["main"]["compaction"]["keep"] = {"kind": "messages", "value": 2}
    if fallback:
        doc["main"]["fallback"] = {**copy.deepcopy(doc["main"]["model"]), "name": "stub-fallback"}
    return doc


# 요약 메시지(안내 문구와 파일 경로)보다 이력이 커야 줄어든 요청으로 다시 보낸다
_PADDING = " " + "x" * 2000


async def _history_then_overflow(
    doc: dict[str, Any],
) -> tuple[CompiledAgent, _OverflowOnceModel, list[tuple[str, dict[str, Any]]], TurnState]:
    model = _OverflowOnceModel(model_name="stub")
    agent = compile_agent(
        parse_config(doc), tools_by_ref=TOOLS, checkpointer=InMemorySaver(), model=model
    )
    for _ in range(2):
        _, state = await _turn(agent, '{"tool": "Add", "args": {"a": 1, "b": 2}}' + _PADDING)
        assert state.error is None
    # 발동 기준(창의 85%)에는 한참 못 미친다. 아래의 요약은 400 이 부른 것이다
    assert SUMMARIZATION_EVENT_KEY not in (await agent.graph.aget_state(thread_config("s1"))).values
    model.arm()
    events, state = await _turn(agent, '{"tool": "Add", "args": {"a": 3, "b": 4}}')
    return agent, model, events, state


async def test_context_overflow_400_recovers_by_summarizing() -> None:
    """첫 호출이 provider 의 컨텍스트 초과 400 이면 요약 미들웨어가 발동 기준과 상관없이 이력을
    요약해 한 번 더 작게 보내고 턴은 요약 뒤 끝난다."""
    agent, model, events, state = await _history_then_overflow(_overflow_doc(fallback=False))
    assert state.error is None and state.text == "결과: 7"
    assert model.overflows == 1 and not events_named(events, "model_fallback")
    snapshot = await agent.graph.aget_state(thread_config("s1"))
    summary = snapshot.values[SUMMARIZATION_EVENT_KEY]["summary_message"]
    assert SUMMARY_TEXT in summary.text


async def test_context_overflow_400_goes_to_the_fallback_first() -> None:
    """폴백이 있으면 ModelFallback 이 요약 미들웨어 안쪽이라 첫 400 을 폴백 모델이 먼저 받는다.
    요약은 일어나지 않는다."""
    agent, model, events, state = await _history_then_overflow(_overflow_doc(fallback=True))
    assert state.error is None and state.text == "결과: 7"
    assert model.overflows == 1
    assert events_named(events, "model_fallback") == [{"from": "stub", "to": "stub-fallback"}]
    snapshot = await agent.graph.aget_state(thread_config("s1"))
    assert SUMMARIZATION_EVENT_KEY not in snapshot.values
