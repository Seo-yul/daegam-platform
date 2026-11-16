"""실행 세션의 결정과 승인자의 받은 결정함 (P41 7절, `services/runs/approvals.py`). 플랫폼이 결과를
정한다. 스토어를 메모리 가짜로 바꿔 DB 없이 돈다. 런타임을 부르는 라우트의 갈래는
tests/test_agent_runs_api.py 가 본다."""

from __future__ import annotations

import asyncio

import pytest

from daegam.services.runs.approvals import SESSION_CLOSED, RunApprovals
from daegam.services.runs.service import RunError
from daegam.store.agent_runs import AgentRun
from tests.helpers.runs import NOW, FakeRuns, FakeTurns, run_of, service_of, waiting_run_of


def approvals(runs: FakeRuns, turns: FakeTurns | None = None) -> RunApprovals:
    return service_of(runs, turns=turns).approvals


def begin(
    runs: FakeRuns, session_id: str = "s1", *, sub: str = "alice", approver: bool = False
) -> AgentRun | None:
    return asyncio.run(
        approvals(runs).begin("default", "ag-1", session_id, sub=sub, approver=approver)
    )


def test_the_bound_person_moves_a_waiting_run_to_running_before_the_runtime():
    runs = FakeRuns()
    run = runs.add(waiting_run_of())
    assert begin(runs) is not None
    assert run.status == "running" and run.last_event_at > NOW


def test_an_approver_decides_another_persons_run():
    runs = FakeRuns()
    run = runs.add(waiting_run_of())
    assert begin(runs, sub="bob", approver=True) is not None
    assert run.status == "running"


def test_someone_neither_bound_nor_approver_is_refused_before_any_write():
    runs = FakeRuns()
    run = runs.add(waiting_run_of())
    with pytest.raises(RunError) as e:
        begin(runs, sub="viewer")
    assert (e.value.status, e.value.code) == (403, "forbidden")
    # 승인자의 결정을 막도록 실행을 옮겨 두지 못한다
    assert (run.status, run.last_event_at) == ("waiting_approval", NOW)


def test_a_chat_session_is_not_a_run():
    runs = FakeRuns()
    runs.add(waiting_run_of())
    assert begin(runs, "chat-session", sub="viewer") is None


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "refused"},
        {"status": "running"},
        {"deadline": NOW},
        {"stop_requested_at": NOW},
    ],
)
def test_a_run_no_longer_waiting_answers_run_closed(fields):
    runs = FakeRuns()
    run = runs.add(waiting_run_of(**fields))
    with pytest.raises(RunError) as e:
        begin(runs)
    assert (e.value.status, e.value.code) == (409, "run_closed")
    assert run.status == fields.get("status", "waiting_approval")


def test_a_refused_decision_moves_back_and_a_closed_session_refuses():
    runs = FakeRuns()
    run = runs.add(waiting_run_of(status="running"))
    service = approvals(runs)
    asyncio.run(service.refused(run, 422, "decision_count"))
    assert run.status == "waiting_approval"
    run.status = "running"
    asyncio.run(service.refused(run, 410, SESSION_CLOSED))
    assert (run.status, run.reason) == ("refused", SESSION_CLOSED)


def test_a_decision_that_opened_no_turn_waits_again_unless_a_stop_arrived():
    runs = FakeRuns()
    run = runs.add(waiting_run_of(status="running"))
    asyncio.run(approvals(runs).not_sent(run))
    assert run.status == "waiting_approval"
    marked = runs.add(
        waiting_run_of(
            status="running", session_id="s2", stop_requested_at=NOW, stop_reason="paused"
        )
    )
    asyncio.run(approvals(runs).not_sent(marked))
    assert (marked.status, marked.reason) == ("cancelled", "paused")


def test_a_stop_during_an_accepted_decision_stops_the_turn_the_answer_names():
    runs, turns = FakeRuns(), FakeTurns()
    run = runs.add(waiting_run_of(status="running"))
    service = approvals(runs, turns)
    asyncio.run(service.accepted(run, "t2"))
    assert turns.stopped == []
    run.stop_requested_at = NOW
    asyncio.run(service.accepted(run, "t2"))
    assert [(r.id, r.turn_id) for r in turns.stopped] == [(run.id, "t2")]


# -- 받은 결정함 -------------------------------------------------------------------------------


def _rows(*ids: str) -> list[dict]:
    return [{"session_id": sid, "status": "pending"} for sid in ids]


def mark(runs: FakeRuns, ids: tuple[str, ...], runs_mode: str, status: str | None) -> list[dict]:
    return asyncio.run(
        approvals(runs).mark_sessions(
            "default",
            "ag-1",
            _rows(*ids),
            runs=runs_mode,
            status=status,  # type: ignore[arg-type]
        )
    )


def test_session_rows_carry_their_run_with_a_deadline_only_while_it_waits():
    runs = FakeRuns()
    run = runs.add(waiting_run_of())
    runs.add(run_of(status="refused", session_id="s2", deadline=NOW))
    marked = mark(runs, ("s1", "s2", "chat"), "include", None)
    assert [m["run"] and m["run"]["id"] for m in marked][0] == run.id
    assert marked[0]["run"]["deadline"] == run.deadline
    assert marked[1]["run"]["deadline"] is None and marked[2]["run"] is None
    only = mark(runs, ("s1", "s2", "chat"), "only", None)
    assert [m["session_id"] for m in only] == ["s1", "s2"]


def test_the_inbox_keeps_only_runs_that_still_wait():
    runs = FakeRuns()
    runs.add(waiting_run_of())
    # 마감으로 거절된 실행의 세션은 런타임이 닫기 전까지 pending 으로 남는다
    runs.add(run_of(status="refused", session_id="s2"))
    runs.add(run_of(status="cancelled", session_id="s3"))
    marked = mark(runs, ("s1", "s2", "s3", "chat"), "only", "pending")
    assert [m["session_id"] for m in marked] == ["s1"]
