"""평가 미리 보기가 쓰기 Tool 을 잡는 승인을 본다(P45 10절, F0.3).

플랫폼의 평가 컴파일은 읽기라고 알 수 없는 이름의 승인을 결정이 `respond`·`reject` 뿐인 것으로
바꾸고, 미리 보기의 문서는 기능 `in_pod_state` 를 싣는다. 가짜 MCP 서버(`conftest.py` `fake_mcp`)가
Add 의 호출을 센다. 어느 결정에도 Add 는 불리지 않는다. `respond` 의 글은 Tool 의 결과로 모델에
가고, 손으로 보낸 `approve`·`edit` 은 런타임이 `decision_not_allowed` 로 거절한다
(`runtime/hitl.py`). 메인은 소스의 `approval` 을, 서브에이전트는 제 `interrupt_on` 을 쓰므로 둘 다
본다."""

from __future__ import annotations

from typing import Any

from tests.app_fixtures import resume, sid_of, subagent_spec
from tests.attachment_fixtures import document
from tests.conftest import RunningApp, run_app
from tests.safety_fixtures import ADD, interrupt_of, send, turns_doc
from tests.web_fixtures import ScriptModel, call, results_of

HELD = {"Add": ["respond", "reject"]}
SIMULATED = "가상 결과: 3"
APPROVE = [{"type": "approve"}]


def held_doc(url: str) -> dict[str, Any]:
    """평가 미리 보기의 문서다. memory 노드가 없고 상태는 파드 안에 있다."""
    return turns_doc(url, approval=HELD, memory=False, in_pod_state=True)


async def held_session(running: RunningApp, message: str = ADD, **body: Any) -> str:
    """Add 를 부르는 턴을 보내 승인 대기로 끝난 세션의 id 다."""
    sse = await send(running, message, **body)
    [action] = interrupt_of(sse)["action_requests"]
    assert action["name"] == "Add"
    [config] = interrupt_of(sse)["review_configs"]
    assert config["allowed_decisions"] == ["respond", "reject"]
    return sid_of(sse, "interrupt")


async def refused(running: RunningApp, sid: str, decisions: list[dict[str, Any]]) -> None:
    response, sse = await resume(running, sid, decisions)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "decision_not_allowed" and sse == []


async def test_approve_and_edit_are_refused_and_nothing_is_called(fake_mcp) -> None:
    async with run_app(held_doc(fake_mcp.url)) as running:
        sid = await held_session(running)
        edit = [{"type": "edit", "edited_action": {"name": "Add", "args": {"a": 1, "b": 2}}}]
        for decisions in (APPROVE, edit):
            await refused(running, sid, decisions)
        assert fake_mcp.calls["Add"] == 0
        pending = (await running.client.get(f"/sessions/{sid}/pending")).json()
        assert [a["name"] for a in pending["action_requests"]] == ["Add"]


async def test_respond_hands_its_text_to_the_model_without_calling(fake_mcp) -> None:
    async with run_app(held_doc(fake_mcp.url)) as running:
        sid = await held_session(running)
        response, sse = await resume(running, sid, [{"type": "respond", "message": SIMULATED}])
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done"
        [result] = results_of(sse, "Add")
        assert result["status"] == "success" and SIMULATED in result["content"]
        assert fake_mcp.calls["Add"] == 0


async def test_reject_ends_the_call_without_calling(fake_mcp) -> None:
    async with run_app(held_doc(fake_mcp.url)) as running:
        sid = await held_session(running)
        response, sse = await resume(running, sid, [{"type": "reject", "message": "평가 실행"}])
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done"
        [result] = results_of(sse, "Add")
        assert result["status"] == "error"
        assert fake_mcp.calls["Add"] == 0


async def test_a_write_after_outside_content_keeps_the_held_decisions(fake_mcp) -> None:
    """첨부로 바깥 내용을 가진 대화의 쓰기도 같은 결정만 받는다. 결정은 이름마다 고정이다."""
    async with run_app(held_doc(fake_mcp.url)) as running:
        sid = await held_session(running, attachments=[document()])
        await refused(running, sid, APPROVE)
        response, sse = await resume(running, sid, [{"type": "respond", "message": SIMULATED}])
        assert response.status_code == 200 and sse[-1][0] == "done"
        assert fake_mcp.calls["Add"] == 0


async def test_a_sub_agent_holds_the_write_by_its_own_rules(fake_mcp) -> None:
    """서브에이전트는 소스의 `approval` 이 아니라 제 `interrupt_on` 으로 묻는다. 그래서 소스에는
    승인을 두지 않고 서브에이전트에만 잡은 결정을 둔다. 서브에이전트가 부른 Add 가 기다리고,
    `approve` 는 거절되며 불리지 않는다."""
    doc = turns_doc(fake_mcp.url, memory=False, in_pod_state=True)
    doc["subagents"] = [subagent_spec(doc, "calc", interrupt_on=HELD)]
    doc["main"]["subagents"] = ["calc"]
    model = ScriptModel(calls=[call("task", subagent_type="calc", description=ADD)])
    async with run_app(doc, model=model) as running:
        sid = await held_session(running, "계산을 맡겨 줘")
        await refused(running, sid, APPROVE)
        assert fake_mcp.calls["Add"] == 0
        response, sse = await resume(running, sid, [{"type": "respond", "message": SIMULATED}])
        assert response.status_code == 200, response.text
        assert sse[-1][0] == "done" and fake_mcp.calls["Add"] == 0
