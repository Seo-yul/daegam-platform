"""턴 리스의 관리자(P79 1·2절)다. 바퀴, 버림, 잃음, 펜스 마감을 가짜 갱신 쪽으로 보고, Postgres
의 관리자 연결은 실제 Postgres 에서 본다(`DAEGAM_TEST_PG_DSN` 이 없으면 건너뛴다). 시간 값은 줄여
쓴다."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from runtime.fence import TurnFenced, fenced, refused
from runtime.lease_keeper import (
    IndexRenewer,
    LeaseKeeper,
    PostgresRenewer,
    renewer_for,
)
from runtime.sessions import MemorySessionIndex, new_turn_token
from tests.conftest import needs_pg
from tests.store_fixtures import schema_store, session_record

FENCE = 0.45


class FakeRenewer:
    """답을 고를 수 있는 갱신 쪽이다. held 가 None 이면 모두 쥐고 있다고 답한다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.held: set[str] | None = None
        self.hang = False
        self.fail = False
        self.cancel = False
        self.delay = 0.0
        self.abandoned = 0
        self.closed = False
        self._in_flight = 0
        self.most_in_flight = 0

    async def renew(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]:
        self.calls.append(dict(leases))
        self._in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self._in_flight)
        try:
            if self.hang:
                await asyncio.Event().wait()
            await asyncio.sleep(self.delay)
            if self.cancel:
                raise asyncio.CancelledError
            if self.fail:
                raise RuntimeError("store down")
            return set(leases) if self.held is None else set(leases) & self.held
        finally:
            self._in_flight -= 1

    async def abandon(self) -> None:
        self.abandoned += 1

    async def close(self) -> None:
        self.closed = True


def keeper(renewer: FakeRenewer) -> LeaseKeeper:
    return LeaseKeeper(renewer, lease_seconds=0.6, interval=0.1, retry=0.05, fence_seconds=FENCE)


def now() -> float:
    return asyncio.get_running_loop().time()


async def until(check: Any, timeout: float = 2.0) -> None:
    """check() 가 참이 될 때까지 기다린다."""
    deadline = now() + timeout
    while not check():
        assert now() < deadline, "기다린 일이 일어나지 않았다"
        await asyncio.sleep(0.01)


async def test_a_round_renews_every_lease_at_once_and_counts_from_its_send_time() -> None:
    renewer = FakeRenewer()
    renewer.delay = 0.05
    k = keeper(renewer)
    first = k.hold("a", "t-a", sent_at=now() - 0.2)
    second = k.hold("b", "t-b", sent_at=now())
    sent = now()
    assert await k.round() is True
    assert renewer.calls == [{"a": "t-a", "b": "t-b"}]
    # 늦게 온 답도 보낸 시각부터 잰다
    for lease in (first, second):
        assert sent <= lease.fence.deadline - FENCE < sent + 0.04
        assert not lease.fence.standing


async def test_a_lease_the_store_no_longer_holds_is_lost_once_and_never_renewed() -> None:
    renewer = FakeRenewer()
    renewer.held = {"a"}
    k = keeper(renewer)
    k.hold("a", "t-a", sent_at=now())
    gone = k.hold("b", "t-b", sent_at=now())
    stopped: list[str] = []
    gone.on_lost = lambda: stopped.append("b")
    assert await k.round() is True
    assert gone.fence.standing and gone.fence.lost and stopped == ["b"]
    k.lose(gone)
    assert stopped == ["b"]
    await k.round()
    assert renewer.calls[-1] == {"a": "t-a"}


async def test_a_round_without_an_answer_is_abandoned_at_its_bound_and_confirms_nothing() -> None:
    renewer = FakeRenewer()
    renewer.hang = True
    k = keeper(renewer)
    lease = k.hold("a", "t-a", sent_at=now())
    deadline = lease.fence.deadline
    started = now()
    assert await k.round() is False
    assert now() - started < 0.3
    assert renewer.abandoned == 1 and lease.fence.deadline == deadline


async def test_a_round_is_bound_by_the_earliest_fence_among_its_leases() -> None:
    renewer = FakeRenewer()
    renewer.hang = True
    k = LeaseKeeper(renewer, lease_seconds=60, interval=10, retry=5, fence_seconds=FENCE)
    k.hold("a", "t-a", sent_at=now() - FENCE + 0.05)
    started = now()
    assert await k.round() is False
    assert now() - started < 0.3


async def test_the_keeper_loses_a_lease_at_its_fence_while_rounds_keep_failing() -> None:
    renewer = FakeRenewer()
    renewer.fail = True
    k = keeper(renewer)
    k.start()
    try:
        lease = k.hold("a", "t-a", sent_at=now())
        stopped = asyncio.Event()
        lease.on_lost = stopped.set
        await asyncio.wait_for(stopped.wait(), 1.0)
        assert lease.fence.standing
        # 실패 뒤에는 간격보다 일찍 다시 돈다
        assert len(renewer.calls) >= 3
    finally:
        await k.stop()
    assert renewer.closed


