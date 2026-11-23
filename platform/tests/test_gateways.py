"""Gateway (D42): 정의와 vMCP 배포가 한 리소스다. 만들기는 draft, 배포는 따로 누른다.

정의(만들기·검증·리비전·미리보기·복제)와 첫 배포를 본다. fake 렌더 스냅샷(그룹·
MCPExternalAuthConfig·Entry·vMCP), gw- 이름, 명시 conflict_resolution, oidc 모드의 published 422,
서비스 토큰이 가는 곳(D41), 배포 전 백엔드 준비 확인, 순환, ownerReference 다. 배포한 뒤(재배포·
해제·삭제·상태·경합)는 test_gateways_lifecycle.py, 게시는 test_gateways_publish.py, vMCP 의 사람 쪽
인증 렌더는 test_vmcp_render.py 가 본다.
"""

from __future__ import annotations

from daegam.services.instance_status import ROLLOUT_WATCH
from daegam.services.mcp_servers import MCPSERVER_GENERATION_ANNOTATION
from daegam.store.hub import HubItem, HubK8s, HubSource
from tests.helpers.clusters import default_cluster, register_cluster
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB, MS, T2, TIME_MARGIN
from tests.helpers.deployments import deployment, put_deployment
from tests.helpers.docs import age
from tests.helpers.gateways import (
    SURFACE_OFF,
    create_gateway,
    deploy_gateway,
    failure_mode,
    gateway_up,
    vmcp_of,
)
from tests.helpers.http import deployed, detail_code
from tests.helpers.hub import SPIKE_URL, hub_env
from tests.helpers.svc_tokens import entry_object_name

# -- 정의: 만들기·검증·리비전 ---------------------------------------------------------------


