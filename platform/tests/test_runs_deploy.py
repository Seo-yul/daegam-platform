"""배포와 해제가 백그라운드 실행의 소스를 맞춘다 (P41 3·7·10절). 스토어를 메모리 가짜로 바꿔 DB
없이 돈다."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from daegam.auth.principal import Principal
from daegam.services.runs.events import approval_deadline
from daegam.services.runs.service import (
    AGENT_CHANGED,
    AGENT_UNDEPLOYED,
    PREREQUISITES_FAILED,
    RUNS_PAUSED_BY_DEPLOY,
)
from daegam.services.runs.trigger_secrets import TriggerKeys
from daegam.store.agent_runs import ActsAs
from daegam.store.agents import AgentRevision
from tests.helpers.agents import template_spec
from tests.helpers.runs import (
    NOW,
    WRITES,
    FakeAgent,
    FakeRuns,
    FakeSchedules,
    FakeTriggers,
    FakeTriggerSecrets,
    FakeTurns,
    guarded,
    run_of,
    schedule_of,
    service_of,
    trigger_of,
    with_budget,
    with_ttl,
)

ALICE = Principal(sub="alice")


def deploy(
    schedules: FakeSchedules,
    runs: FakeRuns | None = None,
    *,
    agent: FakeAgent | None = None,
    previous: AgentRevision | None = None,
    deployer: Principal = ALICE,
    triggers: FakeTriggers | None = None,
):
    service = service_of(runs, schedules, triggers=triggers)
    return asyncio.run(service.on_deploy(agent or FakeAgent(), deployer, previous))


def test_the_deployers_own_schedules_move_and_others_pause_with_a_warning():
    mine = schedule_of(revision=2)
    bobs = schedule_of(revision=2, acts_as=ActsAs(sub="bob"))
    current = schedule_of(revision=3, acts_as=ActsAs(sub="bob"))
    stopped = schedule_of(
        revision=2, acts_as=ActsAs(sub="bob"), paused=True, paused_reason="paused"
    )
    schedules = FakeSchedules(mine, bobs, current, stopped)
    (warning,) = deploy(schedules)
    assert (mine.revision, mine.paused) == (3, False)
    assert (bobs.paused, bobs.paused_reason) == (True, AGENT_CHANGED)
    # 같은 리비전과 이미 멈춘 스케줄은 바뀌지 않는다
    assert not current.paused and stopped.paused_reason == "paused"
    assert warning.code == RUNS_PAUSED_BY_DEPLOY and warning.level == "warning"
    assert "paused 1 schedule(s)" in warning.message and "and 0 whose" in warning.message


def test_a_revision_that_breaks_a_prerequisite_pauses_every_active_schedule():
    mine = schedule_of(revision=3)
    bobs = schedule_of(revision=2, acts_as=ActsAs(sub="bob"))
    schedules = FakeSchedules(mine, bobs)
    agent = FakeAgent(spec=with_budget(template_spec(), None))
    (warning,) = deploy(schedules, agent=agent)
    assert {s.paused_reason for s in (mine, bobs)} == {PREREQUISITES_FAILED}
    assert "paused 0 schedule(s)" in warning.message and "and 2 whose" in warning.message


def test_triggers_move_or_pause_like_schedules_and_the_warning_counts_them_apart():
    mine, bobs = trigger_of(revision=2), trigger_of(revision=2, acts_as=ActsAs(sub="bob"))
    bobs_schedule = schedule_of(revision=2, acts_as=ActsAs(sub="bob"))
    triggers = FakeTriggers(mine, bobs)
    (warning,) = deploy(FakeSchedules(bobs_schedule), agent=guarded(), triggers=triggers)
    assert (mine.revision, mine.paused) == (3, False)
    assert (bobs.paused, bobs.paused_reason) == (True, AGENT_CHANGED)
    assert "paused 1 schedule(s) and 1 trigger(s)" in warning.message


def test_a_revision_with_unguarded_writes_pauses_the_triggers_but_not_the_schedules():
    t, s = trigger_of(revision=3), schedule_of(revision=3)
    triggers = FakeTriggers(t)
    (warning,) = deploy(FakeSchedules(s), agent=FakeAgent(tool_pins=WRITES), triggers=triggers)
    assert (t.paused, t.paused_reason) == (True, PREREQUISITES_FAILED)
    assert not s.paused
    assert "and 0 trigger(s)" in warning.message and "and 1 whose" in warning.message


def test_a_deploy_that_pauses_nothing_warns_of_nothing():
    schedules = FakeSchedules(schedule_of(revision=3), schedule_of(revision=2))
    assert deploy(schedules) == []


def test_a_new_session_ttl_moves_the_deadlines_of_waiting_runs():
    runs = FakeRuns()
    deadline = approval_deadline(NOW, 3600)
    run = runs.add(run_of(status="waiting_approval", deadline=deadline))
    other = runs.add(run_of(status="running"))
    previous = AgentRevision(revision=2, **with_ttl(template_spec(), 3600))
    agent = FakeAgent(spec=with_ttl(template_spec(), 600))
    deploy(FakeSchedules(), runs, agent=agent, previous=previous)
    assert run.deadline == approval_deadline(NOW, 600)
    assert other.deadline is None


def test_the_same_session_ttl_leaves_deadlines_alone():
    runs = FakeRuns()
    run = runs.add(run_of(status="waiting_approval", deadline=NOW + timedelta(minutes=5)))
    previous = AgentRevision(revision=2, **template_spec())
    deploy(FakeSchedules(), runs, previous=previous)
    assert run.deadline == NOW + timedelta(minutes=5)


def test_undeploying_pauses_the_sources_and_deleting_leaves_them_for_the_delete():
    s, t = schedule_of(), trigger_of()
    schedules, runs, turns = FakeSchedules(s), FakeRuns(), FakeTurns()
    running = runs.add(run_of(source="schedule", source_id=s.id, status="running", turn_id="t1"))
    service = service_of(runs, schedules, turns=turns, triggers=FakeTriggers(t))
    asyncio.run(service.end_agent_runs("default", "ag-1", deleting=True))
    assert not s.paused and not t.paused
    asyncio.run(service.end_agent_runs("default", "ag-1", deleting=False))
    assert (s.paused, s.paused_reason) == (True, AGENT_UNDEPLOYED)
    assert (t.paused, t.paused_reason) == (True, AGENT_UNDEPLOYED)
    # 해제가 인스턴스의 턴을 모두 멈추므로 실행마다 런타임을 부르지 않는다
    assert running.stop_requested_at is not None and turns.stopped == []


def test_a_schedule_deleted_during_the_deploy_is_skipped():
    gone, bobs = schedule_of(revision=2), schedule_of(revision=2, acts_as=ActsAs(sub="bob"))
    schedules = FakeSchedules(gone, bobs)
    update = schedules.update

    async def deleted_meanwhile(schedule_id: str, fields: dict):
        if schedule_id == gone.id:
            return None
        return await update(schedule_id, fields)

    schedules.update = deleted_meanwhile  # type: ignore[method-assign]
    (warning,) = deploy(schedules)
    assert bobs.paused_reason == AGENT_CHANGED and "paused 1 schedule(s)" in warning.message


def test_deleting_an_agent_deletes_its_sources_and_its_webhook_secret():
    triggers, schedules, secrets = (
        FakeTriggers(trigger_of()),
        FakeSchedules(schedule_of()),
        FakeTriggerSecrets(),
    )
    secrets.keys[("ag-1", "t1")] = TriggerKeys("whsec_a", None)
    service = service_of(schedules=schedules, triggers=triggers, secrets=secrets)
    asyncio.run(service.forget_agent("default", "ag-1"))
    assert triggers.docs == {} and schedules.docs == {}
    assert secrets.dropped_agents == ["ag-1"] and secrets.keys == {}


def test_a_secret_that_cannot_be_deleted_does_not_fail_the_agent_delete(
    caplog: pytest.LogCaptureFixture,
):
    triggers, secrets = FakeTriggers(trigger_of()), FakeTriggerSecrets()
    secrets.broken = True
    service = service_of(triggers=triggers, secrets=secrets)
    with caplog.at_level(logging.WARNING, logger="daegam.runs"):
        asyncio.run(service.forget_agent("default", "ag-1"))
    assert triggers.docs == {}
    assert "webhook secret was not deleted" in caplog.text
