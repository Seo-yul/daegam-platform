"""P6 F3b: 다른 테넌트에 열린(shared) 허브 항목. 전 테넌트 노출, 경계 확장은 platform-admin 만,
참조 테넌트 네임스페이스로 CR 복제(라벨 source-id/source-hash), 원본 변경을 복제본에 다시 렌더하기.
D23 뒤로 입력은 `access` 이고 응답의 `scope` 는 파생 값이다. 복제본의 기록과 스윕(P50 1절)은
test_shared_clone_records.py 가 본다."""

from __future__ import annotations

from daegam.auth.cedar import tenant_fence_policies
from daegam.auth.hub_access import Access
from daegam.k8s.labels import LABEL_SOURCE_HASH, LABEL_SOURCE_ID
from daegam.store.hub import HubItem, HubK8s, HubSource
from tests.helpers.auth import bearer
from tests.helpers.clusters import downgrade_toolhive
from tests.helpers.consts import EXT_URL, GV, GW, HUB, OTHER_NS, T2
from tests.helpers.gateways import gateway_up
from tests.helpers.hub import (
    SHARED_ACCESS,
    clone_name_of,
    gateway_on_remote_clone,
    other_tenant_gateway,
    shared_env,
    shared_image_env,
)
from tests.helpers.workers import run_clone_sweep, run_data_plane_auth

# -- 노출·생성 권한 ---------------------------------------------------------------------


def test_reading_tools_needs_consume_and_shared_items_need_manage_or_share(oidc, mongo, token):
    """D48: 도구 조회는 읽기인데도 consume 이다. 내용이 아니라 행동 때문이다. 이 호출은 사용자가
    적은 임의의 주소로 플랫폼이 나가게 하므로, 보기만 하는 주체에게 발신 능력을 주지 않는다. 모든
    테넌트에 열리고 manage 가 platform-admin 인 항목은 developer 가 지우거나 고치지 못한다. 열린
    항목을 만드는 경계 확장은 platform-admin 의 hub:share 라 테넌트 admin 도 못 한다."""
    item = HubItem(
        tenant="a",
        name="org-entry",
        kind="entry",
        source=HubSource(url=EXT_URL, transport="sse"),
        cluster_id="c1",
        k8s=HubK8s(kind=None, name="hub-org-entry", namespace="daegam-a"),
    )
    mongo["hub_items"].insert_one(item.to_doc())

    viewer = bearer(token("v", "daegam-a-view"))
    # 보기는 된다. 이름과 매니페스트까지는 그대로 보인다
    row = oidc.get(f"{HUB}/{item.id}", headers=viewer)
    assert row.status_code == 200
    assert row.json()["permissions"] == {
        "view": True,
        "consume": False,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }
    r = oidc.get(f"{HUB}/{item.id}/tools", headers=viewer)
    assert r.status_code == 403 and r.json()["detail"]["action"] == "consume"

    # consume 이 있으면 된다 (Gateway 를 꾸리는 사람이 이 자리다)
    dev = bearer(token("dev", "daegam-a-dev"))
    r = oidc.get(f"{HUB}/{item.id}/tools", headers=dev)
    assert r.status_code == 200 and r.json()["listed"] is False

    # 모든 테넌트에 열리고 manage 가 platform-admin 인 항목
    old = HubItem(
        tenant="a",
        name="old-entry",
        kind="entry",
        source=HubSource(url=EXT_URL, transport="sse"),
        cluster_id="c1",
        k8s=HubK8s(kind=None, name="hub-old-entry", namespace="daegam-a"),
        access=Access.model_validate(
            {
                "view": {"tenants": ["*"]},
                "consume": {"tenants": ["*"]},
                "manage": {"roles": ["platform-admin"]},
            }
        ),
    )
    mongo["hub_items"].insert_one(old.to_doc())
    row = oidc.get(f"{HUB}/{old.id}", headers=dev).json()
    assert row["scope"] == "shared"
    assert row["permissions"] == {
        "view": True,
        "consume": True,
        "manage": False,
        "replace_header": False,
        "replace_secret_env": False,
    }
    r = oidc.delete(f"{HUB}/{old.id}", headers=dev)
    assert r.status_code == 403 and r.json()["detail"]["action"] == "manage"
    assert oidc.post(f"{HUB}/{old.id}/refresh-tools", headers=dev).status_code == 403
    r = oidc.put(f"{HUB}/{old.id}/access", headers=dev, json={})
    assert r.status_code == 403 and r.json()["detail"]["action"] == "manage"
    # 테넌트 admin 도 열린 항목을 만들지 못한다. 경계 확장은 platform-admin 의 hub:share 다
    admin = bearer(token("u-admin", "daegam-a-admin"))
    shared = {"name": "x", "kind": "entry", "url": EXT_URL, "transport": "streamable-http"}
    r = oidc.post(HUB, json={**shared, "access": SHARED_ACCESS}, headers=admin)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["scope"] == "hub:share"


