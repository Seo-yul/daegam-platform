"""문서 하나의 클러스터 작업을 묶는 클레임 (P66, D78).

클레임을 남이 쥐었을 때의 배포·리컨실, 배포 도중의 삭제, 끝 저장 뒤의 삭제, 클레임을 넘긴 실행의
정리, 기다린 삭제와 미룬 삭제, 리스의 갱신 규칙과 TTL 색인을 본다. 클러스터는 tests/helpers 의
가짜이고 Mongo 는 진짜다.
"""

from __future__ import annotations

import time

import anyio
import pytest
from starlette.concurrency import run_in_threadpool

from daegam.services import claims as claims_mod
from daegam.services.claims import (
    ClaimRun,
    Outcome,
    TickHold,
    claim_name,
    enter_tick,
    leave_tick,
    request_hook,
    tick_item,
)
from daegam.services.mcp_spec import scope_for
from daegam.store.base import hash_token, issue_token
from daegam.store.leases import Lease, LeaseStop
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.consts import BP, DEFAULT_NS, GV, MS
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import reconciler, stale_tick


def _svc(client):
    return client.app.state.mcp_server_service


def _store(client):
    return client.app.state.mcp_servers


def _db(client):
    return client.app.state.backend_apis.db


def _hold(client, doc_id: str, kind: str = "mcp-server", holder: str = "other-pod/run") -> Lease:
    """다른 실행이 이 문서의 클레임을 쥔 것처럼 두고 그 리스를 돌려준다."""
    lease = Lease(_db(client), holder)
    assert client.portal.call(lease.acquire, claim_name(kind, doc_id))
    return lease


def _set_state(client, doc_id: str, state: str) -> None:
    assert client.portal.call(_store(client).set_fields, doc_id, {"state": state})


def _cr(fake, name: str = "mcp-calc-1"):
    return fake.get(GV, "MCPServer", name, DEFAULT_NS)


def _active_spec_tokens(client, bp_id: str) -> list[dict]:
    return [t for t in client.get(f"{BP}/{bp_id}/tokens").json() if t["active"]]


@pytest.fixture
def short_wait(client):
    """삭제·해제가 남이 쥔 클레임을 기다리는 시간을 줄인다."""
    claims = _svc(client).claims
    saved = claims.wait, claims.poll
    claims.wait, claims.poll = 0.3, 0.05
    yield claims
    claims.wait, claims.poll = saved


# -- 남이 쥔 클레임 -------------------------------------------------------------------------


# -- 배포 도중의 삭제 -----------------------------------------------------------------------


def test_delete_during_provision_is_finished_by_the_provision(client, fake_clusters, monkeypatch):
    """삭제가 상태만 옮기고 일을 남기면 provision 이 다음 체크포인트에서 같은 클레임으로 삭제를
    마친다. 배포 요청은 409 state_conflict 이고 클러스터에도 토큰에도 남는 것이 없다."""
    cid, bp, ds, ms = mcp_server_env(client)
    svc = _svc(client)
    real = svc._apply_server

    async def apply_then_deleted(doc, kube, cr, *args, **kwargs):
        await real(doc, kube, cr, *args, **kwargs)
        # 다른 요청의 삭제가 클레임을 쥔 것을 보고 상태만 옮긴 자리다
        assert await _store(client).set_fields(doc.id, {"state": "deleting"})

    monkeypatch.setattr(svc, "_apply_server", apply_then_deleted)
    r = clone_and_deploy(client, ms)
    assert detail_code(r) == (409, "state_conflict"), r.text
    fake = fake_clusters.by_name["edge"]
    assert _cr(fake) is None
    assert not [d for d in client.get(MS).json() if d["name"] == "calc-1"]
    assert _active_spec_tokens(client, bp["id"]) == []


# -- 기다린 삭제와 미룬 삭제 ----------------------------------------------------------------


