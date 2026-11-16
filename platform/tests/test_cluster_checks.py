"""클러스터 점검 (P50 2절): 리컨실이 클러스터마다 한 시간에 한 번 preflight 를 다시 돌리고, 살아
있는 문서의 ToolHive CR 을 Strict dry run 으로 보내 클러스터마다 Strict 로 적용할 kind 를 정한다.
한 틱의 시간을 넘기면 남은 클러스터는 다음 틱이 잇고, 판정하지 못한 게이트 재료는 저장된 값을 둔다.
관리자는 ToolHive 판을 적어 둘 수 있다."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from daegam.api.clusters import _out
from daegam.config import Settings
from daegam.services import reconcile
from daegam.store.base import utcnow
from daegam.store.clusters import ClusterGates, Preflight, SecretRightsCheck, StrictApplyCheck
from tests.helpers.consts import DEFAULT_NS
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.workers import reconcile_tick

REFUSAL = 'strict decoding error: unknown field "spec.bogus"'
OPERATOR = ("apps/v1", "Deployment", "toolhive-system", "toolhive-operator")


def _stored(mongo, cid) -> dict:
    return mongo["clusters"].find_one({"_id": cid})


def _strict(mongo, cid) -> dict | None:
    return (_stored(mongo, cid).get("gates") or {}).get("strict_apply")


def _deployed(client, fake_clusters):
    """MCP Server 하나를 배포한 클러스터. 점검은 틱마다 돈다(점검 간격 0)."""
    cid, _bp, _ds, mp = mcp_server_env(client)
    assert clone_and_deploy(client, mp).status_code == 201
    client.app.state.reconciler.cluster_check_interval = 0
    return cid, fake_clusters.by_name["edge"]


def _refuse_strict_only(fake, message=REFUSAL) -> None:
    """Strict dry run 만 거부한다. Strict 가 아닌 dry run 과 적용은 통과한다."""
    apply = fake.apply

    def apply_(obj, *, dry_run=False, field_validation=None):
        if dry_run and field_validation == "Strict" and obj["kind"] == "MCPServer":
            raise ValueError(message)
        return apply(obj, dry_run=dry_run, field_validation=field_validation)

    fake.apply = apply_


def test_a_strict_only_refusal_takes_that_kind_out(client, fake_clusters, mongo):
    """Strict 만 거부하고 Strict 가 아니면 통과하는 문서가 거부다. 그 kind 는 Strict 적용에서
    빠진다."""
    cid, fake = _deployed(client, fake_clusters)
    reconcile_tick(client, utcnow())
    assert _strict(mongo, cid)["kinds"] == ["MCPServer"]
    _refuse_strict_only(fake)
    res = reconcile_tick(client, utcnow() + timedelta(seconds=1))
    [refused] = res["clusters_strict_refused"]
    assert refused.startswith("edge: MCPServer ") and refused.endswith(REFUSAL)
    check = _strict(mongo, cid)
    assert check["ok"] is False and check["refused"] == 1 and check["kinds"] == []
    assert check["failures"] == [refused.removeprefix("edge: ")]
    # Cluster 응답이 판정을 싣는다 (이 클라이언트는 cluster:write 를 가진다)
    out = client.get(f"/clusters/{cid}").json()["gates"]["strict_apply"]
    assert out["ok"] is False and out["refused"] == 1 and out["failures"] == check["failures"]


def test_a_document_refused_without_strict_is_not_counted(client, fake_clusters, mongo):
    """Strict 가 아니어도 거부되는 문서(이름 공간이 없음, 권한, 충돌)는 Strict 의 문제가 아니다.
    판정에 넣지 않고, 보낸 문서가 없는 점검은 어느 kind 도 켜지 않는다."""
    cid, fake = _deployed(client, fake_clusters)
    fake.reject_dry_run = lambda obj: "namespaces not found"
    res = reconcile_tick(client, utcnow())
    assert res["clusters_strict_refused"] == []
    check = _strict(mongo, cid)
    assert check["checked"] == 0 and check["refused"] == 0
    assert check["ok"] is False and check["kinds"] == []


def test_each_cluster_is_checked_once_an_hour(client, fake_clusters, mongo):
    mcp_server_env(client)
    client.app.state.reconciler.cluster_check_interval = 3600
    now = utcnow()
    # 이 판으로 처음 뜬 뒤에는 Strict 판정이 없으므로 바로 점검한다
    assert "clusters_preflight_checked" in reconcile_tick(client, now)
    res = reconcile_tick(client, now + timedelta(minutes=59))
    assert not any(k.startswith("clusters_") for k in res)
    assert "clusters_preflight_checked" in reconcile_tick(client, now + timedelta(minutes=61))


def test_a_cluster_left_over_comes_on_the_next_tick(client, fake_clusters, mongo, monkeypatch):
    """한 틱의 시간을 다 쓰면 남은 클러스터는 점검 시각이 그대로라 다음 틱이 잇는다."""
    cid, _fake = _deployed(client, fake_clusters)
    monkeypatch.setattr(reconcile, "CLUSTER_CHECK_SECONDS_PER_TICK", 0)
    res = reconcile_tick(client, utcnow())
    assert res["clusters_deferred"] == ["edge"] and res["clusters_preflight_checked"] == []
    assert _strict(mongo, cid) is None
    monkeypatch.setattr(reconcile, "CLUSTER_CHECK_SECONDS_PER_TICK", 20.0)
    assert reconcile_tick(client, utcnow())["clusters_preflight_checked"] == ["edge"]


def test_an_unreachable_cluster_keeps_its_verdict_and_gates(client, fake_clusters, mongo):
    """연결할 수 없는 클러스터는 preflight 가 실패로 남지만 Strict 판정과 게이트 재료, 판은 지난
    것 그대로다. 연결 오류 한 번으로 한 시간 동안 게이트가 닫히지 않는다."""
    cid, fake = _deployed(client, fake_clusters)
    now = utcnow()
    reconcile_tick(client, now)
    fake.unreachable = True
    res = reconcile_tick(client, now + timedelta(seconds=1))
    assert res["clusters_preflight_failed"] == ["edge"]
    stored = _stored(mongo, cid)
    assert stored["gates"]["strict_apply"]["kinds"] == ["MCPServer"]
    assert stored["gates"]["tenant_object_rights"] is True
    assert stored["gates"]["usage_metrics"] is True
    assert stored["status"]["preflight"]["toolhive_version"] == "0.53.0"
    # 오퍼레이터가 없다는 답은 판정한 것이라 재료와 판을 비운다
    fake.unreachable = False
    del fake.objects[OPERATOR]
    client.post(f"/clusters/{cid}/preflight")
    stored = _stored(mongo, cid)
    assert stored["gates"]["usage_metrics"] is None
    assert stored["status"]["preflight"]["toolhive_version"] is None


def test_strict_failures_are_shown_only_to_cluster_writers(client, fake_clusters):
    """거부 목록과 묶이지 않은 네임스페이스의 이름은 여러 테넌트의 것이다. cluster:read 만 가진
    사람에게는 수만 보인다."""
    cid, _bp, _ds, _mp = mcp_server_env(client)
    cluster = client.portal.call(client.app.state.clusters.get, cid)
    failures = ["MCPServer team-a/mcp-x: refused"]
    check = StrictApplyCheck(ok=False, checked=2, refused=1, failures=failures)
    cluster.gates = ClusterGates(strict_apply=check)
    # `_out` 이 읽는 ClusterService 의 둘이다. 설정은 샌드박스 우회의 표시가 읽는다 (P40 8절)
    service = SimpleNamespace(env_managed=lambda _c: [], settings=Settings())
    viewer = _out(cluster, service, full=False)["gates"]["strict_apply"]
    assert viewer["failures"] == [] and viewer["refused"] == 1 and viewer["checked"] == 2
    assert _out(cluster, service)["gates"]["strict_apply"]["failures"] == failures
    rights = SecretRightsCheck(
        cluster_wide=True, role=True, namespaces=2, unbound=1, unbound_namespaces=[DEFAULT_NS]
    )
    cluster.status.preflight = Preflight(ok=True, secret_rights=rights)
    viewer = _out(cluster, service, full=False)["status"]["preflight"]["secret_rights"]
    assert viewer == {**rights.model_dump(), "unbound_namespaces": []}
    full = _out(cluster, service)["status"]["preflight"]["secret_rights"]
    assert full["unbound_namespaces"] == [DEFAULT_NS]


def test_admin_records_and_clears_the_toolhive_version(client, fake_clusters):
    cid, _bp, _ds, _mp = mcp_server_env(client)
    r = client.patch(f"/clusters/{cid}", json={"recorded_toolhive_version": "v0.53.1"})
    assert r.status_code == 200, r.text
    assert r.json()["recorded_toolhive_version"] == "0.53.1"
    assert client.get(f"/clusters/{cid}").json()["recorded_toolhive_version"] == "0.53.1"
    for bad in ("latest", "0.53.1abc", "0.53.1.4"):
        r = client.patch(f"/clusters/{cid}", json={"recorded_toolhive_version": bad})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_value", bad
    r = client.patch(f"/clusters/{cid}", json={"recorded_toolhive_version": None})
    assert r.status_code == 200 and r.json()["recorded_toolhive_version"] is None
    # 다른 필드를 고쳐도 적어 둔 값과 게이트 재료는 그대로다
    client.patch(f"/clusters/{cid}", json={"recorded_toolhive_version": "0.52.0"})
    r = client.patch(f"/clusters/{cid}", json={"display_name": "Edge"})
    assert r.json()["recorded_toolhive_version"] == "0.52.0"
    assert r.json()["gates"]["tenant_object_rights"] is True