# -- 참조 시 복제 ------------------------------------------------------------------------


def test_shared_image_clone_joins_tenant_group(client, fake_clusters):
    shared, fake = shared_image_env(client, fake_clusters)
    profile, r = other_tenant_gateway(client, shared, name="gw-i", tools=["Echo"])
    assert r.status_code == 201, r.text
    b = r.json()["deployed_backends"][0]
    name = clone_name_of(shared)
    assert b["clone_kind"] == "MCPServer" and b["clone_name"] == name
    clone = fake.get(GV, "MCPServer", name, OTHER_NS)
    assert clone["spec"]["groupRef"] == {"name": "other"}  # 테넌트 그룹에 들어간다
    assert clone["spec"]["toolConfigRef"] == {"name": f"{name}-tools"}
    tc = fake.get(GV, "MCPToolConfig", f"{name}-tools", OTHER_NS)
    assert tc["spec"]["toolsFilter"] == ["Echo"]
    assert tc["metadata"]["labels"][LABEL_SOURCE_ID] == shared["id"]
    assert (
        tc["metadata"]["labels"][LABEL_SOURCE_HASH]
        == clone["metadata"]["labels"][LABEL_SOURCE_HASH]
    )
    assert b["url"] == f"http://mcp-{name}-proxy.daegam-other.svc.cluster.local:8080/mcp"
    # 복제본의 울타리는 쓰는 테넌트의 것이다 (P50 11절)
    assert clone["spec"]["authzConfigRef"] == {"name": f"{name}-authz"}
    fence = fake.get(GV, "MCPAuthzConfig", f"{name}-authz", OTHER_NS)
    assert fence["spec"]["config"]["policies"] == tenant_fence_policies("other")
    assert fence["metadata"]["labels"][LABEL_SOURCE_ID] == shared["id"]
    # 쓰는 Gateway 가 없어지면 스윕이 복제본과 그 울타리·도구 필터를 지운다
    assert client.delete(f"{GW}/{r.json()['id']}", headers=T2).status_code == 200
    assert run_clone_sweep(client)["deleted"] == [f"{OTHER_NS}/{name}"]
    for kind, left in (("MCPAuthzConfig", "authz"), ("MCPToolConfig", "tools")):
        assert fake.get(GV, kind, f"{name}-{left}", OTHER_NS) is None


