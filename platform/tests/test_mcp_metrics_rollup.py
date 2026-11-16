"""Gateway·MCP Server 메트릭의 시간 묶음과 롤업 (P51 3절, D90).

감사 저장소가 늦은 호출 이벤트의 시간을 적는지, 시간 하나의 묶음이 이벤트를 바로 센 것과 같은지,
롤업이 바로 셀 시간·표시·backfill·판·리스를 규칙대로 다루는지 본다.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from pymongo.errors import AutoReconnect

from daegam.audit import store as audit_store
from daegam.audit.mcp_metrics import (
    BUCKET_VERSION,
    MongoMcpMetricsStore,
)
from daegam.audit.store import MongoAuditStore
from daegam.services import periodic
from daegam.services.mcp_metrics import rollup as rollup_mod
from daegam.services.mcp_metrics.rollup import LEASE_NAME, McpMetricsRollup, counted_from
from daegam.store.base import insert_new_docs, utcnow
from daegam.store.hour_marks import hour_of
from daegam.store.leases import LEASES, Lease
from tests.helpers.aio import run_stores
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.leases import lease_clock, renewal, slow_first_hour
from tests.helpers.mcp_metrics import mcp_call_event
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import audit_collector

H = timedelta(hours=1)


def _stores(settings, body):
    """감사·묶음 저장소로 body(db, audit, metrics) 를 한 번 돌린다."""
    return run_stores(
        settings,
        (MongoAuditStore, MongoMcpMetricsStore),
        lambda audit, metrics: body(audit.db, audit, metrics),
    )


# -- 넣기와 표시 ------------------------------------------------------------------------------


def test_the_collector_fallback_marks_late_hours_of_a_failed_batch(
    client, fake_clusters, mongo, monkeypatch
) -> None:
    _, _, _, mp = mcp_server_env(client)
    r = clone_and_deploy(client, mp)
    assert r.status_code == 201, r.text
    fake = fake_clusters.by_name["edge"]
    labels = {
        "toolhive": "true",
        "app.kubernetes.io/name": "mcpserver",
        "toolhive-name": "mcp-calc-1",
    }
    fake.add_pod(DEFAULT_NS, "mcp-calc-1-7f9-abc", labels, uid="uid-1")
    late = utcnow() - 3 * H
    for i in (1, 2):
        line = json.dumps(
            {
                "level": "AUDIT",
                "audit_id": f"f{i}",
                "type": "mcp_tool_call",
                "outcome": "success",
                "logged_at": f"{late.isoformat()}Z",
                "target": {"method": "tools/call", "name": "Add"},
            }
        )
        fake.add_pod_log(DEFAULT_NS, "mcp-calc-1-7f9-abc", line, ts=late)
    real = audit_store.insert_new_docs

    async def insert_then_fail(coll, docs):
        out = await real(coll, docs)
        if len(docs) > 1:
            raise AutoReconnect("connection dropped after the insert")
        return out

    monkeypatch.setattr(audit_store, "insert_new_docs", insert_then_fail)
    col = audit_collector(client)
    result = client.portal.call(col.run_once)
    assert result["errors"] == []
    assert mongo["mcp_audit_events"].count_documents({}) == 2
    assert [m["_id"] for m in mongo["mcp_dirty_hours"].find()] == [hour_of(late)]


# -- 시간 하나의 묶음 ---------------------------------------------------------------------------


# -- 롤업 ---------------------------------------------------------------------------------


def _rollup(settings, body, *, holder: str = "pod-a"):
    async def go(db, audit, metrics):
        roll = McpMetricsRollup(metrics, Lease(db, holder), interval=60)
        return await body(db, audit, metrics, roll)

    return _stores(settings, go)


def _count_recounts(monkeypatch) -> list[datetime]:
    seen: list[datetime] = []
    real = MongoMcpMetricsStore.hour_buckets

    async def spy(self, hour):
        seen.append(hour)
        return await real(self, hour)

    monkeypatch.setattr(MongoMcpMetricsStore, "hour_buckets", spy)
    return seen


def test_an_event_inserted_after_the_aggregation_is_counted_next_tick(
    settings, mongo, monkeypatch
) -> None:
    now = utcnow()
    monkeypatch.setattr(rollup_mod, "BACKFILL_DAYS", 0)
    real = MongoMcpMetricsStore.hour_buckets
    raced: list[bool] = []

    async def racing(self, hour):
        out = await real(self, hour)
        if hour == hour_of(now) and not raced:
            raced.append(True)
            await insert_new_docs(self.events, [mcp_call_event(2, now)])
        return out

    monkeypatch.setattr(MongoMcpMetricsStore, "hour_buckets", racing)

    async def body(db, audit, metrics, roll):
        await audit.add_mcp_events([mcp_call_event(1, now)])
        await roll.tick()
        before = (await db["mcp_hourly"].find_one({"tool": "Add"}))["calls"]
        await roll.tick()
        return before, (await db["mcp_hourly"].find_one({"tool": "Add"}))["calls"]

    assert _rollup(settings, body) == (1, 2)


def test_a_tick_without_budget_still_takes_one_mark_and_one_backfill_step(
    settings, mongo, monkeypatch
) -> None:
    now = utcnow()
    monkeypatch.setattr(rollup_mod, "TICK_BUDGET_SECONDS", 0.0)
    marked = hour_of(now) - 10 * H
    seen = _count_recounts(monkeypatch)

    async def body(db, audit, metrics, roll):
        await metrics.marks.mark(marked)
        await metrics.marks.mark(marked - H)
        await roll.tick()
        return await metrics.marks.batch(10), await metrics.get_state()

    left, state = _rollup(settings, body)
    assert seen[2:] == [marked - H, hour_of(now) - 2 * H]
    assert [m["_id"] for m in left] == [marked]
    assert state["backfill"] == hour_of(now) - 3 * H


def _lease_clock(monkeypatch) -> tuple[list[datetime], list[tuple[datetime, bool]]]:
    monkeypatch.setattr(rollup_mod, "BACKFILL_DAYS", 0)
    return lease_clock(monkeypatch)


def test_an_aggregation_longer_than_the_lease_keeps_the_lease_and_writes(
    settings, mongo, monkeypatch
) -> None:
    """리스는 집계하는 동안에도 늘어난다. 집계 하나가 리스 수명(90초)을 넘겨도 다른 레플리카가
    가져가지 못하고 그 시간이 쓰인다. 리스의 시계를 바꿔 수명이 지난 것을 흉내 낸다."""
    clock, renewals = _lease_clock(monkeypatch)
    taken: list[bool] = []

    async def step(store):
        # 집계에 60초가 걸렸다. 그동안 리스가 한 번은 늘어난다
        clock[0] += timedelta(seconds=60)
        await renewal(renewals, True, clock[0])
        # 집계가 120초째다. 처음 잡은 기한(90초)은 지났으니 다른 레플리카가 리스를 잡으려 한다
        clock[0] += timedelta(seconds=60)
        taken.append(await Lease(store.db, "pod-b").acquire(LEASE_NAME))

    slow_first_hour(monkeypatch, MongoMcpMetricsStore, step)
    now = utcnow()

    async def body(db, audit, metrics, roll):
        # 오래 걸린 첫 집계(앞 시간)의 이벤트다. 그 시간이 쓰였는지를 본다
        await audit.add_mcp_events([mcp_call_event(1, now - H)])
        await roll.tick()
        return await metrics.get_state()

    state = _rollup(settings, body)
    assert taken == [False]
    assert mongo["mcp_hourly"].count_documents({"hour": hour_of(now - H)}) >= 1
    assert "tick_at" in state


def test_a_refused_renewal_during_the_aggregation_writes_nothing(
    settings, mongo, monkeypatch
) -> None:
    """집계하는 동안 다른 레플리카가 리스를 가져가면 하트비트의 늘리기가 거절되고, 그 뒤로는 늘리지
    않으며 아무것도 쓰지 않는다."""
    clock, renewals = _lease_clock(monkeypatch)
    after: list[int] = []

    async def step(store):
        await store.db[LEASES].update_one({"_id": LEASE_NAME}, {"$set": {"holder": "pod-b"}})
        await renewal(renewals, False, clock[0])
        after.append(len(renewals))

    slow_first_hour(monkeypatch, MongoMcpMetricsStore, step)
    now = utcnow()

    async def body(db, audit, metrics, roll):
        await audit.add_mcp_events([mcp_call_event(1, now)])
        await roll.tick()
        return await metrics.get_state()

    state = _rollup(settings, body)
    assert len(renewals) == after[0]
    assert mongo["mcp_hourly"].count_documents({}) == 0
    assert "tick_at" not in state


def test_the_heartbeat_stops_at_its_limit() -> None:
    """하트비트는 한도가 지나면 더 늘리지 않는다. 멈춘 집계가 리스를 끝없이 쥐지 않는다(D79)."""
    calls: list[float] = []

    async def renew() -> bool:
        calls.append(time.monotonic())
        return True

    async def main() -> float:
        start = time.monotonic()
        async with periodic.renewing_every(0.15, renew, limit=0.2):
            await asyncio.sleep(0.6)
        return start

    start = asyncio.run(main())
    assert calls
    assert calls[-1] - start < 0.3


def test_counted_from_follows_the_backfill_cursor() -> None:
    now = utcnow()
    cursor = hour_of(now) - 10 * H
    assert counted_from({"v": BUCKET_VERSION, "backfill": cursor}, now) == cursor + H
    assert counted_from({}, now) == hour_of(now) - H


NEWER = BUCKET_VERSION + 1


@pytest.mark.parametrize(
    "stored, last_seen, runs",
    [
        # 더 새 판이 방금 틱을 마쳤다
        (NEWER, {"rolled_up_at": timedelta(0)}, False),
        # 더 새 판이 하루 넘게 돌지 않았다. 되돌린 배포라 넘겨받는다
        (NEWER, {"rolled_up_at": timedelta(days=1), "taken_at": timedelta(days=1)}, True),
    ],
)
def test_an_alive_newer_version_is_left_alone_and_any_other_is_taken_over(
    settings, mongo, stored, last_seen, runs
) -> None:
    now = utcnow()
    # 표시 없이 들어간 지난 이벤트. 판을 넘겨받은 backfill 만 그 시간을 센다
    past = now - 5 * H

    async def body(db, audit, metrics, roll):
        await insert_new_docs(audit.mcp_events, [mcp_call_event(1, past)])
        seen = {field: now - ago for field, ago in last_seen.items()}
        await metrics.set_state({"v": stored, "backfill": None, **seen})
        await roll.tick()
        lease = await db[LEASES].find_one({"_id": LEASE_NAME})
        return await metrics.get_state(), lease

    state, lease = _rollup(settings, body)
    hours = [b["hour"] for b in mongo["mcp_hourly"].find()]
    if runs:
        assert state["v"] == BUCKET_VERSION and "tick_at" in state
        assert hours == [hour_of(past)]
    else:
        assert state["v"] == stored and "tick_at" not in state
        assert hours == []
        assert lease is None