def test_delete_answers_deferred_while_the_claim_stays_held(client, fake_clusters, short_wait):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    lease = _hold(client, inst["id"])
    r = client.delete(f"{MS}/{inst['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is False and body["revoked_token_ids"] == []
    assert body["state_before"] == "deployed" and body["k8s"]["name"] == "mcp-calc-1"
    assert client.get(f"{MS}/{inst['id']}").json()["state"] == "deleting"
    # 홀더가 사라지면 다음 삭제(또는 리컨실)가 마친다
    client.portal.call(lease.release, claim_name("mcp-server", inst["id"]))
    assert client.delete(f"{MS}/{inst['id']}").json()["deleted"] is True
    assert client.get(f"{MS}/{inst['id']}").status_code == 404


def test_delete_answers_as_today_when_the_holder_finishes_inside_the_wait(
    client, fake_clusters, short_wait
):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    lease = _hold(client, inst["id"])
    short_wait.wait = 5.0
    store = _store(client)

    async def holder_finishes():
        await anyio.sleep(0.2)
        await store.delete(inst["id"])

    async def both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(holder_finishes)
            doc = await store.get(inst["id"])
            return await _svc(client).delete(doc)

    assert client.portal.call(both) is True
    client.portal.call(lease.release, claim_name("mcp-server", inst["id"]))


def test_undeploy_answers_undeploying_while_the_claim_stays_held(client, fake_clusters, short_wait):
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    lease = _hold(client, inst["id"])
    r = client.post(f"{MS}/{inst['id']}/undeploy")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "undeploying" and r.json()["k8s"] is not None
    client.portal.call(lease.release, claim_name("mcp-server", inst["id"]))
    assert client.post(f"{MS}/{inst['id']}/undeploy").json()["state"] == "draft"


# -- 클레임을 넘긴 실행 ---------------------------------------------------------------------


def test_a_hook_stop_passes_record_failure_and_cleans_up_a_moved_document(
    client, fake_clusters, monkeypatch
):
    """워커 스레드의 훅이 LeaseStop 을 올리면 record_failure 를 그대로 지나 클레임 범위가 잡는다.
    문서가 다른 네임스페이스로 옮겨 갔으면 이 실행이 낸 토큰만 회수하고 옛 이름의 것을 지우며,
    failed 나 last_error 는 남기지 않는다."""
    cid, bp, ds, ms = mcp_server_env(client)
    svc = _svc(client)
    real = svc._apply_server

    def moved_then_request():
        # 오래된 체크포인트라 다음 클러스터 요청 앞의 훅이 판정한다
        claims_mod.current_run().last = time.monotonic() - 3600
        request_hook()

    async def apply_then_moved(doc, kube, cr, *args, **kwargs):
        await real(doc, kube, cr, *args, **kwargs)
        k8s = doc.k8s.model_dump()
        k8s["namespace"] = "elsewhere"
        assert await _store(client).set_fields(doc.id, {"k8s": k8s})
        await run_in_threadpool(moved_then_request)

    monkeypatch.setattr(svc, "_apply_server", apply_then_moved)
    r = clone_and_deploy(client, ms)
    assert detail_code(r) == (409, "state_conflict"), r.text
    got = next(d for d in client.get(MS).json() if d["name"] == "calc-1")
    assert got["state"] == "deploying" and got["last_error"] is None
    assert _cr(fake_clusters.by_name["edge"]) is None
    assert _active_spec_tokens(client, bp["id"]) == []


def test_a_stopped_run_raises_at_once_without_renewing(client):
    calls: list[int] = []

    async def judge(_run):
        calls.append(1)
        return Outcome.CONTINUE

    run = ClaimRun(Lease(_db(client), "x"), "n", "d", judge, frozenset({"deploying"}))
    run.stopped = Outcome.LOST
    with pytest.raises(LeaseStop):
        run.hook()

    async def checkpoint_stop():
        # LeaseStop 은 BaseException 이라 portal 을 넘기지 않고 여기서 받는다
        try:
            await run.checkpoint()
        except LeaseStop as stop:
            return stop.outcome
        return None

    assert client.portal.call(checkpoint_stop) is Outcome.LOST
    assert calls == []


def test_the_hook_does_nothing_outside_a_worker_thread(client):
    async def judge(_run):
        return Outcome.LOST

    run = ClaimRun(Lease(_db(client), "x"), "n", "d", judge, frozenset({"deploying"}))
    run.last = time.monotonic() - 3600
    run.hook()  # 테스트 스레드는 anyio 워커가 아니다
    assert run.stopped is None

    # renew 는 쥔 사람의 리스만 늘린다. 남이 잡았다가 놓은 뒤에는 다시 가져오지 않는다. acquire 와
    # 다른 점이다
    db = _db(client)
    mine, other = Lease(db, "a"), Lease(db, "b")
    assert client.portal.call(mine.acquire, "x")
    assert client.portal.call(mine.renew, "x")
    assert not client.portal.call(other.renew, "x")
    client.portal.call(mine.release, "x")
    assert client.portal.call(other.acquire, "x")
    client.portal.call(other.release, "x")
    assert not client.portal.call(mine.renew, "x")


# -- Agent 인스턴스 토큰 (2.7절) ------------------------------------------------------------


# -- 남이 쥔 클레임과 배포가 남긴 Secret -------------------------------------------------------


def test_reconcile_does_not_wait_on_a_held_deleting_document(client, fake_clusters, short_wait):
    """리컨실은 남이 쥔 클레임을 기다리지 않는다. 리스 안의 틱이 문서마다 30초를 쓰지 않게 한다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    lease = _hold(client, inst["id"])
    short_wait.wait = 30.0
    _set_state(client, inst["id"], "deleting")
    started = time.monotonic()
    result = stale_tick(client, reconciler(client))
    assert time.monotonic() - started < 5
    assert result["skipped"] == [inst["id"]] and result["failed"] == [], result
    client.portal.call(lease.release, claim_name("mcp-server", inst["id"]))
    assert stale_tick(client, reconciler(client))["deleted"] == [inst["id"]]


def test_spec_token_secret_left_with_revoked_tokens_is_rewritten(
    client, fake_clusters, monkeypatch
):
    """id 를 저장한 뒤 Secret 을 쓰기 전에 멈춘 배포는 Secret 에 회수된 옛 토큰을 남긴다. 다음
    provision 은 기록만이 아니라 Secret 의 토큰 해시까지 견주어 새로 쓴다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    fake = fake_clusters.by_name["edge"]
    secret_name = f"inst-{inst['id']}-token"
    old = fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]["token"]
    real_apply = fake.apply

    def drop_token_secret(obj, *a, **kw):
        # 저장과 Secret 적용 사이에서 멈춘 자리다. Secret 은 옛 토큰을 든 채 남는다
        if obj.get("kind") == "Secret" and obj["metadata"]["name"] == secret_name:
            raise RuntimeError("stopped before the Secret write")
        return real_apply(obj, *a, **kw)

    monkeypatch.setattr(fake, "apply", drop_token_secret)
    doc = client.portal.call(_store(client).get, inst["id"])
    # 회수를 일으키려고 문서의 토큰 하나를 회수해 다시 내게 한다
    client.portal.call(client.app.state.backend_apis.revoke_token_by_id, doc.spec_token_id)
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 502
    assert fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]["token"] == old
    monkeypatch.setattr(fake, "apply", real_apply)
    assert stale_tick(client, reconciler(client))["created"] == [inst["id"]]
    new = fake.get("v1", "Secret", secret_name, DEFAULT_NS)["stringData"]["token"]
    assert new != old
    doc = client.portal.call(_store(client).get, inst["id"])
    tok = client.portal.call(client.app.state.backend_apis.get_token, bp["id"], doc.spec_token_id)
    assert tok.active and tok.token_sha256 == hash_token(new)

    # 삭제는 문서에 적기 전에 만든 스펙 토큰도 지운다. 라벨만 같고 scope 가 다른 사람의 토큰은
    # 남긴다
    backend = client.app.state.backend_apis
    label = f"instance:{inst['id']}"
    _token, digest = issue_token()
    stray = client.portal.call(
        backend.create_token, bp["id"], digest, label, [scope_for(inst["id"])]
    )
    _token, digest = issue_token()
    users = client.portal.call(backend.create_token, bp["id"], digest, label, ["other"])
    assert client.delete(f"{MS}/{inst['id']}").json()["deleted"] is True
    assert not client.portal.call(backend.get_token, bp["id"], stray.id).active
    assert not client.portal.call(backend.get_token, bp["id"], tok.id).active
    assert client.portal.call(backend.get_token, bp["id"], users.id).active


