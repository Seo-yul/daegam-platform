"""HITL(F2b): 인터럽트가 세션을 보류로 두고 다음 턴을 막는 것(session_pending), pending 조회,
decisions 재개(approve·reject·edit·respond), 422, 권한, 교차 앱 재개. 고치기는 같은 도구와
가드레일을 지난 인자만이다(D65): 이름 검사, 결정 때의 인자 검사, 실행 때의 도구 가드가 실제로 도는
호출을 보는 것, 재개가 다시 알리는 호출과 전사 카드의 인자."""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from runtime.config import parse_config
from runtime.guards import HITL_EDITED_CALLS_KEY, ToolGuard, effective_call, parse_groups
from runtime.hitl import Decision, EditedAction, Pending
from runtime.stream import DecidedEdit, TurnState, _translate, thread_config
from runtime.transcript import resolve_interrupts
from tests.app_fixtures import (
    decide,
    deny_rule,
    folded,
    guardrail,
    resume,
    sid_of,
    transcript_items,
    with_subagent,
)
from tests.conftest import chat, event_names, events_named, run_app
from tests.store_fixtures import pod_store

BLOCK_666 = guardrail("tool", [deny_rule("666")], "block", "그 수는 안 된다")
FLAG_666 = guardrail("tool", [deny_rule("666")], "flag")


def _edit(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "edit", "edited_action": {"name": name, "args": args}}


def _flag_event(tool: str, stage: str = "args") -> dict[str, Any]:
    """결정 때와 실행 때가 같은 모양으로 내는 flag 다. 기본은 인자 단계다."""
    return {
        "position": "tool",
        "rule": "deny_pattern",
        "action": "flag",
        "tool": tool,
        "stage": stage,
    }


