"""리컨실 틱의 리스 (P67, D79).

틱이 항목·단계·끝에서 리스를 늘리고, 잃으면 단계의 처리기를 그대로 지나 멈추며, 문서를 몰아가던
중이면 그 문서를 마친 뒤 멈추는 것을 본다. 시간 몫과 이어 돌 자리, 문서 순서와 클러스터별 멈춤도
본다. 리스는 진짜 Mongo 이고 서비스·스토어는 필요한 만큼만 흉내 낸다.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import anyio

from daegam.errors import ServiceError
from daegam.services import claims as claims_mod
from daegam.services import data_plane_auth, shared_clones
from daegam.services import reconcile as reconcile_mod
from daegam.services import svc_token_sweep as sweep_mod
from daegam.services.claims import TickHold, enter_tick, leave_tick, tick_item
from daegam.services.jwks_heal import PROBE_CONCURRENCY, JwksHeal
from daegam.services.reconcile import LEASE_NAME, Reconciler
from daegam.services.svc_token_sweep import SvcTokenSweeper
from daegam.store.auth_reapply import LIVE_STATES
from daegam.store.base import utcnow
from daegam.store.leases import LEASES, Lease, LeaseStop
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import OneItemBudget, reconciler


def _db(client):
    return client.app.state.backend_apis.db


async def _steal(db) -> None:
    """다른 파드가 리스를 가져간 자리다."""
    await db[LEASES].update_one({"_id": LEASE_NAME}, {"$set": {"holder": "pod-b"}})


def test_a_drive_that_loses_the_lease_finishes_its_document_then_the_tick_stops(
    client, monkeypatch
):
    monkeypatch.setattr(claims_mod, "TICK_RENEW_FLOOR", 0.0)
    db = _db(client)
    driven: list[str] = []
    steps_ran: list[str] = []

    class _Service:
        async def drive(self, inst):
            await _steal(db)
            # 몰아가는 중의 항목 확인은 잃었다고 적기만 하고 멈추지 않는다
            await tick_item()
            driven.append(inst.id)
            return True

    class _Store:
        async def live(self):
            return []

        async def stale(self, _states, _before):
            now = utcnow()
            return [
                SimpleNamespace(id="x", state="deploying", cluster_id="c", updated_at=now),
                SimpleNamespace(
                    id="y", state="deploying", cluster_id="c", updated_at=now + timedelta(1)
                ),
            ]

    async def step(_now):
        steps_ran.append("s")
        return {}

    rec = Reconciler(_Store(), _Service(), Lease(db, "pod-a"), steps=(("s", step),))
    result = client.portal.call(rec.run_once)
    assert driven == ["x"] and result["created"] == ["x"]
    assert steps_ran == [] and result["stopped"] == ["lease_lost"]


def test_the_tick_renews_at_its_end_so_the_lease_outlives_the_wait(client):
    db = _db(client)
    rec = reconciler(client)
    started = utcnow()
    client.portal.call(rec.run_once)
    doc = client.portal.call(db[LEASES].find_one, {"_id": LEASE_NAME})
    # 끝의 갱신이 다음 틱(60초 뒤)보다 오래 리스를 남긴다
    assert doc["expires_at"] >= (started + timedelta(seconds=85)).replace(tzinfo=None)
    assert doc["holder"] == "pod-a"


def test_a_stray_lease_stop_does_not_end_the_loop(client):
    rec = reconciler(client)

    async def boom():
        raise LeaseStop("elsewhere")

    rec.run_once = boom  # type: ignore[method-assign]
    client.portal.call(rec._tick)  # 올라오지 않는다


def test_a_lost_hold_raises_at_once_without_renewing_again(client):
    renewed: list[int] = []

    class _Lease:
        async def renew(self, _name):
            renewed.append(1)
            return False

    hold = TickHold(_Lease(), LEASE_NAME)  # type: ignore[arg-type]
    hold.last = time.monotonic() - 60

    async def twice():
        token = enter_tick(hold)
        caught = 0
        try:
            for _ in range(2):
                try:
                    await tick_item()
                except LeaseStop:
                    caught += 1
        finally:
            leave_tick(token)
        return caught

    assert client.portal.call(twice) == 2
    assert renewed == [1]
    # 훅도 늘리지 않고 바로 멈춘다
    try:
        hold.hook()
        raise AssertionError("hook did not stop")
    except LeaseStop:
        pass
    assert renewed == [1]


# -- 문서 몰아가기의 순서와 멈춤 ---------------------------------------------------------------


class _AlwaysLease:
    async def acquire(self, *_a, **_kw):
        return True

    async def renew(self, *_a, **_kw):
        return True

    async def release(self, *_a, **_kw):
        return None


def _doc(i: str, cluster: str, minutes: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=i,
        state="deleting",
        cluster_id=cluster,
        updated_at=utcnow() - timedelta(minutes=minutes),
    )


def test_drives_merge_kinds_by_age_and_stop_a_cluster_at_cluster_unreachable():
    order: list[str] = []

    class _Service:
        async def drive(self, inst):
            order.append(inst.id)
            if inst.id == "m-old":
                raise ServiceError(502, "cluster_unreachable", "edge is down")
            if inst.id == "g-mid":
                raise ServiceError(502, "k8s_error", "one manifest refused")
            return True

    def store(*docs):
        class _S:
            async def live(self):
                return []

            async def stale(self, _states, _before):
                return list(docs)

        return _S()

    mcp = store(_doc("m-old", "edge", 30), _doc("m-new", "edge", 3))
    gws = store(_doc("g-mid", "home", 20))
    ags = store(_doc("a-older", "home", 25), _doc("a-edge", "edge", 10))
    service = _Service()
    rec = Reconciler(mcp, service, _AlwaysLease(), gateways=(gws, service), agents=(ags, service))  # type: ignore[arg-type]
    result = asyncio.run(rec.run_once())
    # 종류를 섞어 오래된 것부터, edge 는 연결할 수 없다는 답 뒤로 묻지 않는다. k8s_error 는 멈추지
    # 않는다
    assert order == ["m-old", "a-older", "g-mid"]
    assert result["failed"] == ["m-old", "g-mid"] and result["deleted"] == ["a-older"]
    assert result["skipped"] == ["a-edge", "m-new"]


def test_drives_beyond_the_budget_wait_for_the_next_tick(monkeypatch):
    monkeypatch.setattr(reconcile_mod, "DRIVE_SECONDS_PER_TICK", 0.0)

    class _Service:
        async def drive(self, _inst):
            return True

    class _S:
        async def live(self):
            return []

        async def stale(self, _states, _before):
            return [_doc("a", "home", 5), _doc("b", "home", 4)]

    rec = Reconciler(_S(), _Service(), _AlwaysLease())  # type: ignore[arg-type]
    result = asyncio.run(rec.run_once())
    assert result["deleted"] == [] and result["deferred"] == ["a", "b"]


# -- 서비스 토큰 스윕의 몫과 이어 돌 자리 -------------------------------------------------------


def _full_sweeper(asked: list[str]) -> SvcTokenSweeper:
    class _Service:
        instances = None

        async def client_for_doc(self, doc):
            asked.append(doc.id)
            raise ServiceError(502, "k8s_error", "not looked at")

        async def settle_token_objects(self, *_a):
            return False

        async def prune_token_tables(self, *_a):
            return False

        async def instance_token_current(self, *_a):
            return True

    def store(*ids: str):
        docs = [
            SimpleNamespace(
                id=i,
                cluster_id="home",
                state=next(iter(LIVE_STATES)),
                k8s=object(),
                tenant="acme",
                exposure=SimpleNamespace(enabled=True),
                # Agent 의 미리 보기 표시다. 스윕은 미리 보기를 건너뛴다 (P44b 8절)
                preview=None,
            )
            for i in ids
        ]

        class _S:
            async def live(self):
                return list(docs)

        return _S()

    service = _Service()
    sweeper = SvcTokenSweeper.__new__(SvcTokenSweeper)
    sweeper.issuer = SimpleNamespace(loaded=True)  # type: ignore[assignment]
    sweeper.gateways = (store("g2", "g1"), service)  # type: ignore[assignment]
    sweeper.mcp_instances = (store("m1"), service)  # type: ignore[assignment]
    sweeper.agents = (store("a1"), service)  # type: ignore[assignment]
    sweeper.resume_after = None
    return sweeper


def test_a_cut_token_sweep_resumes_across_kinds_one_consumer_at_a_time(monkeypatch):
    monkeypatch.setattr(sweep_mod, "Budget", OneItemBudget)
    asked: list[str] = []
    sweeper = _full_sweeper(asked)
    seen = []
    for _ in range(4):
        result = asyncio.run(sweeper.run())
        seen.append(result.get("resume_after"))
    # (종류 순번, id) 차례로 한 회차에 하나씩, 종류가 바뀌는 자리도 몫에 걸린다
    assert asked == ["g1", "g2", "m1", "a1"]
    assert seen == [["g1"], ["g2"], ["m1"], None]
    assert sweeper.resume_after is None


def test_reconcile_counts_the_sweep_hour_after_it_finished_or_failed():
    class _Sweeper:
        def __init__(self, outcome):
            self.outcome = outcome

        async def run(self, now=None):
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

    def fresh():
        rec = Reconciler.__new__(Reconciler)
        rec._last_svc_sweep = None
        rec.svc_sweep_interval = 3600.0
        return rec

    now = utcnow()
    rec = fresh()
    asyncio.run(rec._sweep_svc_tokens(_Sweeper({"renewed": [], "resume_after": ["x"]}), now))
    assert rec._last_svc_sweep is None
    asyncio.run(rec._sweep_svc_tokens(_Sweeper({"renewed": []}), now))
    assert rec._last_svc_sweep == now
    # 실패한 스윕은 다음 틱마다 되풀이하지 않는다
    rec = fresh()
    try:
        asyncio.run(rec._sweep_svc_tokens(_Sweeper(RuntimeError("boom")), now))
    except RuntimeError:
        pass
    assert rec._last_svc_sweep == now
    # 리스를 잃은 멈춤은 한 시간을 세지 않는다
    rec = fresh()

    async def stopped():
        try:
            await rec._sweep_svc_tokens(_Sweeper(LeaseStop(LEASE_NAME)), now)
        except LeaseStop:
            return True
        return False

    assert asyncio.run(stopped())
    assert rec._last_svc_sweep is None
    assert sweep_mod.SWEEP_SECONDS_PER_TICK == 20.0


def test_a_fresh_take_of_the_lease_forgets_the_sweep_resume_point(client):
    rec = reconciler(client)
    rec.svc_token_sweeper = SimpleNamespace(resume_after=(1, "m1"))  # type: ignore[assignment]
    rec._steps = lambda: []  # type: ignore[method-assign]
    client.portal.call(rec.run_once)
    assert rec.svc_token_sweeper.resume_after is None
    rec.svc_token_sweeper.resume_after = (1, "m1")
    client.portal.call(rec.run_once)
    # 같은 파드가 이어 쥔 틱은 자리를 남긴다
    assert rec.svc_token_sweeper.resume_after == (1, "m1")


# -- 리스를 잃은 멈춤이 단계의 처리기를 지나는가 -----------------------------------------------


def _stop() -> LeaseStop:
    return LeaseStop(LEASE_NAME)


async def _raises_stop(coro) -> bool:
    try:
        await coro
    except LeaseStop:
        return True
    return False


def test_lease_stop_in_the_auth_sweep_marks_no_cluster_unreachable(
    client, fake_clusters, monkeypatch
):
    cid, _bp, _ds, ms = mcp_server_env(client)
    clone_and_deploy(client, ms)
    auth = client.app.state.data_plane_auth

    def lost(*_a, **_kw):
        raise _stop()

    monkeypatch.setattr(data_plane_auth, "live_oidc_configs", lost)
    assert client.portal.call(lambda: _raises_stop(auth.run()))
    states = client.portal.call(auth.store.clusters)
    assert not any(s.unreachable or s.last_error for s in states)


def test_lease_stop_in_ensure_backs_off_no_namespace(client, fake_clusters, monkeypatch):
    cid, _bp, _ds, ms = mcp_server_env(client)
    clone_and_deploy(client, ms)
    auth = client.app.state.data_plane_auth
    monkeypatch.setattr(data_plane_auth, "live_oidc_configs", lambda _c: {})

    def lost(*_a, **_kw):
        raise _stop()

    monkeypatch.setattr(data_plane_auth, "ensure_oidc_configs", lost)
    assert client.portal.call(lambda: _raises_stop(auth.run()))
    states = client.portal.call(auth.store.clusters)
    assert not any(s.drifted or s.last_error for s in states)


def test_lease_stop_in_reapply_one_writes_no_back_off(client):
    auth = client.app.state.data_plane_auth
    held: list[Any] = []

    async def hold(*a):
        held.append(a)

    auth._hold = hold  # type: ignore[method-assign]

    async def reapply_auth(_doc):
        raise _stop()

    target = SimpleNamespace(
        service=SimpleNamespace(reapply_auth=reapply_auth, profile_kind="mcp server"),
        doc=SimpleNamespace(id="x"),
    )
    tick = SimpleNamespace(result={"reapplied": []})
    assert client.portal.call(lambda: _raises_stop(auth._reapply_one(target, tick)))
    assert held == [] and tick.result["reapplied"] == []


def test_lease_stop_in_the_clone_sweep_records_no_failed_cluster(
    client, fake_clusters, monkeypatch
):
    mcp_server_env(client)

    async def lost(self):
        raise _stop()

    monkeypatch.setattr(shared_clones._ClusterSweep, "run", lost)
    sweep = client.app.state.shared_clones.sweep
    assert client.portal.call(lambda: _raises_stop(sweep()))


def test_lease_stop_in_the_token_sweep_lists_nothing_as_failed():
    class _Service:
        async def client_for_doc(self, _doc):
            raise _stop()

    doc = SimpleNamespace(
        id="a", cluster_id="edge", state=next(iter(LIVE_STATES)), k8s=object(), tenant="acme"
    )
    sweeper = SvcTokenSweeper.__new__(SvcTokenSweeper)
    result: dict[str, list[str]] = {"renewed": [], "failed": []}
    down: set[str] = set()
    coro = sweeper._sweep("agent", [doc], _Service(), lambda _c, _d: None, utcnow(), result, down)
    assert asyncio.run(_raises_stop(coro))
    assert result == {"renewed": [], "failed": []} and down == set()


# -- 틱 홀더의 훅과 시계 ----------------------------------------------------------------------


class _CountingLease:
    def __init__(self, clock=None):
        self.renewals: list[float] = []
        self.clock = clock

    async def acquire(self, *_a, **_kw):
        return True

    async def renew(self, *_a, **_kw):
        self.renewals.append(self.clock() if self.clock else time.monotonic())
        return True

    async def release(self, *_a, **_kw):
        return None


def test_tick_hook_renews_from_a_worker_thread_and_not_on_the_loop():
    lease = _CountingLease()
    hold = TickHold(lease, LEASE_NAME)  # type: ignore[arg-type]

    async def go():
        hold.last = time.monotonic() - 60
        hold.hook()  # 이벤트 루프 스레드: 늘리지 않는다
        on_loop = len(lease.renewals)
        await anyio.to_thread.run_sync(hold.hook)
        return on_loop, len(lease.renewals)

    assert asyncio.run(go()) == (0, 1)


def test_a_tick_longer_than_the_lease_keeps_every_renewal_gap_short(monkeypatch):
    """가짜 시계로 150초짜리 틱을 돈다. 항목은 15초씩 걸리고, 갱신 사이의 틈은 75초를 넘지
    않는다."""
    now = [1000.0]
    monkeypatch.setattr(claims_mod.time, "monotonic", lambda: now[0])
    lease = _CountingLease(lambda: now[0])
    started = now[0]

    async def slow_step(_now):
        for _ in range(5):
            now[0] += 15.0
            await tick_item()
        return {}

    class _S:
        async def live(self):
            return []

        async def stale(self, _states, _before):
            return []

    rec = Reconciler(_S(), object(), lease, steps=(("a", slow_step), ("b", slow_step)))  # type: ignore[arg-type]
    asyncio.run(rec.run_once())
    times = [started, *lease.renewals]
    assert now[0] - started >= 150
    assert max(b - a for a, b in itertools.pairwise(times)) <= 75


def test_jwks_probes_check_the_tick_lease_per_probe(monkeypatch):
    monkeypatch.setattr(claims_mod, "TICK_RENEW_FLOOR", 0.0)
    lease = _CountingLease()
    hold = TickHold(lease, LEASE_NAME)  # type: ignore[arg-type]
    heal = JwksHeal.__new__(JwksHeal)

    async def probe(_client, verifier):
        return verifier

    heal._probe = probe  # type: ignore[method-assign]
    verifiers = [object() for _ in range(PROBE_CONCURRENCY * 2 + 1)]

    async def go():
        token = enter_tick(hold)
        try:
            return await heal._probe_all(object(), verifiers)  # type: ignore[arg-type]
        finally:
            leave_tick(token)

    assert asyncio.run(go()) == verifiers
    assert len(lease.renewals) == len(verifiers)