# -- F3: 편집·재렌더·복제본 옮기기·스윕 쓰기의 클레임 -------------------------------------------


def test_user_edits_meet_a_held_claim_with_in_progress(client, fake_clusters):
    """Basic 편집과 Advanced 편집은 남이 쥔 클레임에 409 in_progress 이고 아무것도 저장하지
    않는다. 놓이면 그대로 된다."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    lease = _hold(client, inst["id"])
    patch = client.patch(f"{MS}/{inst['id']}", json={"replicas": 2})
    assert detail_code(patch) == (409, "in_progress"), patch.text
    overrides = client.put(
        f"{MS}/{inst['id']}/overrides", json={"patch": {"metadata": {"labels": {"x": "y"}}}}
    )
    assert detail_code(overrides) == (409, "in_progress"), overrides.text
    for action in ("reset", "absorb"):
        r = client.post(f"{MS}/{inst['id']}/drift/{action}")
        assert detail_code(r) == (409, "in_progress"), (action, r.text)
    got = client.get(f"{MS}/{inst['id']}").json()
    assert got["revision"] == inst["revision"] and got["state"] == "deployed"
    client.portal.call(lease.release, claim_name("mcp-server", inst["id"]))
    r = client.patch(f"{MS}/{inst['id']}", json={"replicas": 2})
    assert r.status_code == 200 and r.json()["state"] == "deployed", r.text


def test_the_token_sweep_skips_held_documents_without_failing(
    client, fake_clusters, mongo, monkeypatch
):
    """스윕의 문서 단위 쓰기(Agent 의 표 거두기)는 남이 쥔 클레임이면 건너뛴다. 스윕은 그 문서를
    failed 로 적지 않고, 놓인 뒤의 다음 회차가 거둔다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    service = client.app.state.agent_service
    pruned: list[str] = []

    async def prune_tables(doc, _client):
        pruned.append(doc.id)
        return True

    monkeypatch.setattr(service.agent_tokens, "prune_tables", prune_tables)
    lease = _hold(client, agent.id, kind="agent")
    doc = client.portal.call(client.app.state.agents.get, agent.id)
    assert client.portal.call(service.prune_token_tables, doc, agent.fake) is False
    sweeper = client.app.state.reconciler.svc_token_sweeper
    result = client.portal.call(sweeper.run)
    assert agent.id not in result["failed"] and pruned == [], result
    client.portal.call(lease.release, claim_name("agent", agent.id))
    assert client.portal.call(service.prune_token_tables, doc, agent.fake) is True
    assert pruned == [agent.id]