def test_create_is_a_draft_with_validation_and_revisions(client, fake_clusters, mongo):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    r = create_gateway(client, items, name="gw-a")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "draft" and p["revision"] == 1 and p["deployed_revision"] is None
    assert p["failure_mode"] == "best_effort" and p["conflict_resolution"] == "auto"
    assert p["auth"] == "anonymous" and p["runtime"] == {"replicas": 1, "resources": None}
    calculator = {"hub_item_id": items["calculator"]["id"], "tools": ["Add"]}
    # 타임아웃을 정하지 않은 백엔드는 배포가 기본값을 정한다 (P50 7절)
    assert p["backends"][0] == {**calculator, "timeout_seconds": None}
    assert p["deployed_backends"] == [] and p["deployed_conflict_resolution"] == "priority"
    assert p["cluster_id"] is None and p["k8s"] is None and p["status"] is None
    assert p["exposure"]["enabled"] is False
    assert p["warnings"] == [] and "llm" not in p and "bindings" not in p
    # 클러스터에는 아무것도 없다. 목록에는 draft 도 든다
    assert vmcp_of(fake, "gw-gw-a") is None and fake.find("MCPServerEntry", DEFAULT_NS) == []
    listed = client.get(GW).json()
    assert [d["id"] for d in listed] == [p["id"]] and listed[0]["status"] is None
    assert deployed(client, GW) == []
    # 배포된 것만 받는 동작은 draft 에서 409 not_deployed
    for path in ("manifest",):
        r = client.get(f"{GW}/{p['id']}/{path}")
        assert detail_code(r) == (409, "not_deployed")
    for method, path, body in (
        ("post", "redeploy", None),
        ("post", "undeploy", None),
        ("post", "publish", {}),
        ("put", "overrides", {"patch": {}}),
        ("patch", "", {"exposure": {"enabled": False}}),
        ("post", "drift/reset", None),
    ):
        url = f"{GW}/{p['id']}/{path}" if path else f"{GW}/{p['id']}"
        r = (
            getattr(client, method)(url, json=body)
            if body is not None
            else getattr(client, method)(url)
        )
        assert detail_code(r) == (409, "not_deployed"), (path, r.text)
    # tools ⊄ tool_names → 422
    r = create_gateway(
        client,
        items,
        name="bad",
        backends=[{"hub_item_id": items["calculator"]["id"], "tools": ["Nope"]}],
    )
    assert detail_code(r) == (422, "unknown_tools")
    assert r.json()["detail"]["available"] == ["Add", "Divide"]
    # tool_names 가 비어 있는 remote 에 필터를 걸면 경고만
    r = create_gateway(
        client, items, name="warn", backends=[{"hub_item_id": remote["id"], "tools": ["x"]}]
    )
    assert r.status_code == 201 and "refresh-tools" in r.json()["warnings"][0]
    # 없는 허브 항목, 중복, 빈 backends, 타 테넌트, 옛 Agent 필드 거부
    assert (
        create_gateway(client, items, name="u", backends=[{"hub_item_id": "nope"}]).json()[
            "detail"
        ]["code"]
        == "unknown_hub_item"
    )
    assert (
        create_gateway(
            client,
            items,
            name="d",
            backends=[{"hub_item_id": remote["id"]}, {"hub_item_id": remote["id"]}],
        ).json()["detail"]["code"]
        == "duplicate_backend"
    )
    assert (
        create_gateway(client, items, name="e", backends=[]).json()["detail"]["code"]
        == "no_backends"
    )
    r = client.post(
        GW,
        json={"name": "x", "backends": [{"hub_item_id": remote["id"]}]},
        headers=T2,
    )
    assert detail_code(r) == (422, "unknown_hub_item")
    assert create_gateway(client, items, name="gw-a").status_code == 409
    assert create_gateway(client, items, name="llm", llm={"mode": "stub"}).status_code == 422
    assert create_gateway(client, items, name="cr", conflict_resolution="magic").status_code == 422
    assert (
        create_gateway(client, items, name="rt", runtime={"replicas": 0}).status_code == 422
    )  # ge=1
    # 리비전 (runtime·conflict_resolution 왕복). 저장은 draft 를 유지한다
    r = client.put(
        f"{GW}/{p['id']}",
        json={
            "name": "gw-a",
            "failure_mode": "fail",
            "conflict_resolution": "prefix",
            "runtime": {"replicas": 2, "resources": {"limits": {"cpu": "1"}}},
            "backends": [{"hub_item_id": items["calculator"]["id"], "tools": ["Divide"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2 and r.json()["conflict_resolution"] == "prefix"
    assert r.json()["state"] == "draft" and r.json()["deployed_revision"] is None
    assert r.json()["runtime"] == {"replicas": 2, "resources": {"limits": {"cpu": "1"}}}
    assert client.get(f"{GW}/{p['id']}/revisions/1").json()["backends"][0]["tools"] == ["Add"]
    rev2 = client.get(f"{GW}/{p['id']}/revisions/2").json()
    assert rev2["failure_mode"] == "fail" and rev2["runtime"]["replicas"] == 2
    assert [x["revision"] for x in client.get(f"{GW}/{p['id']}/revisions").json()] == [1, 2]
    assert mongo["gateways"].find_one({"_id": p["id"]})["revision"] == 2
    assert mongo["agents"].count_documents({}) == 0
    # 허브 항목 삭제는 draft 정의가 참조해도 409 (gateway_ids)
    r = client.delete(f"{HUB}/{remote['id']}")
    assert detail_code(r) == (409, "in_use")
    assert r.json()["detail"]["gateway_ids"]
    # draft 삭제는 문서만 지운다
    r = client.delete(f"{GW}/{p['id']}")
    assert r.status_code == 200 and r.json() == {
        "id": p["id"],
        "deleted": True,
        "state_before": "draft",
        "k8s": None,
    }
    assert client.get(f"{GW}/{p['id']}").status_code == 404


def test_preview_from_draft_renders_group_entries_vmcp(client, fake_clusters):
    """fake 렌더 스냅샷: MCPGroup gw-<name>, Entry 래퍼, VirtualMCPServer. 배포 전에도 된다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(
        client, items, runtime={"replicas": 2, "resources": {"limits": {"memory": "256Mi"}}}
    ).json()
    r = client.get(f"{GW}/{p['id']}/revisions/1/preview")
    assert r.status_code == 200, r.text
    cr = r.json()["manifest"]
    assert cr["kind"] == "VirtualMCPServer" and cr["metadata"]["name"] == "gw-a"
    labels = cr["metadata"]["labels"]
    assert labels["daegam.devops.ai.kr/resource"] == "gateway-instance"
    assert labels["daegam.devops.ai.kr/gateway-profile"] == p["id"]
    assert labels["daegam.devops.ai.kr/profile-revision"] == "1"
    assert labels["daegam.devops.ai.kr/id"] == f"preview-{p['id']}"
    assert (
        "daegam.devops.ai.kr/agent-instance" not in labels
        and "daegam.devops.ai.kr/agent-profile" not in labels
    )
    agg = cr["spec"]["config"]["aggregation"]
    assert agg["tools"] == [
        {"workload": "a-calculator", "filter": ["Add"]},
        {"workload": "a-orders"},
    ]
    assert agg["defaultToolVisibility"] == "deny"
    # auto + 모든 백엔드의 tool_names 를 알므로 priority 모드
    assert agg["conflictResolution"] == "priority"
    assert agg["conflictResolutionConfig"] == {"priorityOrder": ["a-calculator", "a-orders"]}
    assert r.json()["conflict_resolution"] == "priority" and r.json()["tool_overrides"] == {}
    assert failure_mode(cr) == "best_effort"
    # 새 Gateway 는 resources·prompts 를 끈 채로 시작한다 (P50 13절). Cedar 는 vMCP 가 참조하는
    # MCPAuthzConfig 에 있다 (8절)
    assert cr["spec"]["incomingAuth"] == {
        "type": "anonymous",
        "authzConfigRef": {"name": "gw-a-authz"},
    }
    authz = next(d for d in r.json()["documents"] if d["kind"] == "MCPAuthzConfig")
    assert authz["metadata"]["name"] == "gw-a-authz"
    assert authz["spec"] == {
        "type": "cedarv1",
        "config": {"policies": ["permit(principal, action, resource);", *SURFACE_OFF]},
    }
    # 서비스 토큰은 vMCP 가 발견하지 않고 Entry 마다 적힌 설정으로 낸다 (12절, G5). Entry 마다 그
    # 서버 하나를 여는 토큰이다 (9절)
    assert cr["spec"]["outgoingAuth"] == {
        "source": "inline",
        "backends": {
            name: {
                "type": "externalAuthConfigRef",
                "externalAuthConfigRef": {"name": entry_object_name("gw-a", name)},
            }
            for name in ("a-calculator", "a-orders")
        },
    }
    assert cr["spec"]["groupRef"] == {"name": "gw-a"}
    assert cr["spec"]["replicas"] == 2
    # ToolHive 는 VirtualMCPServer 의 spec.resources 를 읽지 않는다. 리비전 자원은 podTemplateSpec
    # 의 vmcp 컨테이너로 간다 (D54)
    assert "resources" not in cr["spec"]
    assert cr["spec"]["podTemplateSpec"]["spec"]["containers"] == [
        {"name": "vmcp", "resources": {"limits": {"memory": "256Mi"}}}
    ]
    docs = r.json()["documents"]
    # 미리보기는 토큰 Secret 없이 렌더한다. MCPExternalAuthConfig(headerInjection)는 vMCP 가 내부
    # 백엔드에 낼 서비스 토큰의 자리이고 토큰을 받는 Entry 마다 하나다 (D41, P50 9절)
    assert [d["kind"] for d in docs] == [
        "MCPGroup",
        "MCPExternalAuthConfig",
        "MCPExternalAuthConfig",
        "MCPServerEntry",
        "MCPServerEntry",
        "MCPAuthzConfig",
        "VirtualMCPServer",
    ]
    group = docs[0]
    assert group["metadata"]["name"] == "gw-a"
    assert group["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "gateway-group"
    assert group["spec"] == {"description": "daegam gateway a (default)"}
    auth = docs[1]
    svc_name = entry_object_name(cr["metadata"]["name"], "a-calculator")
    assert auth["metadata"]["name"] == svc_name
    assert docs[2]["metadata"]["name"] == entry_object_name(cr["metadata"]["name"], "a-orders")
    assert auth["spec"] == {
        "type": "headerInjection",
        "headerInjection": {
            "headerName": "Authorization",
            "valueSecretRef": {"name": svc_name, "key": "authorization"},
        },
    }
    entry = docs[3]
    assert entry["metadata"]["name"] == "a-calculator"
    assert entry["spec"] == {
        "remoteUrl": "http://mcp-mcp-calculator-proxy.daegam-default.svc.cluster.local:8080/mcp",
        "transport": "streamable-http",
        "allowPrivateEndpoint": True,
        "groupRef": {"name": "gw-a"},
        # 우리 MCPServer(internal) 로 가는 래퍼라 토큰을 단다
        "externalAuthConfigRef": {"name": svc_name},
    }
    assert entry["metadata"]["labels"]["daegam.devops.ai.kr/hub-item"] == items["calculator"]["id"]
    assert entry["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "gateway-backend"
    assert [b["ready"] for b in r.json()["backends"]] == [True, True]
    assert "sessionStorage" not in cr["spec"] and "sessionAffinity" not in cr["spec"]
    assert "spec.groupRef" in r.json()["locked"]
    assert deployed(client, GW) == [] and vmcp_of(fake, "gw-a") is None


def test_explicit_conflict_modes(client, fake_clusters):
    """auto 는 plan_conflicts 그대로. 명시 priority 는 tool_names 를 모르는 백엔드가 있어도 priority
    이고 경고를 남긴다. 명시 prefix 는 전부 알아도 prefix 다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    assert remote["tool_names"] == []
    mixed = [{"hub_item_id": items["calculator"]["id"]}, {"hub_item_id": remote["id"]}]
    # auto → prefix 폴백 + 경고
    p_auto = create_gateway(client, items, name="auto", backends=mixed).json()
    pv = client.get(f"{GW}/{p_auto['id']}/revisions/1/preview").json()
    assert pv["conflict_resolution"] == "prefix" and any("prefix mode" in w for w in pv["warnings"])
    # priority 명시 → 저장 응답부터 경고, 미리보기·배포도 priority 유지 + 경고
    r = create_gateway(client, items, name="prio", backends=mixed, conflict_resolution="priority")
    assert r.status_code == 201, r.text
    assert any("priority mode with unknown tool_names for spike" in w for w in r.json()["warnings"])
    pv = client.get(f"{GW}/{r.json()['id']}/revisions/1/preview").json()
    assert pv["conflict_resolution"] == "priority"
    assert pv["manifest"]["spec"]["config"]["aggregation"]["conflictResolution"] == "priority"
    assert any("priority mode with unknown" in w for w in pv["warnings"])
    inst = deploy_gateway(client, r.json()["id"])
    assert inst.status_code == 201, inst.text
    assert inst.json()["deployed_conflict_resolution"] == "priority"
    assert inst.json()["conflict_resolution"] == "priority"
    assert any("priority mode with unknown" in w for w in inst.json()["warnings"])
    assert vmcp_of(fake, "gw-prio")["spec"]["config"]["aggregation"][
        "conflictResolutionConfig"
    ] == {"priorityOrder": ["prio-calculator", "prio-spike"]}
    # prefix 명시 → 전부 알아도 prefix, override 없음, 경고 없음
    r = create_gateway(client, items, name="pfx", conflict_resolution="prefix")
    assert r.status_code == 201 and r.json()["warnings"] == []
    inst = deploy_gateway(client, r.json()["id"]).json()
    assert inst["deployed_conflict_resolution"] == "prefix" and inst["tool_overrides"] == {}
    assert inst["warnings"] == []
    agg = vmcp_of(fake, "gw-pfx")["spec"]["config"]["aggregation"]
    assert agg["conflictResolution"] == "prefix"
    assert agg["conflictResolutionConfig"] == {"prefixFormat": "{workload}_"}
    # priority 명시 + 충돌 툴 → override 만 (auto 와 같은 계산)

    async def lister(_client, url, bearer=None):
        return ["Add", "Multiply"]

    client.app.state.hub_service.tool_lister = lister
    assert client.post(f"{HUB}/{remote['id']}/refresh-tools").status_code == 200
    r = create_gateway(client, items, name="both", backends=mixed, conflict_resolution="priority")
    assert r.status_code == 201 and r.json()["warnings"] == []
    inst = deploy_gateway(client, r.json()["id"]).json()
    assert inst["tool_overrides"] == {
        "both-calculator": {"Add": "calculator_Add"},
        "both-spike": {"Add": "spike_Add"},
    }


def test_unknown_name_hints_match_the_kind():
    """파생 항목(internal·published)에는 도구 새로고침이 없다. 그 종류로 refresh-tools 를 부르면
    422 라, "새로고침하라" 고만 적으면 없는 버튼을 찾게 된다."""
    from daegam.services.gateway_plan import priority_unknown_warning, unknown_names_hint

    assert unknown_names_hint(["remote"]) == "run refresh-tools"
    assert "no refresh-tools" in unknown_names_hint(["internal"])
    assert "no refresh-tools" in unknown_names_hint(["published"])
    both = unknown_names_hint(["remote", "internal"])
    assert "run refresh-tools" in both and "redeploy the source" in both
    # 문구는 한 곳에서만 만든다. 갈라지면 같은 경고가 두 번 나간다
    w = priority_unknown_warning([("calc", "internal")])
    assert "priority mode with unknown tool_names for calc" in w
    assert "no refresh-tools" in w and "use prefix mode" in w


def test_unknown_resolution_falls_back_to_the_stored_names_not_to_empty():
    """`Origin.tool_names is None` 은 "모른다" 이고 "도구가 없다" 가 아니다 (D49). 빈 목록으로
    뭉개면 plan_conflicts 가 이름 규칙을 통째로 prefix 로 떨어뜨린다. 배포 스냅샷을 만드는
    _snapshot_backends 가 이미 저장된 값으로 떨어지므로 검증 경로도 같아야 한다."""
    from daegam.services.gateway_plan import tool_names_of
    from daegam.services.hub_tools import Origin
    from daegam.store.hub import HubItem, HubK8s, HubSource

    item = HubItem(
        id="h1",
        tenant="default",
        kind="internal",
        cluster_id="c1",
        name="calc",
        source=HubSource(),
        k8s=HubK8s(name="hub-calc", namespace=DEFAULT_NS),
        tool_names=["Add", "Divide"],
    )
    assert tool_names_of(item, {}) == ["Add", "Divide"]  # 해석 대상이 아니면 저장된 값
    assert tool_names_of(item, {"h1": Origin(tool_names=["Add"])}) == ["Add"]  # 해석되면 그것
    assert tool_names_of(item, {"h1": Origin(tool_names=None)}) == [
        "Add",
        "Divide",
    ]  # 모르면 저장된 값
    assert tool_names_of(item, {"h1": Origin(tool_names=[])}) == []  # 정말 없는 것은 빈 목록


def test_published_backend_rejected_in_oidc_mode(oidc_client, oidc_headers, fake_clusters, mongo):
    h = oidc_headers
    cid = default_cluster(oidc_client, headers=h)
    entry = {"name": "spike", "kind": "entry", "url": SPIKE_URL, "transport": "streamable-http"}
    hub = oidc_client.post(HUB, json=entry, headers=h).json()
    pub = HubItem(
        tenant="acme",
        name="pub",
        kind="published",
        cluster_id=cid,
        source=HubSource(
            gateway_instance_id="some-gateway",
            url="http://vmcp-gw-x.daegam-acme.svc.cluster.local:4483/mcp",
            transport="streamable-http",
        ),
        k8s=HubK8s(kind="VirtualMCPServer", name="gw-x", namespace="daegam-acme"),
    )
    mongo["hub_items"].insert_one(pub.to_doc())
    r = oidc_client.post(
        GW,
        json={"name": "bot", "backends": [{"hub_item_id": hub["id"]}, {"hub_item_id": pub.id}]},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "published_oidc_unsupported"
    assert r.json()["detail"]["hub_item_id"] == pub.id
    # entry 만이면 되고 응답 auth 는 oidc 다
    r = oidc_client.post(
        GW, json={"name": "bot", "backends": [{"hub_item_id": hub["id"]}]}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "oidc"
    spec = oidc_client.get(f"{GW}/{r.json()['id']}/revisions/1/preview", headers=h).json()[
        "manifest"
    ]["spec"]
    assert spec["incomingAuth"]["type"] == "oidc"
    assert spec["incomingAuth"]["oidcConfigRef"] == {
        "name": "daegam-oidc",
        "audience": "daegam-mcp",
    }


def test_service_token_goes_only_to_our_servers(client, fake_clusters, mongo):
    """D39: 토큰은 우리 MCPServer(internal·image)로 가는 래퍼에만 달린다. 사용자가 넣은 주소(remote·
    entry)로는 나가지 않는다. 게시된 vMCP 는 사람 경계라 header 모드에서 백엔드로 쓸 수 있지만
    서비스 토큰을 내지 않는다 (D41)."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    ia = gateway_up(client, items, name="a")
    pub = client.post(f"{GW}/{ia['id']}/publish", json={}).json()
    entry_item = client.post(
        HUB,
        json={
            "name": "calc-entry",
            "kind": "entry",
            "url": SPIKE_URL,
            "transport": "streamable-http",
        },
    ).json()
    p = create_gateway(
        client,
        items,
        name="mixed",
        backends=[
            {"hub_item_id": items["calculator"]["id"]},
            {"hub_item_id": remote["id"]},
            {"hub_item_id": entry_item["id"]},
            {"hub_item_id": pub["id"]},
        ],
    ).json()
    docs = client.get(f"{GW}/{p['id']}/revisions/1/preview").json()["documents"]
    refs = {
        d["metadata"]["name"]: (d["spec"].get("externalAuthConfigRef") or {}).get("name")
        for d in docs
        if d["kind"] == "MCPServerEntry"
    }
    svc = entry_object_name(docs[-1]["metadata"]["name"], "mixed-calculator")
    assert refs == {
        "mixed-calculator": svc,
        f"mixed-{remote['name']}": None,
        "mixed-calc-entry": None,
        f"mixed-{pub['name']}": None,
    }
    # 토큰은 우리 MCPServer 로 가는 Entry 에만 적는다. default 는 두지 않는다 (P50 12절)
    assert docs[-1]["spec"]["outgoingAuth"] == {
        "source": "inline",
        "backends": {
            "mixed-calculator": {
                "type": "externalAuthConfigRef",
                "externalAuthConfigRef": {"name": svc},
            }
        },
    }


# -- 배포 ---------------------------------------------------------------------------------


def test_deploy_requires_ready_backends_then_renders(client, fake_clusters, mongo):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(client, items).json()
    fake.set_status(GV, "MCPServer", "mcp-orders", DEFAULT_NS, {"phase": "Pending"})
    r = deploy_gateway(client, p["id"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "backends_not_ready"
    assert [b["ready"] for b in r.json()["detail"]["backends"]] == [True, False]
    # 배포 전 검증에서 걸리면 draft 그대로다
    assert client.get(f"{GW}/{p['id']}").json()["state"] == "draft" and deployed(client, GW) == []
    fake.set_status(GV, "MCPServer", "mcp-orders", DEFAULT_NS, {"phase": "Ready"})
    r = deploy_gateway(client, p["id"])
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["id"] == p["id"] and inst["state"] == "deployed"
    assert inst["deployed_revision"] == 1 and inst["revision"] == 1
    assert inst["k8s"] == {"name": "gw-a", "namespace": DEFAULT_NS, "group": "gw-a"}
    assert "gateway_profile_id" not in inst and "agent_profile_id" not in inst
    assert inst["exposure"] == {
        "enabled": False,
        "host": None,
        "ingress_class": None,
        "tls": {"mode": "none", "secret_name": None, "issuer": None},
    }
    assert inst["warnings"] == []
    assert [b["workload"] for b in inst["deployed_backends"]] == ["a-calculator", "a-orders"]
    group = fake.get(GV, "MCPGroup", "gw-a", DEFAULT_NS)
    assert group["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "gateway-group"
    assert group["metadata"]["labels"]["daegam.devops.ai.kr/id"] == inst["id"]
    entries = sorted(e["metadata"]["name"] for e in fake.find("MCPServerEntry", DEFAULT_NS))
    assert entries == ["a-calculator", "a-orders"]
    assert fake.get(GV, "MCPServerEntry", "a-orders", DEFAULT_NS)["spec"]["groupRef"] == {
        "name": "gw-a"
    }
    kinds = [
        o["kind"]
        for o in fake.applied
        if o["kind"] in ("MCPGroup", "MCPServerEntry", "VirtualMCPServer")
    ]
    # 그룹·Entry 를 먼저 만들고, vMCP 로 uid 를 얻은 뒤 ownerReference 를 얹어 재적용한다
    assert kinds[-7:] == [
        "MCPGroup",
        "MCPServerEntry",
        "MCPServerEntry",
        "VirtualMCPServer",
        "MCPGroup",
        "MCPServerEntry",
        "MCPServerEntry",
    ]
    st = inst["status"]
    assert st["phase"] == "Pending" and st["url"] == "http://vmcp-gw-a.daegam-default.svc:4483/mcp"
    assert st["external_url"] is None and st["warnings"] == []
    cr = vmcp_of(fake, "gw-a")
    assert cr["spec"]["config"]["aggregation"]["tools"] == [
        {"workload": "a-calculator", "filter": ["Add"]},
        {"workload": "a-orders"},
    ]
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/gateway-profile"] == p["id"]
    assert cr["metadata"]["labels"]["daegam.devops.ai.kr/id"] == inst["id"]
    doc = mongo["gateways"].find_one({"_id": inst["id"]})
    assert doc["state"] == "deployed" and doc["deployed_revision"] == 1
    assert mongo["agents"].count_documents({}) == 0
    assert [d["id"] for d in deployed(client, GW)] == [inst["id"]]

    # status 매핑: Ready + discoveredBackends, Degraded 는 60초 안이면 Rolling
    fake.set_status(
        GV,
        "VirtualMCPServer",
        "gw-a",
        DEFAULT_NS,
        {
            "phase": "Ready",
            "url": "http://vmcp-gw-a.daegam-default.svc.cluster.local:4483",
            "backendCount": 2,
            "observedGeneration": 1,
            "discoveredBackends": [
                {"name": "a-calculator", "status": "ready", "url": "u1"},
                {"name": "a-orders", "status": "ready"},
            ],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "lastTransitionTime": "2026-09-16T01:02:03Z",
                }
            ],
        },
    )
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["phase"] == "Ready"
    # 조건의 전이 시각도 넘어온다. MCP Server·Agent 와 같은 필드다 (D44 셋째)
    assert st["conditions"][0]["last_transition"] == "2026-09-16T01:02:03Z"
    assert st["url"] == "http://vmcp-gw-a.daegam-default.svc.cluster.local:4483/mcp"
    assert [b["status"] for b in st["backends"]] == ["ready", "ready"] and st["backend_count"] == 2
    fake.set_status(
        GV, "VirtualMCPServer", "gw-a", DEFAULT_NS, {"phase": "Degraded", "observedGeneration": 1}
    )
    assert client.get(f"{GW}/{inst['id']}").json()["status"]["phase"] == "Rolling"
    age(mongo, "gateways", inst["id"])
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["phase"] == "Degraded" and st["badge"] == "일부 백엔드 불가"
    # 화면은 코드로 화면 언어의 말을 그린다. 한국어 badge 는 비서가 읽으므로 그대로 둔다 (P49 6절)
    assert st["badge_code"] == "some_backends_down"
    assert st["expected_backends"] == 2
    # ToolHive 0.47+: 백엔드가 모두 죽으면 윗단은 Degraded 이고
    # 런타임 판정 Failed 는 status.runtime 에만 있다.
    # 백엔드 행은 런타임이 직접 쓴 목록을 먼저 읽는다
    fake.set_status(
        GV,
        "VirtualMCPServer",
        "gw-a",
        DEFAULT_NS,
        {
            "phase": "Degraded",
            "observedGeneration": 1,
            "backendCount": 0,
            "discoveredBackends": [{"name": "a-calculator", "status": "ready"}],
            "runtime": {
                "phase": "Failed",
                "discoveredBackends": [
                    {"name": "a-calculator", "status": "unavailable"},
                    {"name": "a-orders", "status": "unavailable"},
                ],
            },
        },
    )
    fake.set_status(GV, "MCPServerEntry", "a-calculator", DEFAULT_NS, {"phase": "Valid"})
    fake.set_status(GV, "MCPServerEntry", "a-orders", DEFAULT_NS, {"phase": "Valid"})
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["phase"] == "Degraded" and st["badge"] == "모든 백엔드 불가"
    assert st["badge_code"] == "all_backends_down"
    assert [b["status"] for b in st["backends"]] == ["unavailable", "unavailable"]

    # 배포된 것에 다시 배포하면 409, 클러스터 삭제 409, 이름 충돌 409
    r = deploy_gateway(client, p["id"])
    assert detail_code(r) == (409, "already_deployed")
    assert client.delete(f"/clusters/{cid}").json()["detail"]["code"] == "instances_exist"
    assert create_gateway(client, items, name="a").status_code == 409


def test_clone_copies_the_current_revision_and_keeps_exposure(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    src = create_gateway(client, items, name="src").json()
    client.put(
        f"{GW}/{src['id']}",
        json={
            "name": "src",
            "conflict_resolution": "prefix",
            "backends": [{"hub_item_id": items["calculator"]["id"], "tools": ["Divide"]}],
        },
    )
    r = client.post(
        f"{GW}/{src['id']}/clone",
        json={
            "name": "copy",
            "display_name": "복제본",
            "exposure": {"enabled": True, "host": "copy.e2e.local"},
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["id"] != src["id"] and c["state"] == "draft" and c["revision"] == 1
    assert c["display_name"] == "복제본" and c["conflict_resolution"] == "prefix"
    divide = {"hub_item_id": items["calculator"]["id"], "tools": ["Divide"]}
    assert c["backends"] == [{**divide, "timeout_seconds": None}]
    assert c["exposure"]["enabled"] is True and c["exposure"]["host"] == "copy.e2e.local"
    assert "warnings" in c
    # 같은 이름은 409, 원본은 그대로 draft r2
    assert client.post(f"{GW}/{src['id']}/clone", json={"name": "copy"}).status_code == 409
    assert client.get(f"{GW}/{src['id']}").json()["revision"] == 2


def test_deploy_failure_paths(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    p = create_gateway(client, items).json()
    assert deploy_gateway(client, "nope").status_code == 404
    assert create_gateway(client, items, name="bad name").json()["detail"]["code"] == "invalid_name"
    assert client.post(GW, json={"name": "x", "agent_profile_id": p["id"]}).status_code == 422
    other = register_cluster(client, "c2")
    r = deploy_gateway(client, p["id"], cluster_id=other)
    assert detail_code(r) == (422, "cluster_mismatch")
    fake.unreachable = True
    r = deploy_gateway(client, p["id"])
    assert detail_code(r) == (409, "backends_not_ready")
    assert client.get(f"{GW}/{p['id']}").json()["state"] == "draft"
    fake.unreachable = False
    inst = deploy_gateway(client, p["id"]).json()
    fake.unreachable = True
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["phase"] is None and st["error"].startswith("cluster_unreachable")
    r = client.delete(f"{GW}/{inst['id']}")
    assert r.status_code == 502
    assert deployed(client, GW)[0]["state"] == "deleting"


def test_entry_backend_precheck_uses_tools_list(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    e = client.post(
        HUB,
        json={
            "name": "ext",
            "kind": "entry",
            "url": "https://ext.example.com/mcp",
            "transport": "streamable-http",
        },
    ).json()
    p = create_gateway(client, items, name="ext-gw", backends=[{"hub_item_id": e["id"]}]).json()

    async def dead(_client, url, bearer=None):
        raise ConnectionError("refused")

    client.app.state.hub_service.tool_lister = dead
    r = deploy_gateway(client, p["id"])
    assert detail_code(r) == (409, "backends_not_ready")
    assert "unreachable" in r.json()["detail"]["backends"][0]["error"]

    async def alive(_client, url, bearer=None):
        assert url == "https://ext.example.com/mcp"
        return ["Ping"]

    client.app.state.hub_service.tool_lister = alive
    r = deploy_gateway(client, p["id"])
    assert r.status_code == 201, r.text
    assert r.json()["deployed_backends"][0]["tool_names"] == ["Ping"]
    assert r.json()["deployed_conflict_resolution"] == "priority"


def test_a_backend_server_in_rollout_stops_the_deploy_as_rolling(client, fake_clusters, mongo):
    """internal 백엔드는 원본 MCP Server 자신의 상태로 본다 (T55). 롤아웃 중이면 답하는 파드가
    있어도 409 backends_not_ready 이고, 백엔드 표와 허브 화면의 phase 는 MCP Server 화면과 같은
    Rolling 이다. 옛 파드가 답하는 동안 배포하면 스냅샷의 도구 이름(D49, 새 리비전)과 실제 서버가
    어긋날 수 있다. Ready 가 되면 배포된다. 지켜보는 시간이 지나면 `map_cr_status` 가 말하는
    대로다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    key = (GV, "MCPServer", "mcp-calculator", DEFAULT_NS)
    url = fake.get(*key)["status"]["url"]
    gen = fake.get(*key)["metadata"]["generation"]
    a = create_gateway(client, items).json()

    def calc_status(phase: str, observed: int, ready_replicas: int) -> None:
        st = {"phase": phase, "url": url, "observedGeneration": observed, "message": "m"}
        fake.set_status(*key, {**st, "readyReplicas": ready_replicas})

    def refused_as(gateway_id: str) -> str:
        r = deploy_gateway(client, gateway_id)
        assert detail_code(r) == (409, "backends_not_ready")
        row = next(b for b in r.json()["detail"]["backends"] if b["name"] == "calculator")
        assert row["ready"] is False
        hub_phase = client.get(f"{HUB}/{items['calculator']['id']}").json()["status"]["phase"]
        server_phase = client.get(f"{MS}/{calc['id']}").json()["status"]["phase"]
        assert row["phase"] == hub_phase == server_phase
        return row["phase"]

    # 오퍼레이터가 새 스펙을 아직 보지 않았다. 옛 파드가 답해도, 아무 파드가 없어도 Rolling 이다
    calc_status("Ready", gen - 1, 1)
    assert refused_as(a["id"]) == "Rolling"
    calc_status("Pending", gen - 1, 0)
    assert refused_as(a["id"]) == "Rolling"
    # 스펙을 바꾼 직후의 잠깐의 Failed (설정 오류 갈래라 지난 readyReplicas 가 남는다)
    calc_status("Failed", gen, 1)
    assert refused_as(a["id"]) == "Rolling"
    # 새 세대를 보았지만 프록시 Deployment 가 아직 그 세대로 굴러가지 않았다
    calc_status("Ready", gen, 1)
    proxy = {MCPSERVER_GENERATION_ANNOTATION: str(gen - 1)}
    put_deployment(fake, deployment("mcp-calculator", proxy))
    assert refused_as(a["id"]) == "Rolling"
    # 다 굴러갔다
    put_deployment(fake, deployment("mcp-calculator", {MCPSERVER_GENERATION_ANNOTATION: str(gen)}))
    r = deploy_gateway(client, a["id"])
    assert r.status_code == 201, r.text
    snap = next(b for b in r.json()["deployed_backends"] if b["hub_item_name"] == "calculator")
    assert snap["url"] == url

    # 유예가 지난 Failed 는 Failed 다. 오퍼레이터가 보지 않은 세대는 시간과 상관없이 Rolling 이다
    b = create_gateway(client, items, name="b").json()
    calc_status("Failed", gen, 1)
    age(mongo, "mcp_servers", calc["id"])
    assert refused_as(b["id"]) == "Failed"
    calc_status("Ready", gen - 1, 1)
    age(mongo, "mcp_servers", calc["id"], by=ROLLOUT_WATCH + TIME_MARGIN)
    assert refused_as(b["id"]) == "Rolling"


def test_too_many_backends_warns(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    ids = [
        client.post(
            HUB,
            json={
                "name": f"e{i}",
                "kind": "entry",
                "url": f"http://e{i}.svc/mcp",
                "transport": "streamable-http",
            },
        ).json()["id"]
        for i in range(21)
    ]
    r = create_gateway(client, items, name="many", backends=[{"hub_item_id": i} for i in ids])
    assert r.status_code == 201, r.text
    assert any("21 backends" in w for w in r.json()["warnings"])
