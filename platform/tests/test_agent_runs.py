"""실행 기록과 스케줄의 Mongo 스토어 (P41 1·3·5절). 테스트 Mongo 가 있어야 한다. 발사 워커의
분기는 tests/test_runs_rules.py 가 메모리 가짜로 본다."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daegam.store.agent_runs import ActsAs, MongoAgentRunStore, new_run, skip_row_id
from daegam.store.agent_schedules import AgentSchedule, MongoAgentScheduleStore
from daegam.store.base import ItemLease, new_id, utcnow
from tests.helpers.aio import run_stores

ALICE = ActsAs(sub="alice", username="alice", tv=3)
# naive UTC. 저장 형식과 같다 (store/base.py utcnow). 실행 기록의 `expires_at` 이 이것에서 나오고
# TTL 인덱스가 그 시각에 지우므로 실제 시계를 따른다. 정해 둔 날짜면 그 날이 지난 뒤의 시험에서
# 기록이 사라진다. Mongo 의 밀리초 정밀도에 맞게 분으로 자른다
NOW = utcnow().replace(second=0, microsecond=0)
STORES = [MongoAgentRunStore, MongoAgentScheduleStore]


def run_of(**fields: Any):
    base = new_run(tenant="default", agent_id="ag-1", source="manual", acts_as=ALICE, now=NOW)
    return base.model_copy(update=fields)


def schedule_of(**fields: Any) -> AgentSchedule:
    base: dict[str, Any] = dict(
        id=new_id(),
        tenant="default",
        agent_id="ag-1",
        acts_as=ALICE,
        revision=1,
        cron="*/15 * * * *",
        time_zone="UTC",
        message="report",
        expires_at=NOW + timedelta(days=90),
        next_run_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
    return AgentSchedule(**{**base, **fields})


# -- 기록 ---------------------------------------------------------------------------------


def test_one_slot_and_one_delivery_make_one_record_and_manual_runs_never_collide(settings):
    async def body(runs, schedules):
        slot = run_of(source="schedule", source_id="s1", scheduled_for=NOW)
        assert await runs.insert(slot)
        assert not await runs.insert(run_of(source="schedule", source_id="s1", scheduled_for=NOW))
        assert await runs.insert(run_of(source="webhook", source_id="t1", delivery_id="d1"))
        assert not await runs.insert(run_of(source="webhook", source_id="t1", delivery_id="d1"))
        assert await runs.insert(run_of())
        assert await runs.insert(run_of())
        return (
            await runs.coll.find_one({"_id": slot.id}),
            await runs.coll.find_one({"source": "manual"}),
            await runs.find_slot("s1", NOW),
        )

    stored, manual, found = run_stores(settings, STORES, body)
    assert stored["scheduled_for"] == NOW
    assert stored["acts_as"]["tv"] == 3
    assert "scheduled_for" not in manual and "delivery_id" not in manual
    assert found is not None and found.id == stored["_id"]


def test_skips_of_one_source_and_reason_merge_into_one_row(settings):
    async def body(runs, schedules):
        for i in range(3):
            await runs.bump_skip(
                tenant="default",
                agent_id="ag-1",
                source="schedule",
                source_id="s1",
                reason="missed",
                acts_as=ALICE,
                now=NOW + timedelta(minutes=i),
            )
        return await runs.coll.find_one({"_id": skip_row_id("s1", "missed")})

    row = run_stores(settings, STORES, body)
    assert (row["count"], row["status"]) == (3, "skipped")
    assert row["first_at"] == NOW
    assert row["last_at"] == NOW + timedelta(minutes=2)


def test_pausing_a_source_ends_its_waiting_runs_and_marks_running_ones(settings):
    async def body(runs, schedules):
        waiting = run_of(source="schedule", source_id="s1", status="waiting_approval")
        running = run_of(source="schedule", source_id="s1", status="running")
        done = run_of(source="schedule", source_id="s1", status="succeeded")
        for r in (waiting, running, done):
            await runs.insert(r)
        ended = await runs.cancel_open_for_source("s1", "paused", NOW)
        return ended, [await runs.get("default", "ag-1", r.id) for r in (waiting, running, done)]

    marked, (waiting, running, done) = run_stores(settings, STORES, body)
    # 표시만 남은 도는 실행을 돌려준다. 그 턴은 런타임에서 멈춰야 끝난다
    assert [r.id for r in marked] == [running.id]
    assert (waiting.status, waiting.reason) == ("cancelled", "paused")
    assert (running.status, running.stop_requested_at, running.stop_reason) == (
        "running",
        NOW,
        "paused",
    )
    assert (done.status, done.stop_requested_at) == ("succeeded", None)


# -- 스케줄의 리스 --------------------------------------------------------------------------


def test_two_workers_never_hold_one_schedule(settings):
    async def body(runs, schedules):
        await schedules.insert(schedule_of())
        first = await schedules.acquire_due("w1", 60, NOW)
        second = await schedules.acquire_due("w2", 60, NOW)
        later = await schedules.acquire_due("w2", 60, NOW + timedelta(seconds=61))
        return first, second, later

    first, second, later = run_stores(settings, STORES, body)
    assert first is not None and second is None
    # 리스가 끝나면 다른 워커가 이어받는다
    assert later is not None and later.lease.owner == "w2"


def test_an_advance_moves_the_slot_and_records_what_fired(settings):
    async def body(runs, schedules):
        await schedules.insert(schedule_of())
        held = await schedules.acquire_due("w1", 60, NOW)
        ok = await schedules.advance(
            held,
            "w1",
            next_run_at=NOW + timedelta(minutes=15),
            slot=NOW,
            fired_at=NOW + timedelta(seconds=7),
            run_id="r1",
        )
        return ok, await schedules.get("default", "ag-1", held.id)

    ok, s = run_stores(settings, STORES, body)
    assert ok
    assert (s.next_run_at, s.last_slot_at, s.last_fired_at, s.last_run_id) == (
        NOW + timedelta(minutes=15),
        NOW,
        NOW + timedelta(seconds=7),
        "r1",
    )
    assert s.lease is None


def test_a_change_made_while_a_worker_holds_the_lease_is_not_overwritten(settings):
    async def body(runs, schedules):
        await schedules.insert(schedule_of())
        held = await schedules.acquire_due("w1", 60, NOW)
        await schedules.update(
            held.id, {"next_run_at": NOW + timedelta(hours=5), "updated_at": NOW}
        )
        moved = await schedules.advance(held, "w1", next_run_at=NOW + timedelta(minutes=15))
        paused = await schedules.pause_held(held, "w1", "expired")
        return moved, paused, await schedules.get("default", "ag-1", held.id)

    moved, paused, s = run_stores(settings, STORES, body)
    assert not moved and not paused
    assert (s.next_run_at, s.paused) == (NOW + timedelta(hours=5), False)


def test_a_failed_schedule_backs_off_and_leaves_the_queue(settings):
    async def body(runs, schedules):
        failing = schedule_of(next_run_at=NOW - timedelta(minutes=1))
        other = schedule_of()
        await schedules.insert(failing)
        await schedules.insert(other)
        first = await schedules.acquire_due("w1", 60, NOW)
        await schedules.back_off(first.id, "w1", NOW + timedelta(minutes=2))
        second = await schedules.acquire_due("w1", 60, NOW)
        return first, second, await schedules.get("default", "ag-1", failing.id)

    first, second, failing = run_stores(settings, STORES, body)
    assert first.id == failing.id and second.id != failing.id
    assert failing.fire_failures == 1
    assert failing.lease.until == NOW + timedelta(minutes=2)


def test_a_deferred_schedule_waits_without_counting_a_failure(settings):
    async def body(runs, schedules):
        await schedules.insert(schedule_of())
        held = await schedules.acquire_due("w1", 60, NOW)
        # 리스를 쥐지 않은 워커의 미루기는 아무것도 바꾸지 않는다
        await schedules.defer(held.id, "w2", NOW + timedelta(hours=1))
        await schedules.defer(held.id, "w1", NOW + timedelta(minutes=1))
        again = await schedules.acquire_due("w1", 60, NOW + timedelta(seconds=30))
        return again, await schedules.get("default", "ag-1", held.id)

    again, s = run_stores(settings, STORES, body)
    assert again is None
    assert (s.fire_failures, s.lease.owner) == (0, "w1")
    assert s.lease.until == NOW + timedelta(minutes=1)


# -- 시작 워커의 리스 (5절) --------------------------------------------------------------------


def test_the_start_lease_takes_due_runs_oldest_first_and_skips_busy_agents(settings):
    async def body(runs, schedules):
        oldest = run_of(next_attempt_at=NOW - timedelta(minutes=2))
        same_agent = run_of(next_attempt_at=NOW - timedelta(minutes=1))
        other_agent = run_of(agent_id="ag-2", next_attempt_at=NOW - timedelta(minutes=1))
        later = run_of(next_attempt_at=NOW + timedelta(minutes=1))
        running = run_of(status="running")
        for r in (oldest, same_agent, other_agent, later, running):
            await runs.insert(r)
        first = await runs.acquire_start("w1", 60, NOW)
        second = await runs.acquire_start("w1", 60, NOW, busy={"ag-1"})
        third = await runs.acquire_start("w1", 60, NOW, busy={"ag-1", "ag-2"})
        elsewhere = await runs.acquire_start("w2", 60, NOW)
        return [first, second, third, elsewhere], [oldest, other_agent, same_agent]

    got, want = run_stores(settings, STORES, body)
    first, second, third, elsewhere = got
    oldest, other_agent, same_agent = want
    assert (first.id, second.id, third) == (oldest.id, other_agent.id, None)
    assert first.lease.owner == "w1"
    # 다른 워커는 리스가 걸린 것을 건너뛴다
    assert elsewhere.id == same_agent.id


def test_a_retry_releases_the_lease_and_waits_for_its_time(settings):
    async def body(runs, schedules):
        run = run_of()
        await runs.insert(run)
        await runs.acquire_start("w1", 60, NOW)
        foreign = await runs.retry_start(
            run.id, "w2", attempts=1, at=NOW + timedelta(seconds=30), now=NOW
        )
        mine = await runs.retry_start(
            run.id, "w1", attempts=1, at=NOW + timedelta(seconds=30), now=NOW
        )
        early = await runs.acquire_start("w1", 60, NOW + timedelta(seconds=10))
        due = await runs.acquire_start("w1", 60, NOW + timedelta(seconds=30))
        return foreign, mine, early, due

    foreign, mine, early, due = run_stores(settings, STORES, body)
    assert (foreign, mine, early) == (False, True, None)
    assert (due.attempts, due.last_event_at) == (1, NOW)


# -- 이벤트와 지켜보기 (7·8절) -----------------------------------------------------------------


def test_an_event_binds_the_session_and_older_or_foreign_session_events_are_dropped(settings):
    async def body(runs, schedules):
        run = run_of(message="check")
        await runs.insert(run)
        bound = await runs.apply_event(
            run.id, "s1", ts=NOW, to_status="running", now=NOW, fields={"session_id": "s1"}
        )
        foreign = await runs.apply_event(run.id, "s2", ts=NOW, to_status="succeeded", now=NOW)
        older = await runs.apply_event(
            run.id, "s1", ts=NOW - timedelta(seconds=1), to_status="succeeded", now=NOW
        )
        ended = await runs.apply_event(
            run.id, "s1", ts=NOW + timedelta(seconds=5), to_status="succeeded", now=NOW
        )
        reopened = await runs.apply_event(
            run.id, "s1", ts=NOW + timedelta(seconds=9), to_status="running", now=NOW
        )
        return (bound, foreign, older, ended, reopened), await runs.coll.find_one({"_id": run.id})

    flags, doc = run_stores(settings, STORES, body)
    assert flags == (True, False, False, True, False)
    assert (doc["status"], doc["session_id"], doc["event_at"]) == (
        "succeeded",
        "s1",
        NOW + timedelta(seconds=5),
    )
    assert doc["message"] is None and doc["ended_at"] == NOW


def test_the_watch_lease_takes_past_deadlines_and_quiet_runs(settings):
    async def body(runs, schedules):
        due = run_of(status="waiting_approval", deadline=NOW - timedelta(seconds=1))
        not_due = run_of(status="waiting_approval", deadline=NOW + timedelta(minutes=1))
        quiet = run_of(status="running", last_event_at=NOW - timedelta(minutes=10))
        busy = run_of(status="running", last_event_at=NOW)
        for r in (due, not_due, quiet, busy):
            await runs.insert(r)
        quiet_since = NOW - timedelta(minutes=1)
        got = [await runs.acquire_watch("w1", 60, NOW, quiet_since=quiet_since) for _ in range(3)]
        return [r.id if r else None for r in got], due.id, quiet.id

    got, due_id, quiet_id = run_stores(settings, STORES, body)
    assert sorted(got[:2]) == sorted([due_id, quiet_id]) and got[2] is None


def test_the_deadline_refusal_and_quiet_settling_are_conditional(settings):
    async def body(runs, schedules):
        waiting = run_of(status="waiting_approval", deadline=NOW)
        later = run_of(status="waiting_approval", deadline=NOW + timedelta(minutes=1))
        quiet = run_of(status="running", last_event_at=NOW - timedelta(minutes=10))
        for r in (waiting, later, quiet):
            await runs.insert(r)
        refused = await runs.refuse_due(waiting.id, NOW, reason="approval_deadline")
        early = await runs.refuse_due(later.id, NOW, reason="approval_deadline")
        stale = quiet.model_copy(update={"last_event_at": NOW - timedelta(minutes=11)})
        moved_meanwhile = await runs.settle_quiet(stale, to_status="succeeded", now=NOW)
        settled = await runs.settle_quiet(
            quiet, to_status="waiting_approval", now=NOW, deadline=NOW + timedelta(minutes=55)
        )
        return (refused, early, moved_meanwhile, settled), [
            await runs.coll.find_one({"_id": r.id}) for r in (waiting, quiet)
        ]

    flags, (waiting, quiet) = run_stores(settings, STORES, body)
    assert flags == (True, False, False, True)
    assert (waiting["status"], waiting["reason"]) == ("refused", "approval_deadline")
    assert (quiet["status"], quiet["deadline"]) == (
        "waiting_approval",
        NOW + timedelta(minutes=55),
    )


def test_a_move_with_an_owner_needs_the_lease(settings):
    async def body(runs, schedules):
        run = run_of()
        await runs.insert(run)
        await runs.acquire_start("w1", 60, NOW)
        foreign = await runs.move(
            run.id, from_status=("starting",), to_status="failed", now=NOW, owner="w2"
        )
        mine = await runs.move(
            run.id, from_status=("starting",), to_status="failed", now=NOW, owner="w1"
        )
        # unmarked 는 멈춤 표시가 붙은 실행을 옮기지 않는다
        marked = run_of(status="running", stop_requested_at=NOW)
        await runs.insert(marked)
        blocked = await runs.move(
            marked.id,
            from_status=("running",),
            to_status="waiting_approval",
            now=NOW,
            unmarked=True,
        )
        return foreign, mine, blocked

    assert run_stores(settings, STORES, body) == (False, True, False)


def test_an_interrupt_waits_only_without_a_stop_mark_and_any_event_clears_the_lease(settings):
    async def body(runs, schedules):
        lease = ItemLease(owner="watch", until=NOW)
        run = run_of(status="running", session_id="s1", lease=lease)
        await runs.insert(run)
        await runs.mark_stop(run, NOW, "paused")
        waiting = await runs.apply_event(
            run.id, "s1", ts=NOW, to_status="waiting_approval", now=NOW, unmarked=True
        )
        cancelled = await runs.apply_event(
            run.id, "s1", ts=NOW, to_status="cancelled", now=NOW, reason="paused"
        )
        return waiting, cancelled, await runs.coll.find_one({"_id": run.id})

    waiting, cancelled, doc = run_stores(settings, STORES, body)
    assert (waiting, cancelled) == (False, True)
    assert (doc["status"], doc["reason"], doc["lease"]) == ("cancelled", "paused", None)


def test_a_start_is_still_recorded_after_an_event_released_the_lease(settings):
    async def body(runs, schedules):
        run = run_of(message="check the queue")
        await runs.insert(run)
        await runs.acquire_start("w1", 60, NOW)
        noted = {"turn_id": "t1", "grant_id": None, "acts_as": ActsAs(sub="alice"), "now": NOW}
        # 리스를 쥔 워커만 적는다
        foreign = await runs.note_start(run.id, "w2", session_id="s1", **noted)
        await runs.apply_event(
            run.id, "s1", ts=NOW, to_status="running", now=NOW, fields={"session_id": "s1"}
        )
        other_session = await runs.note_start(run.id, "w1", session_id="s2", **noted)
        mine = await runs.note_start(run.id, "w1", session_id="s1", **noted)
        # 한 번 적은 시작은 리스 없이 다시 적지 않는다
        again = await runs.note_start(run.id, "w1", session_id="s1", **noted)
        return (foreign, other_session, mine, again), await runs.coll.find_one({"_id": run.id})

    flags, doc = run_stores(settings, STORES, body)
    assert flags == (False, False, True, False)
    # 적기는 상태를 옮기지 않고 입력 본문을 지운다
    assert (doc["status"], doc["session_id"], doc["turn_id"], doc["message"]) == (
        "running",
        "s1",
        "t1",
        None,
    )
    assert doc["started_at"] == NOW


# -- 결정 (7절) --------------------------------------------------------------------------------


def test_the_schedules_bound_to_a_person_are_found_across_tenants(settings):
    async def body(runs, schedules):
        mine, elsewhere = schedule_of(), schedule_of(tenant="other")
        bobs = schedule_of(acts_as=ActsAs(sub="bob"))
        for s in (mine, elsewhere, bobs):
            await schedules.insert(s)
        listed = await schedules.list_bound("alice")
        return (
            {s.id for s in listed} == {mine.id, elsewhere.id},
            await schedules.get_bound("alice", bobs.id),
            (await schedules.get_bound("bob", bobs.id)).id == bobs.id,
            # 남에게 묶인 소스는 멈추지 않는다
            await schedules.pause_own("alice", bobs.id, "paused", NOW),
            (await schedules.pause_own("alice", mine.id, "paused", NOW)).paused_reason,
            (await schedules.get("default", "ag-1", bobs.id)).paused,
        )

    assert run_stores(settings, STORES, body) == (True, None, True, None, "paused", False)


def test_a_ttl_change_shifts_only_waiting_deadlines(settings):
    async def body(runs, schedules):
        waiting = run_of(status="waiting_approval", deadline=NOW)
        running = run_of(status="running", deadline=NOW)
        for r in (waiting, running):
            await runs.insert(r)
        moved = await runs.shift_deadlines("default", "ag-1", timedelta(minutes=-10))
        return moved, [await runs.coll.find_one({"_id": r.id}) for r in (waiting, running)]

    moved, (waiting, running) = run_stores(settings, STORES, body)
    assert moved == 1
    assert (waiting["deadline"], running["deadline"]) == (NOW - timedelta(minutes=10), NOW)
