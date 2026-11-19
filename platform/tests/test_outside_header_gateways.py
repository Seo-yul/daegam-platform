"""P86 6.2절: Gateway 의 Entry 가 받는 고정 헤더 사본. 렌더와 값의 사본, 토큰 길과의 분리, 값
바꾸기와 consume 경계의 재렌더, 사라진 값의 경고, 해제."""

from __future__ import annotations

import pytest

from daegam.k8s.templates.svc_token import (
    OUTSIDE_HEADER_REV_ANNOTATION,
    SVC_TOKEN_REV_ANNOTATION,
    entry_header_objects,
    svc_token_refs,
)
from daegam.services.outside_headers import VALUE_KEY, managed_name
from daegam.store.gateways import GatewayBackendSnapshot
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB, OTHER_NS, T2
from tests.helpers.gateways import create_gateway, deploy_gateway, gateway_up, vmcp_of
from tests.helpers.hub import SHARED_ACCESS
from tests.helpers.workers import run_data_plane_auth

OUTSIDE_URL = "https://mcp.example.com/mcp"
SECRET = "Bearer p86-gateway-secret-cd34"


async def lister(_client, url, bearer=None):
    return ["echo_header"]


@pytest.fixture
def env(client, fake_clusters):
    """기본 클러스터, 다른 테넌트에도 열린 header 항목 하나, Tool 목록을 답하는 lister."""
    default_cluster(client)
    client.app.state.hub_service.tool_lister = lister
    r = client.post(
        HUB,
        json={
            "name": "outside",
            "kind": "entry",
            "url": OUTSIDE_URL,
            "transport": "streamable-http",
            "access": SHARED_ACCESS,
            "auth": {"kind": "header", "header_name": "X-API-Key", "header_value": SECRET},
        },
    )
    assert r.status_code == 201, r.text
    return r.json(), fake_clusters.by_name["edge"]


HEADER_ITEM = {
    "name": "outside",
    "kind": "entry",
    "url": OUTSIDE_URL,
    "transport": "streamable-http",
    "auth": {"kind": "header", "header_name": "X-API-Key", "header_value": SECRET},
}


def header_gateway(client, item, name="gw-h", *, headers=None) -> dict:
    backends = [{"hub_item_id": item["id"]}]
    r = create_gateway(client, None, name=name, backends=backends, headers=headers)
    assert r.status_code == 201, r.text
    d = deploy_gateway(client, r.json()["id"], headers=headers)
    assert d.status_code == 201, d.text
    return d.json()


def copy_name(inst: dict) -> str:
    rows = [GatewayBackendSnapshot.model_validate(b) for b in inst["deployed_backends"]]
    (name,) = entry_header_objects(inst["k8s"]["name"], rows).values()
    return name


def copy_objects(fake, inst: dict, namespace: str = DEFAULT_NS) -> dict[str, dict | None]:
    name = copy_name(inst)
    return {kind: fake.get(api, kind, ref, namespace) for api, kind, ref in svc_token_refs(name)}


def entry_of(fake, inst: dict, namespace: str = DEFAULT_NS) -> dict:
    (workload,) = [b["workload"] for b in inst["deployed_backends"]]
    return fake.get(GV, "MCPServerEntry", workload, namespace)


def test_a_header_entry_gets_its_own_copy_and_config(client, env, mongo):
    item, fake = env
    inst = header_gateway(client, item)
    assert inst["deployed_backends"][0]["auth_kind"] == "header"
    name = copy_name(inst)
    assert name.startswith(f"hdr-{inst['k8s']['name']}-")
    objs = copy_objects(fake, inst)
    assert objs["Secret"]["stringData"] == {VALUE_KEY: SECRET}
    assert objs["MCPExternalAuthConfig"]["spec"] == {
        "type": "headerInjection",
        "headerInjection": {
            "headerName": "X-API-Key",
            "valueSecretRef": {"name": name, "key": VALUE_KEY},
        },
    }
    assert entry_of(fake, inst)["spec"]["externalAuthConfigRef"] == {"name": name}
    vmcp = vmcp_of(fake, inst["k8s"]["name"])
    outgoing = vmcp["spec"]["outgoingAuth"]
    assert outgoing["source"] == "inline", outgoing
    workload = inst["deployed_backends"][0]["workload"]
    assert outgoing["backends"][workload]["externalAuthConfigRef"] == {"name": name}
    annotations = vmcp["spec"]["podTemplateSpec"]["metadata"]["annotations"]
    assert annotations[OUTSIDE_HEADER_REV_ANNOTATION] == item["auth"]["rev"]
    # 토큰을 내지 않는다. 헤더 사본은 토큰 판에 들지 않는다
    assert annotations[SVC_TOKEN_REV_ANNOTATION] == "0"
    # 매니페스트와 응답에는 값이 없다
    manifest = client.get(f"{GW}/{inst['id']}/manifest")
    assert manifest.status_code == 200 and SECRET not in manifest.text
    assert SECRET not in str(mongo.gateways.find_one({"_id": inst["id"]}))


