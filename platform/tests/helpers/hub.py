"""MCP Hub 항목을 쓰는 테스트가 함께 쓰는 도우미 (D22, D23).

`hub_env` 는 header 모드 플랫폼에 게시한 MCP Server 둘(calculator·orders)과 remote 항목 하나를 깔고
fake 클러스터에서 셋을 Ready 로 둔다. Gateway·노출·shared 복제·네임스페이스 테스트가 그 위에서 돈다.
`shared_env` 는 여기에 모든 테넌트에 열린 remote 항목을 하나 더하고, `shared_image_env` 는 image
항목을 하나 더한다.
"""

from __future__ import annotations

from types import SimpleNamespace

from daegam.services.hub import shared_clone_name
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.consts import DEFAULT_NS, EXT_URL, GV, HUB, MS, T2
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import create_gateway, deploy_gateway
from tests.helpers.mcp_servers import clone_and_deploy, create_server, mcp_server_env

SPIKE_URL = "http://mcp-calculator-proxy.daegam-spike.svc.cluster.local:8080/mcp"
# 옛 scope=shared 와 같은 뜻의 정책 (모두 보고 developer 이상이 고르며 platform-admin 만 관리)
SHARED_ACCESS = {
    "view": {"tenants": ["*"]},
    "consume": {"tenants": ["*"]},
    "manage": {"roles": ["platform-admin"]},
}


def hub_env(client, fake_clusters):
    """허브 환경을 깐다. MCP Server 둘(calculator [Add, Divide], orders)과 remote 항목 하나를 만들고
    모두 Ready 로 둔다."""
    cid, bp, ds, mp = mcp_server_env(client)  # calc-mcp: tools [Add]
    r = client.put(
        f"{MS}/{mp['id']}",
        json={
            "name": "calc-mcp",
            "backend_profile_id": bp["id"],
            "spec_version": 1,
            "tools": ["Add", "Divide"],
            "bindings": [{"data_service_id": ds["id"], "env_prefix": "CACHE"}],
        },
    )
    assert r.status_code == 200, r.text
    calc = clone_and_deploy(client, mp, name="calculator").json()
    bp2 = uploaded_backend(client, "orders", "orders.wsdl")
    mp2 = create_server(client, bp2, "orders-mcp").json()
    orders = clone_and_deploy(client, mp2, name="orders").json()
    # 허브 등재는 수동 게시다 (D23)
    for inst in (calc, orders):
        r = client.post(f"{MS}/{inst['id']}/publish", json={})
        assert r.status_code == 201, r.text
    remote = client.post(HUB, json={"name": "spike", "kind": "remote", "url": SPIKE_URL}).json()
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    fake.set_status(
        GV,
        "MCPServer",
        "mcp-calculator",
        DEFAULT_NS,
        {
            "phase": "Ready",
            "url": "http://mcp-mcp-calculator-proxy.daegam-default.svc.cluster.local:8080/mcp",
        },
    )
    fake.set_status(
        GV,
        "MCPServer",
        "mcp-orders",
        DEFAULT_NS,
        {
            "phase": "Ready",
            "url": "http://mcp-mcp-orders-proxy.daegam-default.svc.cluster.local:8080/mcp",
        },
    )
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-spike",
        DEFAULT_NS,
        {
            "phase": "Ready",
            "url": "http://mcp-hub-spike-remote-proxy.daegam-default.svc.cluster.local:8080",
        },
    )
    items = {i["name"]: i for i in client.get(HUB).json()}
    return cid, items, calc, orders, remote, fake


def shared_env(client, fake_clusters, **item_extra):
    """기본 테넌트 env + 모든 테넌트에 열린 remote 항목 하나(Ready)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    r = client.post(
        HUB,
        json={
            "name": "org-tools",
            "kind": "remote",
            "url": EXT_URL,
            "access": SHARED_ACCESS,
            **item_extra,
        },
    )
    assert r.status_code == 201, r.text
    shared = r.json()
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-org-tools",
        DEFAULT_NS,
        {"phase": "Ready", "url": "http://mcp-hub-org-tools-remote-proxy.daegam-default.svc:8080"},
    )
    return cid, items, shared, fake


def shared_image_env(client, fake_clusters):
    """기본 테넌트 env + 모든 테넌트에 열린 image 항목 하나(Ready, 도구 필터 Echo). (항목, 가짜)."""
    _cid, _items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    return shared_image_item(client, fake), fake


def shared_image_item(client, fake) -> dict:
    """모든 테넌트에 열린 image 항목 org-img 하나를 만들고 Ready 로 둔다. 기본 테넌트 env 위에서
    부른다."""
    r = client.post(
        HUB,
        json={
            "name": "org-img",
            "kind": "image",
            "image": "ghcr.io/acme/tools:1",
            "tools_filter": ["Echo"],
            "access": SHARED_ACCESS,
        },
    )
    assert r.status_code == 201, r.text
    fake.set_status(
        GV,
        "MCPServer",
        "hub-org-img",
        DEFAULT_NS,
        {"phase": "Ready", "url": "http://mcp-hub-org-img-proxy.daegam-default.svc:8080/mcp"},
    )
    return r.json()


def other_tenant_gateway(client, shared, name="gw-x", tools=None):
    """다른 테넌트가 shared 항목 하나로 Gateway 를 만들고 배포한다. (정의, 배포 응답)."""
    backends = [{"hub_item_id": shared["id"], "tools": tools or []}]
    r = create_gateway(client, None, name=name, backends=backends, headers=T2)
    assert r.status_code == 201, r.text
    profile = r.json()
    return profile, deploy_gateway(client, profile["id"], headers=T2)


def clone_name_of(item: dict) -> str:
    """다른 테넌트 네임스페이스에 생기는 그 항목(응답 dict)의 복제본 이름."""
    return shared_clone_name(SimpleNamespace(id=item["id"], name=item["name"]))
