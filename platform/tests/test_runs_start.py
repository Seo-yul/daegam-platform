"""시작 워커 (P41 5·6절). 런타임 프록시, 턴 위임과 스토어를 메모리 가짜로 바꿔 DB 없이 돈다. 위임을
싣고 답을 보는 것은 진짜 `TurnDelegation` 이다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.services.agent_runtime import ClosingBody, ProxyResponse
from daegam.services.lease_worker import LeaseWorker
from daegam.services.runs.events import RunEvents
from daegam.services.runs.fire import ScheduleFireWorker
from daegam.services.runs.person import BoundPersonCheck
from daegam.services.runs.service import MAX_RUNNING
from daegam.services.runs.start import MAX_PARALLEL, RETRY_BASE, RunStartWorker
from daegam.services.runs.watch import RunWatchWorker
from daegam.services.turn_grants import TurnTokensUnavailable
from daegam.services.turn_person import TurnDelegation
from daegam.store.agent_runs import ActsAs, AgentRun
from tests.helpers.agents import template_spec
from tests.helpers.runs import (
    BOUND,
    LOCAL,
    NOW,
    OIDC,
    SETTINGS,
    WRITES,
    FakeAgent,
    FakeAgents,
    FakeOwners,
    FakePeople,
    FakeRuns,
    FakeSchedules,
    FakeTriggers,
    FakeTurns,
    FakeUsers,
    alice,
    guarded,
    idp_person,
    person_check,
    run_of,
    schedule_of,
    service_of,
    trigger_of,
    with_budget,
)
from tests.helpers.turns import FakeGrants

RUN_HEADERS = {
    "x-daegam-session-id": "s1",
    "x-daegam-turn-id": "t1",
    "x-daegam-detached-turn": "1",
}


class FakeProxy:
    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self.read_to_end: list[bool] = []
        self.before_answer: Any = None

    def answer(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> Any:
        async def chunks():
            yield body
            self.read_to_end.append(True)

        async def on_close() -> None:
            self.closed.append("closed")

        return ProxyResponse(status, headers or {}, ClosingBody(chunks(), on_close))

    async def chat(self, agent: Any, **kwargs: Any) -> ProxyResponse:
        self.calls.append(kwargs)
        if self.before_answer is not None:
            await self.before_answer()
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Rig:
    """워커 하나와 그 가짜들."""

    def __init__(
        self,
        settings: Settings = SETTINGS,
        *,
        agent: FakeAgent | None = None,
        grants: FakeGrants | None = None,
        person: BoundPersonCheck | None = None,
        schedules: tuple[Any, ...] = (),
        triggers: tuple[Any, ...] = (),
        choices: Any = None,
    ) -> None:
        self.runs = FakeRuns()
        self.schedules = FakeSchedules(*schedules)
        self.triggers = FakeTriggers(*triggers)
        self.proxy = FakeProxy()
        self.grants = grants or FakeGrants()
        self.owners = FakeOwners()
        self.turns = FakeTurns()
        self.service = service_of(
            self.runs,
            self.schedules,
            settings,
            person=person,
            turns=self.turns,
            triggers=self.triggers,
        )
        self.worker = RunStartWorker(
            FakeAgents(agent),  # type: ignore[arg-type]
            self.service,
            self.proxy,  # type: ignore[arg-type]
            TurnDelegation(self.grants, self.owners),  # type: ignore[arg-type]
            choices=choices,
            owner="w1",
            interval=15,
            lease_seconds=60,
        )

    def add(self, **fields: Any) -> AgentRun:
        return self.runs.add(run_of(**{"message": "check the queue", **fields}))

    def start(self, run: AgentRun, at: datetime = NOW) -> AgentRun:
        """리스를 걸고 시작 하나를 처리한다."""

        async def go() -> None:
            held = await self.runs.acquire_start("w1", 60, at + timedelta(days=1))
            assert held is not None and held.id == run.id
            await self.worker.start_run(held, at)

        asyncio.run(go())
        return self.runs.docs[run.id]

    def accepted(self, run: AgentRun, **extra: str) -> ProxyResponse:
        return self.proxy.answer(200, {**RUN_HEADERS, "x-daegam-run-id": run.id, **extra})


def _local_rig(grants: FakeGrants | None = None) -> Rig:
    person = person_check(LOCAL, FakeUsers(alice()), ("alice", "default", "developer"))
    return Rig(LOCAL, person=person, grants=grants)


# -- 받아들인 시작 ----------------------------------------------------------------------------


def test_an_accepted_start_moves_to_running_and_closes_the_answer():
    rig = Rig()
    run = rig.add()
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert (stored.status, stored.session_id, stored.turn_id) == ("running", "s1", "t1")
    assert stored.message is None and stored.started_at == NOW and stored.lease is None
    call = rig.proxy.calls[0]
    assert (call["run_id"], call["ephemeral"], call["session_id"]) == (run.id, True, None)
    assert (call["user_sub"], call["message"]) == ("alice", "check the queue")
    # 헤더만 읽고 닫는다. 턴은 떼어 낸 채로 이어 돈다
    assert rig.proxy.closed == ["closed"] and rig.proxy.read_to_end == []
    # header 모드는 아무도 확인하지 않으므로 Agent 의 이름으로 돌고 소유자 기록도 없다
    assert (stored.acts_as.kind, stored.acts_as.cause) == ("agent", "header_mode")
    assert rig.owners.confirmed == []


class FakeChoiceService:
    """묶인 사람이 기억한 거부를 돌려준다. fail 이면 읽지 못한다."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.asked: list[str] = []

    async def for_run_start(self, agent: Any, sub: str) -> list[dict[str, str]] | None:
        self.asked.append(sub)
        if self.fail:
            raise OSError("mongo down")
        return [{"tool": "Divide", "choice": "reject"}]


