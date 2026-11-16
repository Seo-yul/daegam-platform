"""스케줄의 시계 (P41 3절).

cron 규칙은 cronsim 이 지역 시각(naive)으로 늘어놓고, 그 시각을 UTC 로 옮기는 규칙은 여기 둔다.
F0a(docs/archive/reviews/2026-10-29-p41-f0a.md)에서 어느 라이브러리도 혼자서는 시간대 규칙을 맞추지
못했다. 규칙은 셋이다.

- 두 번 오는 지역 시각(서머타임이 끝날 때)은 첫 번째(fold=0)에만 울린다.
- 없는 지역 시각(서머타임이 시작할 때)은 그 뒤 처음 존재하는 분에 울린다.
- 그렇게 옮겨 같은 UTC 로 모인 시각은 한 번만 울린다.

연속한 두 울림이 1년 안에 5분보다 가까운 규칙은 저장하지 않는다(`schedule_too_frequent`). 1년을
걸어 보는 검사라 저장 경로는 스레드에서 부른다.

시각은 저장 형식과 같은 naive UTC 로 받고 낸다(`store/base.py` `naive_utc`). aware 값도 받는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import cronsim

from daegam.store.base import naive_utc

# 두 울림 사이의 바닥 (P41 목표 5). 발사 워커도 마지막 울림에서 이만큼 지나기 전에는 울리지 않는다
MIN_GAP = timedelta(minutes=5)
# 간격 검사가 내다보는 기간
GAP_HORIZON = timedelta(days=366)
# 없는 시각에서 다음 유효한 분을 찾는 한도. 전환은 길어야 몇 시간이다
_MAX_GAP_MINUTES = 24 * 60


class CronError(ValueError):
    """읽을 수 없는 cron 규칙. 다섯 칸이 아니거나 cronsim 이 받지 않는 값이다."""


def parse_rule(expr: str) -> str:
    """다섯 칸 규칙을 공백 하나로 맞춰 돌려준다. 읽을 수 없으면 CronError 다."""
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("a schedule takes a five-field cron rule")
    rule = " ".join(fields)
    try:
        cronsim.CronSim(rule, datetime(2000, 1, 1))
    except cronsim.CronSimError as e:
        raise CronError(str(e)) from e
    return rule


def to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    """지역 시각(naive)을 naive UTC 로 옮긴다. 두 번 오는 시각은 첫 번째로, 없는 시각은 그 뒤
    처음 존재하는 분으로 간다."""
    probe = local
    for _ in range(_MAX_GAP_MINUTES + 1):
        utc = naive_utc(probe.replace(tzinfo=zone, fold=0))
        if utc.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None) == probe:
            return utc
        probe += timedelta(minutes=1)
    raise CronError(f"no valid minute after {local.isoformat()} in {zone.key}")


def _firings(rule: str, zone: ZoneInfo, after: datetime) -> Iterator[datetime]:
    """after 뒤의 울림을 naive UTC 로 차례대로 낸다. 같은 UTC 는 한 번만 낸다.

    after 의 지역 시각부터 늘어놓으면 된다. 그보다 이른 지역 시각은 없는 시각이라도 after 이전으로
    옮겨 가기 때문이다."""
    after = naive_utc(after)
    start = after.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
    last: datetime | None = None
    for local in cronsim.CronSim(rule, start):
        at = to_utc(local, zone)
        if at <= after or (last is not None and at <= last):
            continue
        last = at
        yield at


def next_fire(rule: str, zone: ZoneInfo, after: datetime) -> datetime:
    """after 뒤 첫 울림의 naive UTC 시각."""
    return next(_firings(rule, zone, after))


def latest_fire(rule: str, zone: ZoneInfo, since: datetime, until: datetime) -> datetime | None:
    """since 뒤, until 이전(포함)의 마지막 울림. 없으면 None 이다. 발사 워커가 늦은 울림 가운데
    아직 칠 수 있는 것을 찾을 때 쓰고, 부르는 쪽이 since 를 한 시간 안으로 잡는다."""
    until = naive_utc(until)
    found: datetime | None = None
    for at in _firings(rule, zone, since):
        if at > until:
            break
        found = at
    return found


def too_frequent(rule: str, zone: ZoneInfo, now: datetime) -> bool:
    """1년 안에 연속한 두 울림이 MIN_GAP 보다 가까운지 본다. 처음 나온 짧은 간격에서 멈춘다."""
    end = naive_utc(now) + GAP_HORIZON
    prev: datetime | None = None
    for at in _firings(rule, zone, now):
        if prev is not None and at - prev < MIN_GAP:
            return True
        if at > end:
            return False
        prev = at
    return False


__all__ = [
    "MIN_GAP",
    "CronError",
    "latest_fire",
    "next_fire",
    "parse_rule",
    "to_utc",
    "too_frequent",
]
