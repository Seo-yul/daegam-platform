"""MCP Hub 항목을 쓰는 테스트가 함께 쓰는 도우미 (D22, D23).

`hub_env` 는 header 모드 플랫폼에 게시한 MCP Server 둘(calculator·orders)과 remote 항목 하나를 깔고
fake 클러스터에서 셋을 Ready 로 둔다. Gateway·노출·shared 복제·네임스페이스 테스트가 그 위에서 돈다.
`shared_env` 는 여기에 모든 테넌트에 열린 remote 항목을 하나 더하고, `shared_image_env` 는 image
항목을 하나 더한다.

허브 API 는 remote 항목과 proxy 모드 게시를 만들지 않고, 배포는 remote 항목의 복제본을 만들지
않는다. 전에 만든 것을 읽고 렌더하는 테스트는 `stored_remote`, `stored_proxy_publish`,
`gateway_on_remote_proxy`, `gateway_on_remote_clone` 으로 저장소에 둔다.
"""

from __future__ import annotations

from types import SimpleNamespace

from daegam.auth.hub_access import Access
from daegam.k8s.templates.common import proxy_service_url
from daegam.services.hub import CR_KIND, hub_k8s_name, shared_clone_name
from daegam.store.base import fields_of
from daegam.store.hub import DEFAULT_TRANSPORT, HubItem, HubK8s, HubSource
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.consts import DEFAULT_NS, EXT_URL, GV, GW, HUB, MS, T2
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


# `stored_remote` 가 항목을 두는 테넌트. 테스트 클라이언트의 기본 테넌트다
OWNER = "default"


def _stored(client, item_id: str) -> dict:
    return client.get(f"{HUB}/{item_id}").json()


def stored_remote(client, name: str, url: str, *, access: dict | None = None) -> dict:
    """전에 만든 remote 항목. 기본 테넌트의 문서를 저장소에 두고 그 MCPRemoteProxy 를 적용한 뒤
    항목 응답을 돌려준다."""
    hub = client.app.state.hub_service

    async def seed() -> str:
        cluster = await hub.tenancy.resolve_cluster(OWNER, None)
        namespace = await hub.tenancy.ensure_tenant_namespace(OWNER, cluster)
        item = HubItem(
            tenant=OWNER,
            name=name,
            display_name=name,
            kind="remote",
            source=HubSource(url=url, transport=DEFAULT_TRANSPORT),
            cluster_id=cluster.id,
            k8s=HubK8s(kind=CR_KIND["remote"], name=hub_k8s_name(name), namespace=namespace),
            access=Access.model_validate(access or {}),
            state="creating",
        )
        await hub.store.create(item)
        await hub.apply(item)
        return item.id

    return _stored(client, client.portal.call(seed))


def stored_proxy_publish(client, item: dict) -> dict:
    """전에 proxy 모드로 게시한 항목. entry 모드로 게시한 항목(응답 dict)의 문서가 MCPRemoteProxy
    를 가리키게 바꾸고 그 CR 을 적용한 뒤 항목 응답을 돌려준다."""
    hub = client.app.state.hub_service

    async def seed() -> None:
        doc = await hub.store.get_unscoped(item["id"])
        doc.k8s = HubK8s(
            kind=CR_KIND["remote"], name=hub_k8s_name(doc.name), namespace=doc.k8s.namespace
        )
        doc.state = "creating"
        await hub.store.set_fields(doc.id, fields_of(doc, {"k8s", "state"}))
        await hub.apply(doc)

    client.portal.call(seed)
    return _stored(client, item["id"])


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
    remote = stored_remote(client, "spike", SPIKE_URL)
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


def shared_env(client, fake_clusters):
    """기본 테넌트 env + 모든 테넌트에 열린 remote 항목 하나(Ready)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    shared = stored_remote(client, "org-tools", EXT_URL, access=SHARED_ACCESS)
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


def _deployed_before(client, deploy, row: dict) -> str:
    """deploy 로 배포한 Gateway 의 스냅샷 첫 행에 row 를 덮고 다시 provision 해, 전에 배포한 꼴의
    Entry 를 적용한다. Gateway id 를 돌려준다. 배포의 도달 확인이 항목의 주소로 나가지 않게 그동안
    Tool 목록을 빈 답으로 둔다."""
    portal, state = client.portal, client.app.state
    hub = state.hub_service

    async def no_tools(_client, _url, _headers=None):
        return []

    lister, hub.tool_lister = hub.tool_lister, no_tools
    try:
        r = deploy()
    finally:
        hub.tool_lister = lister
    assert r.status_code == 201, r.text
    gateway_id = r.json()["id"]
    fields = {f"deployed_backends.0.{k}": v for k, v in row.items()}
    portal.call(
        state.gateways.coll.update_one, {"_id": gateway_id}, {"$set": {**fields, "state": "failed"}}
    )
    portal.call(state.gateway_service.provision, portal.call(state.gateways.get, gateway_id))
    return gateway_id


def gateway_on_remote_proxy(client, remote, name="gw-p") -> dict:
    """전에 배포해 자기 테넌트의 remote 항목을 그 프록시로 쓰는 Gateway. 배포한 뒤 스냅샷의 그
    행을 프록시의 Service 를 부르던 꼴로 바꾸고 다시 provision 한다. Gateway 응답을 돌려준다."""
    backends = [{"hub_item_id": remote["id"]}]

    def deploy():
        r = create_gateway(client, None, name=name, backends=backends)
        assert r.status_code == 201, r.text
        return deploy_gateway(client, r.json()["id"])

    k8s = remote["k8s"]
    url = proxy_service_url(k8s["name"], k8s["namespace"], remote=True)
    gateway_id = _deployed_before(client, deploy, {"kind": "remote", "url": url})
    return client.get(f"{GW}/{gateway_id}").json()


def gateway_on_remote_clone(client, shared, name="gw-x") -> dict:
    """전에 배포해 shared remote 항목의 복제본을 쓰는 다른 테넌트의 Gateway. 배포한 뒤 스냅샷의 그
    행을 복제본을 쓰던 꼴로 바꾸고 다시 provision 해 복제본과 그 기록을 만든다. Gateway 응답을
    돌려준다."""
    row = {"kind": "remote", "shared_source_id": shared["id"], "clone_kind": CR_KIND["remote"]}
    gateway_id = _deployed_before(
        client, lambda: other_tenant_gateway(client, shared, name=name)[1], row
    )
    return client.get(f"{GW}/{gateway_id}", headers=T2).json()


def clone_name_of(item: dict) -> str:
    """다른 테넌트 네임스페이스에 생기는 그 항목(응답 dict)의 복제본 이름."""
    return shared_clone_name(SimpleNamespace(id=item["id"], name=item["name"]))
