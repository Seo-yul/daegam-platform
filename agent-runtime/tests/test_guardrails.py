"""가드레일(F2c): input 차단·표시·PII 마스킹, output 차단·표시, tool 인자 차단·표시, 잘못된 패턴.
출력 가드레일은 가린 뒤에 낸다(D65): 글·구조화 결과·task 결과가 같은 필터를 지난다."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langgraph.checkpoint.memory import InMemorySaver

from runtime.compile import compile_agent
from runtime.config import ConfigError, parse_config
from runtime.guards import OutputFilter, build_guardrails, parse_groups
from runtime.stream import TurnState, _translate, stream_turn, thread_config
from runtime.stub import StubChatModel
from runtime.tool_names import STRUCTURED_RESPONSE_TOOL
from tests.app_fixtures import (
    STRUCTURED_SCHEMA,
    STRUCTURED_VALUE,
    Add,
    JsonModel,
    deny_rule,
    guardrail,
    mask_rule,
    sid_of,
    with_subagent,
)
from tests.conftest import chat, event_names, events_named, offline_doc, run_app


async def test_input_max_length_block_returns_422(support_doc) -> None:
    support_doc["guardrails"] = [
        guardrail("input", [{"kind": "max_length", "value": 5}], "block", "너무 길다")
    ]
    async with run_app(support_doc) as running:
        response, _ = await chat(running, "123456")
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "guardrail_blocked"
        assert body["message"] == "너무 길다" and body["rule"] == "max_length"
        assert running.rejections() == ["guardrail_blocked"]
        # 세션은 만들어지지 않는다
        assert (await running.client.get("/sessions")).json() == {"sessions": []}
        event = running.observed("guardrail")[0]
        assert event["position"] == "input" and event["rule"] == "max_length"
        assert event["action"] == "block" and event["user_sub"] == "alice"

        # 한도 안이면 통과한다
        response, sse = await chat(running, "12345")
        assert response.status_code == 200 and events_named(sse, "done")


async def test_input_deny_pattern_flag_emits_event_and_continues(support_doc) -> None:
    support_doc["guardrails"] = [guardrail("input", [deny_rule("비밀")], "flag")]
    async with run_app(support_doc) as running:
        response, sse = await chat(running, "비밀 이야기")
        assert response.status_code == 200
        names = event_names(sse)
        assert names[0] == "guardrail" and names[-1] == "done"
        flag = events_named(sse, "guardrail")[0]
        assert flag == {"position": "input", "rule": "deny_pattern", "action": "flag"}
        event = running.observed("guardrail")[0]
        assert event["action"] == "flag" and event["session_id"] == sid_of(sse)
        assert event["turn"] == 1

        # 걸리지 않는 입력에는 이벤트가 없다
        _, sse = await chat(running, "평범한 이야기")
        assert not events_named(sse, "guardrail")


async def test_input_pii_block_uses_fixture_rule(support_doc) -> None:
    # support-agent.json 의 입력 가드레일: max_length 4000 + pii_mask credit_card, block
    async with run_app(support_doc) as running:
        response, _ = await chat(running, "내 카드는 4111 1111 1111 1111 이다")
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "guardrail_blocked" and body["rule"] == "pii_mask"
        assert body["message"].startswith("카드 번호 같은 민감 정보")
        response, sse = await chat(running, "카드 번호 없는 문장")
        assert response.status_code == 200 and events_named(sse, "done")


async def test_input_pii_flag_masks_before_model(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [
        guardrail("input", [{"kind": "pii_mask", "value": {"types": ["email"]}}], "flag")
    ]
    async with run_app(support_doc) as running:
        response, sse = await chat(running, "메일은 alice@example.com 이다")
        assert response.status_code == 200
        # 스텁은 받은 입력을 그대로 되돌리므로 모델이 본 것이 가려졌는지 바로 보인다
        text = events_named(sse, "done")[0]["text"]
        assert text == "Tool 이 없어 답만 합니다: 메일은 alice@****.com 이다"
        mask = events_named(sse, "guardrail")[0]
        assert mask["position"] == "input" and mask["rule"] == "pii_mask"
        assert mask["action"] == "mask" and mask["pii_type"] == "email"
        sid = sid_of(sse)
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert "alice@example.com" not in detail["messages"][0]["text"]


async def test_output_deny_pattern_block_replaces_answer(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("output", [deny_rule("답만")], "block", "대체 문구")]
    async with run_app(support_doc) as running:
        response, sse = await chat(running, "hi")
        assert response.status_code == 200
        block = events_named(sse, "guardrail")[0]
        assert block == {
            "position": "output",
            "rule": "deny_pattern",
            "action": "block",
            "message": "대체 문구",
        }
        done = events_named(sse, "done")[0]
        assert done["text"] == "대체 문구"
        sid = done["session_id"]
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["messages"][-1]["role"] == "ai"
        assert detail["messages"][-1]["text"] == "대체 문구"
        assert detail["session"]["status"] == "active" and detail["session"]["turns"] == 1
        end = running.observed("turn_end")[0]
        assert end["status"] == "done"


async def test_output_deny_pattern_flag_keeps_answer(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("output", [deny_rule("답만")], "flag")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        assert events_named(sse, "guardrail")[0] == {
            "position": "output",
            "rule": "deny_pattern",
            "action": "flag",
        }
        assert events_named(sse, "done")[0]["text"] == "Tool 이 없어 답만 합니다: hi"


async def test_output_max_length_block(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [
        guardrail("output", [{"kind": "max_length", "value": 10}], "block", None)
    ]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        block = events_named(sse, "guardrail")[0]
        assert block["rule"] == "max_length" and block["action"] == "block"
        # message 가 null 이면 런타임 기본 문구다
        assert events_named(sse, "done")[0]["text"] == block["message"]
        assert "가드레일" in block["message"]


async def test_tool_args_deny_pattern_block_skips_tool(support_doc, fake_mcp) -> None:
    support_doc["guardrails"] = [guardrail("tool", [deny_rule("666")], "block", "그 수는 안 된다")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 666, "b": 1}}')
        assert fake_mcp.calls["Add"] == 0
        block = events_named(sse, "guardrail")[0]
        assert block["position"] == "tool" and block["action"] == "block"
        assert block["tool"] == "Add" and block["stage"] == "args"
        result = events_named(sse, "tool_result")[0]
        assert result["status"] == "error" and result["content"] == "그 수는 안 된다"
        assert events_named(sse, "done")[0]["text"] == "결과: 그 수는 안 된다"
        event = running.observed("guardrail")[0]
        assert event["tool"] == "Add" and event["action"] == "block"

        # 걸리지 않는 인자는 그대로 실행된다
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        assert fake_mcp.calls["Add"] == 1 and not events_named(sse, "guardrail")


async def test_tool_args_deny_pattern_flag_runs_tool(support_doc, fake_mcp) -> None:
    support_doc["guardrails"] = [guardrail("tool", [deny_rule("666")], "flag")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 666, "b": 1}}')
        assert fake_mcp.calls["Add"] == 1
        flag = events_named(sse, "guardrail")[0]
        assert flag["action"] == "flag" and flag["tool"] == "Add" and flag["stage"] == "args"
        assert events_named(sse, "tool_result")[0]["content"] == "667"


async def test_tool_result_pii_mask(support_doc, fake_mcp) -> None:
    support_doc["guardrails"] = [guardrail("tool", [mask_rule(r"\d{3}")], "flag")]
    async with run_app(support_doc) as running:
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 100, "b": 23}}')
        assert fake_mcp.calls["Add"] == 1
        mask = events_named(sse, "guardrail")[0]
        assert mask["rule"] == "pii_mask" and mask["action"] == "mask"
        assert mask["stage"] == "result"
        # 결과 123 은 세 자리 숫자라 가려진 채 모델과 클라이언트에 간다
        result = events_named(sse, "tool_result")[0]
        assert result["content"] == "****"
        assert events_named(sse, "done")[0]["text"] == "결과: ****"


def test_invalid_pattern_is_config_error(minimal_doc) -> None:
    minimal_doc["guardrails"] = [guardrail("input", [deny_rule("(")], "block")]
    with pytest.raises(ConfigError) as info:
        build_guardrails(parse_config(minimal_doc))
    assert info.value.code == "guardrail_pattern_invalid"


def test_guardrail_middleware_names_do_not_collide(minimal_doc) -> None:
    minimal_doc["guardrails"] = [
        guardrail("input", [{"kind": "pii_mask", "value": {"types": ["email", "ip"]}}], "flag"),
        guardrail("output", [{"kind": "pii_mask", "value": {"types": ["email"]}}], "flag"),
        guardrail("output", [deny_rule("x")], "flag"),
        guardrail("tool", [{"kind": "max_length", "value": 10}], "block"),
    ]
    guardrails = build_guardrails(parse_config(minimal_doc))
    names = [m.name for m in guardrails.middleware]
    assert len(names) == len(set(names))
    assert names == [
        "DaegamPII_input_email",
        "DaegamPII_input_ip",
        "DaegamPII_output_email",
        "DaegamOutputGuard",
        "DaegamToolGuard",
    ]


# ---- 출력 가드레일은 가린 뒤에 낸다 (D65) ---------------------------------------------------


def _text_only_doc() -> dict[str, Any]:
    """MCP 도구 없이 스텁으로 도는 support 문서다. fake_mcp 없이 stream_turn 을 바로 돌릴 때
    쓴다."""
    doc = offline_doc()
    doc["main"]["tools"] = []
    return doc


async def _last_ai_text(graph: Any, sid: str) -> str:
    """체크포인트의 마지막 AI 글이다. 요약이 아니라 그래프 상태를 직접 읽는다."""
    snapshot = await graph.aget_state(thread_config(sid))
    return [m.text for m in snapshot.values["messages"] if isinstance(m, AIMessage)][-1]


async def _run_turn(
    doc: dict[str, Any], text: str, *, model: StubChatModel | None = None, tools: Any = None
) -> tuple[list[tuple[str, dict[str, Any]]], TurnState, Any]:
    """앱 없이 stream_turn 한 번이다. TurnState 를 직접 보려는 자리다."""
    agent = compile_agent(
        parse_config(doc), tools_by_ref=tools or {}, checkpointer=InMemorySaver(), model=model
    )
    state = TurnState()
    events = [
        event
        async for event in stream_turn(
            agent, session_id="s1", turn_input=text, state=state, owner_sub="alice"
        )
    ]
    return events, state, agent


class _ChunkingModel(StubChatModel):
    """답을 두 조각으로 흘리는 스텁이다. 조각 단위와 메시지 단위의 차이를 드러낸다."""

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        text = self.reply(messages).text
        half = len(text) // 2
        for piece in (text[:half], text[half:]):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


MASKED_VALUE = {"answer": "번호 ****", "items": ["****", 7]}
"""STRUCTURED_VALUE 의 세 자리 숫자가 가려진 꼴이다."""


def test_output_filter_blocks_first_then_masks(minimal_doc) -> None:
    """flag 만 있는 규칙은 필터에 들지 않고, 막기가 가리기보다 먼저다(그래프의 훅 차례와 같다)."""
    minimal_doc["guardrails"] = [
        guardrail("output", [deny_rule("x")], "flag"),
        guardrail("input", [mask_rule(r"\d{3}")], "flag"),
    ]
    assert OutputFilter.build(parse_groups(parse_config(minimal_doc))) is None

    minimal_doc["guardrails"] += [
        guardrail("output", [mask_rule(r"\d{3}")], "flag"),
        guardrail("output", [{"kind": "max_length", "value": 5}], "block", "길다"),
    ]
    output_filter = OutputFilter.build(parse_groups(parse_config(minimal_doc)))
    assert output_filter is not None
    assert output_filter.apply("123") == "****"
    assert output_filter.apply("1234567") == "길다"
    assert output_filter.mask_leaves({"a": ["123", 1, {"b": "456"}], "c": None}) == {
        "a": ["****", 1, {"b": "****"}],
        "c": None,
    }


async def test_output_flag_only_streams_chunks(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("output", [deny_rule("답만")], "flag")]
    async with run_app(support_doc, model=_ChunkingModel()) as running:
        _, sse = await chat(running, "hi")
        tokens = [t["text"] for t in events_named(sse, "token")]
        assert len(tokens) == 2 and "".join(tokens) == "Tool 이 없어 답만 합니다: hi"
        assert events_named(sse, "done")[0]["text"] == "Tool 이 없어 답만 합니다: hi"


async def test_output_pii_mask_emits_the_masked_message_once(support_doc) -> None:
    """가리기가 있으면 글은 메시지 단위로, 가려진 뒤 나간다. token·done.text·전사·요약 어디에도
    원문이 없고 체크포인트의 글과 같다."""
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    async with run_app(support_doc, model=_ChunkingModel()) as running:
        _, sse = await chat(running, "번호 123")
        masked = "Tool 이 없어 답만 합니다: 번호 ****"
        assert [t["text"] for t in events_named(sse, "token")] == [masked]
        assert event_names(sse) == ["token", "guardrail", "done"]
        done = events_named(sse, "done")[0]
        assert done["text"] == masked
        sid = done["session_id"]
        assert await _last_ai_text(running.runtime.agent.graph, sid) == masked
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        assert detail["messages"][-1]["text"] == masked
        bubbles = [i for i in detail["transcript"]["items"] if i["kind"] == "assistant"]
        assert [b["text"] for b in bubbles] == [masked]


async def test_output_block_emits_only_the_message(support_doc) -> None:
    support_doc["main"]["tools"] = []
    support_doc["guardrails"] = [guardrail("output", [deny_rule("답만")], "block", "대체 문구")]
    async with run_app(support_doc, model=_ChunkingModel()) as running:
        response, sse = await chat(running, "hi")
        assert [t["text"] for t in events_named(sse, "token")] == ["대체 문구"]
        assert event_names(sse) == ["token", "guardrail", "done"]
        assert "답만" not in response.text
        done = events_named(sse, "done")[0]
        assert done["text"] == "대체 문구"
        assert await _last_ai_text(running.runtime.agent.graph, done["session_id"]) == "대체 문구"


async def test_subagent_text_and_task_result_pass_the_filter(support_doc, fake_mcp) -> None:
    """서브에이전트의 글과 메인의 task 결과, 세션 요약이 가려진다. 체크포인트의 task 결과는 메인
    모델이 읽는 것이라 원문이다."""
    with_subagent(support_doc)
    support_doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    description = '{"tool": "Add", "args": {"a": 100, "b": 23}}'
    task = {"tool": "task", "args": {"description": description, "subagent_type": "calc"}}
    async with run_app(support_doc) as running:
        _, sse = await chat(running, json.dumps(task))
        assert fake_mcp.calls["Add"] == 1
        tokens = events_named(sse, "token")
        assert [t["text"] for t in tokens if t.get("agent")] == ["결과: ****"]
        assert [t["text"] for t in tokens if not t.get("agent")] == ["결과: 결과: ****"]
        task_result = next(r for r in events_named(sse, "tool_result") if r["name"] == "task")
        assert task_result["content"] == "결과: ****" and "agent" not in task_result
        done = events_named(sse, "done")[0]
        assert done["text"] == "결과: 결과: ****"
        sid = done["session_id"]
        detail = (await running.client.get(f"/sessions/{sid}")).json()
        summary = next(m for m in detail["messages"] if m.get("tool") == "task")
        assert summary["text"] == "결과: ****"
        cards = [i for i in detail["transcript"]["items"] if i["kind"] == "tool_call"]
        task_cards = [c["result"]["content"] for c in cards if c["call"]["name"] == "task"]
        assert task_cards == ["결과: ****"]
        snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
        raw = [m for m in snapshot.values["messages"] if isinstance(m, ToolMessage)]
        assert [m.text for m in raw if m.name == "task"] == ["결과: 123"]


async def test_structured_result_leaves_are_masked_and_kept() -> None:
    """provider 전략의 구조화 결과는 문자열 잎마다 가려져 TurnState.structured 에 남고, 모델의 JSON
    글은 가려진 채 token 하나로 나가 체크포인트의 글과 같다."""
    doc = _text_only_doc()
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": STRUCTURED_SCHEMA}
    doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    events, state, agent = await _run_turn(doc, "hi", model=JsonModel())
    assert state.structured == MASKED_VALUE
    masked_text = json.dumps(MASKED_VALUE, ensure_ascii=False)
    assert [d["text"] for d in events_named(events, "token")] == [masked_text]
    assert state.text == masked_text == await _last_ai_text(agent.graph, "s1")


async def test_structured_result_block_applies_to_the_whole() -> None:
    """막기는 잎이 아니라 그래프의 OutputGuard 가 본 글 전체에 걸린다. 키 이름에 걸리면 칸이 빠지고
    그 문구가 done.text 이며 체크포인트와 같다."""
    doc = _text_only_doc()
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": STRUCTURED_SCHEMA}
    doc["guardrails"] = [guardrail("output", [deny_rule('"answer"')], "block", "막았다")]
    events, state, agent = await _run_turn(doc, "hi", model=JsonModel())
    assert state.structured is None
    assert [d["text"] for d in events_named(events, "token")] == ["막았다"]
    assert state.text == "막았다" == await _last_ai_text(agent.graph, "s1")
    assert [d["action"] for d in events_named(events, "guardrail")] == ["block"]


class _StructuredWithToolModel(StubChatModel):
    """구조화 출력 도구와 Add 를 한 호출에 함께 부른다. 구조화 출력을 모르는 모델이라(profile 에
    structured_output 이 없다) 도구 전략으로 돌고, langchain 은 Add 를 돌린 뒤 구조화 도구가
    실행됐다고 보아 턴을 끝낸다."""

    def reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {"name": STRUCTURED_RESPONSE_TOOL, "args": dict(STRUCTURED_VALUE), "id": "call_s"},
                {"name": "Add", "args": {"a": 1, "b": 2}, "id": "call_add"},
            ],
        )


@pytest.mark.parametrize("masked", [False, True])
async def test_tool_strategy_hides_the_structured_tool(masked) -> None:
    """도구 전략에서는 구조화 도구의 tool_call·tool_result 가 나가지 않고 값은 TurnState 에만
    남는다. 사용자가 title 을 실제 도구 이름(Add)으로 적어도 Add 의 이벤트는 나간다."""
    doc = offline_doc()
    doc["main"]["response_format"] = {
        "kind": "json_schema",
        "schema": {**STRUCTURED_SCHEMA, "title": "Add"},
    }
    if masked:
        doc["guardrails"] = [guardrail("output", [mask_rule(r"\d{3}")], "flag")]
    events, state, _ = await _run_turn(
        doc, "hi", model=_StructuredWithToolModel(), tools={"tools-calculator": [Add]}
    )
    assert [(e, d["name"]) for e, d in events] == [("tool_call", "Add"), ("tool_result", "Add")]
    assert state.tool_calls == 1 and state.text == ""
    assert state.structured == (MASKED_VALUE if masked else STRUCTURED_VALUE)


def test_later_none_does_not_clear_the_structured_value() -> None:
    """model 노드는 구조화 결과가 없는 호출마다 structured_response: None 을 싣는다. 번역은 마지막
    None 아닌 값을 지킨다."""
    state = TurnState()
    first = {"messages": [AIMessage(content="", id="m1")], "structured_response": STRUCTURED_VALUE}
    later = {"messages": [AIMessage(content="끝", id="m2")], "structured_response": None}
    for update in (first, later):
        _translate((), "updates", {"model": update}, state, None)
    assert state.structured == STRUCTURED_VALUE and state.text == ""


async def test_tool_strategy_block_serializes_the_value() -> None:
    """도구 전략은 모델 글이 비어 있어 값을 직렬화한 전체에 막기를 댄다. 막히면 칸이 빠지고 그
    문구가 token 과 done.text 다."""
    doc = offline_doc()
    doc["main"]["response_format"] = {"kind": "json_schema", "schema": STRUCTURED_SCHEMA}
    doc["guardrails"] = [guardrail("output", [deny_rule('"answer"')], "block", "막았다")]
    events, state, _ = await _run_turn(
        doc, "hi", model=_StructuredWithToolModel(), tools={"tools-calculator": [Add]}
    )
    assert state.structured is None
    assert [d["text"] for d in events_named(events, "token")] == ["막았다"]
    assert state.text == "막았다"


@pytest.mark.parametrize(
    "group",
    [
        guardrail("output", [mask_rule(r"\d{3}")], "flag"),
        guardrail("output", [deny_rule('"answer"')], "block", "막았다"),
    ],
    ids=["mask", "block"],
)
async def test_session_summary_leaves_out_the_structured_tool(support_doc, fake_mcp, group) -> None:
    """도구 전략의 구조화 출력 배관(호출과 결과)은 세션 요약에도 실리지 않는다. 출력 필터가 가리거나
    막은 원값이 GET /sessions/{sid} 로 나가지 않는다."""
    support_doc["main"]["response_format"] = {"kind": "json_schema", "schema": STRUCTURED_SCHEMA}
    support_doc["guardrails"] = [group]
    async with run_app(support_doc, model=_StructuredWithToolModel()) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        messages = (await running.client.get(f"/sessions/{sid}")).json()["messages"]
    assert fake_mcp.calls["Add"] == 1
    summary = json.dumps(messages, ensure_ascii=False)
    assert "123" not in summary and "456" not in summary
    assert STRUCTURED_RESPONSE_TOOL not in summary
    assert [m["tool"] for m in messages if m["role"] == "tool"] == ["Add"]