def test_a_start_carries_the_bound_persons_remembered_rejects():
    choices = FakeChoiceService()
    rig = Rig(choices=choices)
    run = rig.add()
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"
    assert choices.asked == ["alice"]
    assert rig.proxy.calls[0]["approval_choices"] == [{"tool": "Divide", "choice": "reject"}]


def test_a_start_whose_choices_cannot_be_read_retries_without_calling_the_runtime():
    rig = Rig(choices=FakeChoiceService(fail=True))
    run = rig.add()
    stored = rig.start(run)
    assert (stored.status, stored.attempts) == ("starting", 1)
    assert rig.proxy.calls == []


def test_an_answer_from_an_old_pod_is_read_to_its_end_and_fails_the_run():
    rig = Rig()
    run = rig.add()
    rig.proxy.answers.append(rig.proxy.answer(200, {"x-daegam-session-id": "s1"}))
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("failed", "runs_unsupported")
    assert rig.proxy.read_to_end == [True]


def test_an_answer_for_another_run_id_is_not_taken():
    rig = Rig()
    run = rig.add()
    rig.proxy.answers.append(rig.accepted(run, **{"x-daegam-run-id": "someone-else"}))
    assert rig.start(run).reason == "runs_unsupported"


def test_a_schedule_run_reads_its_message_from_the_schedule():
    s = schedule_of(message="daily report")
    rig = Rig(schedules=(s,))
    run = rig.add(source="schedule", source_id=s.id, scheduled_for=NOW, message=None)
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"
    assert rig.proxy.calls[0]["message"] == "daily report"


def test_a_webhook_run_sends_its_recorded_message_as_untrusted():
    t = trigger_of()
    rig = Rig(agent=guarded(), triggers=(t,))
    run = rig.add(source="webhook", source_id=t.id, delivery_id="msg-1", message="pushed main")
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"
    call = rig.proxy.calls[0]
    assert (call["message"], call["untrusted"], call["ephemeral"]) == ("pushed main", True, True)
    # 다른 실행은 untrusted 가 아니다
    other = rig.add()
    rig.proxy.answers.append(rig.accepted(other))
    rig.start(other)
    assert rig.proxy.calls[1]["untrusted"] is False


