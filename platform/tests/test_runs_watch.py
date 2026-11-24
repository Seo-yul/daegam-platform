"""턴 이벤트로 옮기는 실행의 상태와 지켜보기 워커 (P41 7·8절). 스토어와 런타임 프록시를 메모리
가짜로 바꿔 DB 없이 돈다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from daegam.services.runs.events import RunEvents, approval_deadline, turn_end_outcome
from daegam.services.runs.watch import DEADLINE_PASSED, RunWatchWorker
from daegam.store.agent_runs import STOPPED, AgentRun
from daegam.store.base import ItemLease
from tests.helpers.runs import NOW, FakeAgent, FakeAgents, FakeRuns, run_of

# 템플릿의 턴 시한(120초)과 60초 (tests/helpers/agents.py template_spec)
QUIET = timedelta(seconds=120 + 60)


def event(name: str, run: AgentRun, ts: datetime, **fields: Any) -> dict[str, Any]:
    data = {"run_id": run.id, **fields.pop("data", {})}
    return {
        "_id": f"{name}-{ts.isoformat()}",
        "event": name,
        "ts": ts,
        "session_id": fields.pop("session_id", "s1"),
        "turn_id": "t1",
        "data": data,
        **fields,
    }


def apply(runs: FakeRuns, *docs: dict[str, Any], agent_id: str = "ag-1") -> None:
    asyncio.run(RunEvents(runs, FakeAgents()).apply(agent_id, "default", list(docs)))  # type: ignore[arg-type]


def started(runs: FakeRuns, **fields: Any) -> AgentRun:
    return runs.add(run_of(**{"status": "running", "session_id": "s1", "turn_id": "t1", **fields}))


# -- 턴의 끝이 정하는 것 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        ("done", None, ("succeeded", None)),
        ("pending", None, ("waiting_approval", None)),
        ("cancelled", "turn_cancelled", ("cancelled", STOPPED)),
        ("cancelled", "turn_access_lost", ("failed", "turn_access_lost")),
        ("turn_timeout", None, ("failed", "turn_timeout")),
        ("turn_aborted", None, ("failed", "turn_aborted")),
    ],
)
def test_a_turn_end_decides_the_run_status(status, code, outcome):
    assert turn_end_outcome(status, code) == outcome


def test_the_deadline_comes_before_the_session_closes():
    assert approval_deadline(NOW, 3600) == NOW + timedelta(minutes=55)
    # 짧은 TTL 은 그 절반만 남긴다
    assert approval_deadline(NOW, 120) == NOW + timedelta(seconds=60)


# -- 이벤트 ------------------------------------------------------------------------------------


def test_a_turn_start_binds_the_session_and_moves_a_starting_run():
    runs = FakeRuns()
    run = runs.add(run_of(message="check"))
    apply(runs, event("turn_start", run, NOW))
    assert (run.status, run.session_id, run.turn_id, run.message) == ("running", "s1", "t1", None)
    assert run.event_at == NOW


def test_an_interrupt_waits_with_a_deadline_from_the_turn_end_time():
    runs = FakeRuns()
    run = started(runs, lease=ItemLease(owner="watch", until=NOW))
    apply(runs, event("turn_end", run, NOW, status="pending"))
    assert run.status == "waiting_approval" and run.lease is None
    # 템플릿의 세션 TTL 은 1시간이다
    assert run.deadline == approval_deadline(NOW, 3600)


def test_an_interrupt_of_a_marked_run_cancels_it_with_the_mark_reason():
    runs = FakeRuns()
    run = started(runs, stop_requested_at=NOW - timedelta(seconds=1), stop_reason="paused")
    apply(runs, event("turn_end", run, NOW, status="pending"))
    assert (run.status, run.reason) == ("cancelled", "paused")


def test_a_mark_left_after_the_read_still_cancels_the_interrupt():
    runs = FakeRuns()
    run = started(runs)
    events = RunEvents(runs, FakeAgents())  # type: ignore[arg-type]
    read = runs.get

    async def marked_after_read(tenant: str, agent_id: str, run_id: str) -> AgentRun | None:
        found = await read(tenant, agent_id, run_id)
        copy = found.model_copy(deep=True) if found is not None else None
        run.stop_requested_at, run.stop_reason = NOW, "paused"
        return copy

    runs.get = marked_after_read  # type: ignore[method-assign]
    doc = event("turn_end", run, NOW, status="pending")
    asyncio.run(events.apply("ag-1", "default", [doc]))
    # 표시의 까닭은 다시 읽어 남긴다
    assert (run.status, run.reason) == ("cancelled", "paused")


def test_a_stopped_turn_takes_the_reason_of_its_mark():
    runs = FakeRuns()
    run = started(runs, stop_requested_at=NOW, stop_reason="acts_as_lost_access")
    apply(runs, event("turn_end", run, NOW, status="cancelled", data={"code": "turn_cancelled"}))
    assert (run.status, run.reason) == ("cancelled", "acts_as_lost_access")


def test_a_resumed_turn_start_moves_a_waiting_run_back_to_running():
    runs = FakeRuns()
    run = started(runs, status="waiting_approval", deadline=NOW + timedelta(minutes=5))
    apply(runs, event("turn_start", run, NOW))
    assert run.status == "running"


def test_events_apply_in_their_own_time_order_and_never_reopen_an_ended_run():
    runs = FakeRuns()
    run = started(runs)
    # 한 봉투 안에서 순서가 뒤집혀 와도 시각대로 옮긴다
    apply(
        runs,
        event("turn_end", run, NOW + timedelta(seconds=5), status="done"),
        event("turn_start", run, NOW),
    )
    assert run.status == "succeeded"
    apply(runs, event("turn_start", run, NOW + timedelta(seconds=10)))
    assert run.status == "succeeded"


def test_an_older_event_is_dropped():
    runs = FakeRuns()
    run = started(runs, event_at=NOW)
    apply(runs, event("turn_end", run, NOW - timedelta(seconds=1), status="done"))
    assert run.status == "running"


def test_events_from_another_agent_or_session_do_not_move_the_run():
    runs = FakeRuns()
    run = started(runs)
    apply(runs, event("turn_end", run, NOW, status="done"), agent_id="ag-2")
    apply(runs, event("turn_end", run, NOW, status="done", session_id="other"))
    assert run.status == "running"


def test_events_without_a_run_id_are_left_alone():
    runs = FakeRuns()
    run = started(runs)
    plain = event("turn_end", run, NOW, status="done")
    plain["data"] = {}
    apply(runs, plain, {**event("model_call", run, NOW), "event": "model_call"})
    assert run.status == "running"


# -- 지켜보기 ----------------------------------------------------------------------------------


class SessionProxy:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.reads: list[dict[str, Any]] = []

    async def session(self, agent: Any, session_id: str, **kwargs: Any) -> tuple[int, Any]:
        self.reads.append({"session_id": session_id, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def watch(
    runs: FakeRuns,
    run: AgentRun,
    answer: Any = None,
    *,
    at: datetime,
    agents: FakeAgents | None = None,
    stale: AgentRun | None = None,
) -> SessionProxy:
    """at 에 기록 하나를 지켜본다. stale 을 주면 워커가 잡은 때의 기록이 그것이다."""
    proxy = SessionProxy(answer)
    worker = RunWatchWorker(
        agents or FakeAgents(),  # type: ignore[arg-type]
        runs,  # type: ignore[arg-type]
        proxy,  # type: ignore[arg-type]
        owner="w1",
        interval=15,
        lease_seconds=60,
    )
    asyncio.run(worker.watch(stale or run.model_copy(deep=True), at))
    return proxy


def test_a_run_past_its_deadline_is_refused_without_calling_the_runtime():
    runs = FakeRuns()
    run = started(runs, status="waiting_approval", deadline=NOW)
    proxy = watch(runs, run, at=NOW)
    assert (run.status, run.reason) == ("refused", DEADLINE_PASSED)
    assert proxy.reads == []


def test_a_decision_that_moved_the_run_first_wins_over_the_deadline():
    runs = FakeRuns()
    run = started(runs, status="waiting_approval", deadline=NOW)
    stale = run.model_copy(deep=True)
    run.status = "running"
    watch(runs, run, at=NOW, stale=stale)
    assert run.status == "running"


def test_a_run_not_yet_quiet_long_enough_waits_until_it_is():
    runs = FakeRuns()
    run = started(runs, last_event_at=NOW)
    proxy = watch(runs, run, at=NOW + timedelta(minutes=2))
    assert proxy.reads == [] and runs.deferred[run.id] == NOW + QUIET


def test_a_deploying_agent_is_looked_at_after_its_rollout():
    runs = FakeRuns()
    run = started(runs)
    proxy = watch(runs, run, at=NOW + QUIET, agents=FakeAgents(FakeAgent(state="deploying")))
    assert run.status == "running" and run.id in runs.deferred and proxy.reads == []


def _session(status: str = "active", running: bool = False, items: list | None = None) -> Any:
    session = {"status": status, "running": running, "last_turn_at": NOW.isoformat()}
    return 200, {"session": session, "pending": False, "transcript": {"items": items or []}}


@pytest.mark.parametrize(
    ("answer", "status", "reason"),
    [
        (_session(items=[{"kind": "user"}, {"kind": "done"}]), "succeeded", None),
        (_session(items=[{"kind": "error", "code": "turn_timeout"}]), "failed", "turn_timeout"),
        (_session(items=[{"kind": "assistant"}]), "failed", "run_lost"),
        (_session(), "failed", "run_lost"),
        ((404, {"code": "session_not_found"}), "failed", "run_lost"),
        (
            _session(status="closed", items=[{"kind": "interrupt", "resolved": False}]),
            "refused",
            DEADLINE_PASSED,
        ),
    ],
)
def test_a_quiet_run_is_settled_from_the_session(answer, status, reason):
    runs = FakeRuns()
    run = started(runs)
    proxy = watch(runs, run, answer, at=NOW + QUIET)
    assert (run.status, run.reason) == (status, reason)
    assert proxy.reads[0]["view"] == "progress" and proxy.reads[0]["approver"] is True


def test_a_quiet_run_with_an_open_interrupt_waits_with_a_deadline():
    runs = FakeRuns()
    run = started(runs)
    items = [{"kind": "interrupt", "resolved": False}]
    watch(runs, run, _session(status="pending", items=items), at=NOW + QUIET)
    assert run.status == "waiting_approval"
    assert run.deadline == approval_deadline(NOW, 3600)


@pytest.mark.parametrize("answer", [_session(running=True), (503, None), OSError("tunnel closed")])
def test_a_running_session_or_an_unreachable_runtime_is_looked_at_again_later(answer):
    runs = FakeRuns()
    run = started(runs)
    watch(runs, run, answer, at=NOW + QUIET)
    assert run.status == "running" and run.id in runs.deferred


def test_an_event_that_arrives_while_settling_wins():
    runs = FakeRuns()
    run = started(runs)
    stale = run.model_copy(deep=True)
    run.last_event_at = NOW + timedelta(minutes=1)
    watch(runs, run, _session(items=[{"kind": "done"}]), at=NOW + QUIET, stale=stale)
    assert run.status == "running"


@pytest.mark.parametrize(
    ("fields", "agents", "reason"),
    [
        ({"stop_requested_at": NOW, "stop_reason": "paused"}, FakeAgents(), "paused"),
        ({}, FakeAgents(gone=True), "agent_deleted"),
        ({}, FakeAgents(FakeAgent(state="draft")), "agent_undeployed"),
    ],
)
def test_a_quiet_marked_run_or_one_whose_agent_is_gone_is_cancelled(fields, agents, reason):
    runs = FakeRuns()
    run = started(runs, **fields)
    proxy = watch(runs, run, at=NOW + QUIET, agents=agents)
    assert (run.status, run.reason) == ("cancelled", reason)
    assert proxy.reads == []


def test_each_tick_sweeps_the_calls_of_ended_turns_first_and_survives_its_failure():
    """틱은 부른 턴이 끝난 Agent 호출을 먼저 취소한다 (P46 6.5절). 쓸기가 실패해도 지켜보기는
    돈다."""
    swept: list[str] = []

    async def sweep() -> int:
        swept.append("swept")
        if len(swept) == 2:
            raise RuntimeError("the grant store is down")
        return 0

    worker = RunWatchWorker(
        FakeAgents(),  # type: ignore[arg-type]
        FakeRuns(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        owner="w1",
        interval=15,
        lease_seconds=60,
        caller_sweep=sweep,
    )

    async def acquire() -> None:
        swept.append("acquired")
        return None

    worker.acquire = acquire  # type: ignore[method-assign]
    assert asyncio.run(worker.tick()) == 0 and asyncio.run(worker.tick()) == 0
    assert swept == ["swept", "acquired", "swept", "acquired"]