def test_a_remote_item_deploys_as_an_entry_row_with_no_proxy_and_no_clone(
    client, fake_clusters, mongo
):
    """전에 만든 remote 항목을 쓰는 배포와 재배포는 그 행을 entry 행으로 짓는다. Entry 가 항목의
    주소를 가리키고, 주인 테넌트에서는 항목의 프록시를 거치지 않으며 다른 테넌트에서는 복제본을
    만들지 않는다. 재배포한 Gateway 가 쓰던 복제본은 스윕이 지운다."""
    _cid, _items, shared, fake = shared_env(client, fake_clusters)
    called: list[str] = []

    async def lister(_client, url, _headers=None):
        called.append(str(url))
        return ["Add"]

    client.app.state.hub_service.tool_lister = lister

    def row_of(gateway: dict, namespace: str) -> dict:
        (row,) = gateway["deployed_backends"]
        assert (row["kind"], row["url"], row["transport"]) == ("entry", EXT_URL, "streamable-http")
        assert row["shared_source_id"] is None
        assert row["clone_kind"] is None and row["clone_name"] is None
        assert row["tool_names"] == ["Add"]
        entry = fake.get(GV, "MCPServerEntry", row["workload"], namespace)
        assert entry["spec"]["remoteUrl"] == EXT_URL
        return row

    mine = gateway_up(client, None, name="gw-m", backends=[{"hub_item_id": shared["id"]}])
    row_of(mine, "daegam-default")
    _profile, r = other_tenant_gateway(client, shared, name="gw-o")
    assert r.status_code == 201, r.text
    row_of(r.json(), OTHER_NS)
    assert called == [EXT_URL, EXT_URL]
    assert fake.find("MCPRemoteProxy", OTHER_NS) == []
    assert mongo["shared_clones"].count_documents({}) == 0
    # 저장된 항목은 그대로 remote 다
    assert client.get(f"{HUB}/{shared['id']}").json()["kind"] == "remote"

    # 복제본을 쓰던 Gateway 를 재배포하면 행이 entry 행이 되고 복제본은 쓰는 곳이 없어진다
    old = gateway_on_remote_clone(client, shared, name="gw-c")
    name = clone_name_of(shared)
    assert old["deployed_backends"][0]["clone_name"] == name
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is not None
    r = client.post(f"{GW}/{old['id']}/redeploy", json={}, headers=T2)
    assert r.status_code in (200, 201), r.text
    row_of(client.get(f"{GW}/{old['id']}", headers=T2).json(), OTHER_NS)
    assert run_clone_sweep(client)["deleted"] == [f"{OTHER_NS}/{name}"]
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is None


def test_a_remote_item_that_keeps_its_proxy_deploys_through_it(client, fake_clusters, mongo):
    """프록시를 떼지 못하는 remote 항목(SSE 만 말하는 서버, 감사 게이트 아래의 클러스터)을 쓰는
    배포와 재배포는 지금처럼 그 프록시를, 다른 테넌트에서는 복제본을 부르는 행을 짓는다. 플랫폼은 그
    주소를 직접 부르지 않고 프록시의 상태로 준비됐는지 본다."""
    cid, _items, shared, fake = shared_env(client, fake_clusters)
    item = {"_id": shared["id"]}

    async def lister(_client, url, _headers=None):
        raise AssertionError(f"the platform called {url}")

    client.app.state.hub_service.tool_lister = lister
    proxy_url = "http://mcp-hub-org-tools-remote-proxy.daegam-default.svc:8080"

    def through_the_proxy(gateway: dict, path: str) -> None:
        (row,) = gateway["deployed_backends"]
        assert (row["kind"], row["url"]) == ("remote", f"{proxy_url}{path}")
        assert row["clone_name"] is None
        entry = fake.get(GV, "MCPServerEntry", row["workload"], "daegam-default")
        assert entry["spec"]["remoteUrl"] == row["url"]

    # SSE 만 말하는 서버
    mongo["hub_items"].update_one(item, {"$set": {"source.transport": "sse"}})
    mine = gateway_up(client, None, name="gw-m", backends=[{"hub_item_id": shared["id"]}])
    through_the_proxy(mine, "/sse")
    r = client.post(f"{GW}/{mine['id']}/redeploy", json={})
    assert r.status_code in (200, 201), r.text
    through_the_proxy(client.get(f"{GW}/{mine['id']}").json(), "/sse")
    # 다른 테넌트의 Gateway 는 복제본을 둔다
    _profile, r = other_tenant_gateway(client, shared, name="gw-o")
    assert r.status_code == 201, r.text
    (row,) = r.json()["deployed_backends"]
    name = clone_name_of(shared)
    assert (row["kind"], row["clone_kind"], row["clone_name"]) == ("remote", "MCPRemoteProxy", name)
    assert row["url"].startswith(f"http://mcp-{name}-remote-proxy.daegam-other.svc.cluster.local")
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is not None

    # 감사 게이트 아래의 클러스터에서는 프록시만 이 호출을 감사에 남긴다
    mongo["hub_items"].update_one(item, {"$set": {"source.transport": "streamable-http"}})
    downgrade_toolhive(mongo, cid)
    r = client.post(f"{GW}/{mine['id']}/redeploy", json={})
    assert r.status_code in (200, 201), r.text
    through_the_proxy(client.get(f"{GW}/{mine['id']}").json(), "/mcp")


# -- 원본 변경과 스윕 (P50 1절) ------------------------------------------------------------