def test_a_stop_marked_during_the_call_stops_the_new_turn():
    rig = Rig()
    run = rig.add()

    async def stop_meanwhile() -> None:
        await rig.service.stop(rig.runs.docs[run.id])

    rig.proxy.before_answer = stop_meanwhile
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert stored.status == "cancelled" and stored.session_id == "s1"
    assert [(r.id, r.session_id, r.turn_id) for r in rig.turns.stopped] == [(run.id, "s1", "t1")]


@pytest.mark.parametrize(
    ("event", "status"),
    [("turn_start", "running"), ("turn_end", "waiting_approval")],
)
def test_events_that_moved_the_run_first_keep_their_status_and_the_start_is_recorded(event, status):
    # 이벤트가 헤더보다 먼저 와서 상태를 옮기고 리스를 놓아도 시작 워커가 실행 이름을 적는다
    rig = Rig()
    run = rig.add()
    docs = [{"event": "turn_start", "data": {"run_id": run.id}}]
    if event == "turn_end":
        docs.append({"event": "turn_end", "status": "pending", "data": {"run_id": run.id}})

    async def events_first() -> None:
        for n, doc in enumerate(docs):
            ts = NOW + timedelta(seconds=n)
            await RunEvents(rig.runs, FakeAgents()).apply(  # type: ignore[arg-type]
                "ag-1", "default", [{**doc, "_id": f"e{n}", "ts": ts, "session_id": "s1"}]
            )

    rig.proxy.before_answer = events_first
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert (stored.status, stored.session_id, stored.message) == (status, "s1", None)
    assert (stored.started_at, stored.acts_as.cause) == (NOW, "header_mode")
    assert rig.turns.stopped == []


def test_a_stop_marked_before_events_moved_the_run_still_stops_the_turn():
    rig = Rig()
    run = rig.add()

    async def stop_then_event() -> None:
        await rig.service.stop(rig.runs.docs[run.id])
        await rig.runs.apply_event(
            run.id, "s1", ts=NOW, to_status="running", now=NOW, fields={"session_id": "s1"}
        )

    rig.proxy.before_answer = stop_then_event
    rig.proxy.answers.append(rig.accepted(run))
    rig.start(run)
    assert [r.id for r in rig.turns.stopped] == [run.id]


def test_a_stop_after_an_event_released_the_lease_still_stops_the_turn():
    rig = Rig()
    run = rig.add()

    async def event_then_stop() -> None:
        await rig.runs.apply_event(
            run.id, "s1", ts=NOW, to_status="running", now=NOW, fields={"session_id": "s1"}
        )
        await rig.service.stop(rig.runs.docs[run.id])

    rig.proxy.before_answer = event_then_stop
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    # 이벤트가 리스를 풀어 시작은 리스 없는 길로 적히고, 그 뒤 표시를 보고 턴을 다시 멈춘다.
    # 앞의 멈춤은 service.stop 이 도는 실행에 보낸 것이다
    assert stored.started_at is not None
    assert [r.id for r in rig.turns.stopped] == [run.id, run.id]


def test_a_worker_that_lost_its_lease_neither_records_nor_moves_the_run():
    rig = Rig()
    run = rig.add()

    async def taken_meanwhile() -> None:
        rig.runs.docs[run.id].lease.owner = "w2"

    rig.proxy.before_answer = taken_meanwhile
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert (stored.status, stored.session_id) == ("starting", None)

    refused = rig.add()
    rig.proxy.before_answer = None
    rig.runs.docs[refused.id].lease = None
    rig.proxy.answers.append(rig.proxy.answer(429, body=b'{"code": "rate_limited"}'))

    async def go() -> None:
        held = await rig.runs.acquire_start("w1", 60, NOW)
        assert held is not None
        rig.runs.docs[held.id].lease.owner = "w2"
        await rig.worker.start_run(held, NOW)

    asyncio.run(go())
    assert rig.runs.docs[refused.id].status == "starting"


