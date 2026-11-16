"""리스 시계와 늘리기 기록. 시간 묶음 롤업의 집계가 리스 수명보다 길 때를 흉내 낸다."""

from __future__ import annotations

import asyncio
from datetime import datetime

from daegam.services import periodic
from daegam.store import leases as leases_mod
from daegam.store.base import utcnow
from daegam.store.leases import Lease


def lease_clock(monkeypatch) -> tuple[list[datetime], list[tuple[datetime, bool]]]:
    """리스의 시계를 바꾸고, 늘리기가 끝난 시각과 결과를 적는다. 하트비트 간격은 가장 짧게 둔다."""
    monkeypatch.setattr(periodic, "RENEW_EVERY_SHARE", 0.0)
    clock = [utcnow()]
    monkeypatch.setattr(leases_mod, "utcnow", lambda: clock[0])
    renewals: list[tuple[datetime, bool]] = []
    real_renew = Lease.renew

    async def renew(self, name, now=None):
        at = clock[0]
        ok = await real_renew(self, name, now)
        renewals.append((at, ok))
        return ok

    monkeypatch.setattr(Lease, "renew", renew)
    return clock, renewals


async def renewal(renewals: list[tuple[datetime, bool]], ok: bool, since: datetime) -> None:
    """since 뒤의 시각으로 ok 인 늘리기가 끝날 때까지 기다린다."""

    async def landed() -> None:
        while not any(at >= since and done is ok for at, done in renewals):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(landed(), 10)


def slow_first_hour(monkeypatch, store_cls: type, step) -> None:
    """store_cls 의 첫 집계(`hour_buckets`) 뒤에 step(store) 를 한 번 돌린다. 집계가 오래 걸리는
    동안의 일을 흉내 낸다."""
    real = store_cls.hour_buckets
    done: list[bool] = []

    async def slow(self, hour):
        out = await real(self, hour)
        if not done:
            done.append(True)
            await step(self)
        return out

    monkeypatch.setattr(store_cls, "hour_buckets", slow)
