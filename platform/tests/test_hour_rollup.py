"""시간 묶음 롤업이 함께 쓰는 시간 고르기와 표시 처리 (P37 10절, P51 3절)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from daegam.services.hour_rollup import drain_dirty, queue_hours, recount_hours, retention_floor
from daegam.store.hour_marks import HourMarks, hour_of
from tests.helpers.aio import run_db

NOW = datetime(2026, 10, 23, 10, 30)
H = timedelta(hours=1)
FAR_FLOOR = datetime(2025, 1, 1)


def test_without_a_previous_tick_the_current_and_previous_hour_are_recounted() -> None:
    assert recount_hours(None, NOW, FAR_FLOOR) == [hour_of(NOW) - H, hour_of(NOW)]


def test_a_gap_since_the_previous_tick_is_caught_up_from_its_previous_hour() -> None:
    # 앞 틱이 H+1 의 끝에 돌았으면, 그 뒤 H+1:59:40 에 들어온 H 의 이벤트도 다음 틱의 범위에 든다
    hours = recount_hours(NOW - 5 * H, NOW, FAR_FLOOR)
    assert hours[0] == hour_of(NOW) - 6 * H
    assert hours[-1] == hour_of(NOW)
    assert len(hours) == 7


def test_hours_older_than_the_floor_are_never_recounted() -> None:
    floor = hour_of(NOW) - 3 * H
    hours = recount_hours(NOW - 10 * H, NOW, floor)
    assert hours[0] == floor


def test_the_floor_is_the_first_hour_wholly_inside_the_retention() -> None:
    assert retention_floor(NOW, timedelta(days=1)) == hour_of(NOW - timedelta(days=1)) + H


def test_older_hours_of_a_gap_are_marked_and_the_direct_ones_returned(settings) -> None:
    async def body(marks: HourMarks):
        direct = await queue_hours(marks, NOW - 4 * H, NOW, FAR_FLOOR)
        return direct, [m["_id"] for m in await marks.batch(10)]

    direct, marked = _drain(settings, body)
    assert direct == [hour_of(NOW) - H, hour_of(NOW)]
    assert marked == [hour_of(NOW) - i * H for i in (5, 4, 3, 2)]


def _drain(settings, body):
    async def go(db):
        marks = HourMarks(db["test_hour_marks"])
        await db["test_hour_marks"].delete_many({})
        return await body(marks)

    return run_db(settings, go)


def test_marks_below_the_floor_are_dropped_without_a_recount(settings) -> None:
    floor = hour_of(NOW) - 48 * H
    old, late = floor - 2 * H, hour_of(NOW) - 5 * H
    counted: list[datetime] = []

    async def body(marks: HourMarks):
        for ts in (old, late):
            await marks.mark(ts)

        async def recount(hour: datetime) -> None:
            counted.append(hour)

        done = await drain_dirty(marks, recount, time.monotonic() + 10, floor=floor)
        return done, await marks.batch(5)

    done, left = _drain(settings, body)
    assert counted == [late]
    assert done == 2
    assert left == []


def test_a_mark_set_again_during_its_recount_is_counted_again(settings) -> None:
    hour = hour_of(NOW) - 4 * H
    counted: list[datetime] = []

    async def body(marks: HourMarks):
        await marks.mark(hour)

        async def recount(h: datetime) -> None:
            counted.append(h)
            if len(counted) == 1:
                # 다시 세는 동안 늦은 이벤트가 또 들어왔다. marked_at 은 밀리초까지라 조금 뒤에
                # 적는다
                await asyncio.sleep(0.01)
                await marks.mark(h)

        await drain_dirty(marks, recount, time.monotonic() + 10, floor=FAR_FLOOR)
        return await marks.batch(5)

    left = _drain(settings, body)
    assert counted == [hour, hour]
    assert left == []
