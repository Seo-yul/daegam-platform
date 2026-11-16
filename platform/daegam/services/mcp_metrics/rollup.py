"""MCP 메트릭 롤업 (P51 3절, D90). 한 레플리카만 도는 루프다(리스 `mcp-metrics-rollup`).

`DAEGAM_MCP_METRICS_ROLLUP_SECONDS`(120초)마다 앞 틱의 앞 시간부터 지금 시간까지를 감사 이벤트에서
다시 센다. 지금과 앞 시간은 바로 세되, 그 시간의 호출 이벤트 수가 지난번 셀 때와 같으면 건너뛴다.
수는 집계 전에 센다. 그래서 집계와 수 사이에 들어온 이벤트는 다음 틱의 수를 바꿔 다시 세어진다.
그 다음 20초 안에서 다시 셀 시간의 표시를 처리하고 남은 시간에 backfill 을 한 걸음씩 나아간다.
예산이 다해도 틱마다 표시 하나와 backfill 한 걸음은 한다.

수집기의 틱 안에서 돌지 않는다. 수집기는 파드 로그를 읽는 데 묶여 있고, 긴 집계가 그 사이에 끼면
로그 회전으로 잃는 줄이 늘어난다. 리스가 따로라 둘은 다른 레플리카에서 돌 수 있다.

묶음 판(`BUCKET_VERSION`)이 상태와 다르면 이 레플리카가 넘겨받아 판을 적고 backfill 을 처음부터
다시 한다. 다만 상태가 더 새 판이고 그 판이 최근에 넘겨받았거나 틱을 마쳤으면(주기의 세 배 안)
틱을 건너뛰고 리스를 놓는다. 롤링 배포 중에 두 판이 서로의 backfill 과 묶음을 되돌리지 않게 하기
위해서다. 새 판이 오래 돌지 않았으면 되돌린 배포라 보고 넘겨받는다. backfill 은 두 시간 전부터 한
시간씩 30일 전까지 거꾸로 간다. 그래서 오늘의 늦은 시간이 지난 기록 뒤에서 기다리지 않는다.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from daegam.audit.mcp_metrics import BUCKET_VERSION, MongoMcpMetricsStore
from daegam.services.claims import TickHold
from daegam.services.hour_rollup import (
    DIRECT_HOURS,
    LeasedLoop,
    drain_dirty,
    queue_hours,
    retention_floor,
)
from daegam.store.base import utcnow
from daegam.store.hour_marks import HOUR, hour_of
from daegam.store.leases import Lease

logger = logging.getLogger("daegam.mcp_metrics.rollup")

LEASE_NAME = "mcp-metrics-rollup"
# 틱 하나가 표시와 backfill 에 쓰는 초
TICK_BUDGET_SECONDS = 20.0
# backfill 이 거슬러 가는 날 수. 화면이 읽는 가장 긴 창이다
BACKFILL_DAYS = 30
# 더 새 판이 이 주기 수 안에 틱을 마쳤으면 살아 있는 것으로 본다
ALIVE_INTERVALS = 3


class McpMetricsRollup(LeasedLoop):
    lease_name = LEASE_NAME
    label = "mcp metrics rollup"
    log = logger

    def __init__(self, store: MongoMcpMetricsStore, lease: Lease, *, interval: float):
        super().__init__(lease, interval=interval)
        self.store = store

    async def _run(self, now: datetime, hold: TickHold) -> None:
        state = await self.store.get_state()
        if self._newer_version_alive(state, now):
            await self.lease.release(LEASE_NAME)
            return
        if state.get("v") != BUCKET_VERSION:
            restart = {
                "v": BUCKET_VERSION,
                "taken_at": now,
                "backfill": hour_of(now) - DIRECT_HOURS * HOUR,
                "counts": {},
            }
            await self.store.set_state(restart)
            state = {**state, **restart}
        deadline = time.monotonic() + TICK_BUDGET_SECONDS
        floor = retention_floor(now, self.store.retention)
        hours = await queue_hours(self.store.marks, state.get("tick_at"), now, floor)
        counts = await self._direct_hours(hours, state.get("counts") or {}, hold)
        await drain_dirty(self.store.marks, self.recount, deadline, floor=floor, at_least=1)
        backfill = await self._backfill(state.get("backfill"), now, floor, deadline)
        await hold.item()
        await self.store.set_state(
            {"tick_at": now, "rolled_up_at": utcnow(), "counts": counts, "backfill": backfill}
        )

    def _newer_version_alive(self, state: dict[str, Any], now: datetime) -> bool:
        """더 새 판의 레플리카가 지금도 묶음을 짓고 있는가. 그 판이 넘겨받은 때나 마지막으로 틱을
        마친 때가 오래됐으면 되돌린 배포다."""
        version = state.get("v")
        if version is None or version <= BUCKET_VERSION:
            return False
        seen = [t for t in (state.get("taken_at"), state.get("rolled_up_at")) if t is not None]
        alive = timedelta(seconds=ALIVE_INTERVALS * self.interval)
        return bool(seen) and now - max(seen) < alive

    async def _direct_hours(
        self, hours: list[datetime], kept: dict[str, int], hold: TickHold
    ) -> dict[str, int]:
        """지금과 앞 시간을 센다. 호출 이벤트 수가 지난번과 같은 시간은 건너뛴다. 이번 수를
        돌려준다."""
        counts: dict[str, int] = {}
        for hour in hours:
            await hold.item()
            key = hour.isoformat()
            counts[key] = await self.store.count_calls(hour)
            if kept.get(key) != counts[key]:
                await self.recount(hour)
        return counts

    async def _backfill(
        self, cursor: datetime | None, now: datetime, floor: datetime, deadline: float
    ) -> datetime | None:
        """backfill 을 예산 안에서 나아간다. 적어도 한 걸음은 간다. 다음 커서를 돌려주고, 30일
        전이나 보관 기간의 경계에 닿았으면 None 이다."""
        if cursor is None:
            return None
        low = max(hour_of(now - timedelta(days=BACKFILL_DAYS)), floor)
        steps = 0
        while cursor >= low and (steps == 0 or time.monotonic() < deadline):
            await self.recount(cursor)
            cursor -= HOUR
            steps += 1
        return cursor if cursor >= low else None


def counted_from(state: dict[str, Any], now: datetime) -> datetime | None:
    """지금 판이 센 가장 오래된 시간. backfill 이 끝났으면 None 이다. 판을 넘겨받기 전이면
    지금과 앞 시간만 센 것으로 본다."""
    if state.get("v") != BUCKET_VERSION:
        return hour_of(now) - HOUR
    cursor = state.get("backfill")
    return None if cursor is None else cursor + HOUR
