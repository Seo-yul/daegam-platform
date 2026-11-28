"""작업 세션이다(runs.py, 기능 `agent_tasks`). 플랫폼처럼 `/chat` 에 `run_id` 와 `task` 를 싣고, 턴
사이에 열려 있는 세션, 같은 세션의 새 턴으로 도는 다음 실행, 실행마다의 `run_exists`, 다음 실행을
받지 않는 세션과 상태를 본다. 색인이 작업 세션의 실행 id 를 지키는 계약은 store_fixtures.py 에
있다."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException

from runtime import stream
from runtime.auth import get_caller
from runtime.features import AGENT_TASKS, CAPABILITIES, DOCUMENT_FEATURES
from runtime.runs import RUN_ID_HEADER, check_run_fields
from runtime.sessions import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PENDING, utcnow
from tests.app_fixtures import (
    FAIL_MARK,
    FailOnceModel,
    regenerate,
    resume,
    sid_of,
    status_and_code,
)
from tests.conftest import chat
from tests.safety_fixtures import ADD, interrupt_of, safety_app
from tests.test_runs import OUTSIDE, record_of, write_doc

FIRST, SECOND, THIRD = "run-0001", "run-0002", "run-0003"


async def test_a_task_session_stays_open_and_takes_the_next_run_as_a_new_turn(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        index = running.runtime.store.index
        response, sse = await chat(running, ADD, run_id=FIRST, task=True)
        assert response.status_code == 200, response.text
        assert response.headers[RUN_ID_HEADER] == FIRST
        sid = sid_of(sse, "done")
        first = await record_of(running, sid)
        assert (first.run_id, first.task, first.status, first.turns) == (
            FIRST,
            True,
            STATUS_ACTIVE,
            1,
        )
        # 턴 사이에 체크포인트가 남아 다음 실행이 앞 턴의 대화를 잇는다
        snapshot = await running.runtime.agent.graph.aget_state(stream.thread_config(sid))
        assert snapshot.values["messages"]

        response, sse = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        assert response.status_code == 200, response.text
        assert response.headers[RUN_ID_HEADER] == SECOND
        assert response.headers["X-Daegam-Session-Id"] == sid
        assert sid_of(sse, "done") == sid and fake_mcp.calls["Add"] == 2
        second = await record_of(running, sid)
        assert (second.run_id, second.status, second.turns) == (SECOND, STATUS_ACTIVE, 2)
        assert second.turn_id != first.turn_id

        starts, ends = running.observed("turn_start"), running.observed("turn_end")
        assert [(s["run_id"], s["ephemeral"]) for s in starts] == [(FIRST, True), (SECOND, True)]
        assert [(e["run_id"], e["turn_id"]) for e in ends] == [
            (FIRST, first.turn_id),
            (SECOND, second.turn_id),
        ]
        # 세션은 두 실행의 id 를 모두 쥔다. 어느 쪽의 둘째 시작도 새 턴을 열지 않고, 턴 id 는
        # 가장 최근 실행의 것만 싣는다
        for run_id, session_id, turn_id in ((FIRST, None, None), (SECOND, sid, second.turn_id)):
            again, _ = await chat(running, ADD, session_id=session_id, run_id=run_id, task=True)
            assert status_and_code(again) == (409, "run_exists")
            assert (again.json()["session_id"], again.json()["turn_id"]) == (sid, turn_id)
        assert fake_mcp.calls["Add"] == 2
        assert [row.session_id for row in await index.list(runs="only")] == [sid]
        assert await index.list() == []


async def test_only_a_task_session_takes_a_next_run(fake_mcp) -> None:
    """다음 실행은 작업 세션만 받는다. 실행 하나로 끝나는 세션과 채팅 세션은 409 `run_session` 이고,
    작업 세션도 run_id 없는 새 말과 다시 생성은 받지 않는다."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        once, _ = await chat(running, ADD, run_id=FIRST, untrusted=True)
        talk, sse = await chat(running, ADD)
        for response in (once, talk):
            sid = response.headers["X-Daegam-Session-Id"]
            refused, _ = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
            assert status_and_code(refused) == (409, "run_session")
        assert await running.runtime.store.index.find_run(SECOND) is None

        response, sse = await chat(running, ADD, run_id=THIRD, task=True)
        sid = sid_of(sse, "done")
        message, _ = await chat(running, ADD, session_id=sid)
        assert status_and_code(message) == (409, "run_session")
        again, _ = await regenerate(running, sid)
        assert status_and_code(again) == (409, "run_session")
        bob, _ = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True, user_sub="bob")
        assert status_and_code(bob) == (403, "not_session_owner")
        assert (await record_of(running, sid)).run_id == THIRD


async def test_an_error_turn_leaves_a_task_session_open(fake_mcp) -> None:
    doc = write_doc(fake_mcp.url)
    doc["main"]["limits"]["retries"] = 0
    async with safety_app(doc, model=FailOnceModel(model_name="stub")) as running:
        response, sse = await chat(running, f"{FAIL_MARK} 해라", run_id=FIRST, task=True)
        assert response.status_code == 200 and sse[-1][0] == "error"
        sid = response.headers["X-Daegam-Session-Id"]
        assert (await record_of(running, sid)).status == STATUS_ACTIVE
        [end] = running.observed("turn_end")
        assert (end["run_id"], end["code"]) == (FIRST, sse[-1][1]["code"])

        response, sse = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        assert response.status_code == 200 and sid_of(sse, "done") == sid
        assert (await record_of(running, sid)).status == STATUS_ACTIVE


