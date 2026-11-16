"""스케줄 시계의 시간대 규칙 (P41 3절, F0a). DB 가 필요 없다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daegam.services.runs.clock import (
    CronError,
    latest_fire,
    next_fire,
    parse_rule,
    to_utc,
    too_frequent,
)

NY = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")


def u(*args: int) -> datetime:
    """naive UTC. 저장 형식과 같다."""
    return datetime(*args)


def test_a_skipped_local_time_fires_at_the_next_valid_minute():
    # 2026-03-08 02:30 은 뉴욕에 없다(02:00 → 03:00). 03:00 EDT = 07:00 UTC 에 울린다
    assert next_fire("30 2 * * *", NY, u(2026, 3, 8, 5, 0)) == u(2026, 3, 8, 7, 0)


def test_a_repeated_local_time_fires_only_at_its_first_occurrence():
    # 2026-11-01 01:30 은 두 번 온다. 첫 번째(EDT, 05:30 UTC)에만 울리고 다음은 이튿날이다
    first = next_fire("30 1 * * *", NY, u(2026, 11, 1, 4, 0))
    assert first == u(2026, 11, 1, 5, 30)
    assert next_fire("30 1 * * *", NY, first) == u(2026, 11, 2, 6, 30)


def test_skipped_minutes_that_land_on_one_utc_time_fire_once():
    # 02:00·02:15·02:30·02:45 이 모두 03:00 EDT 로 옮겨 가도 한 번만 울린다
    after = u(2026, 3, 8, 6, 59)
    first = next_fire("*/15 2-3 * * *", NY, after)
    assert first == u(2026, 3, 8, 7, 0)
    assert next_fire("*/15 2-3 * * *", NY, first) == u(2026, 3, 8, 7, 15)


def test_a_zone_without_daylight_saving_keeps_wall_clock_times():
    assert next_fire("0 9 * * 1-5", SEOUL, u(2026, 10, 30, 1, 0)) == u(
        2026, 10, 30, 0, 0
    ) + timedelta(days=3)


def test_an_aware_start_gives_the_same_naive_utc_firing():
    # 2026-03-08 14:00 +09:00 = 05:00 UTC
    aware = datetime(2026, 3, 8, 14, 0, tzinfo=timezone(timedelta(hours=9)))
    assert next_fire("30 2 * * *", NY, aware) == u(2026, 3, 8, 7, 0)


def test_a_rule_too_frequent_only_across_a_transition_months_ahead_is_refused():
    # 01:57 과 03:00 은 평소 63분 떨어져 있지만 3월 8일 봄 전환에는 3분이다
    assert too_frequent("57,0 1,3 * * *", NY, u(2026, 1, 1))


def test_the_latest_firing_inside_a_window_is_found():
    assert latest_fire("0 * * * *", NY, u(2026, 10, 29, 9, 10), u(2026, 10, 29, 10, 10)) == u(
        2026, 10, 29, 10, 0
    )
    assert latest_fire("0 9 * * *", SEOUL, u(2026, 10, 29, 1, 0), u(2026, 10, 29, 2, 0)) is None


def test_to_utc_of_a_normal_time_is_its_offset():
    assert to_utc(datetime(2026, 7, 1, 12, 0), SEOUL) == u(2026, 7, 1, 3, 0)


def test_every_five_minutes_is_allowed_and_four_is_too_frequent():
    now = u(2026, 1, 1)
    assert not too_frequent("*/5 * * * *", NY, now)
    assert too_frequent("*/4 * * * *", NY, now)
    assert too_frequent("0,3 9 * * *", SEOUL, now)


def test_a_daily_rule_is_not_too_frequent_across_daylight_saving():
    assert not too_frequent("30 1 * * *", NY, u(2026, 1, 1))


@pytest.mark.parametrize("rule", ["* * * *", "* * * * * *", "61 * * * *", "* * * * mon-xyz", ""])
def test_unreadable_rules_are_refused(rule):
    with pytest.raises(CronError):
        parse_rule(rule)


def test_a_rule_is_normalised_to_single_spaces():
    assert parse_rule("  0   9 * *  1-5 ") == "0 9 * * 1-5"