async def _interrupt(
    running, message: str, *, user_sub: str = "alice"
) -> tuple[str, dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """인터럽트로 끝나는 턴 하나를 보낸다. 세션 id, interrupt 이벤트, 그 턴의 SSE 를 준다."""
    _, sse = await chat(running, message, user_sub=user_sub)
    return sid_of(sse, "interrupt"), sse[-1][1], sse


async def test_interrupt_holds_the_session_pending(support_doc, fake_mcp) -> None:
    """승인이 걸린 호출은 도구를 부르지 않고 interrupt 로 턴을 끝낸다. 결정 전에는 세션이 보류이고
    결정 없이 다음 턴을 보내지 못한다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit", "reject"]}
    async with run_app(support_doc) as running:
        sid, interrupt, sse = await _interrupt(running, '{"tool": "Add", "args": {"a": 3, "b": 4}}')
        names = event_names(sse)
        assert names[-1] == "interrupt" and "done" not in names
        assert "tool_call" in names and "tool_result" not in names
        assert fake_mcp.calls["Add"] == 0
        request = interrupt["action_requests"][0]
        assert request["name"] == "Add" and request["args"] == {"a": 3, "b": 4}
        assert interrupt["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]

        row = (await running.client.get(f"/sessions/{sid}")).json()
        assert row["session"]["status"] == "pending" and row["pending"] is True
        assert row["session"]["pending_since"] is not None and row["session"]["turns"] == 1
        listed = (await running.client.get("/sessions", params={"status": "pending"})).json()
        assert [s["session_id"] for s in listed["sessions"]] == [sid]
        response, _ = await chat(running, "또", session_id=sid)
        assert response.status_code == 409 and response.json()["code"] == "session_pending"

        interrupted, ended = running.observed("interrupt", "turn_end")
        assert interrupted["daegam_event"] == "interrupt" and interrupted["tools"] == ["Add"]
        assert ended["status"] == "pending"
        metrics = (await running.client.get("/metrics")).json()
        assert metrics["sessions"]["pending"] == 1


async def test_pending_then_approve_runs_tool(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit", "reject"]}
    async with run_app(support_doc) as running:
        sid, interrupt, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 3, "b": 4}}')
        assert fake_mcp.calls["Add"] == 0
        before = (await running.client.get(f"/sessions/{sid}")).json()["session"]

        pending = (await running.client.get(f"/sessions/{sid}/pending")).json()
        assert pending["session_id"] == sid and pending["user_sub"] == "alice"
        assert pending["pending_since"] is not None
        assert pending["action_requests"] == interrupt["action_requests"]
        assert pending["review_configs"] == interrupt["review_configs"]
        assert pending["action_requests"][0]["args"] == {"a": 3, "b": 4}

        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        names = event_names(sse)
        assert names[-1] == "done" and "interrupt" not in names
        result = events_named(sse, "tool_result")[0]
        assert result["name"] == "Add" and result["content"] == "7"
        assert fake_mcp.calls["Add"] == 1
        done = events_named(sse, "done")[0]
        assert done["text"] == "결과: 7" and done["turn"] == 1

        after = (await running.client.get(f"/sessions/{sid}")).json()
        assert after["session"]["status"] == "active" and after["pending"] is False
        assert after["session"]["pending_since"] is None
        # 재개해도 턴은 그대로 1 이고 usage 는 앞 스트림이 센 모델 호출을 다시 세지 않는다
        assert after["session"]["turns"] == 1
        assert done["usage"]["input_tokens"] > 0
        assert (
            after["session"]["usage"]["input_tokens"]
            == before["usage"]["input_tokens"] + done["usage"]["input_tokens"]
        )

        # 더 이상 보류가 없다
        response = await running.client.get(f"/sessions/{sid}/pending")
        assert response.status_code == 404 and response.json()["code"] == "not_pending"
        response = await decide(running, sid, [{"type": "approve"}])
        assert response.status_code == 409 and response.json()["code"] == "not_pending"

        # 다음 턴을 이어갈 수 있다(스텁이 또 Add 를 부르므로 다시 인터럽트가 난다)
        response, sse = await chat(running, "다시", session_id=sid)
        assert response.status_code == 200 and sse[-1][0] == "interrupt"
        assert (await running.client.get(f"/sessions/{sid}")).json()["session"]["turns"] == 2

        decision = running.observed("decision")[0]
        assert decision["session_id"] == sid and decision["user_sub"] == "alice"
        assert decision["owner_sub"] == "alice" and decision["approver"] is False
        assert decision["decisions"] == ["approve"] and decision["tools"] == ["Add"]
        starts = running.observed("turn_start")
        assert starts[1]["resumed"] is True and starts[1]["decided_by"] == "alice"
        assert starts[1]["turn"] == 1 and "resumed" not in starts[0]
        ends = running.observed("turn_end")
        assert [e["status"] for e in ends] == ["pending", "done", "pending"]


async def test_resume_reannounces_only_the_pending_call(support_doc) -> None:
    """재개 턴의 씨앗에는 답이 있는 호출만 든다. 승인을 기다리던 호출은 재개가 인자를 실어 다시
    알리고(화면 카드의 승인 대기가 그것으로 풀린다) 앞 턴에 답이 있던 호출은 다시 내지 않는다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        # 첫 턴은 승인 없이 끝난 세션이 아니라 같은 세션에 답이 있는 호출을 먼저 남긴다
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "interrupt")
        await decide(running, sid, [{"type": "approve"}])
        _, sse = await chat(running, '{"tool": "Add", "args": {"a": 3, "b": 4}}', session_id=sid)
        assert sse[-1][0] == "interrupt"
        pending_call = events_named(sse, "tool_call")[0]
        assert pending_call["args"] == {"a": 3, "b": 4}

        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200
        names = event_names(sse)
        assert names[:2] == ["tool_call", "tool_result"] and names[-1] == "done"
        assert names.count("tool_call") == 1 and names.count("tool_result") == 1
        assert events_named(sse, "tool_call")[0] == pending_call
        result = events_named(sse, "tool_result")[0]
        assert result["id"] == pending_call["id"] and result["content"] == "7"
        assert events_named(sse, "done")[0]["tool_calls"] == 1

        # 복원한 전사에서 그 카드는 인자와 결과를 함께 가진 하나다
        items = await transcript_items(running, sid)
        cards = [
            i for i in items if i["kind"] == "tool_call" and i["call"]["id"] == pending_call["id"]
        ]
        assert len(cards) == 1
        assert cards[0]["call"]["args"] == {"a": 3, "b": 4} and cards[0]["result"]["content"] == "7"