async def test_a_task_run_waiting_for_approval_is_decided_before_the_next_run(fake_mcp) -> None:
    """승인을 기다리는 작업 세션은 다음 실행을 받지 않고(409 `session_pending`), 결정은 그 세션의
    가장 최근 실행의 id 를 싣는다. 재개된 턴이 끝나도 세션은 열려 있고 표시된 대화로 남는다."""
    approve = [{"type": "approve"}]
    async with safety_app(write_doc(fake_mcp.url)) as running:
        response, sse = await chat(running, ADD, run_id=FIRST, task=True, untrusted=True)
        interrupt_of(sse)
        sid = response.headers["X-Daegam-Session-Id"]
        waiting, _ = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        assert status_and_code(waiting) == (409, "session_pending")
        resumed, sse = await resume(running, sid, approve, run_id=FIRST)
        assert resumed.headers[RUN_ID_HEADER] == FIRST and sse[-1][0] == "done"
        assert (await record_of(running, sid)).status == STATUS_ACTIVE

        response, sse = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        [action] = interrupt_of(sse)["action_requests"]
        assert action["reason"] == "outside" and fake_mcp.calls["Add"] == 1
        assert (await record_of(running, sid)).status == STATUS_PENDING
        stale, _ = await resume(running, sid, approve, run_id=FIRST)
        assert status_and_code(stale) == (409, "run_closed")
        resumed, sse = await resume(running, sid, approve, run_id=SECOND)
        assert resumed.headers[RUN_ID_HEADER] == SECOND and sse[-1][0] == "done"
        assert fake_mcp.calls["Add"] == 2
        assert (await record_of(running, sid)).status == STATUS_ACTIVE


async def test_a_next_run_that_fails_before_its_turn_keeps_the_session_and_frees_the_run(
    fake_mcp,
) -> None:
    """턴이 열리기 전에 실패한 다음 실행은 있던 작업 세션을 지우지 않고 그 실행 id 도 쥐지 않는다.
    다시 한 시작이 그 실행을 새 턴으로 연다. 위임의 사람이 어긋난 시작이 그런 실패다."""
    async with safety_app(write_doc(fake_mcp.url)) as running:
        _, sse = await chat(running, ADD, run_id=FIRST, task=True)
        sid = sid_of(sse, "done")
        bob = {"id": "g-1", "secret": "s", "sub": "bob", "expires_in": 60, "renew_url": "http://x"}
        body = {"message": ADD, "user_sub": "alice", "session_id": sid, "run_id": SECOND}
        refused = await running.client.post("/chat", json={**body, "task": True, "turn_grant": bob})
        assert status_and_code(refused) == (409, "turn_grant_mismatch")
        kept = await record_of(running, sid)
        assert (kept.run_id, kept.status) == (FIRST, STATUS_ACTIVE)
        assert await running.runtime.store.index.find_run(SECOND) is None
        response, sse = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        assert response.status_code == 200 and sid_of(sse, "done") == sid


async def test_a_task_session_past_its_ttl_takes_no_run(fake_mcp) -> None:
    doc = write_doc(fake_mcp.url)
    async with safety_app(doc) as running:
        _, sse = await chat(running, ADD, run_id=FIRST, task=True)
        sid = sid_of(sse, "done")
        index = running.runtime.store.index
        idle = utcnow() - timedelta(seconds=doc["entry"]["session_ttl_seconds"] + 1)
        await index.update(sid, last_turn_at=idle)
        late, _ = await chat(running, ADD, session_id=sid, run_id=SECOND, task=True)
        assert status_and_code(late) == (410, "session_closed")
        assert await running.runtime.store.close_expired(utcnow() - timedelta(seconds=1)) == [sid]
        assert (await record_of(running, sid)).status == STATUS_CLOSED
        # 닫힌 세션도 실행 id 를 쥐고 있다
        again, _ = await chat(running, ADD, run_id=FIRST, task=True)
        assert status_and_code(again) == (409, "run_exists")


def test_task_fields_mark_a_run() -> None:
    check_run_fields(FIRST, False, None, True)
    check_run_fields(SECOND, False, "s1", True)
    for run_id, session_id, task, field in (
        (None, None, True, "task"),
        (SECOND, "s1", False, "run_id"),
    ):
        with pytest.raises(HTTPException) as e:
            check_run_fields(run_id, False, session_id, task)
        [entry] = e.value.detail["errors"]
        assert entry["loc"] == ["body", field] and entry["type"] == "run_fields"


async def test_the_task_field_is_refused_on_the_external_path(fake_mcp) -> None:
    async with safety_app(write_doc(fake_mcp.url)) as running:
        running.app.dependency_overrides[get_caller] = lambda: OUTSIDE
        response = await running.client.post("/chat", json={"message": "hi", "task": True})
        assert status_and_code(response) == (422, "validation_error")
        assert [err["loc"][-1] for err in response.json()["errors"]] == ["task"]


def test_agent_tasks_is_a_capability_not_a_document_feature() -> None:
    assert AGENT_TASKS in CAPABILITIES and AGENT_TASKS not in DOCUMENT_FEATURES