def test_a_failed_redeploy_still_finishes_a_delete_left_to_it(client, fake_clusters, monkeypatch):
    """재배포가 적용하는 사이 삭제가 상태만 옮기고 일을 남겼는데 provision 이 403 으로 실패해도,
    재배포가 놓은 클레임 밖에서 상태를 다시 보아 삭제를 마친다 (C1)."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    svc = _svc(client)

    async def deleted_then_forbidden(doc, kube, cr, *_args, **_kwargs):
        assert await _store(client).set_fields(doc.id, {"state": "deleting"})
        raise RuntimeError("403 forbidden")

    monkeypatch.setattr(svc, "_apply_server", deleted_then_forbidden)
    r = client.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code >= 400, r.text
    monkeypatch.undo()
    assert client.get(f"{MS}/{inst['id']}").status_code == 404
    assert _cr(fake_clusters.by_name["edge"]) is None


def test_a_background_write_finishes_when_the_tick_lease_is_lost(
    client, fake_clusters, monkeypatch
):
    """리컨실 틱의 리스를 잃어도 배경 쓰기는 몰아가는 중으로 돌아 자기 클레임으로 마친다 (C2)."""
    cid, bp, ds, ms = mcp_server_env(client)
    inst = clone_and_deploy(client, ms).json()
    svc = _svc(client)
    real = svc._apply_consumer
    monkeypatch.setattr(claims_mod, "TICK_RENEW_FLOOR", 0.0)

    async def item_then_apply(doc, kube, *, rotate):
        # 틱의 항목 확인. 리스를 잃었어도 몰아가는 중이면 멈추지 않는다
        await tick_item()
        await real(doc, kube, rotate=rotate)

    monkeypatch.setattr(svc, "_apply_consumer", item_then_apply)

    class _LostLease:
        async def renew(self, _name):
            return False

    hold = TickHold(_LostLease(), "mcp-instances")  # type: ignore[arg-type]
    doc = client.portal.call(_store(client).get, inst["id"])

    async def in_tick():
        token = enter_tick(hold)
        try:
            return await svc.reapply_auth(doc)
        finally:
            leave_tick(token)

    assert client.portal.call(in_tick) is True
    assert hold.lost
