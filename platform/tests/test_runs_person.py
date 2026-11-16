"""실행이 묶인 사람 (P41 6절)과 중지 (7절). 묶을 때와 시작할 때의 확인, 접근을 잃은 사람의 소스
멈춤, 도는 턴의 중지, Agent 삭제의 정리를 메모리 가짜로 DB 없이 본다."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from daegam.auth.principal import Principal
from daegam.services.access_revocation import AccessRevoker
from daegam.services.runs.person import (
    CAUSE_ACCOUNT,
    CAUSE_PASSWORD,
    CAUSE_ROLE,
    CAUSE_UNCONFIRMED,
    LOST_ACCESS,
    BoundPersonCheck,
)
from daegam.services.runs.service import AGENT_DELETED, RunError, ScheduleInput
from daegam.store.agent_runs import STOPPED, ActsAs
from daegam.store.personal_tokens import DISABLED, MEMBERSHIP, PASSWORD
from tests.helpers.runs import (
    BOUND,
    LOCAL,
    NOW,
    OIDC,
    SETTINGS,
    FakeAgent,
    FakeAudit,
    FakePeople,
    FakeRuns,
    FakeSchedules,
    FakeTriggers,
    FakeTurns,
    FakeUsers,
    alice,
    idp_person,
    person_check,
    run_of,
    schedule_of,
    service_of,
    trigger_of,
)

DAILY = ScheduleInput(cron="0 9 * * *", time_zone="UTC", message="report", expires_in_days=90)


def lost(person: BoundPersonCheck, acts_as: ActsAs = BOUND) -> str | None:
    return asyncio.run(person.lost(acts_as, "default"))


# -- 묶을 때 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "binds"),
    [
        ([("alice", "default", "developer")], True),
        ([("alice", "default", "tenant-admin")], True),
        # 보기만 하는 role 은 agent:write 가 없다
        ([("alice", "default", "viewer")], False),
        # platform-admin 의 `*` 줄은 소속이 아니다 (D58)
        ([("alice", "*", "platform-admin")], False),
        ([("alice", "other", "developer")], False),
        ([], False),
    ],
)
def test_binding_needs_a_role_in_the_tenant_that_grants_write_and_chat(rows, binds):
    person = person_check(LOCAL, FakeUsers(alice()), *rows)
    assert asyncio.run(person.may_bind("alice", "default")) is binds


def test_header_mode_binds_and_checks_nobody():
    person = person_check(SETTINGS)
    assert asyncio.run(person.may_bind("header", "default"))
    assert lost(person, ActsAs(sub="header")) is None


def test_a_platform_admin_without_a_role_cannot_set_up_a_schedule():
    runs, schedules = FakeRuns(), FakeSchedules()
    person = person_check(LOCAL, FakeUsers(alice()), ("alice", "*", "platform-admin"))
    service = service_of(runs, schedules, LOCAL, person=person)
    user = Principal(sub="alice", raw={"tv": 3})
    with pytest.raises(RunError) as e:
        asyncio.run(service.create_schedule(FakeAgent(), user, DAILY))  # type: ignore[arg-type]
    assert (e.value.status, e.value.code) == (403, "forbidden")
    with pytest.raises(RunError):
        asyncio.run(service.run_now(FakeAgent(), user, "hi"))  # type: ignore[arg-type]
    assert schedules.docs == {} and runs.docs == {}


def test_a_developer_is_bound_with_their_token_version():
    person = person_check(LOCAL, FakeUsers(alice()), ("alice", "default", "developer"))
    service = service_of(settings=LOCAL, person=person)
    user = Principal(sub="alice", raw={"tv": 3, "preferred_username": "alice"})
    created = asyncio.run(service.create_schedule(FakeAgent(), user, DAILY))  # type: ignore[arg-type]
    assert created.acts_as == BOUND


# -- 시작할 때 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "acts_as", "rows", "cause"),
    [
        (alice(), BOUND, [("alice", "default", "developer")], None),
        (None, BOUND, [("alice", "default", "developer")], CAUSE_ACCOUNT),
        (alice(disabled=True), BOUND, [("alice", "default", "developer")], CAUSE_ACCOUNT),
        (alice(token_version=4), BOUND, [("alice", "default", "developer")], CAUSE_PASSWORD),
        # 묶을 때의 tv 를 모르면 비밀번호를 바꾼 것으로 본다
        (alice(), ActsAs(sub="alice"), [("alice", "default", "developer")], CAUSE_PASSWORD),
        (alice(), BOUND, [("alice", "default", "viewer")], CAUSE_ROLE),
        (alice(), BOUND, [("alice", "*", "platform-admin")], CAUSE_ROLE),
        (alice(), BOUND, [], CAUSE_ROLE),
    ],
)
def test_local_mode_checks_the_account_the_token_version_and_the_role(user, acts_as, rows, cause):
    users = FakeUsers(*([user] if user is not None else []))
    assert lost(person_check(LOCAL, users, *rows), acts_as) == cause


def test_oidc_mode_checks_only_the_role_and_never_reads_the_user_document():
    """기록이 한 번도 없었던 사람은 role 만 본다. 업그레이드가 아무 실행도 멈추지 않는다."""
    # 사용자 문서가 없는 모드라 users 가 없어도 돈다
    assert lost(person_check(OIDC, None, ("alice", "default", "developer"))) is None
    assert lost(person_check(OIDC, None, ("alice", "default", "viewer"))) == CAUSE_ROLE


@pytest.mark.parametrize(
    ("record", "cause"),
    [
        (idp_person("alice", age=timedelta(days=6)), None),
        (idp_person("alice", age=timedelta(days=7, minutes=1)), CAUSE_UNCONFIRMED),
        # 위임과 같은 끝이다. 한도까지 토큰 한 수명(300초)이 남지 않으면 Agent 로 돌지 않고 멈춘다
        (idp_person("alice", age=timedelta(days=7, seconds=-100)), CAUSE_UNCONFIRMED),
        # 비운 기록은 기록이 있었던 것이다
        (idp_person("alice", cleared_at=NOW), CAUSE_UNCONFIRMED),
    ],
)
def test_oidc_mode_pauses_a_person_past_the_long_bound(record, cause):
    """사람 기록이 있었던 사람은 마지막 IdP 확인에서 긴 한도(7일)까지 토큰 한 수명이 남아야 한다
    (P53b 6.3절)."""
    people = FakePeople(record)
    check = person_check(OIDC, None, ("alice", "default", "developer"), people=people)
    assert lost(check) == cause


def test_oidc_runs_act_as_the_person_once_they_had_a_record():
    """local 모드의 실행은 늘 그 사람의 이름으로 돌고, oidc 모드는 사람 기록이 있었던 사람만이다."""
    recorded = person_check(OIDC, people=FakePeople(idp_person("alice", cleared_at=NOW)))
    assert asyncio.run(recorded.runs_as_person("alice")) is True
    assert asyncio.run(person_check(OIDC).runs_as_person("alice")) is False
    assert asyncio.run(person_check(LOCAL).runs_as_person("alice")) is True


# -- 접근을 잃은 사람 ---------------------------------------------------------------------------


def test_pausing_a_person_pauses_only_their_schedules_and_stops_their_open_runs():
    mine = schedule_of()
    other_agent = schedule_of(agent_id="ag-2")
    bobs = schedule_of(acts_as=ActsAs(sub="bob"))
    schedules, runs, turns = FakeSchedules(mine, other_agent, bobs), FakeRuns(), FakeTurns()
    waiting = runs.add(run_of(source="schedule", source_id=mine.id, status="waiting_approval"))
    running = runs.add(
        run_of(source="schedule", source_id=mine.id, status="running", session_id="s1")
    )
    service = service_of(runs, schedules, LOCAL, turns=turns)
    assert asyncio.run(service.pause_person("alice", CAUSE_ROLE, agent_id="ag-1")) == 1
    stored = schedules.docs[mine.id]
    assert (stored.paused, stored.paused_reason, stored.paused_cause) == (
        True,
        LOST_ACCESS,
        CAUSE_ROLE,
    )
    assert not schedules.docs[other_agent.id].paused and not schedules.docs[bobs.id].paused
    assert (waiting.status, waiting.reason) == ("cancelled", LOST_ACCESS)
    # 도는 실행은 표시를 남기고 런타임에 그 턴을 멈추라고 한다. 턴의 끝이 기록을 끝낸다
    assert running.status == "running" and running.stop_reason == LOST_ACCESS
    assert [r.id for r in turns.stopped] == [running.id]


@pytest.mark.parametrize(
    ("reason", "tenant", "cause", "paused"),
    [
        (PASSWORD, None, CAUSE_PASSWORD, {"default", "other"}),
        (DISABLED, None, CAUSE_ACCOUNT, {"default", "other"}),
        (MEMBERSHIP, "other", CAUSE_ROLE, {"other"}),
    ],
)
def test_the_lost_access_hook_pauses_by_account_everywhere_and_by_membership_in_its_tenant(
    reason, tenant, cause, paused
):
    schedules = FakeSchedules(schedule_of(), schedule_of(tenant="other"))
    service = service_of(schedules=schedules, settings=LOCAL)
    asyncio.run(service.on_lost_access("alice", reason, tenant))
    assert {s.tenant for s in schedules.docs.values() if s.paused} == paused
    assert {s.paused_cause for s in schedules.docs.values() if s.paused} == {cause}


def test_resuming_clears_the_cause():
    s = schedule_of(paused=True, paused_reason=LOST_ACCESS, paused_cause=CAUSE_ROLE)
    person = person_check(LOCAL, None, ("bob", "default", "developer"))
    service = service_of(schedules=FakeSchedules(s), settings=LOCAL, person=person)
    bob = Principal(sub="bob", raw={"tv": 1})
    resumed = asyncio.run(service.resume_schedule(FakeAgent(), s, bob))  # type: ignore[arg-type]
    assert (resumed.paused, resumed.paused_reason, resumed.paused_cause) == (False, None, None)
    assert resumed.acts_as.sub == "bob"


class Failing:
    async def live_ids(self, *_: Any, **__: Any) -> list[str]:
        raise RuntimeError("store down")


def test_the_revocation_hook_pauses_runs_even_when_token_revocation_fails():
    called: list[tuple[str, str, str | None]] = []

    async def on_lost(sub: str, reason: str, tenant: str | None) -> None:
        called.append((sub, reason, tenant))
        raise RuntimeError("runs store down")

    revoker = AccessRevoker(
        Failing(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        Failing(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        settings=LOCAL,
        people=None,  # type: ignore[arg-type]
        on_lost=on_lost,
    )
    # 둘 다 실패해도 훅은 삼킨다. 계정 사건은 이미 일어났다
    asyncio.run(revoker.revoke_for("alice", MEMBERSHIP, by="admin", tenant="default"))
    assert called == [("alice", MEMBERSHIP, "default")]


def test_a_pause_leaves_one_system_audit_row_per_tenant_with_the_cause():
    schedules = FakeSchedules(schedule_of(), schedule_of(agent_id="ag-2"), schedule_of(tenant="b"))
    audit = FakeAudit()
    service = service_of(schedules=schedules, settings=LOCAL, audit=audit)
    assert asyncio.run(service.pause_person("alice", CAUSE_ACCOUNT)) == 3
    rows = [(e.tenant, e.action, e.error_code, e.subject_type) for e in audit.events]
    assert rows == [
        ("b", LOST_ACCESS, CAUSE_ACCOUNT, "system"),
        ("default", LOST_ACCESS, CAUSE_ACCOUNT, "system"),
    ]
    # 이미 멈춘 것을 다시 멈추면 행이 없다
    assert asyncio.run(service.pause_person("alice", CAUSE_ACCOUNT)) == 0
    assert len(audit.events) == 2


def test_a_pause_stops_the_persons_triggers_and_their_runs_too():
    t, bobs = trigger_of(), trigger_of(acts_as=ActsAs(sub="bob"))
    triggers, runs, audit = FakeTriggers(t, bobs), FakeRuns(), FakeAudit()
    waiting = runs.add(run_of(source="webhook", source_id=t.id, status="waiting_approval"))
    service = service_of(runs, settings=LOCAL, audit=audit, triggers=triggers)
    assert asyncio.run(service.pause_person("alice", CAUSE_ROLE, tenant="default")) == 1
    assert (t.paused_reason, t.paused_cause, bobs.paused) == (LOST_ACCESS, CAUSE_ROLE, False)
    assert (waiting.status, waiting.reason) == ("cancelled", LOST_ACCESS)
    assert [(e.resource, e.tenant) for e in audit.events] == [("agent-triggers", "default")]


# -- 중지 (7절) --------------------------------------------------------------------------------


def test_a_stop_ends_a_waiting_run_and_asks_the_runtime_to_stop_a_running_turn():
    runs, turns = FakeRuns(), FakeTurns()
    waiting = runs.add(run_of(status="waiting_approval"))
    running = runs.add(run_of(status="running", session_id="s1", turn_id="t1"))
    service = service_of(runs, turns=turns)
    stopped = asyncio.run(service.stop(waiting))
    assert (stopped.status, stopped.reason, turns.stopped) == ("cancelled", STOPPED, [])
    stopped = asyncio.run(service.stop(running))
    assert (stopped.status, stopped.stop_reason) == ("running", STOPPED)
    assert [r.id for r in turns.stopped] == [running.id]


def test_pausing_a_schedule_by_hand_stops_its_running_turn():
    s = schedule_of()
    runs, turns = FakeRuns(), FakeTurns()
    running = runs.add(run_of(source="schedule", source_id=s.id, status="running"))
    service = service_of(runs, FakeSchedules(s), turns=turns)
    asyncio.run(service.pause_schedule(s, "paused"))
    assert running.stop_reason == "paused" and turns.stopped == [running]


# -- Agent 삭제 --------------------------------------------------------------------------------


def test_deleting_an_agent_ends_its_open_runs_and_deletes_its_schedules():
    s, kept = schedule_of(), schedule_of(agent_id="ag-2")
    schedules, runs, turns = FakeSchedules(s, kept), FakeRuns(), FakeTurns()
    manual = runs.add(run_of(status="starting"))
    slot = runs.add(run_of(source="schedule", source_id=s.id, status="waiting_approval"))
    running = runs.add(run_of(status="running", session_id="s1", turn_id="t1"))
    done = runs.add(run_of(status="succeeded", ended_at=NOW - timedelta(hours=1)))
    asyncio.run(service_of(runs, schedules, turns=turns).forget_agent("default", "ag-1"))
    assert list(schedules.docs) == [kept.id]
    assert (manual.status, manual.reason) == ("cancelled", AGENT_DELETED)
    assert (slot.status, slot.reason) == ("cancelled", AGENT_DELETED)
    assert done.status == "succeeded"
    # 삭제가 파드를 내리기 전에 인스턴스의 턴을 모두 멈췄으므로 런타임을 다시 부르지 않는다
    assert running.stop_reason == AGENT_DELETED and turns.stopped == []


@pytest.mark.parametrize(
    ("deleting", "reason"), [(True, "agent_deleted"), (False, "agent_undeployed")]
)
def test_undeploying_or_deleting_marks_the_runs_before_their_turns_stop(deleting, reason):
    runs, turns = FakeRuns(), FakeTurns()
    starting = runs.add(run_of())
    running = runs.add(run_of(status="running", session_id="s1", turn_id="t1"))
    service = service_of(runs, turns=turns)
    asyncio.run(service.end_agent_runs("default", "ag-1", deleting=deleting))
    assert (starting.status, starting.reason) == ("cancelled", reason)
    # 인스턴스의 턴은 해제와 삭제가 멈춘다. 그 turn_end 가 이 까닭으로 끝낸다
    assert (running.status, running.stop_reason, turns.stopped) == ("running", reason, [])