def test_gateways_without_header_items_render_as_before(client, env):
    _item, fake = env
    plain = client.post(
        HUB,
        json={"name": "plain", "kind": "entry", "url": OUTSIDE_URL, "transport": "streamable-http"},
    ).json()
    inst = header_gateway(client, plain, name="gw-p")
    assert "auth_kind" not in inst["deployed_backends"][0]
    vmcp = vmcp_of(fake, inst["k8s"]["name"])
    assert (
        OUTSIDE_HEADER_REV_ANNOTATION
        not in vmcp["spec"]["podTemplateSpec"]["metadata"]["annotations"]
    )
    assert "externalAuthConfigRef" not in entry_of(fake, inst)["spec"]


def test_a_new_value_rerenders_the_consumer_and_rolls_its_pod(client, env):
    item, fake = env
    inst = header_gateway(client, item)
    before = vmcp_of(fake, inst["k8s"]["name"])["spec"]["podTemplateSpec"]["metadata"]
    r = client.put(f"{HUB}/{item['id']}/auth/header", json={"value": "Bearer replaced"})
    assert r.status_code == 200, r.text
    assert inst["id"] in str(run_data_plane_auth(client))
    assert copy_objects(fake, inst)["Secret"]["stringData"] == {VALUE_KEY: "Bearer replaced"}
    after = vmcp_of(fake, inst["k8s"]["name"])["spec"]["podTemplateSpec"]["metadata"]
    assert (
        after["annotations"][OUTSIDE_HEADER_REV_ANNOTATION]
        != before["annotations"][OUTSIDE_HEADER_REV_ANNOTATION]
    )


def test_a_tenant_outside_the_consume_boundary_loses_its_copy(client, env):
    item, fake = env
    mine = header_gateway(client, item)
    theirs = header_gateway(client, item, name="gw-t2", headers=T2)
    assert copy_objects(fake, theirs, OTHER_NS)["Secret"] is not None
    narrowed = {**SHARED_ACCESS, "consume": {"tenants": []}, "view": {"tenants": []}}
    r = client.put(f"{HUB}/{item['id']}/access", json=narrowed)
    assert r.status_code == 200, r.text
    run_data_plane_auth(client)
    assert copy_objects(fake, theirs, OTHER_NS) == {"Secret": None, "MCPExternalAuthConfig": None}
    assert "externalAuthConfigRef" not in entry_of(fake, theirs, OTHER_NS)["spec"]
    # 주인 테넌트의 Gateway 는 그대로 받는다
    assert copy_objects(fake, mine)["Secret"]["stringData"] == {VALUE_KEY: SECRET}


def test_a_missing_value_renders_without_the_header_and_warns(client, env):
    item, fake = env
    store = client.app.state.cluster_service.secret_store()
    store.delete(managed_name(item["id"]), namespace=client.app.state.settings.namespace)
    inst = header_gateway(client, item)
    assert copy_objects(fake, inst) == {"Secret": None, "MCPExternalAuthConfig": None}
    assert "externalAuthConfigRef" not in entry_of(fake, inst)["spec"]
    status = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert any(
        w.startswith("outside_header_missing:") and "outside" in w for w in status["warnings"]
    )


def test_rotation_and_the_token_sweep_keep_the_copy(client, fake_clusters):
    """토큰 회전(`fresh=True`)과 스윕의 정리는 헤더 사본을 토큰으로 보지 않는다. 같은 Gateway 에
    토큰을 받는 내부 백엔드를 두어 `issue(fresh=True)` 가 헤더 Entry 옆에서 돈다."""
    from tests.helpers.hub import hub_env

    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    client.app.state.hub_service.tool_lister = lister
    item = client.post(HUB, json=HEADER_ITEM).json()
    backends = [{"hub_item_id": items["calculator"]["id"]}, {"hub_item_id": item["id"]}]
    inst = gateway_up(client, items, name="gw-rot", backends=backends)
    svc = client.app.state.gateway_service
    doc = client.portal.call(svc.gateways.get, inst["id"])
    before = doc.svc_token_rev
    assert client.portal.call(svc.rotate_svc_token, doc)
    doc = client.portal.call(svc.gateways.get, inst["id"])
    assert doc.svc_token_rev != before
    assert copy_objects(fake, inst)["Secret"]["stringData"] == {VALUE_KEY: SECRET}
    fake.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": inst["k8s"]["name"], "namespace": DEFAULT_NS, "generation": 1},
            "spec": {"replicas": 1, "template": {"metadata": {}}},
        }
    )
    fake.set_status(
        "apps/v1",
        "Deployment",
        inst["k8s"]["name"],
        DEFAULT_NS,
        {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1, "availableReplicas": 1},
    )
    client.portal.call(svc.entry_tokens.settle, doc, fake)
    assert copy_objects(fake, inst)["Secret"] is not None