# -- 앞 시도와 다시 하기 ----------------------------------------------------------------------


def _run_exists(rig: Rig, turn_id: str | None) -> ProxyResponse:
    turn = f'"{turn_id}"' if turn_id else "null"
    body = f'{{"code": "run_exists", "message": "x", "session_id": "s9", "turn_id": {turn}}}'
    return rig.proxy.answer(409, body=body.encode())


def test_run_exists_binds_the_earlier_turn_without_claiming_whose_name_it_ran_in():
    rig = Rig()
    run = rig.add()
    rig.proxy.answers.append(_run_exists(rig, "t9"))
    stored = rig.start(run)
    assert (stored.status, stored.session_id, stored.turn_id) == ("running", "s9", "t9")
    assert (stored.acts_as.kind, stored.acts_as.cause) == ("agent", "run_exists")

    lost = rig.add()
    rig.proxy.answers.append(_run_exists(rig, None))
    assert (rig.start(lost).status, rig.runs.docs[lost.id].reason) == ("failed", "run_lost")


def test_run_exists_keeps_what_the_earlier_attempt_recorded():
    rig = Rig()
    person = BOUND.model_copy(update={"kind": "person"})
    run = rig.add(session_id="s9", grant_id="g7", acts_as=person, started_at=NOW)
    rig.proxy.answers.append(_run_exists(rig, "t9"))
    stored = rig.start(run)
    assert (stored.status, stored.grant_id, stored.acts_as) == ("running", "g7", person)


@pytest.mark.parametrize(
    "answer",
    [
        ("status", 503, b'{"code": "tools_unavailable", "message": "x"}'),
        ("status", 504, b""),
        ("status", 409, b'{"code": "tools_unavailable", "message": "x"}'),
        ("raise", ServiceError(502, "runtime_unreachable", "no pod")),
        ("raise", OSError("connection reset")),
    ],
)
def test_transient_answers_retry_with_back_off_then_fail(answer):
    rig = Rig()
    run = rig.add()

    def one() -> Any:
        kind, *rest = answer
        if kind == "raise":
            return rest[0]
        return rig.proxy.answer(rest[0], body=rest[1])

    rig.proxy.answers.extend(one() for _ in range(3))
    first = rig.start(run)
    assert (first.status, first.attempts) == ("starting", 1)
    assert first.next_attempt_at == NOW + RETRY_BASE
    second = rig.start(run, NOW + RETRY_BASE)
    assert (second.status, second.attempts) == ("starting", 2)
    assert second.next_attempt_at == NOW + RETRY_BASE * 3
    third = rig.start(run, NOW + RETRY_BASE * 3)
    assert (third.status, third.attempts) == ("failed", 3)


def test_a_retry_past_the_ten_minute_window_fails_at_once():
    rig = Rig()
    run = rig.add(created_at=NOW - timedelta(minutes=10))
    rig.proxy.answers.append(rig.proxy.answer(503, body=b""))
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("failed", "http_503")


@pytest.mark.parametrize(
    "code", ["rate_limited", "max_concurrent_sessions", "guardrail_blocked", "max_input_chars"]
)
def test_any_other_refusal_fails_with_its_code_and_no_retry(code):
    rig = Rig()
    run = rig.add()
    body = f'{{"code": "{code}", "message": "no"}}'.encode()
    rig.proxy.answers.append(rig.proxy.answer(429, body=body))
    stored = rig.start(run)
    assert (stored.status, stored.reason, stored.attempts) == ("failed", code, 1)


