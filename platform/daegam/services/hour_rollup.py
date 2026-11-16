"""시간 묶음 롤업이 함께 쓰는 시간 고르기와 표시 처리 (P37 10절, P51 3절).

사용량 롤업(`services/usage/rollup.py`)과 MCP 메트릭 롤업(`services/mcp_metrics/rollup.py`)이
같은 규칙을 따른다. 틱은 앞 틱의 앞 시간부터 지금 시간까지를 다시 세되 지금과 앞 시간만 바로
세고 그보다 오래된 시간은 표시로 넘긴다. 표시는 오래된 것부터 틱의 예산 안에서 다시 센다. 보관
기간의 경계(floor)보다 오래된 시간은 세지 않는다. 행이 TTL 로 지워진 시간을 다시 세면 실제 묶음이
빈 결과로 바뀌기 때문이다. 두 롤업은 리스 하나로 한 레플리카만 돌고(`LeasedLoop`), 첫 틱은 한
주기 뒤다. 기동 직후의 일과 겹치지 않게 하고, 롤업은 조금 늦어도 된다. 두 롤업은 시간 하나를
다시 세는 일(`recount_hour`)도 함께 쓴다. 집계하고 쓰는 동안 리스를 늘리고, 쓰기 전에 리스를 다시
본다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol

import pymongo

from daegam.services.claims import TickHold, leased_tick, renewing, tick_fence, tick_item
from daegam.services.periodic import cancel_task, run_periodic
from daegam.store.hour_marks import HOUR, HourMarks, hour_of
from daegam.store.leases import LEASE_TTL, Lease

# 틱이 바로 세는 시간 수(지금과 앞 시간). 더 오래된 시간은 다시 셀 시간으로 적는다
DIRECT_HOURS = 2
# 표시를 한 번에 읽는 수
DIRTY_BATCH = 24
# 시간 하나의 집계에 주는 가장 긴 초. 그동안 리스를 늘리고, 넘으면 집계가 실패하고 리스를 더 늘리지
# 않아 리스는 수명이 다하면 풀린다. 멈춘 집계가 리스를 끝없이 쥐면 어느 레플리카도 묶음을 짓지
# 못한다(D79)
AGGREGATION_LIMIT_SECONDS = 10 * LEASE_TTL.total_seconds()


class HourBuckets(Protocol):
    """시간 하나의 묶음을 세고 쓰는 스토어. 세기와 쓰기가 나뉘어 있어야 그 사이에 리스를 본다."""

    async def hour_buckets(self, hour: datetime) -> list[dict[str, Any]]: ...

    async def replace_hour(self, hour: datetime, buckets: list[dict[str, Any]]) -> None: ...


async def recount_hour(store: HourBuckets, hour: datetime) -> None:
    """시간 하나를 다시 센다. 집계가 리스 수명보다 길 수 있어 집계하고 쓰는 동안 리스를 늘리되,
    집계가 `AGGREGATION_LIMIT_SECONDS` 를 넘으면 실패한다. 쓰기 전에는 리스를 다시
    늘리고, 잃었거나 리스 수명 안에 늘린 적이 없으면 멈춘다(`tick_fence`). 그 사이 리스를 잡은
    레플리카가 더 새 결과를 쓴 뒤 옛 결과로 덮지 않기 위해서다."""
    async with renewing(AGGREGATION_LIMIT_SECONDS):
        with pymongo.timeout(AGGREGATION_LIMIT_SECONDS):
            buckets = await store.hour_buckets(hour)
        await tick_fence()
        await store.replace_hour(hour, buckets)


def retention_floor(now: datetime, retention: timedelta) -> datetime:
    """다시 셀 수 있는 가장 오래된 시간. 보관 경계가 든 시간의 다음 시간이다. 경계가 든 시간은
    TTL 이 행을 지우기 시작했고 그 묶음도 이미 TTL 로 지워졌으니, 다시 세면 반쯤 빈 묶음만 다시
    생긴다."""
    return hour_of(now - retention) + HOUR


def recount_hours(last_tick: datetime | None, now: datetime, floor: datetime) -> list[datetime]:
    """앞 틱의 앞 시간부터 지금 시간까지. 앞 틱이 없으면 지금과 앞 시간이다. floor 보다 오래된
    시간은 세지 않는다."""
    current = hour_of(now)
    start = max(hour_of(last_tick) if last_tick else current, floor + HOUR)
    start = min(start, current) - HOUR
    count = int((current - start) / HOUR) + 1
    return [start + i * HOUR for i in range(count)]


async def queue_hours(
    marks: HourMarks, last_tick: datetime | None, now: datetime, floor: datetime
) -> list[datetime]:
    """이번 틱이 다시 셀 시간을 고른다. 바로 셀 시간(지금과 앞 시간)을 돌려주고, 그보다 오래된
    시간(루프가 한 시간 넘게 멈췄던 사이)은 표시로 넘긴다."""
    hours = recount_hours(last_tick, now, floor)
    for hour in hours[:-DIRECT_HOURS]:
        await marks.mark(hour)
    return hours[-DIRECT_HOURS:]


async def drain_dirty(
    marks: HourMarks,
    recount: Callable[[datetime], Awaitable[object]],
    deadline: float,
    *,
    floor: datetime,
    at_least: int = 0,
) -> int:
    """표시된 시간을 오래된 것부터 deadline(monotonic)까지 다시 센다. floor 보다 오래된 표시는
    세지 않고 지운다. 예산이 다 했어도 at_least 개는 한다. 처리한 표시 수를 돌려준다."""
    done = 0
    while True:
        batch = await marks.batch(DIRTY_BATCH)
        if not batch:
            return done
        for mark in batch:
            if done >= at_least and time.monotonic() >= deadline:
                return done
            await tick_item()
            if mark["_id"] >= floor:
                await recount(mark["_id"])
            await marks.clear(mark["_id"], mark["marked_at"])
            done += 1


class LeasedLoop:
    """리스 하나로 한 레플리카만 도는 주기 작업의 시작·틱·멈춤. 하위 클래스가 리스 이름
    (`lease_name`), 로그의 이름(`label`)과 로거(`log`), 틱의 일(`_run`), 시간 묶음 스토어
    (`store`)를 둔다."""

    lease_name: ClassVar[str]
    label: ClassVar[str]
    log: ClassVar[logging.Logger]
    store: HourBuckets

    def __init__(self, lease: Lease, *, interval: float):
        self.lease = lease
        self.interval = interval
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    async def tick(self) -> None:
        await leased_tick(self.lease, self.lease_name, self._run, log=self.log, label=self.label)

    async def _run(self, now: datetime, hold: TickHold) -> None:
        raise NotImplementedError

    async def recount(self, hour: datetime) -> None:
        """시간 하나를 다시 센다. 리스는 `recount_hour` 가 지킨다."""
        await recount_hour(self.store, hour)

    def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop(), name=f"daegam-{self.lease_name}")

    async def _loop(self) -> None:
        await run_periodic(
            self.tick,
            interval=self.interval,
            stop=self._stop,
            log=self.log,
            label=self.label,
            delay_first=True,
        )

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None
        await self.lease.release(self.lease_name)
