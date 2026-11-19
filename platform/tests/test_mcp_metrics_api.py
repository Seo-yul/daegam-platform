"""Gateway·MCP Server 메트릭 API 와 보고 (P51 4절, D90)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from daegam.audit.mcp_metrics import RANGE_FIELDS, MongoMcpMetricsStore
from daegam.k8s.gates import Feature
from daegam.services.data_plane_auth import KIND_GATEWAY, RENDER_EPOCHS, render_epoch
from daegam.services.mcp_metrics.reports import McpMetricsReports, percentile, window_steps
from daegam.services.mcp_metrics.rollup import McpMetricsRollup
from daegam.store.backend_apis import MongoBackendAPIStore
from daegam.store.base import utcnow
from daegam.store.hour_marks import HOUR, hour_of
from daegam.store.leases import Lease
from tests.helpers.aio import run_store
from tests.helpers.consts import MS, T2
from tests.helpers.docs import cluster_doc, gateway_doc
from tests.helpers.gateways import backend_snapshot
from tests.helpers.mcp_metrics import mcp_call_event
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env

# 감사 게이트를 지난 렌더의 번호
APPLIED = render_epoch(KIND_GATEWAY, frozenset(Feature))
# -- API --------------------------------------------------------------------------------------


@pytest.fixture
def server_with_calls(client, fake_clusters, mongo) -> dict[str, Any]:
    """배포한 MCP Server 하나와 지금 시간의 호출 이벤트, 그 시간을 센 묶음."""
    _, _, _, ms = mcp_server_env(client)
    r = clone_and_deploy(client, ms)
    assert r.status_code == 201, r.text
    doc = r.json()
    now = utcnow()
    mongo["mcp_audit_events"].insert_many(
        [
            mcp_call_event(1, now, doc["id"], d=4),
            mcp_call_event(2, now, doc["id"], d=40),
            mcp_call_event(3, now, doc["id"], d=400, outcome="application_error"),
            # 광고하지 않는 이름은 나머지 행으로 간다
            mcp_call_event(4, now, doc["id"], tool="Nope"),
            mcp_call_event(5, now, doc["id"], type="mcp_resource_read", tool=None),
            # vMCP 앞단의 이벤트는 서버의 프록시에도 닿은 호출이라 세지 않는다 (P54 11절)
            mcp_call_event(6, now, doc["id"], proxy_kind="virtualmcpserver", backend="front-x"),
        ]
    )
    store = client.app.state.mcp_metrics_reports.store
    rollup = McpMetricsRollup(store, Lease(store.db, "pod-a"), interval=60)
    client.portal.call(rollup.recount, hour_of(now))
    return doc


def test_another_tenants_server_is_not_found(client, server_with_calls) -> None:
    assert client.get(f"{MS}/{server_with_calls['id']}/metrics", headers=T2).status_code == 404


def test_the_window_and_time_zone_are_checked(client, server_with_calls) -> None:
    url = f"{MS}/{server_with_calls['id']}/metrics"
    week = client.get(url, params={"window": "7d", "tz": "Asia/Seoul"}).json()
    assert week["step"] == "day" and len(week["series"]) == 7
    assert sum(s["calls"] for s in week["series"]) == 4
    assert client.get(url, params={"window": "1h"}).status_code == 422
    assert client.get(url, params={"tz": "Mars/Base"}).status_code == 422


def test_a_call_through_the_exposure_front_counts_once(client, fake_clusters, mongo) -> None:
    """노출한 MCP Server 의 vMCP 앞단을 거친 호출은 앞단과 서버의 프록시에 이벤트를 하나씩
    남기고 둘 다 그 서버의 것이다. 서버의 메트릭은 서버 자신의 프록시만 세어 한 번이다. 묶음은
    종류와 백엔드마다 따로 남아 앞단의 Entry 에도 한 번이다 (P54 11절)."""
    _, _, _, ms = mcp_server_env(client)
    r = clone_and_deploy(client, ms)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    now = utcnow()
    mongo["mcp_audit_events"].insert_many(
        [
            mcp_call_event("front", now, sid, proxy_kind="virtualmcpserver", backend="front-x"),
            mcp_call_event("server", now, sid),
        ]
    )
    store = client.app.state.mcp_metrics_reports.store
    rollup = McpMetricsRollup(store, Lease(store.db, "pod-a"), interval=60)
    client.portal.call(rollup.recount, hour_of(now))
    out = client.get(f"{MS}/{sid}/metrics").json()
    assert out["totals"]["calls"] == 1
    assert [(t["tool"], t["calls"]) for t in out["by_tool"]] == [("Add", 1)]
    assert sum(b["calls"] for b in out["series"]) == 1
    assert "by_backend" not in out
    every = client.portal.call(
        lambda: store.window_sums(sid, hour_of(now), now + HOUR, names=["Add"], day_tz=None)
    )
    assert {b["_id"]: b["calls"] for b in every["backends"]} == {"front-x": 1, None: 1}


# -- 보고 ---------------------------------------------------------------------------------------


class _Clusters:
    """클러스터 저장소 대신. id 하나에 클러스터 하나다."""

    def __init__(self, **by_id: Any):
        self.by_id = by_id

    async def get(self, cluster_id: str):
        return self.by_id.get(cluster_id)


def _reports(settings, clusters: _Clusters, body):
    """묶음·Backend API 저장소로 만든 보고와 롤업으로 body(db, reports, roll) 를 한 번 돌린다."""

    async def go(store: MongoMcpMetricsStore):
        await store.events.delete_many({})
        backend_apis = MongoBackendAPIStore(settings.mongo_url, settings.mongo_db)
        reports = McpMetricsReports(store, clusters, backend_apis, collector_enabled=True)
        roll = McpMetricsRollup(store, Lease(store.db, "pod-a"), interval=60)
        try:
            return await body(store.db, reports, roll)
        finally:
            await backend_apis.close()

    return run_store(settings, MongoMcpMetricsStore, go)


def _gateway(**kw: Any):
    snapshots = [
        backend_snapshot("calc", "internal", ["Add", "Divide"]),
        backend_snapshot("orders", "internal", ["GetOrder"]),
    ]
    kw.setdefault("render_epoch", APPLIED)
    return gateway_doc(state="deployed", cluster="c1", deployed_backends=snapshots, **kw)


def test_gateway_backends_add_up_to_the_totals(settings, mongo) -> None:
    doc = _gateway()
    now = utcnow()
    events = [
        mcp_call_event(1, now, doc.id, proxy_kind="virtualmcpserver", backend="gw-calc"),
        mcp_call_event(
            2, now, doc.id, proxy_kind="virtualmcpserver", backend="gw-orders", tool="GetOrder"
        ),
        # 지금 배포에 없는 Entry 와, vMCP 에 없는 Tool 이라 백엔드가 없는 호출
        mcp_call_event(
            3, now, doc.id, proxy_kind="virtualmcpserver", backend="gw-gone", tool="Old"
        ),
        mcp_call_event(
            4, now, doc.id, proxy_kind="virtualmcpserver", tool="Nope", outcome="application_error"
        ),
    ]
    clusters = _Clusters(c1=cluster_doc(recorded_toolhive_version="0.53.0"))

    async def body(db, reports, roll):
        await db["mcp_audit_events"].insert_many(events)
        await roll.recount(hour_of(now))
        return await reports.gateway(doc, window="24h", tz="UTC")

    out = _reports(settings, clusters, body)
    rows = {r["entry"]: r for r in out["by_backend"]}
    assert set(rows) == {"gw-calc", "gw-orders", "gw-gone", None}
    assert (rows["gw-calc"]["hub_item_id"], rows["gw-calc"]["name"]) == ("hi-calc", "calc")
    assert rows["gw-gone"]["hub_item_id"] is None and rows["gw-gone"]["name"] is None
    assert rows[None]["errors"] == 1
    assert sum(r["calls"] for r in out["by_backend"]) == out["totals"]["calls"] == 4
    assert [t["tool"] for t in out["by_tool"]] == ["Add", "GetOrder", None]
    assert out["source"]["audit"] == "on"


def test_a_previous_release_render_that_missed_audit_is_pending(settings, mongo, monkeypatch):
    """게이트를 빠뜨린 번호는 판이 올라 다시 렌더되기 전에도 그 게이트를 읽는다 (T63 이 Gateway
    판을 올렸다)."""
    monkeypatch.setitem(RENDER_EPOCHS, KIND_GATEWAY, RENDER_EPOCHS[KIND_GATEWAY] - 1)
    previous = render_epoch(KIND_GATEWAY, frozenset())
    monkeypatch.undo()
    doc = _gateway(render_epoch=previous)
    clusters = _Clusters(c1=cluster_doc(recorded_toolhive_version="0.53.0"))

    async def body(db, reports, roll):
        return await reports.gateway(doc, window="24h", tz="UTC")

    assert _reports(settings, clusters, body)["source"]["audit"] == "pending"


def test_days_start_at_local_midnight_and_half_hour_zones_at_the_next_hour() -> None:
    now = datetime(2026, 10, 23, 3, 0)
    since, steps, tz = window_steps("7d", "Asia/Seoul", now)
    assert tz == "Asia/Seoul" and len(steps) == 7
    assert steps[-1] == datetime(2026, 10, 22, 15, 0) and since == steps[0]
    since, steps, _ = window_steps("7d", "Asia/Kolkata", now)
    assert steps[-1] == datetime(2026, 10, 22, 18, 30) and since == steps[0] + timedelta(minutes=30)
    since, steps, tz = window_steps("24h", "Asia/Seoul", now)
    assert tz is None and len(steps) == 24 and since == datetime(2026, 10, 22, 4, 0)


def test_percentiles_interpolate_inside_the_range_that_holds_the_rank() -> None:
    row = {f"duration_{f}": 0 for f in RANGE_FIELDS}
    row.update(duration_count=10, duration_max_ms=900, duration_le_10=5, duration_le_25=5)
    assert percentile(row, 0.5) == 10.0
    assert percentile(row, 0.95) == 23.5
    row.update(duration_le_10=0, duration_le_25=0, duration_gt_300000=10, duration_max_ms=400000)
    assert percentile(row, 0.5) == 350000.0
    assert percentile({**row, "duration_count": 0}, 0.5) is None


@pytest.mark.parametrize("duration, p50, p95", [(0, 0.0, 0.0), (3, 2.5, 3.0)])
def test_percentiles_never_exceed_the_longest_duration(duration, p50, p95) -> None:
    row = {f"duration_{f}": 0 for f in RANGE_FIELDS}
    row.update(duration_count=10, duration_max_ms=duration, duration_le_5=10)
    assert (percentile(row, 0.5), percentile(row, 0.95)) == (p50, p95)


@pytest.mark.parametrize(
    "version, epoch, audit",
    [
        ("0.50.0", APPLIED, "off"),
        ("0.53.0", 0, "pending"),
    ],
)
def test_gateway_audit_reads_the_gate_and_the_applied_render(
    settings, mongo, version, epoch, audit
) -> None:
    doc = _gateway(render_epoch=epoch)
    clusters = _Clusters(c1=cluster_doc(recorded_toolhive_version=version))

    async def body(db, reports, roll):
        return await reports.gateway(doc, window="24h", tz="UTC")

    assert _reports(settings, clusters, body)["source"]["audit"] == audit