async def test_reject_gives_model_the_refusal(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "reject"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response, sse = await resume(
            running, sid, [{"type": "reject", "message": "지금은 계산하지 마"}]
        )
        assert response.status_code == 200
        assert fake_mcp.calls["Add"] == 0
        result = events_named(sse, "tool_result")[0]
        assert result["status"] == "error" and "지금은 계산하지 마" in result["content"]
        done = events_named(sse, "done")[0]
        assert done["text"].startswith("결과: User rejected the tool call")
        assert (await running.client.get(f"/sessions/{sid}")).json()["session"][
            "status"
        ] == "active"


async def test_edit_changes_arguments(support_doc, fake_mcp) -> None:
    """이름이 같은 edit 은 고친 인자로 돈다. 다시 알린 tool_call 과 전사 카드도 고친 인자다.
    langchain 은 모델의 메시지에 모델이 낸 인자를 남기고 결과 앞에 바뀐 호출을 알리는 안내문을
    붙이므로 결과는 끝만 본다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response, sse = await resume(running, sid, [_edit("Add", {"a": 10, "b": 20})])
        assert response.status_code == 200
        call = events_named(sse, "tool_call")[0]
        assert call["name"] == "Add" and call["args"] == {"a": 10, "b": 20}
        result = events_named(sse, "tool_result")[0]
        assert result["id"] == call["id"] and result["content"].endswith("30")
        assert events_named(sse, "done")[0]["text"].endswith("30")
        assert fake_mcp.calls["Add"] == 1
        cards = [i for i in await transcript_items(running, sid) if i["kind"] == "tool_call"]
        assert len(cards) == 1 and cards[0]["call"]["args"] == {"a": 10, "b": 20}
        assert cards[0]["result"]["content"].endswith("30")
        # 체크포인트의 모델 메시지는 모델이 낸 인자 그대로다. 카드의 인자는 메시지가 아니라 실제로
        # 돈 호출에서 왔다
        snapshot = await running.runtime.agent.graph.aget_state(thread_config(sid))
        model_calls = [c for m in snapshot.values["messages"] for c in getattr(m, "tool_calls", [])]
        assert model_calls == [{**call, "args": {"a": 1, "b": 1}, "type": "tool_call"}]


async def test_edit_must_keep_the_tool_name(support_doc, fake_mcp) -> None:
    """다른 이름의 edit 은 422 이고 아무것도 재개하지 않는다. 내장 도구 이름도 같다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        for name in ("write_file", "task", "add"):
            response = await decide(running, sid, [_edit(name, {"a": 1, "b": 2})])
            assert response.status_code == 422, name
            assert response.json()["code"] == "edit_name_mismatch"
        assert (await running.client.get(f"/sessions/{sid}/pending")).status_code == 200
        assert (await running.client.get(f"/sessions/{sid}")).json()["session"][
            "status"
        ] == "pending"
        assert fake_mcp.calls["Add"] == 0
        assert not running.observed("decision")

        response, sse = await resume(running, sid, [_edit("Add", {"a": 1, "b": 2})])
        assert response.status_code == 200 and events_named(sse, "done")[0]["text"].endswith("3")
        assert fake_mcp.calls["Add"] == 1


async def test_edited_args_pass_the_tool_guardrail(support_doc, fake_mcp) -> None:
    """고친 인자는 결정 때 도구 가드레일의 인자 규칙을 지난다. block 은 입력 가드와 같은 422
    guardrail_blocked(rule 포함)이고 보류는 그대로다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    support_doc["guardrails"] = [BLOCK_666]
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response = await decide(running, sid, [_edit("Add", {"a": 666, "b": 1})])
        assert response.status_code == 422
        assert response.json() == {
            "code": "guardrail_blocked",
            "message": "그 수는 안 된다",
            "rule": "deny_pattern",
        }
        assert (await running.client.get(f"/sessions/{sid}/pending")).status_code == 200
        assert fake_mcp.calls["Add"] == 0
        assert running.rejections() == ["guardrail_blocked"]
        event = running.observed("guardrail")[0]
        assert event["session_id"] == sid and event["user_sub"] == "alice"
        assert event["position"] == "tool" and event["action"] == "block"
        assert event["tool"] == "Add" and event["stage"] == "args"
        assert event["rule"] == "deny_pattern" and event["message"] == "그 수는 안 된다"

        response, sse = await resume(running, sid, [_edit("Add", {"a": 1, "b": 2})])
        assert response.status_code == 200 and fake_mcp.calls["Add"] == 1
        assert not events_named(sse, "guardrail")


async def test_edited_args_flag_leads_the_resume_stream(support_doc, fake_mcp) -> None:
    """flag 는 재개 스트림의 앞머리(prelude)다. 메인의 호출은 실행 때 ToolGuard 가 같은 인자를 한 번
    더 보므로 인자 flag 가 둘이고 도구는 돈다. langchain 이 결과 앞에 붙이는 안내문이 고친 인자를
    되풀이하므로 결과 단계의 flag 가 하나 더 난다. 모델이 보는 결과 그대로를 가드가 본다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    support_doc["guardrails"] = [FLAG_666]
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response, sse = await resume(running, sid, [_edit("Add", {"a": 666, "b": 1})])
        assert response.status_code == 200
        assert sse[0] == ("guardrail", _flag_event("Add"))
        assert events_named(sse, "guardrail") == [
            _flag_event("Add"),
            _flag_event("Add"),
            _flag_event("Add", stage="result"),
        ]
        assert events_named(sse, "tool_call")[0]["args"] == {"a": 666, "b": 1}
        result = events_named(sse, "tool_result")[0]["content"]
        assert "Executed instead: Add with arguments" in result and result.endswith("667")
        assert fake_mcp.calls["Add"] == 1
        flags = [i for i in await transcript_items(running, sid) if i["kind"] == "guardrail"]
        assert flags and all(i["action"] == "flag" and i["position"] == "tool" for i in flags)


async def test_edit_replaces_the_blocked_model_args(support_doc, fake_mcp) -> None:
    """모델이 금지 인자를 내고 승인자가 허용 인자로 고치면 실행 때의 도구 가드도 고친 호출을 보아
    고친 호출이 돈다. 모델의 원래 인자로 막히지 않는다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    support_doc["guardrails"] = [BLOCK_666]
    async with run_app(support_doc) as running:
        sid, interrupt, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 666, "b": 1}}')
        assert interrupt["action_requests"][0]["args"] == {"a": 666, "b": 1}
        response, sse = await resume(running, sid, [_edit("Add", {"a": 1, "b": 2})])
        assert response.status_code == 200
        assert not events_named(sse, "guardrail")
        assert events_named(sse, "tool_call")[0]["args"] == {"a": 1, "b": 2}
        result = events_named(sse, "tool_result")[0]
        assert result["status"] == "success" and result["content"].endswith("3")
        assert fake_mcp.calls["Add"] == 1

        # 승인만 하면 모델의 인자 그대로라 실행 때 막힌다
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 666, "b": 2}}')
        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200
        block = events_named(sse, "guardrail")[0]
        assert block["action"] == "block" and block["tool"] == "Add" and block["stage"] == "args"
        assert events_named(sse, "tool_result")[0]["status"] == "error"
        assert fake_mcp.calls["Add"] == 1


def _tool_guard(doc: dict[str, Any], *groups: dict[str, Any]) -> ToolGuard:
    doc["guardrails"] = list(groups)
    return ToolGuard(parse_groups(parse_config(doc)))


def _request(state: dict[str, Any], args: dict[str, Any]) -> ToolCallRequest:
    call = {"name": "Add", "args": args, "id": "call_1", "type": "tool_call"}
    return ToolCallRequest(tool_call=call, tool=None, state=state, runtime=None)


def test_effective_call_is_the_edited_one(support_doc) -> None:
    """상태 키에 같은 id 의 고친 호출이 있으면 그것이고, 없거나 id 가 다르면 요청의 호출이다."""
    edited = {"call_1": {"name": "Add", "args": {"a": 1, "b": 2}}}
    request = _request({HITL_EDITED_CALLS_KEY: edited}, {"a": 666, "b": 1})
    assert effective_call(request) == {**request.tool_call, "args": {"a": 1, "b": 2}}
    assert effective_call(_request({}, {"a": 666, "b": 1}))["args"] == {"a": 666, "b": 1}
    other = {"call_9": {"name": "Add", "args": {"a": 1, "b": 2}}}
    assert effective_call(_request({HITL_EDITED_CALLS_KEY: other}, {"a": 666}))["args"] == {
        "a": 666
    }


async def test_tool_guard_checks_the_call_that_runs(support_doc) -> None:
    """1.4.3 의 모양이다. 요청의 호출은 모델의 것이고 고친 호출은 상태 키에 있다. 가드는 고친 인자로
    막거나 보내고 flag 도 그 인자로 낸다."""
    guard = _tool_guard(support_doc, BLOCK_666)
    ran: list[dict[str, Any]] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        ran.append(dict(request.tool_call))
        return ToolMessage(content="ok", name="Add", tool_call_id="call_1")

    clean = {"call_1": {"name": "Add", "args": {"a": 1, "b": 2}}}
    result = await guard.awrap_tool_call(
        _request({HITL_EDITED_CALLS_KEY: clean}, {"a": 666}), handler
    )
    assert isinstance(result, ToolMessage) and result.content == "ok" and len(ran) == 1

    bad = {"call_1": {"name": "Add", "args": {"a": 666, "b": 2}}}
    result = await guard.awrap_tool_call(_request({HITL_EDITED_CALLS_KEY: bad}, {"a": 1}), handler)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert result.content == "그 수는 안 된다" and len(ran) == 1


def test_args_violations_stop_at_the_first_block(support_doc) -> None:
    """flag 그룹의 위반은 차례로 모이고 block 이 나오면 거기서 끝난다. 신호는 내지 않는다."""
    guard = _tool_guard(support_doc, FLAG_666, BLOCK_666, FLAG_666)
    violations = guard.args_violations("Add", {"a": 666})
    assert [v.action for v in violations] == ["flag", "block"]
    assert violations[0].to_dict() == _flag_event("Add")
    assert violations[1].message == "그 수는 안 된다"
    assert guard.args_violations("Add", {"a": 1}) == []


def test_reannounced_call_carries_the_edited_args() -> None:
    """1.4.3 의 재개 update 다. 메시지에는 모델의 호출이 남고 같은 update 의 상태 키에 고친 호출이
    있다. 다시 알리는 tool_call 은 고친 이름과 인자다."""
    message = AIMessage(
        id="ai-1",
        content="",
        tool_calls=[
            {"name": "Add", "args": {"a": 666, "b": 1}, "id": "call_1", "type": "tool_call"}
        ],
    )
    state = TurnState.seeded([message], resumed=True)
    update = {
        "HumanInTheLoopMiddleware.after_model": {
            "messages": [message],
            HITL_EDITED_CALLS_KEY: {"call_1": {"name": "Add", "args": {"a": 1, "b": 2}}},
        }
    }
    events = _translate((), "updates", update, state, None)
    assert events == [("tool_call", {"id": "call_1", "name": "Add", "args": {"a": 1, "b": 2}})]
    assert state.known_calls == {"call_1": {"a": 1, "b": 2}} and state.tool_calls == 1
    # 같은 update 를 다시 보아도(after_model 훅의 되돌림) 인자가 같으면 내지 않는다
    assert _translate((), "updates", update, state, None) == []


def test_decided_edit_backs_the_reannouncement_without_the_state_key() -> None:
    """서브에이전트의 update 는 상태 키 없이 모델의 호출만 싣는다. 씨앗에 실린 edit 결정이 같은
    이름·인자의 호출에 고친 인자를 얹고, 같은 메시지가 다시 와도 같은 인자라 다시 내지 않는다.
    다른 인자의 호출은 그대로다."""
    pending = Pending(
        action_requests=[{"name": "Add", "args": {"a": 666, "b": 1}}],
        review_configs=[{"action_name": "Add", "allowed_decisions": ["approve", "edit"]}],
        seed=TurnState.seeded([], resumed=True),
    )
    decision = Decision(type="edit", edited_action=EditedAction(name="Add", args={"a": 1, "b": 2}))
    state = pending.seed_with([decision])
    assert state.edits == [DecidedEdit("Add", {"a": 666, "b": 1}, {"a": 1, "b": 2})]

    def message(args: dict[str, Any], call_id: str) -> AIMessage:
        return AIMessage(
            id=f"ai-{call_id}",
            content="",
            tool_calls=[{"name": "Add", "args": args, "id": call_id, "type": "tool_call"}],
        )

    after_model = {"messages": [message({"a": 666, "b": 1}, "c1")]}
    update = {"HumanInTheLoopMiddleware.after_model": after_model}
    events = _translate(("tools:1",), "updates", update, state, None)
    assert events == [
        ("tool_call", {"id": "c1", "name": "Add", "args": {"a": 1, "b": 2}, "agent": "tools:1"})
    ]
    assert state.edits == [] and state.edited_by_id == {"c1": {"a": 1, "b": 2}}
    assert _translate(("tools:1",), "updates", update, state, None) == []
    other = {"model": {"messages": [message({"a": 3, "b": 4}, "c2")]}}
    assert _translate(("tools:1",), "updates", other, state, None) == [
        ("tool_call", {"id": "c2", "name": "Add", "args": {"a": 3, "b": 4}, "agent": "tools:1"})
    ]


async def test_decision_event_carries_edited_args_with_record_bodies(support_doc) -> None:
    """고친 인자는 record_bodies 일 때만 결정 이벤트에 실린다. edit 이 없으면 칸도 없다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "edit"]}
    support_doc["service"]["observability"]["record_bodies"] = True
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        await decide(running, sid, [_edit("Add", {"a": 3, "b": 4})])
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        await decide(running, sid, [{"type": "approve"}])
        decisions = running.observed("decision")
        assert decisions[0]["decisions"] == ["edit"] and decisions[0]["tools"] == ["Add"]
        assert json.loads(decisions[0]["args"]) == [{"a": 3, "b": 4}]
        assert decisions[1]["decisions"] == ["approve"] and "args" not in decisions[1]

    support_doc["service"]["observability"]["record_bodies"] = False
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        await decide(running, sid, [_edit("Add", {"a": 3, "b": 4})])
        decision = running.observed("decision")[0]
        assert decision["decisions"] == ["edit"] and "args" not in decision