# -- 시작 전에 다시 보기 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"paused": True, "paused_reason": "paused"}, "paused"),
        ({"expires_at": NOW - timedelta(minutes=1)}, "expired"),
        ({"revision": 2}, "agent_changed"),
    ],
)
def test_a_schedule_that_changed_ends_its_run_before_the_call(change, reason):
    s = schedule_of(**change)
    rig = Rig(schedules=(s,))
    run = rig.add(source="schedule", source_id=s.id, scheduled_for=NOW)
    assert (rig.start(run).status, rig.runs.docs[run.id].reason) == ("cancelled", reason)
    assert rig.proxy.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"paused": True, "paused_reason": "acts_as_lost_access"}, "acts_as_lost_access"),
        ({"expires_at": NOW - timedelta(minutes=1)}, "expired"),
        ({"revision": 2}, "agent_changed"),
        (None, "source_deleted"),
    ],
)
def test_a_trigger_that_changed_or_went_ends_its_run_before_the_call(change, reason):
    t = trigger_of(**(change or {}))
    rig = Rig(agent=guarded(), triggers=() if change is None else (t,))
    run = rig.add(source="webhook", source_id=t.id, delivery_id="msg-1")
    assert (rig.start(run).status, rig.runs.docs[run.id].reason) == ("cancelled", reason)
    assert rig.proxy.calls == []


def test_a_webhook_run_on_an_agent_with_unguarded_writes_ends_and_pauses_its_trigger():
    t = trigger_of()
    rig = Rig(agent=FakeAgent(tool_pins=WRITES), triggers=(t,))
    run = rig.add(source="webhook", source_id=t.id, delivery_id="msg-1")
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("cancelled", "trigger_needs_approval")
    assert (t.paused, t.paused_reason) == (True, "prerequisites_failed")
    assert rig.proxy.calls == []


def test_a_deleted_schedule_or_agent_ends_the_run():
    rig = Rig()
    run = rig.add(source="schedule", source_id="gone", scheduled_for=NOW)
    assert rig.start(run).reason == "source_deleted"
    gone = Rig(agent=FakeAgent(state="undeploying"))
    run = gone.add()
    assert gone.start(run).reason == "agent_undeployed"


def test_a_deploying_agent_defers_the_start_without_counting_an_attempt():
    rig = Rig(agent=FakeAgent(state="deploying"))
    run = rig.add()
    stored = rig.start(run)
    assert (stored.status, stored.attempts) == ("starting", 0)
    assert rig.runs.deferred[run.id] == NOW + timedelta(seconds=30)


class FakeSleep:
    """쉬기의 게이트 (P44 8절). waking 이면 깨우는 중으로 답한다."""

    def __init__(self, *, waking: bool) -> None:
        self.waking = waking
        self.admitted: list[str] = []

    async def admit(self, doc: Any, reason: str) -> None:
        self.admitted.append(reason)
        if self.waking:
            raise ServiceError(503, "agent_waking", "starting the pod again")


def test_a_sleeping_agent_wakes_and_defers_the_start_without_counting_an_attempt():
    """실행 시작은 위임을 열기 전에 잠든 Agent 를 깨운다. 깨우는 중이면 시도로 세지 않고 Retry-After
    만큼 미룬다 (P44 8절)."""
    rig = _local_rig()
    rig.worker.sleep = FakeSleep(waking=True)
    run = rig.add(acts_as=BOUND)
    stored = rig.start(run)
    assert (stored.status, stored.attempts) == ("starting", 0)
    assert rig.runs.deferred[run.id] == NOW + timedelta(seconds=5)
    assert rig.worker.sleep.admitted == ["run"]
    assert rig.proxy.calls == [] and rig.grants.opened == []


def test_an_awake_agent_passes_the_gate_and_starts():
    rig = Rig()
    rig.worker.sleep = FakeSleep(waking=False)
    run = rig.add()
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"
    assert rig.worker.sleep.admitted == ["run"]


def test_a_drain_refusal_answered_as_waking_defers_without_counting_an_attempt():
    """프록시가 런타임의 드레인 거절을 바꾼 503 agent_waking 도 시도로 세지 않는다 (P44 8절)."""
    rig = Rig()
    run = rig.add()
    body = b'{"code": "agent_waking", "message": "starting the pod again"}'
    rig.proxy.answers.append(rig.proxy.answer(503, {"retry-after": "5"}, body))
    stored = rig.start(run)
    assert (stored.status, stored.attempts) == ("starting", 0)
    assert rig.runs.deferred[run.id] == NOW + timedelta(seconds=5)


