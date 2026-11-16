"""발사 워커와 실행 규칙 (P41 3·5·10절). 스토어를 메모리 가짜로 바꿔 DB 없이 돈다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from daegam.api.agent_runs import may_stop, router
from daegam.api.agents import CHAT, READ, WRITE
from daegam.auth.principal import Membership, Principal
from daegam.config import Settings, settings_from_env
from daegam.services.runs.fire import MAX_BACKOFF, ScheduleFireWorker
from daegam.services.runs.service import (
    MAX_RUNNING,
    MAX_RUNS_PER_DAY,
    MAX_WAITING,
    RunError,
    _rule,
    acts_as_of,
)
from daegam.store.agent_runs import ActsAs
from daegam.store.base import utcnow
from tests.helpers.agents import template_spec, without_memory
from tests.helpers.runs import (
    NOW,
    SETTINGS,
    FakeAgent,
    FakeAgents,
    FakeRuns,
    FakeSchedules,
    run_of,
    schedule_of,
    service_of,
    with_budget,
)


def worker(
    schedules: FakeSchedules,
    runs: FakeRuns,
    agent: FakeAgent | None = None,
    by_id: dict[str, FakeAgent] | None = None,
) -> ScheduleFireWorker:
    service = service_of(runs, schedules, SETTINGS)
    agents = FakeAgents(agent, by_id)
    return ScheduleFireWorker(agents, service, owner="w1", interval=15, lease_seconds=60)  # type: ignore[arg-type]


def fire_at(
    schedules: FakeSchedules, runs: FakeRuns, at: datetime, agent: FakeAgent | None = None
) -> str | None:
    """at 에 울릴 때가 된 스케줄 하나를 잡아 처리한다."""

    async def go() -> str | None:
        w = worker(schedules, runs, agent)
        item = await schedules.acquire_due("w1", 60, at)
        return None if item is None else await w.fire(item, at)

    return asyncio.run(go())


# -- 바닥과 놓친 울림 (M1·M2) ------------------------------------------------------------------


def test_a_five_minute_rule_fires_every_slot_when_ticks_run_late():
    s = schedule_of(cron="*/5 * * * *")
    schedules, runs = FakeSchedules(s), FakeRuns()
    for minute in range(0, 30, 5):
        run_id = fire_at(schedules, runs, NOW + timedelta(minutes=minute, seconds=7))
        assert run_id is not None, minute
        runs.docs[run_id].status = "succeeded"
    assert [r.scheduled_for for r in runs.slots()] == [
        NOW + timedelta(minutes=m) for m in range(0, 30, 5)
    ]
    assert schedules.docs[s.id].last_slot_at == NOW + timedelta(minutes=25)


def test_a_late_slot_lets_the_newest_due_slot_fire_once():
    # 08:40 은 놓쳤고 09:00 은 지금 칠 때다. 놓친 것은 하나로 세고 09:00 은 친다
    s = schedule_of(next_run_at=NOW - timedelta(minutes=20))
    schedules, runs = FakeSchedules(s), FakeRuns()
    run_id = fire_at(schedules, runs, NOW)
    assert run_id is not None
    assert [r.scheduled_for for r in runs.slots()] == [NOW]
    assert runs.skips() == {"missed": 1}
    assert schedules.docs[s.id].next_run_at == NOW + timedelta(minutes=15)


def test_after_a_long_outage_the_slot_inside_the_window_still_fires():
    # 매시 정각 규칙이 08:59 부터 10:10 까지 멈췄다. 10:00 은 10분 늦었으니 친다
    s = schedule_of(cron="0 * * * *")
    schedules, runs = FakeSchedules(s), FakeRuns()
    assert fire_at(schedules, runs, NOW + timedelta(hours=1, minutes=10)) is not None
    assert [r.scheduled_for for r in runs.slots()] == [NOW + timedelta(hours=1)]
    assert schedules.docs[s.id].next_run_at == NOW + timedelta(hours=2)


def test_a_daily_report_fires_the_next_day_after_a_day_long_outage():
    s = schedule_of(cron="0 9 * * *")
    schedules, runs = FakeSchedules(s), FakeRuns()
    assert fire_at(schedules, runs, NOW + timedelta(days=1, minutes=30)) is not None
    assert [r.scheduled_for for r in runs.slots()] == [NOW + timedelta(days=1)]


def test_a_slot_older_than_every_window_is_skipped_to_the_next_one():
    s = schedule_of(cron="0 9 * * *")
    schedules, runs = FakeSchedules(s), FakeRuns()
    assert fire_at(schedules, runs, NOW + timedelta(hours=2)) is None
    assert runs.slots() == [] and runs.skips() == {"missed": 1}
    assert schedules.docs[s.id].next_run_at == NOW + timedelta(days=1)


def test_the_floor_compares_slot_times():
    s = schedule_of(cron="* * * * *", last_slot_at=NOW - timedelta(minutes=2))
    schedules, runs = FakeSchedules(s), FakeRuns()
    assert fire_at(schedules, runs, NOW + timedelta(seconds=20)) is None
    assert schedules.docs[s.id].next_run_at == NOW + timedelta(minutes=3)


# -- 남은 기록, 건너뛰기, 순서 ------------------------------------------------------------------


def test_a_record_left_by_a_crash_is_advanced_to_and_sets_the_last_slot():
    s = schedule_of()
    schedules, runs = FakeSchedules(s), FakeRuns()
    left = run_of(source="schedule", source_id=s.id, scheduled_for=NOW)
    runs.docs[left.id] = left
    assert fire_at(schedules, runs, NOW + timedelta(seconds=5)) is None
    stored = schedules.docs[s.id]
    assert (stored.last_slot_at, stored.last_run_id) == (NOW, left.id)
    assert stored.next_run_at == NOW + timedelta(minutes=15)
    assert runs.skips() == {}


def test_an_open_previous_run_and_the_agent_cap_skip_without_a_record():
    s = schedule_of()
    schedules, runs = FakeSchedules(s), FakeRuns()
    prev = run_of(
        source="schedule",
        source_id=s.id,
        scheduled_for=NOW - timedelta(minutes=15),
        status="running",
    )
    runs.docs[prev.id] = prev
    assert fire_at(schedules, runs, NOW) is None
    assert runs.skips() == {"previous_running": 1}

    s2 = schedule_of()
    schedules, runs = FakeSchedules(s2), FakeRuns()
    for _ in range(MAX_RUNNING):
        r = run_of(status="running")
        runs.docs[r.id] = r
    assert fire_at(schedules, runs, NOW) is None
    assert runs.skips() == {"agent_run_limit": 1}


def test_a_failed_insert_leaves_the_slot_unadvanced():
    s = schedule_of()
    schedules, runs = FakeSchedules(s), FakeRuns()
    runs.fail_insert = True
    with pytest.raises(RuntimeError):
        fire_at(schedules, runs, NOW)
    assert schedules.docs[s.id].next_run_at == NOW


# -- 멈춤과 사람의 변경 (S8) ------------------------------------------------------------------


def test_expiry_pauses_and_stops_the_open_runs_of_the_schedule():
    s = schedule_of(expires_at=NOW - timedelta(seconds=1))
    schedules, runs = FakeSchedules(s), FakeRuns()
    waiting = run_of(
        source="schedule",
        source_id=s.id,
        scheduled_for=NOW - timedelta(hours=1),
        status="waiting_approval",
    )
    runs.docs[waiting.id] = waiting
    fire_at(schedules, runs, NOW)
    stored = schedules.docs[s.id]
    assert (stored.paused, stored.paused_reason, stored.next_run_at) == (True, "expired", NOW)
    assert (waiting.status, waiting.reason) == ("cancelled", "expired")


@pytest.mark.parametrize(
    ("state", "paused"), [("undeploying", True), ("deleting", True), ("deploying", False)]
)
def test_an_agent_going_away_pauses_and_a_redeploy_only_waits(state, paused):
    s = schedule_of()
    schedules, runs = FakeSchedules(s), FakeRuns()
    fire_at(schedules, runs, NOW, FakeAgent(state=state))
    stored = schedules.docs[s.id]
    assert stored.paused is paused and runs.slots() == []
    # 멈춘 스케줄은 리스를 놓고, 배포 중인 Agent 의 스케줄은 리스를 늘려 미룬다
    if paused:
        assert stored.lease is None
    else:
        assert stored.lease is not None and stored.lease.until == NOW + timedelta(minutes=1)


def test_a_change_made_during_a_firing_wins_over_the_advance():
    s = schedule_of()
    schedules, runs = FakeSchedules(s), FakeRuns()

    async def go() -> None:
        w = worker(schedules, runs)
        item = await schedules.acquire_due("w1", 60, NOW)
        stored = schedules.docs[s.id]
        stored.next_run_at, stored.updated_at = NOW + timedelta(hours=5), NOW
        await w.fire(item, NOW)

    asyncio.run(go())
    assert schedules.docs[s.id].next_run_at == NOW + timedelta(hours=5)


# -- 실패한 스케줄은 물러난다 (S1) ----------------------------------------------------------


def test_a_schedule_that_keeps_failing_backs_off_and_lets_the_tick_move_on():
    # 틱은 실제 시계로 돈다. 두 스케줄 모두 지금 울릴 때가 지났다
    now = utcnow()
    due = {"next_run_at": now - timedelta(minutes=1), "expires_at": now + timedelta(days=1)}
    broken = schedule_of(time_zone="Not/AZone", **due)
    good = schedule_of(**due)
    schedules, runs = FakeSchedules(broken, good), FakeRuns()

    async def go() -> int:
        return await worker(schedules, runs).tick()

    assert asyncio.run(go()) == 2
    # 실패한 스케줄을 다시 잡지 않고 틱이 끝났다
    assert schedules.acquired == 2
    stored = schedules.docs[broken.id]
    assert stored.fire_failures == 1
    assert stored.lease is not None and stored.lease.until > now + timedelta(seconds=59)


def test_a_deploying_agent_defers_its_schedule_and_the_tick_moves_on():
    now = utcnow()
    due = {"next_run_at": now - timedelta(minutes=1), "expires_at": now + timedelta(days=1)}
    waiting = schedule_of(agent_id="ag-deploying", **due)
    good = schedule_of(**due)
    schedules, runs = FakeSchedules(waiting, good), FakeRuns()

    async def go() -> int:
        w = worker(schedules, runs, by_id={"ag-deploying": FakeAgent(state="deploying")})
        return await w.tick()

    assert asyncio.run(go()) == 2
    assert schedules.acquired == 2
    stored = schedules.docs[waiting.id]
    assert (stored.paused, stored.fire_failures) == (False, 0)
    assert stored.lease is not None and stored.lease.until > now + timedelta(seconds=59)
    assert [r.source_id for r in runs.slots()] == [good.id]


def test_a_record_left_for_the_latest_late_slot_is_advanced_to_without_skips():
    # 09:00 의 기록을 넣고 옮기기 전에 죽었다. 다음 차례는 놓친 것을 다시 세지 않는다
    s = schedule_of(next_run_at=NOW - timedelta(minutes=20))
    schedules, runs = FakeSchedules(s), FakeRuns()
    left = run_of(source="schedule", source_id=s.id, scheduled_for=NOW)
    runs.docs[left.id] = left
    assert fire_at(schedules, runs, NOW + timedelta(seconds=30)) is None
    stored = schedules.docs[s.id]
    assert (stored.last_slot_at, stored.last_run_id) == (NOW, left.id)
    assert stored.next_run_at == NOW + timedelta(minutes=15)
    assert runs.skips() == {}


def test_the_backoff_doubles_and_stops_at_an_hour():
    w = worker(FakeSchedules(), FakeRuns())
    assert w.backoff(schedule_of()) == timedelta(minutes=1)
    assert w.backoff(schedule_of(fire_failures=2)) == timedelta(minutes=4)
    assert w.backoff(schedule_of(fire_failures=30)) == MAX_BACKOFF


# -- 전제 조건과 상한 (10절) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "settings", "message", "code"),
    [
        (FakeAgent(state="draft"), SETTINGS, None, (409, "not_deployed")),
        (FakeAgent(state="deploying"), SETTINGS, None, (409, "not_deployed")),
        (FakeAgent(spec=without_memory()), SETTINGS, None, (409, "runs_need_memory")),
        (
            FakeAgent(spec=with_budget(template_spec(), None)),
            SETTINGS,
            None,
            (409, "run_budget_required"),
        ),
        (
            FakeAgent(),
            Settings(
                mongo_url="mongodb://unused", mongo_db="unused", telemetry_ingest_enabled=False
            ),
            None,
            (409, "runs_need_events"),
        ),
        (FakeAgent(), SETTINGS, "x" * 4001, (413, "max_input_chars")),
    ],
)
def test_each_prerequisite_refuses_with_its_code(agent, settings, message, code):
    service = service_of(FakeRuns(), FakeSchedules(), settings)
    with pytest.raises(RunError) as e:
        service.ready(agent, message)  # type: ignore[arg-type]
    assert (e.value.status, e.value.code) == code


def test_a_ready_agent_gives_its_pinned_revision():
    service = service_of(FakeRuns(), FakeSchedules(), SETTINGS)
    assert service.ready(FakeAgent(), "hello") == 3  # type: ignore[arg-type]


def test_the_bound_person_keeps_the_local_token_version():
    user = Principal(sub="alice", raw={"preferred_username": "alice", "tv": 4})
    assert acts_as_of(user) == ActsAs(sub="alice", username="alice", tv=4)
    assert acts_as_of(Principal(sub="header")).tv is None


@pytest.mark.parametrize(
    ("count", "status", "created"),
    [
        (MAX_WAITING, "waiting_approval", NOW),
        (MAX_RUNS_PER_DAY, "succeeded", NOW),
    ],
)
def test_the_waiting_and_daily_caps_refuse_too(count, status, created):
    runs = FakeRuns()
    for _ in range(count):
        r = run_of(status=status, created_at=created)
        runs.docs[r.id] = r
    service = service_of(runs, FakeSchedules(), SETTINGS)
    error = asyncio.run(service.cap_error("default", "ag-1", NOW))
    assert error is not None and error.code == "agent_run_limit"
    # 어제 만든 실행은 하루 상한에 세지 않는다
    yesterday = FakeRuns()
    for _ in range(MAX_RUNS_PER_DAY):
        r = run_of(status="succeeded", created_at=NOW - timedelta(days=1))
        yesterday.docs[r.id] = r
    service = service_of(yesterday, FakeSchedules(), SETTINGS)
    assert asyncio.run(service.cap_error("default", "ag-1", NOW)) is None


def test_a_rule_is_stored_with_its_zone_name_and_first_firing():
    rule, zone, first = asyncio.run(_rule(" 0  9 * * 1-5", "Asia/Seoul", NOW))
    # 2026-10-29 는 목요일이다. 서울 09:00 은 그날 00:00 UTC 라 지났고 다음은 금요일이다
    assert (rule, zone, first) == ("0 9 * * 1-5", "Asia/Seoul", datetime(2026, 10, 30, 0, 0))


def test_the_cap_answers_429_with_retry_after():
    runs = FakeRuns()
    for _ in range(MAX_RUNNING):
        r = run_of(status="running")
        runs.docs[r.id] = r
    service = service_of(runs, FakeSchedules(), SETTINGS)
    error = asyncio.run(service.cap_error("default", "ag-1", NOW))
    assert error is not None
    assert (error.status, error.code, error.headers) == (
        429,
        "agent_run_limit",
        {"Retry-After": "60"},
    )


# -- 권한 (2절) -----------------------------------------------------------------------------


def _principal(sub: str, role: str) -> Principal:
    return Principal(sub=sub, memberships=[Membership(tenant="acme", role=role)])


def test_only_the_bound_person_or_an_approver_stops_a_run():
    run = run_of(acts_as=ActsAs(sub="alice"))
    assert may_stop(run, _principal("alice", "viewer"), "acme")
    assert may_stop(
        run, _principal("bob", "developer"), "acme"
    )  # developer 는 agent:approve 를 가진다
    assert not may_stop(run, _principal("carol", "viewer"), "acme")
    assert not may_stop(run, _principal("bob", "developer"), "other")


def test_writes_need_agent_write_and_chat_and_reads_need_agent_read():
    deps = {
        (sorted(r.methods)[0], r.path): [d.dependency for d in r.dependencies]  # type: ignore[attr-defined]
        for r in router.routes
    }
    write = [WRITE.dependency, CHAT.dependency]
    assert all(dep in deps[("POST", "/agents/{agent_id}/runs")] for dep in write)
    assert all(dep in deps[("POST", "/agents/{agent_id}/schedules")] for dep in write)
    assert READ.dependency in deps[("GET", "/agents/{agent_id}/runs")]
    assert READ.dependency in deps[("POST", "/agents/{agent_id}/runs/{run_id}/cancel")]


def test_runs_are_on_by_default_and_the_setting_turns_them_off(bare_env):
    assert settings_from_env().runs_enabled is True
    bare_env.setenv("DAEGAM_RUNS_ENABLED", "false")
    assert settings_from_env().runs_enabled is False
