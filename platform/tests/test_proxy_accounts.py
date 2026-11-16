"""원격 프록시의 역할 없는 ServiceAccount (P50 10절, G10).

허브 remote 항목, 그 공유 복제본, MCP Server 노출 앞단의 MCPRemoteProxy 는 테넌트 네임스페이스의
ServiceAccount `daegam-remote-proxy` 로 돌고, 오퍼레이터가 전에 만든 그 프록시의 RoleBinding 은
다시 렌더할 때 지운다. 게이트를 지나지 못한 클러스터에서 이미 도는 프록시는 오늘의 꼴로 남고 새
프록시는 422 로 거절된다. 네임스페이스 스윕은 그 계정이 Secret 을 읽지 못하는지 본다.
"""

from __future__ import annotations

from daegam.services.data_plane_auth import KIND_HUB_ITEM
from tests.helpers.clusters import downgrade_toolhive, revoke_tenant_rights
from tests.helpers.consts import DEFAULT_NS, GV, HUB, OTHER_NS
from tests.helpers.http import detail_code
from tests.helpers.hub import SPIKE_URL, hub_env, other_tenant_gateway, shared_env
from tests.helpers.workers import raise_epoch, run_data_plane_auth

RBAC = "rbac.authorization.k8s.io/v1"
ACCOUNT = "daegam-remote-proxy"


def _runner_binding(fake, proxy: str, *, owned: bool) -> None:
    """오퍼레이터가 프록시마다 만들던 RoleBinding. owned 면 그 프록시가 소유한다."""
    owner = {
        "apiVersion": GV,
        "kind": "MCPRemoteProxy",
        "name": proxy if owned else "x",
        "uid": "u",
    }
    fake.apply(
        {
            "apiVersion": RBAC,
            "kind": "RoleBinding",
            "metadata": {
                "name": f"{proxy}-remote-proxy-runner",
                "namespace": DEFAULT_NS,
                "ownerReferences": [owner],
            },
            "roleRef": {"kind": "Role", "name": f"{proxy}-remote-proxy-runner"},
        }
    )


def test_a_rerendered_remote_proxy_runs_role_less_and_drops_the_operators_binding(
    client, fake_clusters, monkeypatch
):
    _cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    assert fake.get(GV, "MCPRemoteProxy", "hub-spike", DEFAULT_NS)["spec"]["serviceAccount"] == (
        ACCOUNT
    )
    _runner_binding(fake, "hub-spike", owned=True)
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    run_data_plane_auth(client)
    assert fake.get(RBAC, "RoleBinding", "hub-spike-remote-proxy-runner", DEFAULT_NS) is None

    # 그 프록시가 소유하지 않은 같은 이름의 RoleBinding 은 건드리지 않는다
    _runner_binding(fake, "hub-spike", owned=False)
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    run_data_plane_auth(client)
    assert fake.get(RBAC, "RoleBinding", "hub-spike-remote-proxy-runner", DEFAULT_NS) is not None


def test_below_the_gate_a_running_proxy_keeps_todays_form_and_a_new_one_is_refused(
    client, fake_clusters, mongo, monkeypatch
):
    cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    revoke_tenant_rights(mongo, cid)
    raise_epoch(monkeypatch, KIND_HUB_ITEM)
    run_data_plane_auth(client)
    assert "serviceAccount" not in fake.get(GV, "MCPRemoteProxy", "hub-spike", DEFAULT_NS)["spec"]
    r = client.post(HUB, json={"name": "spike-2", "kind": "remote", "url": SPIKE_URL})
    assert detail_code(r) == (422, "cluster_rights_missing")
    assert r.json()["detail"]["feature"] == "roleless_sa"

    downgrade_toolhive(mongo, cid)
    r = client.post(HUB, json={"name": "spike-3", "kind": "remote", "url": SPIKE_URL})
    assert detail_code(r) == (422, "toolhive_too_old")
    # image 항목은 원격 프록시가 아니라 그대로 만든다
    r = client.post(HUB, json={"name": "fetch", "kind": "image", "image": "ghcr.io/x/fetch:1"})
    assert r.status_code == 201, r.text


def test_the_sweep_notes_a_namespace_where_the_proxy_account_reads_secrets(
    client, fake_clusters, mongo
):
    _cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    user = f"system:serviceaccount:{DEFAULT_NS}:{ACCOUNT}"
    fake.user_grants.add((user, "", "secrets", "get", DEFAULT_NS))
    # 테넌트 객체를 다시 맞추게 한다
    mongo["auth_reapply"].delete_many({})
    fake.delete(GV, "MCPOIDCConfig", "daegam-internal-oidc", DEFAULT_NS)
    run_data_plane_auth(client)
    states = client.get("/settings/data-plane-auth").json()["clusters"]
    assert [s["proxy_account_reads"] for s in states] == [[DEFAULT_NS]]


def test_a_new_remote_clone_is_refused_below_the_gate(client, fake_clusters, mongo):
    """다른 테넌트의 remote 항목을 쓰는 Gateway 는 복제본을 처음 만들 때 그 클러스터가 게이트를
    지나야 한다. 오늘의 꼴로 만들면 오퍼레이터가 Secret 을 읽는 Role 을 만든다."""
    cid, _items, shared, fake = shared_env(client, fake_clusters)
    revoke_tenant_rights(mongo, cid)
    _profile, r = other_tenant_gateway(client, shared)
    assert detail_code(r) == (422, "cluster_rights_missing")
    assert fake.find("MCPRemoteProxy", OTHER_NS) == []