def test_a_source_edit_reaches_the_clone_through_the_reapply(client, fake_clusters, mongo):
    """원본을 고치면 복제본 기록의 판이 달라져 재적용이 복제본을 다시 렌더한다 (P50 1절). 원본이
    그대로면 할 일이 없다. 손으로 지운 복제본은 스윕이 찾아 재적용이 다시 만들고, 쓰는 Gateway 를
    지우면 스윕이 복제본을 지운다."""
    cid, items, shared, fake = shared_env(client, fake_clusters)
    # 열린 항목은 다른 테넌트 목록에도 보이고, 테넌트 항목은 여전히 격리된다
    assert shared["scope"] == "shared" and shared["tenant"] == "default"
    mine = {i["name"]: i for i in client.get(HUB).json()}
    assert mine["org-tools"]["scope"] == "shared"
    other = {i["name"]: i for i in client.get(HUB, headers=T2).json()}
    assert set(other) == {"org-tools"}
    assert other["org-tools"]["tenant"] == "default" and other["org-tools"]["scope"] == "shared"
    assert client.get(f"{HUB}/{shared['id']}", headers=T2).status_code == 200
    assert client.get(f"{HUB}/{items['calculator']['id']}", headers=T2).status_code == 404

    inst = gateway_on_remote_clone(client, shared)
    assert inst["state"] == "deployed"
    # 쓰는 테넌트 네임스페이스에 복제본 프록시가 서고 Entry 래퍼가 그것을 가리킨다. 원본은 그대로다
    b = inst["deployed_backends"][0]
    name = clone_name_of(shared)
    assert name.startswith("shared-org-tools-") and len(name) == len("shared-org-tools-") + 6
    assert b["shared_source_id"] == shared["id"]
    assert b["clone_kind"] == "MCPRemoteProxy" and b["clone_name"] == name
    clone_url = f"http://mcp-{name}-remote-proxy.daegam-other.svc.cluster.local:8080/mcp"
    assert b["url"] == clone_url
    before = fake.get(GV, "MCPRemoteProxy", name, OTHER_NS)
    assert before["spec"]["remoteUrl"] == EXT_URL
    assert before["spec"]["allowPrivateEndpoint"] is False
    labels = before["metadata"]["labels"]
    assert labels[LABEL_SOURCE_ID] == shared["id"] and len(labels[LABEL_SOURCE_HASH]) == 32
    assert labels["daegam.devops.ai.kr/tenant"] == "other"
    entry = fake.get(GV, "MCPServerEntry", b["workload"], OTHER_NS)
    assert entry["spec"]["remoteUrl"] == clone_url
    assert entry["spec"]["groupRef"] == {"name": inst["k8s"]["group"]}
    assert fake.get(GV, "MCPRemoteProxy", "hub-org-tools", "daegam-default") is not None
    assert run_data_plane_auth(client)["reapplied"] == []

    mongo["hub_items"].update_one(
        {"_id": shared["id"]}, {"$set": {"source.url": "https://mcp2.example.com/mcp"}}
    )
    record = mongo["shared_clones"].find_one({"source_item_id": shared["id"]})
    assert record["_id"] in run_data_plane_auth(client)["reapplied"]
    after = fake.get(GV, "MCPRemoteProxy", name, OTHER_NS)
    assert after["spec"]["remoteUrl"] == "https://mcp2.example.com/mcp"
    assert (
        after["metadata"]["labels"][LABEL_SOURCE_HASH]
        != before["metadata"]["labels"][LABEL_SOURCE_HASH]
    )
    assert run_data_plane_auth(client)["reapplied"] == []

    fake.delete(GV, "MCPRemoteProxy", name, OTHER_NS)
    assert run_clone_sweep(client)["missing"] == [f"{OTHER_NS}/{name}"]
    assert record["_id"] in run_data_plane_auth(client)["reapplied"]
    assert fake.get(GV, "MCPRemoteProxy", name, OTHER_NS) is not None

    inst_id = client.get(GW, headers=T2).json()[0]["id"]
    assert client.delete(f"{GW}/{inst_id}", headers=T2).status_code == 200
    assert run_clone_sweep(client)["deleted"] == [f"{OTHER_NS}/{name}"]
    assert mongo["shared_clones"].find_one({"_id": record["_id"]}) is None
