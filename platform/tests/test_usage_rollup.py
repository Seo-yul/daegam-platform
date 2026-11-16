"""시간 묶음 롤업과 사용량 보고 (P37 10절·13절): 다시 세기, 다시 셀 시간, 다시 계산, 보고, 권한."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from pymongo.errors import AutoReconnect, PyMongoError

from daegam.services import claims, hour_rollup
from daegam.services.usage.rollup import LEASE_NAME, RepriceRunning
from daegam.store.base import utcnow
from daegam.store.hour_marks import hour_of
from daegam.store.leases import LEASE_TTL, LEASES, Lease
from daegam.store.usage import MongoUsageStore, TokenUsage, UsageRecord, UsageSource
from tests.helpers.auth import bearer
from tests.helpers.http import detail_code
from tests.helpers.leases import lease_clock, renewal, slow_first_hour
from tests.helpers.usage import run_ledger, usage_row

HOUR = hour_of(utcnow())


def _row(call_id: str, **kw) -> UsageRecord:
    """이 시간의 행. 입력 백만 토큰이라 gpt-4o 기본 가격으로 2.5 달러다."""
    ts = kw.pop("ts", HOUR + timedelta(minutes=5))
    return usage_row(call_id, ts=ts, usage=TokenUsage(input_tokens=1_000_000), **kw)


def test_a_running_reprice_is_not_overwritten(settings) -> None:
    """끝나지 않은 다시 계산이 있으면 새 요청은 거절되고, 바뀐 요청의 진행은 옛 틱이 덮지 못한다."""
    since, until = HOUR - timedelta(days=1), HOUR + timedelta(days=1)

    async def body(ledger, rollup, store):
        await ledger.write(_row("c1"))
        await rollup.request_reprice(since, until)
        try:
            await rollup.request_reprice(since, until)
        except RepriceRunning:
            refused = True
        else:
            refused = False
        stale = {**(await store.get_state())["reprice"], "id": "older", "done": 99}
        overwritten = await store.save_reprice(stale)
        await rollup.tick()
        again = await rollup.request_reprice(since, until)
        return refused, overwritten, again

    refused, overwritten, again = run_ledger(settings, body)
    assert refused and not overwritten and again["done"] == 0


def _previous_hour() -> datetime:
    """틱이 처음 세는 앞 시간. 테스트가 도는 때에 정해야 시간의 경계를 넘어도 맞다."""
    return hour_of(utcnow()) - timedelta(hours=1)


async def _take_lease(store) -> None:
    """다른 레플리카가 리스를 잡았다."""
    await store.db[LEASES].update_one({"_id": LEASE_NAME}, {"$set": {"holder": "pod-b"}})


def test_a_failing_renewal_older_than_the_lease_writes_nothing(
    settings, mongo, monkeypatch
) -> None:
    """늘리기가 오류로만 실패해 리스 수명 동안 한 번도 늘지 않았으면 쓰지 않는다. 그 사이 리스가
    끝나 다른 레플리카가 잡았을 수 있다(`tick_fence`)."""

    async def step(store):
        await _take_lease(store)

        async def failing(self, name, now=None):
            raise AutoReconnect("lease write failed")

        monkeypatch.setattr(Lease, "renew", failing)
        # 리스 수명보다 오래 늘리기가 실패했다
        claims._tick.get().last -= LEASE_TTL.total_seconds() + 10

    slow_first_hour(monkeypatch, MongoUsageStore, step)
    prev = _previous_hour()

    async def body(ledger, rollup, store):
        await ledger.write(_row("c1", ts=prev + timedelta(minutes=5)))
        await rollup.tick()
        return await store.get_state()

    state = run_ledger(settings, body)
    assert mongo["usage_hourly"].count_documents({}) == 0
    assert "tick_at" not in state


def test_an_aggregation_longer_than_the_lease_keeps_the_lease_and_writes(
    settings, mongo, monkeypatch
) -> None:
    """리스는 집계하는 동안에도 늘어난다. 집계 하나가 리스 수명(90초)을 넘겨도 다른 레플리카가
    가져가지 못하고 그 시간이 쓰인다. 리스의 시계를 바꿔 수명이 지난 것을 흉내 낸다."""
    clock, renewals = lease_clock(monkeypatch)
    taken: list[bool] = []

    async def step(store):
        # 집계에 60초가 걸렸다. 그동안 리스가 한 번은 늘어난다
        clock[0] += timedelta(seconds=60)
        await renewal(renewals, True, clock[0])
        # 집계가 120초째다. 처음 잡은 기한(90초)은 지났으니 다른 레플리카가 리스를 잡으려 한다
        clock[0] += timedelta(seconds=60)
        taken.append(await Lease(store.db, "pod-b").acquire(LEASE_NAME))

    slow_first_hour(monkeypatch, MongoUsageStore, step)
    prev = _previous_hour()

    async def body(ledger, rollup, store):
        await ledger.write(_row("c1", ts=prev + timedelta(minutes=5)))
        await rollup.tick()
        return await store.get_state()

    state = run_ledger(settings, body)
    assert taken == [False]
    assert mongo["usage_hourly"].count_documents({"hour": prev}) == 1
    assert "tick_at" in state


def test_an_aggregation_past_its_limit_fails_and_stops_renewing(
    settings, mongo, monkeypatch
) -> None:
    """집계가 한도(`AGGREGATION_LIMIT_SECONDS`)를 넘으면 실패하고, 하트비트는 한도에서 멈춰 리스가
    수명이 다하면 풀린다. 멈춘 집계가 리스를 끝없이 쥐지 않는다(D79)."""
    limit = 0.3
    monkeypatch.setattr(hour_rollup, "AGGREGATION_LIMIT_SECONDS", limit)
    _clock, renewals = lease_clock(monkeypatch)
    real = MongoUsageStore.hour_buckets
    seen: list[int] = []

    async def hung(self, hour):
        await asyncio.sleep(2 * limit)
        seen.append(len(renewals))
        await asyncio.sleep(limit)
        seen.append(len(renewals))
        return await real(self, hour)

    monkeypatch.setattr(MongoUsageStore, "hour_buckets", hung)
    prev = _previous_hour()

    async def body(ledger, rollup, store):
        await ledger.write(_row("c1", ts=prev + timedelta(minutes=5)))
        with pytest.raises(PyMongoError) as err:
            await rollup.tick()
        return err.value

    err = run_ledger(settings, body)
    assert err.timeout
    assert seen[0] == seen[1] > 0
    assert mongo["usage_hourly"].count_documents({}) == 0


def _seed(client, *rows: UsageRecord) -> None:
    async def go(ledger, rollup, _store):
        for r in rows:
            await ledger.write(r)
        await rollup.tick()

    run_ledger(client.app.state.settings, go)


def test_tenant_usage_groups_and_series(client, mongo) -> None:
    tenant = "default"
    _seed(
        client,
        *(
            _row(
                f"a{i}", tenant=tenant, source=UsageSource(kind="agent", id=f"ag{i}", name=f"n{i}")
            )
            for i in range(7)
        ),
        _row("bot", tenant=tenant, source=UsageSource(kind="assistant", id="daegam")),
        _row("other", tenant="other"),
    )
    body = client.get("/usage", params={"group": "agent"}).json()
    # 줄은 Agent 의 호출만이고 합계는 기준과 상관없이 테넌트 전체다
    assert body["totals"]["calls"] == 8 and body["totals"]["cost_usd"] == pytest.approx(20)
    assert len(body["rows"]) == 7 and {r["name"] for r in body["rows"]} == {
        f"n{i}" for i in range(7)
    }
    assert body["truncated"] is False
    (day,) = body["series"]
    assert len(day["items"]) == 6 and day["items"][-1]["key"] is None  # 다섯과 other
    by_day = client.get("/usage", params={"group": "day", "tz": "Asia/Seoul"}).json()
    assert len(by_day["rows"]) == 1 and by_day["rows"][0]["calls"] == 8
    # 테넌트 화면에는 테넌트 기준이 없다(라우트의 타입이 거른다)
    assert client.get("/usage", params={"group": "tenant"}).status_code == 422
    assert detail_code(client.get("/usage", params={"tz": "Mars/Base"})) == (422, "invalid_value")


def test_platform_usage_adds_tenants_unpriced_models_and_health(client, mongo) -> None:
    _seed(client, _row("x1", tenant="a"), _row("x2", tenant="b", model="mystery"))
    body = client.get("/admin/usage", params={"group": "tenant"}).json()
    assert {r["tenant"] for r in body["rows"]} == {"a", "b"}
    assert body["unpriced"] == [
        {
            "provider_kind": "openai",
            "provider_name": "p",
            "model": "mystery",
            "calls": 1,
            "input_tokens": 1_000_000,
            "output_tokens": 0,
        }
    ]
    health = body["health"]
    assert health["writer_conflicts"] == 0 and health["tick_at"]
    assert set(health["collection_bytes"]) == {
        "usage_records",
        "usage_hourly",
        "usage_joins",
        "usage_anomalies",
    }


def test_reprice_routes_check_the_range(client) -> None:
    assert detail_code(client.get("/usage/reprice")) == (404, "not_found")
    since = "2026-01-01T00:00:00Z"
    r = client.post("/usage/reprice", json={"since": since, "until": "2026-06-01T00:00:00Z"})
    assert detail_code(r) == (422, "invalid_value")
    r = client.post("/usage/reprice", json={"since": since, "until": "2026-02-01T00:00:00Z"})
    assert r.status_code == 202 and r.json()["done"] == 0
    assert client.get("/usage/reprice").json()["since"].startswith("2026-01-01")
    again = client.post("/usage/reprice", json={"since": since, "until": "2026-02-01T00:00:00Z"})
    assert detail_code(again) == (409, "reprice_running")


def test_usage_needs_tenant_admin_and_platform_usage_needs_platform_admin(oidc, token) -> None:
    viewer = token("v", "daegam-a-view")
    dev = token("d", "daegam-a-dev")
    tadmin = token("ta", "daegam-a-admin")
    for t in (viewer, dev):
        r = oidc.get("/usage", headers=bearer(t, tenant="a"))
        assert r.status_code == 403 and r.json()["detail"]["scope"] == "usage:read"
    assert oidc.get("/usage", headers=bearer(tadmin, tenant="a")).status_code == 200
    assert oidc.get("/admin/usage", headers=bearer(tadmin, tenant="a")).status_code == 403
    root = token("root", "daegam-admins")
    assert oidc.get("/admin/usage", headers=bearer(root)).status_code == 200