def test_a_redeploy_without_the_item_drops_its_copy(client, fake_clusters):
    """`_drop_left_out_headers`: 항목을 뺀 리비전을 재배포하면 그 사본이 곧바로 없고, 남은 백엔드는
    그대로 렌더된다."""
    from tests.helpers.hub import hub_env

    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    client.app.state.hub_service.tool_lister = lister
    item = client.post(HUB, json=HEADER_ITEM).json()
    calc = {"hub_item_id": items["calculator"]["id"]}
    inst = gateway_up(client, items, name="gw-drop", backends=[calc, {"hub_item_id": item["id"]}])
    assert copy_objects(fake, inst)["Secret"] is not None
    r = client.put(f"{GW}/{inst['id']}", json={"name": "gw-drop", "backends": [calc]})
    assert r.status_code == 200, r.text
    assert client.post(f"{GW}/{inst['id']}/redeploy").status_code == 200
    assert copy_objects(fake, inst) == {"Secret": None, "MCPExternalAuthConfig": None}
    tools = vmcp_of(fake, inst["k8s"]["name"])["spec"]["config"]["aggregation"]["tools"]
    assert [t["workload"] for t in tools] == ["gw-drop-calculator"]


def test_copies_of_a_failed_gateway_go_when_its_tenant_leaves_the_boundary(client, env, mongo):
    """재적용이 렌더하지 않는 Gateway(여기서는 failed)의 사본도 리컨실의 한 걸음이 지운다 (S3)."""
    item, fake = env
    theirs = header_gateway(client, item, name="gw-t2f", headers=T2)
    assert copy_objects(fake, theirs, OTHER_NS)["Secret"] is not None
    mongo.gateways.update_one({"_id": theirs["id"]}, {"$set": {"state": "failed"}})
    svc = client.app.state.gateway_service
    assert client.portal.call(svc.drop_stray_header_copies) == {}
    narrowed = {**SHARED_ACCESS, "consume": {"tenants": []}, "view": {"tenants": []}}
    assert client.put(f"{HUB}/{item['id']}/access", json=narrowed).status_code == 200
    assert client.portal.call(svc.drop_stray_header_copies) == {"dropped": [theirs["id"]]}
    assert copy_objects(fake, theirs, OTHER_NS) == {"Secret": None, "MCPExternalAuthConfig": None}


def test_the_status_survives_an_unreachable_home_cluster(client, env, monkeypatch):
    """`outside_header_missing` 는 값이 있는지만 보고, home 에 연결하지 못해도 상태 GET 은 200
    이다."""
    from daegam.k8s.client import ClusterUnreachable

    item, _fake = env
    inst = header_gateway(client, item)
    hub = client.app.state.hub_service

    async def down(_item_id):
        raise ClusterUnreachable("home is down")

    monkeypatch.setattr(hub.outside_headers, "exists", down)
    r = client.get(f"{GW}/{inst['id']}")
    assert r.status_code == 200, r.text


def test_undeploy_removes_the_copy(client, env):
    item, fake = env
    inst = header_gateway(client, item)
    assert client.post(f"{GW}/{inst['id']}/undeploy").status_code == 200
    assert copy_objects(fake, inst) == {"Secret": None, "MCPExternalAuthConfig": None}


def test_earlier_snapshot_rows_ignore_the_auth_kind():
    """앞 판의 스냅샷 모델은 모르는 칸을 버린다 (6.2절, 2.0.0-rc1 다음 후보부터의 바닥)."""
    row = {
        "hub_item_id": "h",
        "hub_item_name": "outside",
        "kind": "entry",
        "workload": "gw-outside",
        "url": OUTSIDE_URL,
        "auth_kind": "header",
    }
    from pydantic import create_model

    older = create_model(
        "OlderSnapshot",
        __config__=GatewayBackendSnapshot.model_config,
        **{
            name: (f.annotation, f)
            for name, f in GatewayBackendSnapshot.model_fields.items()
            if name != "auth_kind"
        },
    )
    assert "auth_kind" not in older.model_validate(row).model_dump()
    assert GatewayBackendSnapshot.model_validate(row).auth_kind == "header"


def test_gateway_up_still_works_for_token_backends(client, fake_clusters):
    """헤더 사본을 다루는 렌더가 토큰을 받는 Entry 의 렌더를 바꾸지 않는다."""
    from tests.helpers.hub import hub_env

    _cid, items, _calc, _orders, _remote, fake = hub_env(client, fake_clusters)
    inst = gateway_up(client, items, name="gw-tok")
    vmcp = vmcp_of(fake, inst["k8s"]["name"])
    assert (
        OUTSIDE_HEADER_REV_ANNOTATION
        not in vmcp["spec"]["podTemplateSpec"]["metadata"]["annotations"]
    )
