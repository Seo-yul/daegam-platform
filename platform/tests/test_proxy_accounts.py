"""원격 프록시의 역할 없는 ServiceAccount (P50 10절, G10).

플랫폼이 렌더하는 MCPRemoteProxy(MCP Server 의 옛 노출 앞단)는 테넌트 네임스페이스의 ServiceAccount
`daegam-remote-proxy` 로 돌고, 오퍼레이터가 전에 만든 그 프록시의 RoleBinding 은 적용할 때 지운다.
게이트를 지나지 못한 클러스터에서는 그 계정을 쓰지 않는다. 허브 항목은 프록시를 렌더하지 않는다.
네임스페이스 스윕은 그 계정이 Secret 을 읽지 못하는지 본다.
"""

from __future__ import annotations

from daegam.k8s.templates import render_remote_proxy
from daegam.services.proxy_accounts import apply_proxy_doc, proxy_service_account
from tests.helpers.clusters import downgrade_toolhive, revoke_tenant_rights
from tests.helpers.consts import DEFAULT_NS, GV, HUB
from tests.helpers.hub import SPIKE_URL, hub_env
from tests.helpers.workers import run_data_plane_auth

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


def _cluster(client, cid: str):
    return client.portal.call(client.app.state.clusters.get, cid)


def _apply_proxy(client, fake, cid: str, name: str) -> dict:
    """그 클러스터의 게이트대로 렌더한 원격 프록시 하나를 적용한다."""
    doc = render_remote_proxy(
        tenant="default",
        item_id="i1",
        name=name,
        namespace=DEFAULT_NS,
        url=SPIKE_URL,
        transport="streamable-http",
        service_account=proxy_service_account(_cluster(client, cid)),
    )
    client.portal.call(apply_proxy_doc, fake, doc)
    return fake.get(GV, "MCPRemoteProxy", name, DEFAULT_NS)


def test_an_applied_remote_proxy_runs_role_less_and_drops_the_operators_binding(
    client, fake_clusters
):
    cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    _runner_binding(fake, "expose-calc", owned=True)
    assert _apply_proxy(client, fake, cid, "expose-calc")["spec"]["serviceAccount"] == ACCOUNT
    assert fake.get(RBAC, "RoleBinding", "expose-calc-remote-proxy-runner", DEFAULT_NS) is None

    # 그 프록시가 소유하지 않은 같은 이름의 RoleBinding 은 건드리지 않는다
    _runner_binding(fake, "expose-calc", owned=False)
    _apply_proxy(client, fake, cid, "expose-calc")
    binding = fake.get(RBAC, "RoleBinding", "expose-calc-remote-proxy-runner", DEFAULT_NS)
    assert binding is not None


def test_below_the_gate_a_proxy_keeps_todays_form_and_new_items_need_no_proxy(
    client, fake_clusters, mongo
):
    cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    revoke_tenant_rights(mongo, cid)
    assert "serviceAccount" not in _apply_proxy(client, fake, cid, "expose-calc")["spec"]
    # entry 와 image 항목은 원격 프록시가 아니라 게이트 아래에서도 그대로 만든다
    downgrade_toolhive(mongo, cid)
    entry = {"name": "spike-2", "kind": "entry", "url": SPIKE_URL, "transport": "streamable-http"}
    assert client.post(HUB, json=entry).status_code == 201
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
