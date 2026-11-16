"""턴을 지나는 바깥 표시와 판정이다(P43). 가짜 MCP 서버의 Add 를 바깥 Tool 이나 안쪽 Tool 로 두고
`/chat`, 결정 재개, 다시 생성으로 본다. 판정 표와 감싸개 자체는 test_tool_safety.py 에 있고, 문서와
앱은 safety_fixtures.py 에 있다."""

from __future__ import annotations

from runtime.outside import SUBAGENT_OUTSIDE_KEY
from tests.app_fixtures import (
    regenerate,
    resume,
    sid_of,
    subagent_spec,
    tool_message,
    transcript_items,
)
from tests.attachment_fixtures import document
from tests.conftest import RunningApp, events_named, run_app
from tests.safety_fixtures import (
    ADD,
    RULE,
    choose,
    interrupt_of,
    safety_app,
    send,
    turns_doc,
    with_outside_source,
    without_memory,
)
from tests.store_fixtures import pod_store
from tests.web_fixtures import ScriptModel, call, code_of, results_of


async def flagged(running: RunningApp, sid: str) -> bool:
    return (await running.runtime.store.index.get(sid)).outside_at is not None


async def test_an_outside_result_is_wrapped_marked_and_the_next_write_asks(fake_mcp) -> None:
    async with safety_app(turns_doc(fake_mcp.url)) as running:
        sse = await send(running)
        [result] = results_of(sse, "Add")
        assert result["status"] == "success" and result["untrusted"] == "outside"
        assert result["content"].startswith('<outside_content source="Add">\n3\n')
        sid = sid_of(sse, "done")
        assert await flagged(running, sid)
        [card] = [i for i in await transcript_items(running, sid) if i.get("kind") == "tool_call"]
        assert card["result"]["untrusted"] == "outside"
        assert "outside_at" not in (await running.client.get(f"/sessions/{sid}")).json()["session"]

        sse = await send(running, tool_message("Add", a=3, b=4), session_id=sid)
        interrupt = interrupt_of(sse)
        [action] = interrupt["action_requests"]
        assert action["name"] == "Add" and action["reason"] == "outside"
        assert not action["description"].startswith("reason:")
        assert interrupt["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]
        assert fake_mcp.calls["Add"] == 1
        pending = (await running.client.get(f"/sessions/{sid}/pending")).json()
        assert pending["action_requests"][0]["reason"] == "outside"

        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200, response.text
        assert fake_mcp.calls["Add"] == 2 and sse[-1][0] == "done"


async def test_tools_marked_inside_and_read_never_ask(fake_mcp) -> None:
    async with safety_app(turns_doc(fake_mcp.url, inside=("Add",), read=("Add",))) as running:
        sse = await send(running)
        [result] = results_of(sse, "Add")
        assert "untrusted" not in result and result["content"] == "3"
        sid = sid_of(sse, "done")
        assert not await flagged(running, sid)
        sse = await send(running, session_id=sid)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 2


async def test_an_inside_write_asks_once_the_chat_holds_an_attachment(fake_mcp) -> None:
    async with safety_app(turns_doc(fake_mcp.url, inside=("Add",))) as running:
        [action] = interrupt_of(await send(running, attachments=[document()]))["action_requests"]
        assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 0


async def test_a_remembered_reject_refuses_without_asking(fake_mcp) -> None:
    async with safety_app(turns_doc(fake_mcp.url, inside=("Add",), approval=RULE)) as running:
        sse = await send(running, approval_choices=choose("Add", "reject"))
        [result] = results_of(sse, "Add")
        assert code_of(result) == "tool_choice_rejected"
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 0


async def test_a_refused_or_rejected_call_does_not_mark_the_chat(fake_mcp) -> None:
    """Add 는 바깥 Tool 이다. 돌지 않은 호출(기억한 거절, 사람의 거절)의 결과는 바깥 내용이 아니라
    다음 턴의 기억한 승인이 그대로 쓰인다."""
    doc = turns_doc(fake_mcp.url, read=("Add",), approval=RULE)
    approve = choose("Add", "approve")
    async with safety_app(doc) as running:
        sid = sid_of(await send(running, approval_choices=choose("Add", "reject")), "done")
        sse = await send(running, session_id=sid, approval_choices=approve)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1

        sid = sid_of(await send(running), "interrupt")
        response, _ = await resume(running, sid, [{"type": "reject"}])
        assert response.status_code == 200, response.text
        sse = await send(running, session_id=sid, approval_choices=approve)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 2


async def test_a_remembered_approve_skips_the_rule_until_the_chat_holds_outside_content(
    fake_mcp,
) -> None:
    doc = turns_doc(fake_mcp.url, inside=("Add",), read=("Add",), approval=RULE)
    approve = choose("Add", "approve")
    async with safety_app(doc) as running:
        sse = await send(running, approval_choices=approve)
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1
        # 기억이 없으면 규칙대로 묻는다
        [action] = interrupt_of(await send(running))["action_requests"]
        assert action["reason"] == "rule"
        # 바깥 내용(첨부)을 가진 대화에서는 기억한 승인이 쓰이지 않는다. 읽기라 이유는 규칙이다
        sse = await send(running, approval_choices=approve, attachments=[document()])
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "rule" and fake_mcp.calls["Add"] == 1


async def test_without_a_memory_node_writes_after_outside_content_are_refused(fake_mcp) -> None:
    async with run_app(turns_doc(fake_mcp.url, memory=False)) as running:
        sid = sid_of(await send(running), "done")
        sse = await send(running, session_id=sid)
        [result] = results_of(sse, "Add")
        assert code_of(result) == "outside_writes_refused"
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1


async def test_without_a_memory_node_a_rule_call_is_refused(fake_mcp) -> None:
    doc = turns_doc(fake_mcp.url, inside=("Add",), read=("Add",), approval=RULE, memory=False)
    async with run_app(doc) as running:
        sse = await send(running)
        [result] = results_of(sse, "Add")
        assert code_of(result) == "approval_unavailable"
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 0


async def test_in_pod_state_waits_for_approval_without_a_memory_node(fake_mcp) -> None:
    """미리 보기의 문서다(P44b 7절). 저장소가 파드 안의 것이어도 규칙이 있는 호출과 바깥 내용 뒤의
    쓰기가 승인을 기다리고, 결정이 같은 파드에서 그 턴을 재개한다."""
    doc = turns_doc(fake_mcp.url, approval=RULE, memory=False, in_pod_state=True)
    async with run_app(doc) as running:
        assert running.runtime.store.shared is False
        sse = await send(running)
        [action] = interrupt_of(sse)["action_requests"]
        assert action["name"] == "Add" and action["reason"] == "rule"
        sid = sid_of(sse, "interrupt")
        assert fake_mcp.calls["Add"] == 0
        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1
        assert await flagged(running, sid)

        sse = await send(running, tool_message("Add", a=3, b=4), session_id=sid)
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "outside"
        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 2
        [result] = results_of(sse, "Add")
        assert result["status"] == "success" and "7" in result["content"]


async def test_a_subagent_read_gates_the_main_agents_next_write(fake_mcp) -> None:
    """서브에이전트의 소스는 Add 가 바깥이고 메인의 소스는 안쪽이다. 서브에이전트가 읽은 뒤 같은
    턴의 메인 쓰기가 묻는다."""
    doc = with_outside_source(turns_doc(fake_mcp.url, inside=("Add",)))
    doc["subagents"] = [subagent_spec(doc, "reader", tools=["tools-outside"])]
    doc["main"]["subagents"] = ["reader"]
    model = ScriptModel(
        calls=[call("task", subagent_type="reader", description=ADD), call("Add", a=3, b=4)]
    )
    async with safety_app(doc, model=model) as running:
        sse = await send(running, "읽고 써 줘")
        [task] = results_of(sse, "task")
        assert task["untrusted"] == "outside" and task["content"].startswith("<outside_content")
        [action] = interrupt_of(sse)["action_requests"]
        assert action["name"] == "Add" and action["reason"] == "outside"
        assert fake_mcp.calls["Add"] == 1
        state = await running.runtime.agent.graph.aget_state(
            {"configurable": {"thread_id": sid_of(sse, "interrupt")}}
        )
        [task_message] = [m for m in state.values["messages"] if getattr(m, "name", "") == "task"]
        assert task_message.artifact[SUBAGENT_OUTSIDE_KEY] is True


async def test_parallel_task_calls_mark_only_the_one_whose_subagent_read_outside(fake_mcp) -> None:
    doc = with_outside_source(turns_doc(fake_mcp.url, inside=("Add",), read=("Add",)))
    doc["subagents"] = [
        subagent_spec(doc, "reader", tools=["tools-outside"]),
        subagent_spec(doc, "calc"),
    ]
    doc["main"]["subagents"] = ["reader", "calc"]
    together = [
        call("task", subagent_type="reader", description=ADD),
        call("task", subagent_type="calc", description=ADD),
    ]
    async with safety_app(doc, model=ScriptModel(calls=[together])) as running:
        sse = await send(running, "둘 다 맡겨 줘")
        tasks = results_of(sse, "task")
        assert sorted(t.get("untrusted", "-") for t in tasks) == ["-", "outside"]
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 2


async def test_a_subagent_interrupt_carries_its_reason(fake_mcp) -> None:
    doc = turns_doc(fake_mcp.url, inside=("Add",), read=("Add",))
    doc["subagents"] = [subagent_spec(doc, interrupt_on=RULE)]
    doc["main"]["subagents"] = ["calc"]
    model = ScriptModel(calls=[call("task", subagent_type="calc", description=ADD)])
    async with safety_app(doc, model=model) as running:
        sse = await send(running, "계산해 줘")
        [action] = interrupt_of(sse)["action_requests"]
        assert action["name"] == "Add" and action["reason"] == "rule"
        response, sse = await resume(running, sid_of(sse, "interrupt"), [{"type": "approve"}])
        assert response.status_code == 200, response.text
        assert fake_mcp.calls["Add"] == 1


async def test_only_the_resumed_task_call_counts_as_outside(fake_mcp) -> None:
    """재개 턴에서 새로 부른 task 는 재개된 호출이 아니다. 그 서브에이전트가 바깥 Tool 을 가졌어도
    바깥 결과를 받지 않았으면 표시하지 않는다."""
    doc = with_outside_source(
        turns_doc(fake_mcp.url, inside=("Add",), read=("Add",), approval=RULE)
    )
    doc["subagents"] = [subagent_spec(doc, "reader", tools=["tools-outside"])]
    doc["main"]["subagents"] = ["reader"]
    # 서브에이전트의 스텁은 sleep 조각이면 Tool 없이 글로 답한다
    quiet = call("task", subagent_type="reader", description='{"sleep": 0}')
    model = ScriptModel(calls=[call("Add", a=1, b=2), quiet])
    async with safety_app(doc, model=model) as running:
        sid = sid_of(await send(running, "더하고 맡겨 줘"), "interrupt")
        response, sse = await resume(running, sid, [{"type": "approve"}])
        assert response.status_code == 200, response.text
        [task] = results_of(sse, "task")
        assert "untrusted" not in task and not await flagged(running, sid)


async def test_a_resume_follows_the_verdicts_stored_with_the_message(fake_mcp) -> None:
    """대기 중에 선택이 바뀌어도 재개는 같은 판정으로 묻고, 그 사이에 생긴 거절은 더 엄하니
    따른다."""
    doc = turns_doc(fake_mcp.url, inside=("Add",), read=("Add",), approval=RULE)
    async with safety_app(doc) as running:
        sid = sid_of(await send(running), "interrupt")
        approve = choose("Add", "approve")
        response, sse = await resume(running, sid, [{"type": "approve"}], approval_choices=approve)
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 1

        sid = sid_of(await send(running), "interrupt")
        reject = choose("Add", "reject")
        response, sse = await resume(running, sid, [{"type": "approve"}], approval_choices=reject)
        assert response.status_code == 200, response.text
        [result] = results_of(sse, "Add")
        assert code_of(result) == "tool_choice_rejected" and fake_mcp.calls["Add"] == 1


async def test_an_older_session_with_an_outside_result_asks_after_the_upgrade(fake_mcp) -> None:
    """옛 판이 쓴 세션에는 표시가 없다. 지금 문서로 바깥인 Tool 의 결과가 기록에 있으면 묻는다."""
    old = turns_doc(fake_mcp.url, feature=False)
    new = turns_doc(fake_mcp.url)
    async with pod_store(without_memory(new)) as store:
        async with safety_app(old, store=store) as running:
            sid = sid_of(await send(running), "done")
        async with safety_app(new, store=store) as running:
            assert not await flagged(running, sid)
            [action] = interrupt_of(await send(running, session_id=sid))["action_requests"]
            assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 1


async def test_a_regenerated_turn_keeps_the_flag(fake_mcp) -> None:
    async with safety_app(turns_doc(fake_mcp.url)) as running:
        sid = sid_of(await send(running), "done")
        response, sse = await regenerate(running, sid)
        assert response.status_code == 200, response.text
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 1


async def test_choices_without_the_feature_change_nothing(fake_mcp) -> None:
    doc = turns_doc(fake_mcp.url, approval=RULE, feature=False)
    async with safety_app(doc) as running:
        sse = await send(running, approval_choices=choose("Add", "reject"))
        [action] = interrupt_of(sse)["action_requests"]
        assert action["name"] == "Add" and "reason" not in action
        assert events_named(sse, "tool_result") == []
