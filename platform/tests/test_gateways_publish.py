"""Gateway (D42) 의 허브 게시를 본다.

entry·proxy 모드, 게시물을 백엔드로 쓰는 Gateway 와 순환, 허브 규칙을 따르는 정책 시각과 프록시
실패, prefix 모드의 게시 도구 이름(D23)을 든다.
"""

from __future__ import annotations

from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB
from tests.helpers.gateways import deploy_gateway, gateway_up, vmcp_of
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env


def test_publish_entry_mode_and_consume(client, fake_clusters, mongo):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    ia = gateway_up(client, items, name="a")
    r = client.post(f"{GW}/{ia['id']}/publish", json={})
    assert r.status_code == 201, r.text
    pub = r.json()
    # 게시 이름 기본값은 문서 name 그대로다 (D24)
    assert pub["kind"] == "published" and pub["name"] == "a" and pub["display_name"] == "a"
    assert pub["source"]["gateway_instance_id"] == ia["id"]
    assert "agent_instance_id" not in pub["source"]
    assert pub["source"]["url"] == "http://vmcp-gw-a.daegam-default.svc.cluster.local:4483/mcp"
    assert pub["tool_names"] == ["Add", "CreateOrder", "GetOrder"]
    assert pub["k8s"]["kind"] == "VirtualMCPServer" and pub["k8s"]["name"] == "gw-a"
    assert [x["metadata"]["name"] for x in fake.find("MCPRemoteProxy", DEFAULT_NS)] == ["hub-spike"]
    r = client.post(f"{GW}/{ia['id']}/publish", json={})
    assert r.status_code == 201 and r.json()["id"] == pub["id"]  # 멱등
    assert client.post(f"{GW}/{ia['id']}/publish", json={"name": "spike"}).status_code == 409
    r = client.delete(f"{GW}/{ia['id']}")
    assert detail_code(r) == (409, "published")
    assert r.json()["detail"]["hub_item_ids"] == [pub["id"]]
    fake.set_status(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, {"phase": "Ready", "observedGeneration": 1}
    )
    c = client.post(GW, json={"name": "c", "backends": [{"hub_item_id": pub["id"]}]}).json()

    async def lister(_client, url, bearer=None):
        assert url == pub["source"]["url"]
        return list(pub["tool_names"])

    client.app.state.hub_service.tool_lister = lister
    ic = deploy_gateway(client, c["id"]).json()
    assert ic["state"] == "deployed" and ic["deployed_conflict_resolution"] == "priority"
    entry = fake.get(GV, "MCPServerEntry", "c-a", DEFAULT_NS)
    assert entry["spec"]["remoteUrl"] == pub["source"]["url"]
    assert entry["spec"]["groupRef"] == {"name": "gw-c"}
    assert vmcp_of(fake, "gw-c")["spec"]["config"]["aggregation"]["tools"] == [{"workload": "c-a"}]
    r = client.put(f"{GW}/{ia['id']}", json={"name": "a", "backends": [{"hub_item_id": pub["id"]}]})
    assert detail_code(r) == (422, "cycle")
    r = client.delete(f"{HUB}/{pub['id']}")
    assert detail_code(r) == (409, "in_use")
    assert r.json()["detail"]["gateway_ids"] == [c["id"]]
    assert client.delete(f"{GW}/{ic['id']}").status_code == 200
    assert client.delete(f"{HUB}/{pub['id']}").status_code == 200
    assert client.delete(f"{GW}/{ia['id']}").status_code == 200


def test_publish_proxy_mode(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    ia = gateway_up(client, items, name="a")
    r = client.post(f"{GW}/{ia['id']}/publish", json={"name": "a-pub", "mode": "proxy"})
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["k8s"]["kind"] == "MCPRemoteProxy" and pub["k8s"]["name"] == "hub-a-pub"
    cr = fake.get(GV, "MCPRemoteProxy", "hub-a-pub", DEFAULT_NS)
    assert cr["spec"]["remoteUrl"] == "http://vmcp-gw-a.daegam-default.svc.cluster.local:4483/mcp"
    assert cr["spec"]["allowPrivateEndpoint"] is True and "groupRef" not in cr["spec"]
    fake.set_status(
        GV,
        "MCPRemoteProxy",
        "hub-a-pub",
        DEFAULT_NS,
        {"phase": "Ready", "url": "http://mcp-hub-a-pub-remote-proxy.daegam-default.svc:8080"},
    )
    st = client.get(f"{HUB}/{pub['id']}").json()["status"]
    assert st["ready"] is True
    assert st["url"] == "http://mcp-hub-a-pub-remote-proxy.daegam-default.svc:8080/mcp"
    assert client.delete(f"{HUB}/{pub['id']}").status_code == 200
    assert fake.get(GV, "MCPRemoteProxy", "hub-a-pub", DEFAULT_NS) is None


def test_publish_follows_the_hub_rules_for_access_stamp_and_proxy_failure(client, fake_clusters):
    """게시는 허브의 절차(`HubService.publish_gateway`)를 탄다. 정책 시각은 허브의 create·
    publish_instance 처럼 access 를 보냈을 때만 찍고, 프록시 적용이 실패하면 항목이 creating 에
    last_error 와 함께 남아 화면이 이유를 읽는다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    ia = gateway_up(client, items, name="a")
    pub = client.post(f"{GW}/{ia['id']}/publish", json={}).json()
    assert pub["access_updated_by"] is None and pub["access_updated_at"] is None
    sent = client.post(f"{GW}/{ia['id']}/publish", json={"name": "a-open", "access": {}}).json()
    assert sent["access_updated_by"] is not None and sent["access_updated_at"] is not None

    fake.unreachable = True
    r = client.post(f"{GW}/{ia['id']}/publish", json={"name": "a-pub", "mode": "proxy"})
    assert detail_code(r) == (502, "cluster_unreachable")
    fake.unreachable = False
    listed = {i["name"]: i for i in client.get(HUB).json()}
    assert listed["a-pub"]["state"] == "creating"
    assert listed["a-pub"]["last_error"].startswith("cluster_unreachable: ")