async def test_the_keeper_renews_on_its_cadence_with_one_round_at_a_time() -> None:
    renewer = FakeRenewer()
    renewer.delay = 0.03
    k = keeper(renewer)
    k.start()
    try:
        lease = k.hold("a", "t-a", sent_at=now())
        await until(lambda: len(renewer.calls) >= 4)
        assert not lease.fence.standing and renewer.most_in_flight == 1
    finally:
        await k.stop()


async def test_the_keeper_survives_a_failing_stop_hook_and_a_cancelled_renewal() -> None:
    """턴 쪽의 멈춤이 예외를 내도, 갱신이 취소로 끝나도 관리자는 이어서 다른 리스를 늘린다."""
    renewer = FakeRenewer()
    renewer.held = {"keep"}
    k = keeper(renewer)
    k.start()
    try:
        broken = k.hold("gone", "t-1", sent_at=now())

        def explode() -> None:
            raise RuntimeError("turn hook failed")

        broken.on_lost = explode
        kept = k.hold("keep", "t-2", sent_at=now())
        await until(lambda: broken.fence.lost)
        renewer.cancel = True
        calls = len(renewer.calls)
        await until(lambda: len(renewer.calls) >= calls + 2)
        renewer.cancel = False
        calls = len(renewer.calls)
        await until(lambda: len(renewer.calls) >= calls + 2)
        assert not kept.fence.standing
    finally:
        await k.stop()


async def test_stopping_during_a_round_cancels_the_renewal() -> None:
    renewer = FakeRenewer()
    renewer.hang = True
    k = LeaseKeeper(renewer, lease_seconds=60, interval=0.05, retry=0.05, fence_seconds=60)
    k.start()
    k.hold("a", "t-a", sent_at=now())
    await until(lambda: renewer.most_in_flight == 1)
    await k.stop()
    await until(lambda: renewer._in_flight == 0)
    assert renewer.closed


async def test_a_round_skips_and_loses_a_lease_whose_fence_already_stands() -> None:
    renewer = FakeRenewer()
    k = keeper(renewer)
    stood = k.hold("late", "t-1", sent_at=now() - FENCE - 1)
    stopped: list[str] = []
    stood.on_lost = lambda: stopped.append("late")
    k.hold("fresh", "t-2", sent_at=now())
    assert await k.round() is True
    assert renewer.calls == [{"fresh": "t-2"}] and stopped == ["late"]


async def test_a_dropped_lease_is_not_renewed_and_a_new_hold_supersedes_the_old_one() -> None:
    renewer = FakeRenewer()
    k = keeper(renewer)
    old = k.hold("a", "t-1", sent_at=now())
    stopped: list[str] = []
    old.on_lost = lambda: stopped.append("old")
    new = k.hold("a", "t-2", sent_at=now())
    assert stopped == ["old"] and old.fence.standing
    await k.round()
    assert renewer.calls == [{"a": "t-2"}]
    k.drop(new)
    k.drop(new)
    await k.round()
    assert len(renewer.calls) == 1 and not new.fence.standing


async def test_memory_and_redis_indexes_renew_through_the_index_itself() -> None:
    index = MemorySessionIndex()
    token = new_turn_token()
    assert await index.claim_turn("s", lease_seconds=60, token=token)
    renewer = IndexRenewer(index)
    assert await renewer.renew({"s": token, "t": token}, lease_seconds=60) == {"s"}


@needs_pg
async def test_real_postgres_keeper_renews_on_its_own_connection_with_a_lock_timeout() -> None:
    """Postgres 의 관리자는 풀 밖의 연결 하나로 늘리고 그 세션에 lock_timeout 3초를 둔다. 버린
    바퀴는 그 연결을 닫고 다음 바퀴가 새로 연다."""
    async with schema_store("inst-1") as store:
        renewer = renewer_for(store)
        assert isinstance(renewer, PostgresRenewer)
        token = new_turn_token()
        await store.index.create(session_record("s-keep", "alice"))
        assert await store.index.claim_turn("s-keep", lease_seconds=60, token=token)
        try:
            assert await renewer.renew({"s-keep": token}, lease_seconds=60) == {"s-keep"}
            conn = renewer._conn
            cur = await conn.execute("SHOW lock_timeout")
            assert (await cur.fetchone())["lock_timeout"] == "3s"
            await renewer.abandon()
            assert conn.closed
            assert await renewer.renew({"s-keep": token}, lease_seconds=60) == {"s-keep"}
            assert renewer._conn is not conn
        finally:
            await renewer.close()


async def test_a_refused_write_drops_the_lease_and_stops_its_turn_at_once() -> None:
    """저장소가 턴의 토큰 검사 쓰기를 거절하면(fence.refused) 그 문맥의 펜스가 서고, 관리자는 다음
    바퀴를 기다리지 않고 리스를 놓고 턴을 멈추게 한다."""
    renewer = FakeRenewer()
    k = keeper(renewer)
    lease = k.hold("a", "t-a", sent_at=now())
    stopped: list[str] = []
    lease.on_lost = lambda: stopped.append("a")
    with fenced(lease.fence):
        error = refused()
    assert isinstance(error, TurnFenced) and lease.fence.lost
    assert stopped == ["a"] and k._leases == {}
    k.lose(lease)
    assert stopped == ["a"]