async def test_subagent_pending_edits_are_checked_too(support_doc, fake_mcp) -> None:
    """서브에이전트의 보류도 같은 /decisions 를 지나므로 이름 검사와 인자 검사가 걸린다. 실행 때의
    도구 가드는 메인에만 있어 결정 때의 검사가 유일한 검사다. 서브에이전트의 update 에는 HITL 상태
    키가 오지 않으므로 다시 알리는 tool_call 은 씨앗의 edit 결정에서 고친 인자를 얻는다."""
    with_subagent(support_doc)
    support_doc["subagents"][0]["interrupt_on"] = {"Add": ["approve", "edit"]}
    support_doc["guardrails"] = [BLOCK_666]
    description = '{"tool": "Add", "args": {"a": 1, "b": 2}}'
    task = {"tool": "task", "args": {"description": description, "subagent_type": "calc"}}
    async with run_app(support_doc) as running:
        sid, interrupt, _ = await _interrupt(running, json.dumps(task))
        assert interrupt["action_requests"][0]["name"] == "Add"

        response = await decide(running, sid, [_edit("write_file", {"a": 5, "b": 6})])
        assert response.status_code == 422 and response.json()["code"] == "edit_name_mismatch"
        response = await decide(running, sid, [_edit("Add", {"a": 666, "b": 6})])
        assert response.status_code == 422 and response.json()["code"] == "guardrail_blocked"
        assert fake_mcp.calls["Add"] == 0
        assert (await running.client.get(f"/sessions/{sid}/pending")).status_code == 200

        response, sse = await resume(running, sid, [_edit("Add", {"a": 5, "b": 6})])
        assert response.status_code == 200
        call = next(c for c in events_named(sse, "tool_call") if c["name"] == "Add")
        assert call["args"] == {"a": 5, "b": 6} and call["agent"]
        result = next(r for r in events_named(sse, "tool_result") if r["name"] == "Add")
        assert result["content"].endswith("11") and fake_mcp.calls["Add"] == 1