def test_a_code_execution_agent_defers_a_run_when_its_sandbox_runs_are_held(monkeypatch):
    """열린 실행 하나가 샌드박스 실행 몫 하나를 쥔다(P40 7절). 몫이 찼으면 시도로 세지 않고
    미룬다. 캔버스의 코드 실행 칸(F5)이 아직 그래프 모델에 없어 켠 것으로 바꿔 둔다."""
    monkeypatch.setattr("daegam.services.runs.start.code_execution_on", lambda _agent: True)
    rig = Rig()
    rig.worker.sandbox_runs = 2
    for _ in range(2):
        held = rig.add()
        rig.runs.docs[held.id] = held.model_copy(update={"status": "running"})
    run = rig.add()
    stored = rig.start(run)
    assert (stored.status, stored.attempts) == ("starting", 0)
    assert rig.runs.deferred[run.id] == NOW + timedelta(seconds=30)
    assert rig.proxy.calls == []
    # 몫이 비면 시작한다
    rig.runs.docs[held.id] = held.model_copy(update={"status": "succeeded"})
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"


def test_an_agent_without_code_execution_ignores_the_sandbox_runs():
    rig = Rig()
    rig.worker.sandbox_runs = 0
    run = rig.add()
    rig.proxy.answers.append(rig.accepted(run))
    assert rig.start(run).status == "running"


def test_a_failed_prerequisite_ends_the_run_and_pauses_its_schedule():
    s = schedule_of()
    rig = Rig(agent=FakeAgent(spec=with_budget(template_spec(), None)), schedules=(s,))
    run = rig.add(source="schedule", source_id=s.id, scheduled_for=NOW)
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("cancelled", "run_budget_required")
    assert (s.paused, s.paused_reason) == (True, "prerequisites_failed")


def test_a_bound_person_who_lost_access_ends_the_run_and_pauses_their_sources():
    s = schedule_of(acts_as=BOUND)
    person = person_check(
        LOCAL, FakeUsers(alice(token_version=4)), ("alice", "default", "developer")
    )
    rig = Rig(LOCAL, person=person, schedules=(s,))
    run = rig.add(source="schedule", source_id=s.id, scheduled_for=NOW, acts_as=BOUND)
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("cancelled", "acts_as_lost_access")
    assert (s.paused_reason, s.paused_cause) == ("acts_as_lost_access", "password")
    assert rig.proxy.calls == []


def test_an_agent_at_its_running_cap_ends_the_run():
    rig = Rig()
    for _ in range(MAX_RUNNING):
        rig.add(status="running")
    run = rig.add()
    assert (rig.start(run).status, rig.runs.docs[run.id].reason) == (
        "cancelled",
        "agent_run_limit",
    )


# -- 누구의 이름으로 --------------------------------------------------------------------------


def test_local_mode_opens_a_run_grant_and_records_the_session_owner():
    rig = _local_rig()
    run = rig.add(acts_as=BOUND)
    rig.proxy.answers.append(rig.accepted(run, **{"x-daegam-turn-grant": "g1"}))
    stored = rig.start(run)
    opened = rig.grants.opened[0]
    assert (opened["trigger"], opened["person"].tv, opened["session_id"]) == ("run", 3, None)
    assert rig.proxy.calls[0]["turn_grant"] == {"id": "g1", "sub": "alice"}
    assert (stored.grant_id, stored.acts_as.kind) == ("g1", "person")
    assert rig.grants.attached == [("g1", "s1")]
    owner = rig.owners.confirmed[0]
    assert (owner.sub, owner.tv, owner.session_id) == ("alice", 3, "s1")


