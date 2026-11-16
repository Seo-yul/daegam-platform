"""시간 묶음 롤업 (P37 10절, D62). 한 레플리카만 도는 루프다(리스 `usage-rollup`).

`DAEGAM_USAGE_ROLLUP_SECONDS`(120초)마다 앞 틱의 앞 시간부터 지금 시간까지를 원장에서 다시 세고,
30초 안에서 다시 셀 시간(`usage_dirty_hours`)과 다시 계산(reprice)의 커서를 진행한다. 루프가 멈췄다
돌아오면 그 사이의 시간도 다시 셀 시간으로 적어 따라잡는다. 시간 하나를 세는 것은 그 시간의 행에
대한 `$group` 하나와 묶음을 결과로 바꾸는 쓰기 하나라, 같은 입력이면 몇 번을 돌아도 같다. 틱 끝에
원장 건강의 수를 `usage_state` 에 적는다. 틱은 단계 사이와 반복의 항목마다 리스를 늘리고, 잃으면
그 자리에서 멈춘다(P67 의 `TickHold`). 시간 하나를 세는 동안에도 리스를 늘리고, 쓰기 전에 리스를
본다(`recount_hour`).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from daegam.services.claims import TickHold, tick_item
from daegam.services.hour_rollup import (
    LeasedLoop,
    drain_dirty,
    queue_hours,
    retention_floor,
)
from daegam.services.usage.pricing import PriceBook
from daegam.store.base import new_id, utcnow
from daegam.store.hour_marks import hour_of
from daegam.store.leases import Lease
from daegam.store.pricing import PricingTable
from daegam.store.usage import ANOMALY_KINDS, MongoUsageStore, UsageRecord

logger = logging.getLogger("daegam.usage.rollup")

LEASE_NAME = "usage-rollup"
# 틱 하나가 다시 셀 시간과 다시 계산에 쓰는 초
TICK_BUDGET_SECONDS = 30.0
# 다시 계산이 한 번에 읽는 행 수와 한 요청의 기간 상한
REPRICE_BATCH = 5000
MAX_REPRICE_DAYS = 92
# 게이트웨이 기록을 기다리는 시간. 이보다 오래 오지 않은 잇기 문서가 `gateway_missing` 이다
GATEWAY_WAIT = timedelta(hours=1)
# 원장 건강 사건을 세는 기간. 사건 문서의 TTL 과 같다. TTL 지우기는 늦게 돌므로 세는 쪽이 같은
# 기간으로 거른다
HEALTH_PERIOD = timedelta(days=30)


class RepriceError(ValueError):
    """다시 계산 요청의 기간이 틀렸다."""


class RepriceRunning(RuntimeError):
    """끝나지 않은 다시 계산이 있다."""


class UsageRollup(LeasedLoop):
    lease_name = LEASE_NAME
    label = "usage rollup"
    log = logger

    def __init__(self, store: MongoUsageStore, prices: PriceBook, lease: Lease, *, interval: float):
        super().__init__(lease, interval=interval)
        self.store = store
        self.prices = prices

    # -- 틱 ---------------------------------------------------------------------------------

    async def _run(self, now: datetime, hold: TickHold) -> None:
        deadline = time.monotonic() + TICK_BUDGET_SECONDS
        floor = retention_floor(now, self.store.retention)
        state = await self.store.get_state()
        for hour in await queue_hours(self.store.marks, state.get("tick_at"), now, floor):
            await hold.item()
            await self.recount(hour)
        await drain_dirty(self.store.marks, self.recount, deadline, floor=floor)
        await self._reprice(deadline)
        await hold.item()
        await self.store.set_state({"tick_at": now, "health": await self.health(now)})

    async def health(self, now: datetime) -> dict[str, int]:
        """0 이어야 하는 원장 건강의 수 (10절). `duplicates` 는 보통의 흐름이라 세지 않는다."""
        counts = await self.store.gateway_missing(now - GATEWAY_WAIT)
        anomalies = await self.store.anomaly_counts(now - HEALTH_PERIOD)
        for kind in ANOMALY_KINDS:
            counts[kind] = anomalies.get(kind, 0)
        return counts

    # -- 다시 계산 ----------------------------------------------------------------------------

    async def request_reprice(self, since: datetime, until: datetime) -> dict[str, Any]:
        """기간의 행을 지금 가격표로 다시 매기는 요청. 요청마다 id 가 있고 커서는 `usage_state` 에
        남아 롤업 틱이 이어 가며, 죽은 파드의 일은 다음 파드가 커서에서 잇는다. 끝나지 않은 요청이
        있으면 `RepriceRunning` 이다."""
        if until <= since:
            raise RepriceError("until must be after since")
        if until - since > timedelta(days=MAX_REPRICE_DAYS):
            raise RepriceError(f"at most {MAX_REPRICE_DAYS} days")
        reprice = {
            "id": new_id(),
            "since": since,
            "until": until,
            "cursor": None,
            "done": 0,
            "total": await self.store.count_rows(since, until),
            "started_at": utcnow(),
            "finished_at": None,
        }
        if not await self.store.start_reprice(reprice):
            raise RepriceRunning("a reprice is still running")
        return reprice_status(reprice)

    async def reprice_status(self) -> dict[str, Any] | None:
        reprice = (await self.store.get_state()).get("reprice")
        return None if reprice is None else reprice_status(reprice)

    async def _reprice(self, deadline: float) -> None:
        """다시 계산을 한 묶음씩 진행하고 묶음마다 진행을 한 번 적는다. 그사이 다른 요청으로
        바뀌었으면 멈춘다."""
        reprice = (await self.store.get_state()).get("reprice")
        if reprice is None or reprice.get("finished_at") is not None:
            return
        table = await self.prices.read()
        while time.monotonic() < deadline:
            await tick_item()
            cursor = reprice.get("cursor")
            after = (cursor["ts"], cursor["id"]) if cursor else None
            rows = await self.store.rows_after(
                reprice["since"], reprice["until"], after, REPRICE_BATCH
            )
            if rows:
                await self._reprice_rows(rows, table)
                reprice["cursor"] = {"ts": rows[-1].ts, "id": rows[-1].id}
                reprice["done"] = int(reprice.get("done") or 0) + len(rows)
            else:
                reprice["finished_at"] = utcnow()
            if not await self.store.save_reprice(reprice) or not rows:
                return

    async def _reprice_rows(self, rows: list[UsageRecord], table: PricingTable) -> None:
        hours = set()
        for row in rows:
            cost, price = await self.prices.price(
                kind=row.provider.kind,
                provider_name=row.provider.name,
                model=row.model,
                at=row.ts,
                usage=row.usage,
                operation=row.operation,
                table=table,
            )
            await self.store.set_price(row.id, cost, price)
            hours.add(hour_of(row.ts))
        for hour in hours:
            await self.store.marks.mark(hour)


def reprice_status(reprice: dict[str, Any]) -> dict[str, Any]:
    """다시 계산의 진행. 남은 시간은 지금까지의 속도로 어림한다."""
    done = int(reprice.get("done") or 0)
    total = int(reprice.get("total") or 0)
    left = max(0, total - done)
    started = reprice.get("started_at")
    finished = reprice.get("finished_at")
    eta: float | None = None
    if finished is None and started is not None and done > 0:
        elapsed = (utcnow() - started).total_seconds()
        eta = left * elapsed / done
    return {
        "since": reprice["since"],
        "until": reprice["until"],
        "done": done,
        "left": 0 if finished is not None else left,
        "started_at": started,
        "finished_at": finished,
        "eta_seconds": 0.0 if finished is not None else eta,
    }