async def test_respond_answers_for_the_tool(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["respond"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response, sse = await resume(running, sid, [{"type": "respond", "message": "42"}])
        assert response.status_code == 200
        result = events_named(sse, "tool_result")[0]
        assert result["status"] == "success" and result["content"] == "42"
        assert events_named(sse, "done")[0]["text"] == "결과: 42"
        assert fake_mcp.calls["Add"] == 0


async def test_decision_validation_422(support_doc, fake_mcp) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve", "reject"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')

        response = await decide(running, sid, [{"type": "approve"}, {"type": "approve"}])
        assert response.status_code == 422
        assert response.json()["code"] == "decision_count_mismatch"

        response = await decide(running, sid, [{"type": "edit"}])
        assert response.status_code == 422
        assert response.json()["code"] == "decision_not_allowed"

        # 세션은 여전히 보류 중이고 툴은 돌지 않았다
        assert (await running.client.get(f"/sessions/{sid}/pending")).status_code == 200
        assert fake_mcp.calls["Add"] == 0

        response = await decide(running, sid, [{"type": "approve"}])
        assert response.status_code == 200 and fake_mcp.calls["Add"] == 1


async def test_a_decision_message_has_the_input_length_limit(support_doc, fake_mcp) -> None:
    """거절·응답 결정의 글도 모델에 가는 사용자의 말이라 max_input_chars 를 넘으면 413 이다
    (P76 F2). 아무것도 재개하지 않고 세션은 보류 그대로다."""
    support_doc["tools"][0]["approval"] = {"Add": ["respond"]}
    support_doc["entry"]["max_input_chars"] = 5
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, "더해 줘")
        response = await decide(running, sid, [{"type": "respond", "message": "123456"}])
        assert response.status_code == 413 and response.json()["code"] == "max_input_chars"
        assert (await running.client.get(f"/sessions/{sid}/pending")).status_code == 200
        assert fake_mcp.calls["Add"] == 0


async def test_an_approve_message_is_not_checked(support_doc, fake_mcp) -> None:
    """승인의 message 는 재개에 싣지 않으므로 길이 상한을 보지 않는다 (P76 검토)."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    support_doc["entry"]["max_input_chars"] = 5
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, "더해 줘")
        response = await decide(running, sid, [{"type": "approve", "message": "123456"}])
        assert response.status_code == 200, response.text


async def test_edit_requires_edited_action(support_doc) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["edit", "respond"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, "더해 줘")
        response = await decide(running, sid, [{"type": "edit"}])
        assert response.status_code == 422 and response.json()["code"] == "decision_invalid"
        response = await decide(running, sid, [{"type": "respond"}])
        assert response.status_code == 422 and response.json()["code"] == "decision_invalid"


async def test_owner_or_approver_header(support_doc) -> None:
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with run_app(support_doc) as running:
        sid, _, _ = await _interrupt(running, '{"tool": "Add", "args": {"a": 1, "b": 1}}')
        response = await decide(running, sid, [{"type": "approve"}], user_sub="bob")
        assert response.status_code == 403 and response.json()["code"] == "not_session_owner"

        response, sse = await resume(
            running, sid, [{"type": "approve"}], user_sub="bob", headers={"X-Daegam-Approver": "1"}
        )
        assert response.status_code == 200 and events_named(sse, "done")
        decision = running.observed("decision")[0]
        assert decision["user_sub"] == "bob" and decision["owner_sub"] == "alice"
        assert decision["approver"] is True

        response = await decide(running, "nope", [{"type": "approve"}])
        assert response.status_code == 404 and response.json()["code"] == "session_not_found"
        assert (await running.client.get("/sessions/nope/pending")).status_code == 404


async def test_not_pending_session_rejects_decisions(support_doc) -> None:
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        response = await running.client.get(f"/sessions/{sid}/pending")
        assert response.status_code == 404 and response.json()["code"] == "not_pending"
        response = await decide(running, sid, [{"type": "approve"}])
        assert response.status_code == 409 and response.json()["code"] == "not_pending"


async def test_resume_on_another_app_sharing_the_store(support_doc, fake_mcp) -> None:
    """파드 둘이 같은 체크포인터·색인을 볼 때 한쪽에서 인터럽트한 세션을 다른 쪽이 재개한다. 전사는
    쓰기가 덧붙이기뿐이라 두 파드 어디서 읽어도 한 대화다."""
    support_doc["tools"][0]["approval"] = {"Add": ["approve"]}
    async with (
        pod_store(support_doc) as store,
        run_app(support_doc, store=store) as pod_a,
        run_app(support_doc, store=store) as pod_b,
    ):
        assert pod_a.runtime.agent.graph is not pod_b.runtime.agent.graph
        message = '{"tool": "Add", "args": {"a": 5, "b": 6}}'
        sid, _, sse1 = await _interrupt(pod_a, message)

        pending = (await pod_b.client.get(f"/sessions/{sid}/pending")).json()
        assert pending["action_requests"][0]["args"] == {"a": 5, "b": 6}

        response, sse2 = await resume(pod_b, sid, [{"type": "approve"}])
        assert response.status_code == 200
        assert events_named(sse2, "tool_result")[0]["content"] == "11"
        assert events_named(sse2, "done")[0]["text"] == "결과: 11"
        assert fake_mcp.calls["Add"] == 1

        row = (await pod_a.client.get(f"/sessions/{sid}")).json()
        assert row["session"]["status"] == "active" and row["pending"] is False
        assert (await pod_a.client.get(f"/sessions/{sid}/pending")).status_code == 404
        expected = folded((message, sse1), (None, sse2))
        resolve_interrupts(expected, pending=False)
        for pod in (pod_a, pod_b):
            assert await transcript_items(pod, sid) == expected