def test_oidc_mode_runs_as_the_person_once_they_have_a_record():
    """oidc 모드의 실행도 사람 기록이 있었던 사람이면 그 사람의 위임으로 돈다(P53b 6.8절). 긴
    한도는 위임 서비스가 열 때와 갱신할 때 본다. 기록이 한 번도 없었던 사람은 업그레이드 전처럼
    Agent 의 이름으로 돌고 까닭은 `oidc_mode` 다."""
    alice_run = ActsAs(sub="alice", username="alice")
    people = FakePeople(idp_person("alice", age=timedelta(hours=2)))
    person = person_check(OIDC, None, ("alice", "default", "developer"), people=people)
    rig = Rig(OIDC, person=person)
    run = rig.add(acts_as=alice_run)
    rig.proxy.answers.append(rig.accepted(run, **{"x-daegam-turn-grant": "g1"}))
    stored = rig.start(run)
    opened = rig.grants.opened[0]
    assert (opened["trigger"], opened["person"].sub, opened["person"].tv) == ("run", "alice", None)
    assert (stored.grant_id, stored.acts_as.kind) == ("g1", "person")
    assert rig.owners.confirmed[0].sub == "alice"

    newcomer = Rig(OIDC, person=person_check(OIDC, None, ("bob", "default", "developer")))
    run = newcomer.add(acts_as=ActsAs(sub="bob"))
    newcomer.proxy.answers.append(newcomer.accepted(run))
    stored = newcomer.start(run)
    assert (stored.acts_as.kind, stored.acts_as.cause) == ("agent", "oidc_mode")
    assert newcomer.grants.opened == []


def test_oidc_mode_pauses_the_runs_of_a_person_past_the_long_bound():
    """기록의 까닭은 `acts_as_unconfirmed` 이고 소스는 `acts_as_lost_access` 와 원인 unconfirmed 로
    멈춘다. 화면이 둘 다 그 사람이 며칠 동안 로그인하지 않았다고 말한다 (P53b 13절)."""
    s = schedule_of(acts_as=ActsAs(sub="alice"))
    people = FakePeople(idp_person("alice", age=timedelta(days=8)))
    person = person_check(OIDC, None, ("alice", "default", "developer"), people=people)
    rig = Rig(OIDC, person=person, schedules=(s,))
    run = rig.add(source="schedule", source_id=s.id, scheduled_for=NOW, acts_as=s.acts_as)
    stored = rig.start(run)
    assert (stored.status, stored.reason) == ("cancelled", "acts_as_unconfirmed")
    assert (s.paused_reason, s.paused_cause) == ("acts_as_lost_access", "unconfirmed")
    assert rig.proxy.calls == []


def test_a_grant_the_pod_did_not_echo_is_ended_and_the_run_says_why():
    rig = _local_rig()
    run = rig.add(acts_as=BOUND)
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert rig.grants.ended == [("g1", "not_accepted")]
    assert (stored.grant_id, stored.acts_as.kind, stored.acts_as.cause) == (
        None,
        "agent",
        "grant_not_accepted",
    )


def test_a_refused_grant_runs_as_the_agent_and_says_why():
    rig = _local_rig(FakeGrants(refuse={"alice"}))
    run = rig.add(acts_as=BOUND)
    rig.proxy.answers.append(rig.accepted(run))
    stored = rig.start(run)
    assert (stored.acts_as.kind, stored.acts_as.cause) == ("agent", "disabled")


def test_a_refused_answer_ends_the_grant_and_an_unavailable_grant_retries():
    rig = _local_rig()
    run = rig.add(acts_as=BOUND)
    rig.proxy.answers.append(rig.proxy.answer(429, body=b'{"code": "rate_limited"}'))
    rig.start(run)
    assert rig.grants.ended == [("g1", "refused")]

    stale = _local_rig(FakeGrants(raises=TurnTokensUnavailable("no recent Gateway verdict")))
    run = stale.add(acts_as=BOUND)
    stored = stale.start(run)
    assert (stored.status, stored.attempts) == ("starting", 1)
    assert stale.proxy.calls == []


# -- 한 바퀴와 수명 ----------------------------------------------------------------------------


