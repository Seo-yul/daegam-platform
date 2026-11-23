"""프록시를 두던 허브 항목을 Gateway 의 Entry 가 주소를 직접 부르는 꼴로 옮기는 리컨실 단계
(`services/entry_conversion.py`).

세 걸음의 순서(스냅샷 행 고쳐 쓰기와 다시 렌더, 항목 옮기기와 프록시 지우기, 복제본 스윕), 항목을
두는 까닭 하나하나, 렌더를 적용하지 못한 Gateway 가 프록시·복제본·종류를 남기는 것, 복제본 스윕의
살아 있는 Entry 규칙, 상태 API 의 꼴, 두 번째 틱의 멱등, 옮길 것이 없는 새 설치를 본다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from daegam.services import entry_conversion
from daegam.services.gateways import GatewayError, GatewayService
from daegam.store.auth_reapply import ClusterAuthState
from daegam.store.base import CLAIM_FIELD, utcnow
from tests.helpers.clusters import downgrade_toolhive
from tests.helpers.consts import DEFAULT_NS, EXT_URL, GV, GW, HUB, OTHER_NS, T2
from tests.helpers.gateways import deploy_gateway, gateway_up
from tests.helpers.hub import (
    clone_name_of,
    gateway_on_remote_clone,
    gateway_on_remote_proxy,
    hub_env,
    shared_env,
    stored_proxy_publish,
)
from tests.helpers.workers import (
    reconcile_tick,
    run_clone_sweep,
    run_data_plane_auth,
    run_entry_conversion,
)

STATUS = "/settings/data-plane-auth"
PROXY = "MCPRemoteProxy"
ENTRY = "MCPServerEntry"
NO_CLONE = {"shared_source_id": None, "clone_kind": None, "clone_name": None}


def _consumers(client, fake_clusters):
    """모든 테넌트에 열린 remote 항목 org-tools 를 자기 테넌트의 Gateway 는 프록시로, 다른 테넌트의
    Gateway 는 복제본으로 쓰는 전에 배포한 꼴. (클러스터 id, 항목, 두 Gateway, 가짜)."""
    cid, _items, shared, fake = shared_env(client, fake_clusters)
    mine = gateway_on_remote_proxy(client, shared)
    theirs = gateway_on_remote_clone(client, shared)
    return cid, shared, mine, theirs, fake


def _entry_url(fake, gateway: dict) -> str:
    row = gateway["deployed_backends"][0]
    return fake.get(GV, ENTRY, row["workload"], gateway["k8s"]["namespace"])["spec"]["remoteUrl"]


def _conversions(client) -> dict:
    r = client.get(STATUS)
    assert r.status_code == 200, r.text
    return r.json()["entry_conversions"]


def _retry(client, mongo, gateway: dict) -> None:
    """실패한 배포의 재시도. 저장된 스냅샷으로 다시 provision 한다."""
    portal, state = client.portal, client.app.state
    mongo["gateways"].update_one({"_id": gateway["id"]}, {"$set": {"state": "failed"}})
    portal.call(state.gateway_service.provision, portal.call(state.gateways.get, gateway["id"]))


def _count_entry_reads(monkeypatch) -> list[object]:
    """단계가 클러스터의 Entry 를 읽은 횟수를 세는 목록. 읽기는 그대로 돈다."""
    reads: list[object] = []
    read = entry_conversion.entry_services

    async def counted(client_):
        reads.append(client_)
        return await read(client_)

    monkeypatch.setattr(entry_conversion, "entry_services", counted)
    return reads


def _held(client, cluster_id: str) -> dict[str, tuple[str, list[str]]]:
    """상태 API 가 그 클러스터에서 옮기지 않고 둔 항목. 이름 → (까닭, 기다리는 Gateway)."""
    rows = _conversions(client).get(cluster_id, {"held": []})["held"]
    return {row["name"]: (row["reason"], row["gateway_ids"]) for row in rows}


def test_a_store_without_proxied_items_has_nothing_to_convert(client, fake_clusters, monkeypatch):
    """프록시를 둔 항목이 없으면 단계가 클러스터를 부르지 않고 결과도 상태도 비어 있다. entry 항목과
    entry 모드로 게시한 항목은 옮길 것이 아니다."""
    _cid, items, _calc, _orders, remote, fake = hub_env(client, fake_clusters)
    assert client.delete(f"{HUB}/{remote['id']}").status_code == 200
    body = {"name": "docs", "kind": "entry", "url": EXT_URL, "transport": "streamable-http"}
    r = client.post(HUB, json=body)
    assert r.status_code == 201, r.text
    gateway = gateway_up(client, items, name="a")
    assert client.post(f"{GW}/{gateway['id']}/publish", json={}).status_code == 201
    reads = _count_entry_reads(monkeypatch)
    assert run_entry_conversion(client) == {}
    assert _conversions(client) == {}
    assert reads == [] and fake.find(PROXY) == []


def test_a_remote_item_is_converted_in_three_steps(client, fake_clusters, mongo):
    """첫 틱은 두 Gateway 의 스냅샷 행을 주소를 직접 부르는 행으로 고쳐 쓰고 재적용이 다시
    렌더한다. Entry 이름과 Tool 이름은 그대로다. 살아 있는 Entry 가 모두 주소를 부르면 다음 틱이
    프록시를 지우고 항목을 entry 로 옮긴다. 복제본은 스윕이 지운다. 그 뒤의 틱은 할 일이 없다."""
    cid, shared, mine, theirs, fake = _consumers(client, fake_clusters)
    clone = clone_name_of(shared)
    before = {g["id"]: g["deployed_backends"][0] for g in (mine, theirs)}
    assert _entry_url(fake, mine).startswith("http://mcp-hub-org-tools-remote-proxy.")
    assert _entry_url(fake, theirs).startswith(f"http://mcp-{clone}-remote-proxy.")
    assert _conversions(client)[cid]["items"] == 2  # hub_env 의 spike 와 org-tools
    assert _conversions(client)[cid]["clones"] == 1

    # 1. 행을 고쳐 쓴다. 클러스터의 Entry 는 아직 프록시와 복제본을 부른다
    first = run_entry_conversion(client)
    assert sorted(first["rewritten"]) == sorted([mine["id"], theirs["id"]])
    assert f"{shared['id']}:consumer_not_applied" in first["held"]
    for gateway, headers in ((mine, None), (theirs, T2)):
        row = client.get(f"{GW}/{gateway['id']}", headers=headers).json()["deployed_backends"][0]
        old = before[gateway["id"]]
        assert row == {**old, "kind": "entry", "url": EXT_URL, **NO_CLONE}
        assert (row["workload"], row["tool_names"]) == (old["workload"], old["tool_names"])
    assert client.get(f"{HUB}/{shared['id']}").json()["kind"] == "remote"
    assert _entry_url(fake, mine) != EXT_URL
    # 행에서 복제본이 빠졌어도 Entry 가 부르는 동안 스윕은 복제본을 두고, 항목도 그대로다
    assert run_clone_sweep(client) == {}
    assert run_entry_conversion(client) == {"held": first["held"]}
    assert _held(client, cid)["org-tools"] == (
        "consumer_not_applied",
        sorted([mine["id"], theirs["id"]]),
    )

    # 재적용이 고쳐 쓴 스냅샷으로 다시 렌더한다
    assert {mine["id"], theirs["id"]} <= set(run_data_plane_auth(client)["reapplied"])
    assert _entry_url(fake, mine) == EXT_URL and _entry_url(fake, theirs) == EXT_URL

    # 2. 항목을 옮기고 프록시를 지운다
    second = run_entry_conversion(client)
    assert second["converted"] == [shared["id"]]
    item = client.get(f"{HUB}/{shared['id']}").json()
    assert (item["kind"], item["k8s"]["kind"], item["state"]) == ("entry", None, "created")
    assert item["source"]["url"] == EXT_URL and item["source"]["transport"] == "streamable-http"
    assert fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS) is None

    # 3. 스윕이 복제본과 그 기록을 지운다
    assert fake.get(GV, PROXY, clone, OTHER_NS) is not None
    assert run_clone_sweep(client)["deleted"] == [f"{OTHER_NS}/{clone}"]
    assert fake.get(GV, PROXY, clone, OTHER_NS) is None
    assert mongo["shared_clones"].count_documents({}) == 0

    # 남은 것은 주소 규칙이 거절한 spike 뿐이고 두 번째 틱은 아무것도 쓰지 않는다
    writes = len(fake.applied), len(fake.deleted)
    assert "converted" not in run_entry_conversion(client)
    assert (len(fake.applied), len(fake.deleted)) == writes
    left = _conversions(client)[cid]
    assert (left["items"], left["clones"]) == (1, 0)
    assert [row["name"] for row in left["held"]] == ["spike"]


def test_the_reconcile_tick_runs_the_step_before_the_reapply(client, fake_clusters):
    """리컨실의 한 틱이 행을 고쳐 쓰고 같은 틱의 재적용이 다시 렌더한다. 다음 틱이 항목을 옮긴다."""
    _cid, shared, mine, theirs, fake = _consumers(client, fake_clusters)
    first = reconcile_tick(client, utcnow())
    assert sorted(first["conversion_rewritten"]) == sorted([mine["id"], theirs["id"]])
    assert {mine["id"], theirs["id"]} <= set(first["auth_reapplied"])
    assert _entry_url(fake, theirs) == EXT_URL
    second = reconcile_tick(client, utcnow())
    assert second["conversion_converted"] == [shared["id"]]
    assert client.get(f"{HUB}/{shared['id']}").json()["kind"] == "entry"


def test_a_consumer_that_cannot_render_keeps_the_proxy_the_clone_and_the_kind(
    client, fake_clusters, mongo, monkeypatch
):
    """다시 렌더하지 못하는 Gateway 의 Entry 는 복제본을 계속 부른다. 그 복제본과 기록, 항목의
    프록시와 종류가 남고 상태가 그 Gateway 를 이름 댄다. 렌더가 지나가면 마저 옮긴다."""
    cid, shared, mine, theirs, fake = _consumers(client, fake_clusters)
    clone = clone_name_of(shared)
    render = GatewayService._apply_consumer

    async def refuse(self, doc, client_, *, rotate):
        if doc.id == theirs["id"]:
            raise GatewayError(422, "locked_field", "the stored overrides touch a locked field")
        await render(self, doc, client_, rotate=rotate)

    monkeypatch.setattr(GatewayService, "_apply_consumer", refuse)
    run_entry_conversion(client)
    auth = run_data_plane_auth(client)
    assert mine["id"] in auth["reapplied"] and theirs["id"] in auth["blocked"]
    assert _entry_url(fake, mine) == EXT_URL and _entry_url(fake, theirs) != EXT_URL

    for _ in range(2):
        held = run_entry_conversion(client)
        assert held == {"held": held["held"]}
        assert f"{shared['id']}:consumer_not_applied" in held["held"]
        assert run_clone_sweep(client) == {}
    assert client.get(f"{HUB}/{shared['id']}").json()["kind"] == "remote"
    assert fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS) is not None
    assert fake.get(GV, PROXY, clone, OTHER_NS) is not None
    assert mongo["shared_clones"].count_documents({}) == 1
    assert _held(client, cid)["org-tools"] == ("consumer_not_applied", [theirs["id"]])

    # 기록을 잃은 복제본도 Entry 가 부르는 동안은 버려진 복제본으로 지우지 않는다
    mongo["shared_clones"].delete_many({})
    assert run_clone_sweep(client) == {}
    assert fake.get(GV, PROXY, clone, OTHER_NS) is not None

    monkeypatch.setattr(GatewayService, "_apply_consumer", render)
    _retry(client, mongo, theirs)
    assert _entry_url(fake, theirs) == EXT_URL
    assert run_entry_conversion(client)["converted"] == [shared["id"]]
    assert run_clone_sweep(client)["deleted"] == [f"{OTHER_NS}/{clone}"]


def test_a_failed_gateway_gets_its_row_rewritten_and_holds_the_item(client, fake_clusters, mongo):
    """실패한 Gateway 의 행도 고쳐 써서 다음 재시도가 주소를 렌더한다. 그 Entry 가 프록시를 부르는
    동안 항목은 남는다. 배포 중인 Gateway 의 행은 건드리지 않고 기다린다."""
    cid, shared, mine, theirs, fake = _consumers(client, fake_clusters)
    mongo["gateways"].update_one({"_id": mine["id"]}, {"$set": {"state": "failed"}})
    mongo["gateways"].update_one({"_id": theirs["id"]}, {"$set": {"state": "deploying"}})
    assert run_entry_conversion(client)["rewritten"] == [mine["id"]]
    rows = {g["_id"]: g["deployed_backends"][0] for g in mongo["gateways"].find()}
    assert (rows[mine["id"]]["kind"], rows[mine["id"]]["url"]) == ("entry", EXT_URL)
    assert rows[theirs["id"]]["kind"] == "remote" and rows[theirs["id"]]["clone_name"]
    assert _held(client, cid)["org-tools"] == (
        "consumer_not_applied",
        sorted([mine["id"], theirs["id"]]),
    )
    _retry(client, mongo, mine)
    assert _entry_url(fake, mine) == EXT_URL


def test_each_reason_holds_the_item_with_its_proxy(client, fake_clusters, mongo, monkeypatch):
    """조건 하나를 지나지 못한 항목은 행도 프록시도 그대로이고 상태가 까닭을 댄다. 조건이 풀리면
    옮기기가 다시 간다."""
    cid, shared, mine, _theirs, fake = _consumers(client, fake_clusters)
    item = {"_id": shared["id"]}

    def held_as(reason: str) -> None:
        result = run_entry_conversion(client)
        assert f"{shared['id']}:{reason}" in result["held"], result
        assert "rewritten" not in result and "converted" not in result
        assert _held(client, cid)["org-tools"] == (reason, [])
        assert mongo["gateways"].find_one({"_id": mine["id"]})["deployed_backends"][0]["kind"] == (
            "remote"
        )
        assert fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS) is not None

    # 감사 게이트 아래의 클러스터에서는 프록시만 이 호출을 감사에 남긴다
    downgrade_toolhive(mongo, cid)
    held_as("audit_old")
    assert _held(client, cid)["spike"] == ("audit_old", [])
    downgrade_toolhive(mongo, cid, "0.53.0")

    # 뒤 판이 쓴 칸을 든 항목
    mongo["hub_items"].update_one(item, {"$set": {"from_a_later_version": True}})
    held_as("hub_item_newer")
    mongo["hub_items"].update_one(item, {"$unset": {"from_a_later_version": ""}})

    # SSE 만 말하는 서버
    mongo["hub_items"].update_one(item, {"$set": {"source.transport": "sse"}})
    held_as("sse_entry")
    mongo["hub_items"].update_one(item, {"$set": {"source.transport": "streamable-http"}})

    # 플랫폼이 그 주소를 직접 부르지 못한다. 다른 네임스페이스의 Service 다
    mongo["hub_items"].update_one(
        item, {"$set": {"source.url": "http://tools.kube-system.svc.cluster.local:8080/mcp"}}
    )
    held_as("entry_address_refused")
    mongo["hub_items"].update_one(item, {"$set": {"source.url": EXT_URL}})

    # 인증 재적용이 연결하지 못해 미뤄 둔 클러스터는 부르지 않는다
    states = client.app.state.data_plane_auth.store
    waiting = ClusterAuthState(
        cluster_id=cid, unreachable=True, retry_at=utcnow() + timedelta(hours=1)
    )
    client.portal.call(states.put_cluster, waiting)
    reads = _count_entry_reads(monkeypatch)
    held_as("cluster_unreachable")
    assert reads == []
    client.portal.call(states.put_cluster, ClusterAuthState(cluster_id=cid))

    assert run_entry_conversion(client)["rewritten"]


def test_entries_that_cannot_be_read_hold_the_items_as_unreachable(
    client, fake_clusters, monkeypatch
):
    """조건을 지난 항목이 있는데 클러스터의 Entry 를 읽지 못하면 그 항목은 cluster_unreachable 이고
    아무것도 쓰지 않는다."""
    cid, shared, _mine, _theirs, _fake = _consumers(client, fake_clusters)

    async def unread(_client):
        raise ConnectionError("no route to the cluster")

    monkeypatch.setattr(entry_conversion, "entry_services", unread)
    result = run_entry_conversion(client)
    assert f"{shared['id']}:cluster_unreachable" in result["held"]
    assert "rewritten" not in result and "failed" not in result
    assert _held(client, cid)["org-tools"] == ("cluster_unreachable", [])


def test_the_status_does_not_wait_for_a_slow_cluster(client, fake_clusters, monkeypatch):
    """상태 API 는 클러스터의 Entry 를 시한 안에 읽지 못하면 기다리지 않고 그 항목을
    cluster_unreachable 로 보인다."""
    cid, _shared, _mine, _theirs, _fake = _consumers(client, fake_clusters)

    async def slow(_client):
        await asyncio.sleep(60)

    monkeypatch.setattr(entry_conversion, "entry_services", slow)
    monkeypatch.setattr(entry_conversion, "STATUS_READ_SECONDS", 0.05)
    assert _held(client, cid)["org-tools"] == ("cluster_unreachable", [])


def test_one_tick_touches_a_few_items_per_cluster(client, fake_clusters, monkeypatch):
    """한 틱이 클러스터 하나에서 손대는 항목 수에는 끝이 있고 남은 항목은 다음 틱이 잇는다. 기다리는
    항목은 그 수에 들지 않는다."""
    _cid, shared, mine, theirs, _fake = _consumers(client, fake_clusters)
    monkeypatch.setattr(entry_conversion, "ITEMS_PER_CLUSTER", 0)
    result = run_entry_conversion(client)
    assert result["deferred"] == [shared["id"]] and "rewritten" not in result
    monkeypatch.setattr(entry_conversion, "ITEMS_PER_CLUSTER", 1)
    assert sorted(run_entry_conversion(client)["rewritten"]) == sorted([mine["id"], theirs["id"]])
    assert "deferred" not in run_entry_conversion(client)


def test_a_proxy_mode_item_is_converted_to_the_source_vmcp(client, fake_clusters):
    """proxy 모드로 게시한 항목도 같은 세 걸음이다. 소비자의 행이 원본 vMCP 의 주소를 부르게 되고,
    그 Entry 가 주소를 부르면 항목의 `k8s` 가 원본 vMCP 를 가리키며 프록시가 지워진다."""
    cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    source = gateway_up(client, items, name="a")
    r = client.post(f"{GW}/{source['id']}/publish", json={"name": "a-pub"})
    assert r.status_code == 201, r.text
    pub = stored_proxy_publish(client, r.json())
    proxy_url = "http://mcp-hub-a-pub-remote-proxy.daegam-default.svc:8080"
    fake.set_status(GV, PROXY, "hub-a-pub", DEFAULT_NS, {"phase": "Ready", "url": proxy_url})
    vmcp_url = pub["source"]["url"]

    async def lister(_client, _url, _headers=None):
        return list(pub["tool_names"])

    client.app.state.hub_service.tool_lister = lister
    c = client.post(GW, json={"name": "c", "backends": [{"hub_item_id": pub["id"]}]}).json()
    consumer = deploy_gateway(client, c["id"]).json()
    old = consumer["deployed_backends"][0]
    assert old["url"] == f"{proxy_url}/mcp" and _entry_url(fake, consumer) == old["url"]

    assert run_entry_conversion(client)["rewritten"] == [consumer["id"]]
    row = client.get(f"{GW}/{consumer['id']}").json()["deployed_backends"][0]
    assert row == {**old, "url": vmcp_url}
    assert _held(client, cid)["a-pub"] == ("consumer_not_applied", [consumer["id"]])
    assert consumer["id"] in run_data_plane_auth(client)["reapplied"]
    assert _entry_url(fake, consumer) == vmcp_url

    assert run_entry_conversion(client)["converted"] == [pub["id"]]
    item = client.get(f"{HUB}/{pub['id']}").json()
    assert item["kind"] == "published" and item["source"]["url"] == vmcp_url
    assert item["k8s"] == {**pub["k8s"], "kind": "VirtualMCPServer", "name": "gw-a"}
    assert fake.get(GV, PROXY, "hub-a-pub", DEFAULT_NS) is None
    assert "a-pub" not in _held(client, cid)


def test_a_proxy_mode_item_whose_source_is_not_deployed_is_held(client, fake_clusters, mongo):
    """proxy 모드로 게시한 항목의 원본 Gateway 가 배포돼 있지 않으면 가리킬 vMCP 가 없다. 항목은
    프록시와 함께 남고 상태가 까닭을 댄다. 원본이 다시 배포되면 옮긴다."""
    cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    source = gateway_up(client, items, name="a")
    r = client.post(f"{GW}/{source['id']}/publish", json={"name": "a-pub"})
    assert r.status_code == 201, r.text
    pub = stored_proxy_publish(client, r.json())
    k8s = mongo["gateways"].find_one({"_id": source["id"]})["k8s"]
    mongo["gateways"].update_one({"_id": source["id"]}, {"$set": {"k8s": None}})

    result = run_entry_conversion(client)
    assert f"{pub['id']}:source_not_deployed" in result["held"] and "converted" not in result
    assert _held(client, cid)["a-pub"] == ("source_not_deployed", [])
    assert fake.get(GV, PROXY, "hub-a-pub", DEFAULT_NS) is not None

    mongo["gateways"].update_one({"_id": source["id"]}, {"$set": {"k8s": k8s}})
    assert run_entry_conversion(client)["converted"] == [pub["id"]]


def test_an_item_someone_is_editing_is_converted_at_a_later_tick(client, fake_clusters, mongo):
    """항목은 그 편집 클레임 안에서 옮긴다. 다른 저장이 클레임을 쥐고 있으면 프록시도 문서도
    건드리지 않고, 클레임이 풀린 뒤의 틱이 옮긴다. 옮긴 뒤에는 클레임이 남지 않는다."""
    _cid, _items, shared, fake = shared_env(client, fake_clusters)
    item = {"_id": shared["id"]}
    claim = {"id": "someone", "until": utcnow() + timedelta(minutes=5)}
    mongo["hub_items"].update_one(item, {"$set": {CLAIM_FIELD: claim}})
    assert "converted" not in run_entry_conversion(client)
    assert fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS) is not None
    assert mongo["hub_items"].find_one(item)["kind"] == "remote"

    mongo["hub_items"].update_one(item, {"$unset": {CLAIM_FIELD: ""}})
    assert run_entry_conversion(client)["converted"] == [shared["id"]]
    stored = mongo["hub_items"].find_one(item)
    assert stored["kind"] == "entry" and CLAIM_FIELD not in stored


def test_the_proxy_of_an_unused_item_goes_with_its_runner_binding(client, fake_clusters):
    """쓰는 Gateway 가 없는 항목은 첫 틱에 옮긴다. P50 전에 만들어져 남아 있는 러너 RoleBinding 도
    함께 지운다."""
    _cid, _items, shared, fake = shared_env(client, fake_clusters)
    proxy = fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS)
    owner = {
        "apiVersion": GV,
        "kind": PROXY,
        "name": "hub-org-tools",
        "uid": proxy["metadata"]["uid"],
    }
    binding = "hub-org-tools-remote-proxy-runner"
    fake.apply(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": binding, "namespace": DEFAULT_NS, "ownerReferences": [owner]},
        }
    )
    assert run_entry_conversion(client)["converted"] == [shared["id"]]
    assert fake.get(GV, PROXY, "hub-org-tools", DEFAULT_NS) is None
    assert fake.get("rbac.authorization.k8s.io/v1", "RoleBinding", binding, DEFAULT_NS) is None


def test_an_item_being_deleted_is_not_converted(client, fake_clusters, mongo):
    """삭제가 시작된 항목은 남은 것으로 세지 않고 옮기지도 않는다."""
    cid, _items, shared, _fake = shared_env(client, fake_clusters)
    mongo["hub_items"].update_one({"_id": shared["id"]}, {"$set": {"state": "deleting"}})
    assert "converted" not in run_entry_conversion(client)
    assert mongo["hub_items"].find_one({"_id": shared["id"]})["kind"] == "remote"
    assert _conversions(client)[cid]["items"] == 1