def test_a_tick_starts_one_run_per_agent_and_refills_a_slot_as_soon_as_it_frees():
    rig = Rig()
    for agent_id in ("ag-1", "ag-1", "ag-2"):
        rig.add(agent_id=agent_id, next_attempt_at=NOW - timedelta(minutes=1))
    order: list[str] = []
    slow = asyncio.Event()

    async def start(run: AgentRun, now: datetime) -> None:
        order.append(run.agent_id)
        if run.agent_id == "ag-2":
            # 옛 파드의 답을 끝까지 읽는 시작처럼 오래 걸린다
            await slow.wait()
        rig.runs.docs[run.id].status = "running"
        if len(order) == 3:
            slow.set()

    rig.worker.start_run = start  # type: ignore[method-assign]
    assert asyncio.run(rig.worker.tick()) == 3
    # ag-2 가 자리를 쥔 동안 ag-1 의 다음 기록이 비는 자리에서 시작한다
    assert order == ["ag-1", "ag-2", "ag-1"]
    assert MAX_PARALLEL == 5


def test_a_record_due_while_every_slot_is_busy_starts_within_one_interval():
    rig = Rig()
    rig.worker.interval = 0.01
    rig.add(agent_id="ag-2")
    later = asyncio.Event()

    async def start(run: AgentRun, now: datetime) -> None:
        rig.runs.docs[run.id].status = "running"
        if run.agent_id == "ag-2":
            # 이 자리가 쥐고 있는 동안 새 기록이 생긴다. 그 시작이 끝나야 이 자리도 끝난다
            rig.add(agent_id="ag-1")
            await later.wait()
        else:
            later.set()

    rig.worker.start_run = start  # type: ignore[method-assign]
    assert asyncio.run(asyncio.wait_for(rig.worker.tick(), 2)) == 2


def test_a_failed_acquire_finishes_the_starts_in_flight():
    rig = Rig()
    rig.add()
    finished: list[str] = []
    acquire = rig.runs.acquire_start
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> AgentRun | None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("mongo is down")
        return await acquire(*args, **kwargs)

    async def start(run: AgentRun, now: datetime) -> None:
        await asyncio.sleep(0.01)
        finished.append(run.id)

    rig.runs.acquire_start = flaky  # type: ignore[method-assign]
    rig.worker.start_run = start  # type: ignore[method-assign]
    assert asyncio.run(rig.worker.tick()) == 1
    assert len(finished) == 1


def test_stopping_the_tick_cancels_the_starts_and_waits_for_them():
    rig = Rig()
    rig.add()
    ended: list[str] = []

    async def start(run: AgentRun, now: datetime) -> None:
        try:
            await asyncio.sleep(60)
        finally:
            ended.append(run.id)

    rig.worker.start_run = start  # type: ignore[method-assign]

    async def go() -> None:
        tick = asyncio.create_task(rig.worker.tick())
        await asyncio.sleep(0.01)
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick

    asyncio.run(go())
    # 틱이 끝나기 전에 도는 시작이 취소되고 끝났다
    assert len(ended) == 1


def test_the_run_workers_keep_the_lease_worker_lifecycle():
    """세 워커가 `LeaseWorker.start()` 를 덮어쓰면 플랫폼이 뜨지 않는다."""
    runs, schedules = FakeRuns(), FakeSchedules()
    service = service_of(runs, schedules)
    agents = FakeAgents()
    workers = [
        ScheduleFireWorker(agents, service, interval=15, lease_seconds=60),  # type: ignore[arg-type]
        RunStartWorker(
            agents,  # type: ignore[arg-type]
            service,
            FakeProxy(),  # type: ignore[arg-type]
            TurnDelegation(FakeGrants(), FakeOwners()),  # type: ignore[arg-type]
            interval=15,
            lease_seconds=60,
        ),
        RunWatchWorker(agents, runs, FakeProxy(), interval=15, lease_seconds=60),  # type: ignore[arg-type]
    ]

    async def go() -> None:
        for w in workers:
            await w.start()
        await asyncio.sleep(0)
        for w in workers:
            await w.stop()

    asyncio.run(go())
    assert all(type(w).start is LeaseWorker.start for w in workers)
