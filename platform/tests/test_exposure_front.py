"""노출한 MCP Server 의 vMCP 앞단 (P54 5·7절).

새 노출은 게이트를 지나는 클러스터에서 vMCP 앞단 `front-<k8s.name>` 으로 열리고, 게이트 아래에서는
그 게이트의 422 다. 게이트 아래로 내려간 클러스터의 노출은 옛 앞단으로 돌고 fallbacks 에 든다.
Entry 이름은 서버가 선점하고(D82) 노출을 끄거나 내리거나 지우면 놓는다. 옛 앞단이 도는 서버는 그
앞단이 Ingress 를 쥐고, vMCP 앞단의 몫은 서버 뒤에 오며 실패해도 렌더를 실패로 만들지 않는다.
"""

from __future__ import annotations

import json
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from daegam.errors import ServiceError
from daegam.k8s.gates import VMCP_GATES
from daegam.k8s.labels import LABEL_ID, LABEL_PROFILE_REVISION
from daegam.k8s.templates import render_entry
from daegam.services.data_plane_auth import front_rev, own_audience_rev, redis_tls_rev
from daegam.services.exposure import RESOURCE_EXPOSE_PROXY
from daegam.services.exposure_front import (
    BACKENDS_HEALTH_PATH,
    FRONT_HANDOVER_PENDING,
    SWITCHED_AT_ANNOTATION,
    FrontState,
    Step,
    available_for,
    backend_status,
    due_step,
    handover_warning,
    stamped_mark,
)
from daegam.services.tenancy import mcp_group_manifest
from daegam.store.base import iso_utc, parse_iso_utc, utcnow
from daegam.store.gateway_entry_names import entry_claim_id
from tests.helpers.clusters import downgrade_toolhive, revoke_tenant_rights
from tests.helpers.consts import DEFAULT_NS, GV, MS, NP_API
from tests.helpers.exposure import cluster_with_ingress, exposed_server, prep_ingress
from tests.helpers.http import detail_code
from tests.helpers.mcp_servers import clone_and_deploy
from tests.helpers.svc_tokens import claims_of, plant_key
from tests.helpers.workers import fallbacks, run_data_plane_auth

ENTRY_NAMES = "gateway_entry_names"
RBAC = "rbac.authorization.k8s.io/v1"
SVC_TOKEN_REV = "daegam.devops.ai.kr/svc-token-rev"
EXPOSURE = {"enabled": True, "host": "calc.e2e.local"}


def _old_front(fake, server: dict) -> None:
    """앞 릴리스가 만든 옛 앞단 MCPRemoteProxy `expose-<k8s.name>` 을 두고 Ingress 를 그 앞단의
    Service 로 돌린다. 앞 릴리스의 렌더가 남긴 자리다."""
    k8s = server["k8s"]["name"]
    _point_ingress(fake, server, f"mcp-expose-{k8s}-remote-proxy", 8080)
    fake.apply(
        {
            "apiVersion": GV,
            "kind": "MCPRemoteProxy",
            "metadata": {
                "name": f"expose-{k8s}",
                "namespace": DEFAULT_NS,
                "labels": {
                    "daegam.devops.ai.kr/resource": RESOURCE_EXPOSE_PROXY,
                    LABEL_ID: server["id"],
                },
            },
            "spec": {"remoteUrl": "http://old"},
        }
    )


def _point_ingress(fake, server: dict, service: str, port: int, **annotations: str) -> None:
    """Ingress 의 모든 경로를 service:port 로 돌리고 annotations 를 얹는다."""
    ing = fake.get(NP_API, "Ingress", server["k8s"]["name"], DEFAULT_NS)
    for path in ing["spec"]["rules"][0]["http"]["paths"]:
        path["backend"] = {"service": {"name": service, "port": {"number": port}}}
    ing["metadata"].setdefault("annotations", {}).update(annotations)
    fake.apply(ing)


def _ingress_service(fake, server: dict) -> str:
    ing = fake.get(NP_API, "Ingress", server["k8s"]["name"], DEFAULT_NS)
    return ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"]


def _claims(mongo) -> dict[str, str]:
    return {d["name"]: d["resource_id"] for d in mongo[ENTRY_NAMES].find()}


def _redeploy(client, server: dict) -> None:
    r = client.post(f"{MS}/{server['id']}/redeploy")
    assert r.status_code == 200, r.text


# -- 이름 ------------------------------------------------------------------------------------


def test_a_32_character_server_name_keeps_every_front_name_within_63(client, fake_clusters):
    name = "f0-front-name-length-is-32-chars"
    assert len(name) == 32
    server, _cid, fake = exposed_server(client, fake_clusters, name=name)
    front = f"front-mcp-{name}"
    for kind in ("VirtualMCPServer", "MCPGroup", "MCPServerEntry", "MCPExternalAuthConfig"):
        assert fake.get(GV, kind, front, DEFAULT_NS) is not None, kind
    assert fake.get(GV, "MCPAuthzConfig", f"{front}-authz", DEFAULT_NS) is not None
    assert _ingress_service(fake, server) == f"vmcp-{front}"
    # ToolHive 가 그 vMCP 에서 짓는 가장 긴 이름(F0.1)도 63자 안이다
    derived = [f"{front}-authz", f"vmcp-{front}", f"{front}-vmcp-config", f"{front}-vmcp"]
    assert max(len(n) for n in derived) == 54


def test_the_front_timeout_is_the_longest_backend_timeout_plus_five_without_the_cap(
    client, fake_clusters
):
    """Gateway 백엔드의 기본값은 300초에서 멈추지만 vMCP 앞단은 서버가 먼저 끝내도록 5초를 더해
    상한 없이 쓴다 (P54 5절)."""
    _cid, bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    backend = {"backend_profile_id": bp["id"], "spec_version": 1, "timeout_seconds": 300}
    r = client.put(f"{MS}/{mp['id']}", json={"name": mp["name"], "backends": [backend]})
    assert r.status_code == 200, r.text
    r = clone_and_deploy(client, r.json(), name="slow", exposure=EXPOSURE)
    assert r.status_code == 201, r.text
    vmcp = fake_clusters.by_name["edge"].get(GV, "VirtualMCPServer", "front-mcp-slow", DEFAULT_NS)
    timeouts = vmcp["spec"]["config"]["operational"]["timeouts"]
    assert timeouts == {"perWorkload": {"front-mcp-slow": "305s"}}


def test_a_cluster_without_ingress_annotations_exposes_through_the_vmcp_front(
    client, fake_clusters
):
    """Cluster 의 ingress.annotations 는 비어 있을 수 있다(기본값 None). 노출의 Ingress 는 그래도
    렌더되고 vMCP 앞단으로 간다."""
    _cid, _bp, _ds, mp = cluster_with_ingress(client, annotations=None)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    r = clone_and_deploy(client, mp, name="plain", exposure=EXPOSURE)
    assert r.status_code == 201, r.text
    assert client.get(f"{MS}/{r.json()['id']}").json()["state"] == "deployed"
    assert _ingress_service(fake, r.json()) == "vmcp-front-mcp-plain"


def test_redeploying_the_server_leaves_the_vmcp_front_pods_alone(client, fake_clusters):
    """서버의 리비전 라벨은 vMCP 앞단의 CR 에만 단다. 앞단에 닿지 않는 변경으로 서버를 새 리비전에
    다시 배포해도 앞단의 파드 템플릿이 그대로라 앞단이 굴러가지 않는다 (P54 5절)."""
    _cid, bp, _ds, mp = cluster_with_ingress(client)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    r = clone_and_deploy(client, mp, exposure=EXPOSURE)
    assert r.status_code == 201, r.text
    server = r.json()
    vmcp = fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    template = vmcp["spec"]["podTemplateSpec"]
    assert vmcp["metadata"]["labels"][LABEL_PROFILE_REVISION] == "1"
    assert LABEL_PROFILE_REVISION not in template["metadata"]["labels"]
    body = {"name": server["name"], "backend_profile_id": bp["id"], "spec_version": 1}
    r = client.put(f"{MS}/{server['id']}", json={**body, "tools": ["Add", "Divide"]})
    assert r.status_code == 200, r.text
    _redeploy(client, server)
    vmcp = fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    assert vmcp["metadata"]["labels"][LABEL_PROFILE_REVISION] == "2"
    assert vmcp["spec"]["podTemplateSpec"] == template


# -- 게이트 ----------------------------------------------------------------------------------


def test_a_new_exposure_below_the_front_gates_answers_422_naming_the_gate(
    client, fake_clusters, mongo
):
    cid, _bp, _ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    downgrade_toolhive(mongo, cid, "0.51.1")
    r = clone_and_deploy(client, mp, name="old", exposure=EXPOSURE)
    assert detail_code(r) == (422, "toolhive_too_old")
    assert r.json()["detail"]["feature"] == VMCP_GATES[0] == "audit"
    downgrade_toolhive(mongo, cid, "0.53.0")
    revoke_tenant_rights(mongo, cid)
    r = clone_and_deploy(client, mp, name="rights", exposure=EXPOSURE)
    assert detail_code(r) == (422, "cluster_rights_missing")
    assert r.json()["detail"]["feature"] == "authz_ref"
    # Basic 편집으로 노출을 켜는 것도 새 노출이다
    r = clone_and_deploy(client, mp, name="plain")
    assert r.status_code == 201, r.text
    r = client.patch(f"{MS}/{r.json()['id']}", json={"exposure": EXPOSURE})
    assert detail_code(r) == (422, "cluster_rights_missing")


def test_a_front_below_the_gates_keeps_the_old_front_and_counts_among_fallbacks(
    client, fake_clusters, mongo
):
    server, cid, fake = exposed_server(client, fake_clusters)
    downgrade_toolhive(mongo, cid, "0.51.1")
    # 게이트 판정이 바뀌어 렌더 번호가 달라지므로 재적용이 다시 렌더한다
    assert server["id"] in run_data_plane_auth(client)["reapplied"]
    proxy = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    assert proxy is not None and proxy["spec"]["externalAuthConfigRef"] == {
        "name": "svc-expose-mcp-calc-1"
    }
    assert _ingress_service(fake, server) == "mcp-expose-mcp-calc-1-remote-proxy"
    np = fake.get(NP_API, "NetworkPolicy", "daegam-expose-mcp-calc-1", DEFAULT_NS)
    assert np["spec"]["podSelector"] == {
        "matchLabels": {
            "app.kubernetes.io/instance": "expose-mcp-calc-1",
            "app.kubernetes.io/name": "mcpremoteproxy",
        }
    }
    counts = fallbacks(client, cid)
    assert {"audit", "timeouts", "static_backends"} <= counts.keys()
    assert counts["audit"] == 1
    # 그 클러스터에서 다시 렌더되지 않을 vMCP 앞단은 지우고 Entry 이름을 놓는다. 두면 옛 Cedar 와
    # 앞단 토큰으로 계속 돈다 (P54 5절)
    for kind in ("VirtualMCPServer", "MCPGroup", "MCPServerEntry", "MCPExternalAuthConfig"):
        assert fake.get(GV, kind, "front-mcp-calc-1", DEFAULT_NS) is None, kind
    assert fake.get(GV, "MCPAuthzConfig", "front-mcp-calc-1-authz", DEFAULT_NS) is None
    assert _claims(mongo) == {}


# -- Entry 이름 -----------------------------------------------------------------------------


def test_the_front_entry_name_is_held_by_the_server_and_refused_to_a_gateway(
    client, fake_clusters, mongo
):
    server, cid, _fake = exposed_server(client, fake_clusters)
    assert _claims(mongo) == {"front-mcp-calc-1": server["id"]}
    # 같은 이름을 만들려는 Gateway 는 그 선점에서 409 이고 서버를 id 로 댄다
    gateways = client.app.state.gateway_service
    gw = SimpleNamespace(
        id="gw-x", tenant="default", cluster_id=cid, k8s=SimpleNamespace(namespace=DEFAULT_NS)
    )
    with pytest.raises(ServiceError) as err:
        client.portal.call(gateways.entries.claim_names, gw, ["front-mcp-calc-1"])
    assert (err.value.status, err.value.code) == (409, "k8s_name_conflict")
    assert server["id"] in err.value.detail
    # 다시 배포해도 자기 기록을 다시 선점할 뿐이다
    _redeploy(client, server)
    assert _claims(mongo) == {"front-mcp-calc-1": server["id"]}


def test_exposure_off_undeploy_and_delete_release_the_entry_name(client, fake_clusters, mongo):
    server, _cid, fake = exposed_server(client, fake_clusters)
    sid = server["id"]
    r = client.patch(f"{MS}/{sid}", json={"exposure": {"enabled": False}})
    assert r.status_code == 200, r.text
    assert _claims(mongo) == {}
    assert fake.get(GV, "MCPServerEntry", "front-mcp-calc-1", DEFAULT_NS) is None
    assert client.patch(f"{MS}/{sid}", json={"exposure": EXPOSURE}).status_code == 200
    assert _claims(mongo) == {"front-mcp-calc-1": sid}
    assert client.post(f"{MS}/{sid}/undeploy").status_code == 200
    assert _claims(mongo) == {}
    r = client.post(f"{MS}/{sid}/deploy", json={"exposure": EXPOSURE})
    assert r.status_code == 201, r.text
    assert _claims(mongo) == {"front-mcp-calc-1": sid}
    assert client.delete(f"{MS}/{sid}").status_code == 200
    assert _claims(mongo) == {}


def test_the_entry_sweep_counts_an_exposed_servers_front_as_in_use(client, fake_clusters, mongo):
    server, _cid, _fake = exposed_server(client, fake_clusters)
    sweep = client.app.state.gateway_service.entries.sweep
    later = utcnow() + timedelta(hours=1)
    assert client.portal.call(sweep, later) == {"released": []}
    # 노출을 끈 기록만 남은 서버(놓기 전에 멈춘 자리)의 선점은 쓸기가 놓는다
    mongo["mcp_servers"].update_one({"_id": server["id"]}, {"$set": {"exposure.enabled": False}})
    released = client.portal.call(sweep, later)["released"]
    assert released == [entry_claim_id(server["cluster_id"], DEFAULT_NS, "front-mcp-calc-1")]


def test_a_fresh_exposure_stops_at_an_entry_of_another_document(client, fake_clusters, mongo):
    """Ingress 가 가리킬 vMCP 앞단의 Entry 를 다른 문서가 쓰고 있으면 서버의 렌더가 409 로
    멈춘다. 남의 Entry 를 덮어쓰지 않는다."""
    cid, _bp, _ds, mp = cluster_with_ingress(client)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    fake.apply(
        render_entry(
            tenant="default",
            instance_id="gw-other",
            name="front-mcp-calc-1",
            namespace=DEFAULT_NS,
            group="gw-front",
            url="http://x.daegam-default.svc.cluster.local:8080/mcp",
            transport="streamable-http",
            hub_item_id="hi-x",
        )
    )
    r = clone_and_deploy(client, mp, exposure=EXPOSURE)
    assert detail_code(r) == (409, "k8s_name_conflict"), r.text
    entry = fake.get(GV, "MCPServerEntry", "front-mcp-calc-1", DEFAULT_NS)
    assert entry["metadata"]["labels"][LABEL_ID] == "gw-other"
    # 막힌 렌더는 자기가 넣은 선점을 지운다. 남기면 그 Entry 를 쥔 Gateway 의 다음 배포가 이 서버에
    # 막힌다
    assert _claims(mongo) == {}


def test_a_tenant_group_of_the_same_name_stops_the_front_and_stays_untouched(
    client, fake_clusters, mongo
):
    """그룹 이름 `front-mcp-<name>` 은 그 이름의 테넌트 그룹과 겹칠 수 있다. 서버의 렌더는 그 그룹을
    덮어쓰거나 소유하지 않고 409 로 멈춘다 (P54 5절)."""
    cid, _bp, _ds, mp = cluster_with_ingress(client)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    fake.apply(mcp_group_manifest("front-mcp-calc-1", DEFAULT_NS))
    r = clone_and_deploy(client, mp, exposure=EXPOSURE)
    assert detail_code(r) == (409, "k8s_name_conflict"), r.text
    assert "MCPGroup" in r.json()["detail"]["detail"]
    group = fake.get(GV, "MCPGroup", "front-mcp-calc-1", DEFAULT_NS)
    assert group["metadata"]["labels"][LABEL_ID] == "front-mcp-calc-1"
    assert "ownerReferences" not in group["metadata"]
    assert _claims(mongo) == {}


# -- 옛 앞단이 도는 서버 ----------------------------------------------------------------------


def test_with_a_live_old_front_the_proxy_serves_and_the_vmcp_front_follows(client, fake_clusters):
    server, _cid, fake = exposed_server(client, fake_clusters)
    _old_front(fake, server)
    _redeploy(client, server)
    assert _ingress_service(fake, server) == "mcp-expose-mcp-calc-1-remote-proxy"
    proxy = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    assert proxy["spec"]["externalAuthConfigRef"] == {"name": "svc-expose-mcp-calc-1"}
    assert fake.get(GV, "VirtualMCPServer", "front-mcp-calc-1", DEFAULT_NS) is not None
    # Ingress 가 가리키는 옛 앞단이 서버보다 먼저이고 vMCP 앞단은 서버 뒤다
    start = len(fake.applied)
    _redeploy(client, server)
    kinds = [o["kind"] for o in fake.applied[start:]]
    assert (
        kinds.index("MCPRemoteProxy") < kinds.index("MCPServer") < kinds.index("VirtualMCPServer")
    )
    np = fake.get(NP_API, "NetworkPolicy", "daegam-expose-mcp-calc-1", DEFAULT_NS)
    assert "matchExpressions" in np["spec"]["podSelector"]


def test_the_vmcp_part_fails_soft_while_the_old_front_serves(client, fake_clusters, mongo, caplog):
    """vMCP 앞단의 몫이 실패해도(이름을 남이 쥐었다) 서버의 렌더는 성공하고 옛 앞단이 새 렌더로
    돈다 (P54 5절)."""
    server, cid, fake = exposed_server(client, fake_clusters)
    _old_front(fake, server)
    key = entry_claim_id(cid, DEFAULT_NS, "front-mcp-calc-1")
    mongo[ENTRY_NAMES].update_one({"_id": key}, {"$set": {"resource_id": "gw-other"}})
    fake.delete(GV, "VirtualMCPServer", "front-mcp-calc-1", DEFAULT_NS)
    _redeploy(client, server)
    assert fake.get(GV, "VirtualMCPServer", "front-mcp-calc-1", DEFAULT_NS) is None
    proxy = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    assert proxy["spec"]["remoteUrl"].startswith("http://mcp-mcp-calc-1-proxy.")
    assert client.get(f"{MS}/{server['id']}").json()["state"] == "deployed"
    assert "the other exposure front was not applied: k8s_name_conflict" in caplog.text


def test_turning_exposure_off_removes_both_fronts_but_not_a_foreign_group(
    client, fake_clusters, mongo
):
    server, _cid, fake = exposed_server(client, fake_clusters)
    _old_front(fake, server)
    _redeploy(client, server)
    # 이름이 `front-mcp-calc-1` 인 테넌트의 그룹 자리. 서버의 라벨이 없으면 지우지 않는다
    group = fake.get(GV, "MCPGroup", "front-mcp-calc-1", DEFAULT_NS)
    group["metadata"]["labels"][LABEL_ID] = "tenant-group"
    fake.apply(group)
    r = client.patch(f"{MS}/{server['id']}", json={"exposure": {"enabled": False}})
    assert r.status_code == 200, r.text
    assert fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS) is None
    assert fake.get(GV, "MCPExternalAuthConfig", "svc-expose-mcp-calc-1", DEFAULT_NS) is None
    assert fake.get("v1", "Secret", "svc-expose-mcp-calc-1", DEFAULT_NS) is None
    for kind in ("VirtualMCPServer", "MCPServerEntry", "MCPExternalAuthConfig"):
        assert fake.get(GV, kind, "front-mcp-calc-1", DEFAULT_NS) is None, kind
    assert fake.get(GV, "MCPGroup", "front-mcp-calc-1", DEFAULT_NS) is not None


# -- 넘겨받기 (P54 6절) -----------------------------------------------------------------------

FRONT = "front-mcp-calc-1"
PROXY_SERVICE = "mcp-expose-mcp-calc-1-remote-proxy"
VMCP_SERVICE = "vmcp-front-mcp-calc-1"


def _handing_over(client, fake_clusters):
    """옛 앞단이 Ingress 를 쥔 노출 서버. vMCP 앞단은 다시 배포가 서버 뒤에 적용했다."""
    server, cid, fake = exposed_server(client, fake_clusters)
    _old_front(fake, server)
    _redeploy(client, server)
    return server, cid, fake


def _ready_front(fake, *, available_since=None) -> None:
    """오퍼레이터가 vMCP 앞단의 지금 세대를 받아 Ready 이고 그 Deployment 가 다 굴러갔다. 가짜는
    같은 문서를 다시 적용해도 세대를 올리므로 렌더한 뒤에 다시 부른다."""
    since = available_since or utcnow()
    vmcp = fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    status = {"observedGeneration": vmcp["metadata"]["generation"], "phase": "Ready"}
    fake.set_status(GV, "VirtualMCPServer", FRONT, DEFAULT_NS, status)
    deployment = fake.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": FRONT, "namespace": DEFAULT_NS},
            "spec": {"replicas": 1},
        }
    )
    status = {
        "observedGeneration": deployment["metadata"]["generation"],
        "replicas": 1,
        "updatedReplicas": 1,
        "availableReplicas": 1,
        "conditions": [
            {"type": "Available", "status": "True", "lastTransitionTime": iso_utc(since)}
        ],
    }
    fake.set_status("apps/v1", "Deployment", FRONT, DEFAULT_NS, status)


def _health_app(answers: dict):
    """vMCP 의 `/api/backends/health` 를 흉내 낸다. answers["status"] 가 백엔드의 상태이고, 받은
    경로를 answers["calls"] 에 남긴다."""

    async def app(scope, receive, send):
        answers["calls"].append(scope["path"])
        if scope["path"] != BACKENDS_HEALTH_PATH:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
            return
        body = {"monitoring_enabled": True, "backends": {FRONT: {"Status": answers["status"]}}}
        headers = [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})

    return app


def _doc(client, server: dict):
    return client.portal.call(client.app.state.mcp_servers.get, server["id"])


def _stamped(client, server: dict) -> Step | None:
    """문서에 적힌 판이 실은 넘겨받기 단계."""
    service = client.app.state.mcp_server_service
    doc = _doc(client, server)
    ts = client.portal.call(service.tenancy.tenant_settings.get, doc.tenant)
    base = client.portal.call(service._base_rev, doc, ts)
    unfronted = own_audience_rev(redis_tls_rev(base, doc.redis_ca), doc.audience_switch)
    return stamped_mark(doc.auth_rev, unfronted)


def test_the_handover_waits_while_the_vmcp_front_is_not_ready(client, fake_clusters):
    server, _cid, fake = _handing_over(client, fake_clusters)
    assert _ingress_service(fake, server) == PROXY_SERVICE
    assert fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS) is not None
    assert _stamped(client, server) is Step.WAIT
    # 클러스터도 wait 를 보이므로 틱마다 다시 렌더하지 않는다
    assert run_data_plane_auth(client)["reapplied"] == []


def test_the_switch_waits_until_the_vmcp_front_routes_the_backend(client, fake_clusters):
    server, _cid, fake = _handing_over(client, fake_clusters)
    answers = {"status": "unknown", "calls": []}
    fake.service_app = _health_app(answers)
    _ready_front(fake)
    # 다 굴러간 앞단은 switch 를 기대하게 해 틱마다 한 번 렌더하고 상태를 한 번 읽는다
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert answers["calls"] == [BACKENDS_HEALTH_PATH]
    assert _ingress_service(fake, server) == PROXY_SERVICE
    assert _stamped(client, server) is Step.WAIT
    _ready_front(fake)
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert len(answers["calls"]) == 2
    # 읽지 못한 것도 기다린다
    fake.service_app = None
    _ready_front(fake)
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert _ingress_service(fake, server) == PROXY_SERVICE
    # degraded 는 라우팅한다. Ingress 를 돌리고 그 때를 적는다
    answers["status"] = "degraded"
    fake.service_app = _health_app(answers)
    _ready_front(fake)
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    ing = fake.get(NP_API, "Ingress", "mcp-calc-1", DEFAULT_NS)
    assert _ingress_service(fake, server) == VMCP_SERVICE
    switched = parse_iso_utc(ing["metadata"]["annotations"][SWITCHED_AT_ANNOTATION])
    assert switched is not None and utcnow() - switched < timedelta(seconds=10)
    assert _stamped(client, server) is Step.DRAIN
    # 클러스터가 drain 을 보이므로 다음 틱은 렌더하지 않는다. 옛 앞단은 그대로 있다
    assert run_data_plane_auth(client)["reapplied"] == []
    assert fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS) is not None


def test_a_switch_render_that_changes_the_vmcp_front_waits_for_the_new_pod(client, fake_clusters):
    """switch 를 볼 렌더가 vMCP 앞단을 바꾸면(여기서는 토큰 회전의 판 어노테이션) 아직 옛 파드가
    답하는 상태로 넘기지 않고, 새 파드가 다 굴러간 뒤의 렌더가 넘긴다 (P54 6절)."""
    server, _cid, fake = _handing_over(client, fake_clusters)
    answers = {"status": "healthy", "calls": []}
    fake.service_app = _health_app(answers)
    sweeper = client.app.state.reconciler.svc_token_sweeper
    tenant_only = client.app.state.internal_issuer.mint(sub="svc:x", tenant="default", groups=[])
    plant_key(fake, "svc-expose-mcp-calc-1", "token", tenant_only)
    _ready_front(fake)
    assert client.portal.call(lambda: sweeper.run())["renewed"] == [server["id"]]
    assert answers["calls"] == []
    assert _ingress_service(fake, server) == PROXY_SERVICE
    _ready_front(fake)
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert answers["calls"] == [BACKENDS_HEALTH_PATH]
    assert _ingress_service(fake, server) == VMCP_SERVICE


def test_a_missing_vmcp_front_is_rendered_again(client, fake_clusters):
    """넘겨받는 동안 vMCP 앞단의 VirtualMCPServer 가 지워지면 재적용이 그 서버를 다시 렌더해 앞단을
    만든다. 그 단계(missing)는 렌더가 적지 않으므로 적힌 표시와 같아지지 않는다 (P54 6절)."""
    server, _cid, fake = _handing_over(client, fake_clusters)
    fake.delete(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS) is not None
    assert _stamped(client, server) is Step.WAIT
    assert run_data_plane_auth(client)["reapplied"] == []


def test_a_front_available_ten_minutes_without_routing_is_held_until_the_backoff(
    client, fake_clusters, mongo
):
    server, _cid, fake = _handing_over(client, fake_clusters)
    answers = {"status": "unhealthy", "calls": []}
    fake.service_app = _health_app(answers)
    _ready_front(fake, available_since=utcnow() - timedelta(minutes=11))
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert _stamped(client, server) is Step.HELD
    stored = mongo["mcp_servers"].find_one({"_id": server["id"]})
    assert stored["auth_failures"] == 1 and stored["auth_retry_at"] is None
    # 백오프 동안은 held 를 기대해 읽지 않는다
    assert run_data_plane_auth(client)["reapplied"] == []
    assert len(answers["calls"]) == 1
    # 그 사이의 인증 변경은 미루지 않는다. 도는 옛 앞단에 곧바로 닿는다
    patch = {"agent_client_ids": ["bot-1"]}
    assert client.patch("/tenants/default/settings", json=patch).status_code == 200
    _ready_front(fake, available_since=utcnow() - timedelta(minutes=11))
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert len(answers["calls"]) == 2
    assert _stamped(client, server) is Step.HELD
    # 백오프가 지나면 다시 읽고, 또 라우팅하지 않으면 두 배로 미룬다
    _ready_front(fake, available_since=utcnow() - timedelta(minutes=11))
    later = utcnow() + timedelta(minutes=11)
    assert run_data_plane_auth(client, now=later)["reapplied"] == [server["id"]]
    assert len(answers["calls"]) == 3
    stored = mongo["mcp_servers"].find_one({"_id": server["id"]})
    assert stored["auth_failures"] == 3
    assert _ingress_service(fake, server) == PROXY_SERVICE


def test_a_failed_vmcp_part_holds_the_handover_and_the_old_front_serves(
    client, fake_clusters, mongo
):
    server, cid, fake = _handing_over(client, fake_clusters)
    key = entry_claim_id(cid, DEFAULT_NS, FRONT)
    mongo[ENTRY_NAMES].update_one({"_id": key}, {"$set": {"resource_id": "gw-other"}})
    _redeploy(client, server)
    assert _stamped(client, server) is Step.HELD
    assert mongo["mcp_servers"].find_one({"_id": server["id"]})["auth_failures"] == 1
    assert _ingress_service(fake, server) == PROXY_SERVICE


def test_the_drain_keeps_the_old_front_and_the_switch_time(client, fake_clusters):
    server, _cid, fake = _handing_over(client, fake_clusters)
    when = iso_utc(utcnow())
    _point_ingress(fake, server, VMCP_SERVICE, 4483, **{SWITCHED_AT_ANNOTATION: when})
    fake.delete(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    _old_front(fake, server)
    _point_ingress(fake, server, VMCP_SERVICE, 4483, **{SWITCHED_AT_ANNOTATION: when})
    _redeploy(client, server)
    ing = fake.get(NP_API, "Ingress", "mcp-calc-1", DEFAULT_NS)
    assert _ingress_service(fake, server) == VMCP_SERVICE
    assert ing["metadata"]["annotations"][SWITCHED_AT_ANNOTATION] == when
    # 옛 앞단을 렌더한다 (앞 릴리스가 남긴 꼴이 아니라 지금 렌더의 것이다)
    proxy = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    assert proxy["spec"]["externalAuthConfigRef"] == {"name": "svc-expose-mcp-calc-1"}
    assert _stamped(client, server) is Step.DRAIN


def test_after_the_drain_the_old_front_goes_and_the_secret_stays(client, fake_clusters):
    server, _cid, fake = _handing_over(client, fake_clusters)
    _runner_binding(fake)
    # 서버의 backend 타임아웃(30초)보다 오래전에 바꿨다
    when = iso_utc(utcnow() - timedelta(seconds=31))
    _point_ingress(fake, server, VMCP_SERVICE, 4483, **{SWITCHED_AT_ANNOTATION: when})
    assert run_data_plane_auth(client)["reapplied"] == [server["id"]]
    assert fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS) is None
    assert fake.get(GV, "MCPExternalAuthConfig", "svc-expose-mcp-calc-1", DEFAULT_NS) is None
    assert (
        fake.get(RBAC, "RoleBinding", "expose-mcp-calc-1-remote-proxy-runner", DEFAULT_NS) is None
    )
    assert fake.get("v1", "Secret", "svc-expose-mcp-calc-1", DEFAULT_NS) is not None
    ing = fake.get(NP_API, "Ingress", "mcp-calc-1", DEFAULT_NS)
    assert SWITCHED_AT_ANNOTATION not in (ing["metadata"].get("annotations") or {})
    assert _ingress_service(fake, server) == VMCP_SERVICE
    assert _stamped(client, server) is None
    assert run_data_plane_auth(client)["reapplied"] == []


def test_handover_marks_are_paced_not_urgent(client, fake_clusters):
    """넘겨받기의 표시만 다른 판은 회수가 아니라 네임스페이스마다의 속도로 간다."""
    server, _cid, _fake = _handing_over(client, fake_clusters)
    service = client.app.state.mcp_server_service
    doc = _doc(client, server)
    ts = client.portal.call(service.tenancy.tenant_settings.get, doc.tenant)
    paced = client.portal.call(service.paced_revs, doc, ts)
    base = client.portal.call(service._base_rev, doc, ts)
    unfronted = own_audience_rev(redis_tls_rev(base, doc.redis_ca), doc.audience_switch)
    assert {front_rev(unfronted, step) for step in Step} <= paced


def _runner_binding(fake) -> None:
    """P50 앞에 오퍼레이터가 옛 앞단마다 만들던 RoleBinding. 그 앞단이 소유한다."""
    owner = {"apiVersion": GV, "kind": "MCPRemoteProxy", "name": "expose-mcp-calc-1", "uid": "u"}
    fake.apply(
        {
            "apiVersion": RBAC,
            "kind": "RoleBinding",
            "metadata": {
                "name": "expose-mcp-calc-1-remote-proxy-runner",
                "namespace": DEFAULT_NS,
                "ownerReferences": [owner],
            },
            "roleRef": {"kind": "Role", "name": "expose-mcp-calc-1-remote-proxy-runner"},
        }
    )


def test_due_step_reads_the_cluster_and_the_drain_follows_the_backend_timeout():
    now = utcnow()
    ready = {
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 2,
            "replicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    vmcp = {"metadata": {"generation": 4}, "status": {"observedGeneration": 4, "phase": "Ready"}}
    due = partial(due_step, drain=timedelta(seconds=120), now=now)
    assert due(FrontState(old=False)) is None
    # VirtualMCPServer 가 없으면 렌더가 다시 만든다. 적은 표시와 같지 않아 다시 렌더된다
    assert due(FrontState(old=True)) is Step.MISSING
    assert due(FrontState(old=True, vmcp=vmcp)) is Step.WAIT
    assert due(FrontState(old=True, vmcp=vmcp, deployment=ready)) is Step.SWITCH
    # 오퍼레이터가 지금 세대를 아직 받지 않았거나 그 스펙이 거절됐으면(Failed) 다 굴러간 것이 아니다
    behind = {**vmcp, "status": {"observedGeneration": 3, "phase": "Ready"}}
    assert due(FrontState(old=True, vmcp=behind, deployment=ready)) is Step.WAIT
    failed = {**vmcp, "status": {"observedGeneration": 4, "phase": "Failed"}}
    assert due(FrontState(old=True, vmcp=failed, deployment=ready)) is Step.WAIT
    young = iso_utc(now - timedelta(seconds=100))
    assert due(FrontState(old=True, on_vmcp=True, switched=young)) is Step.DRAIN
    old = iso_utc(now - timedelta(seconds=121))
    assert due(FrontState(old=True, on_vmcp=True, switched=old)) is Step.DELETE
    # 바꾼 때를 모르는 Ingress 는 drain 을 지난 것으로 본다
    assert due(FrontState(old=True, on_vmcp=True)) is Step.DELETE


def test_backend_status_reads_the_one_backend_of_the_health_body():
    assert backend_status({"backends": {FRONT: {"Status": "degraded"}}}, FRONT) == "degraded"
    assert backend_status({"backends": {"other-id": {"Status": "healthy"}}}, FRONT) == "healthy"
    assert backend_status({"backends": {}}, FRONT) is None
    assert backend_status({"backends": {FRONT: {"Status": 3}}}, FRONT) is None
    assert backend_status("nope", FRONT) is None
    now = utcnow()
    deployment = {
        "status": {
            "conditions": [
                {
                    "type": "Available",
                    "status": "True",
                    "lastTransitionTime": iso_utc(now - timedelta(minutes=12)),
                }
            ]
        }
    }
    assert available_for(deployment, now) >= timedelta(minutes=12)
    assert available_for(None, now) == timedelta(0)


# -- 상태와 API (P54 11절) ---------------------------------------------------------------------


def _warnings(client, server: dict) -> list[str]:
    return client.get(f"{MS}/{server['id']}").json()["status"]["warnings"]


def _pending(client, server: dict) -> list[str]:
    return [w for w in _warnings(client, server) if w.startswith(FRONT_HANDOVER_PENDING)]


def test_the_server_warns_while_the_old_front_holds_the_ingress(client, fake_clusters, mongo):
    server, cid, fake = _handing_over(client, fake_clusters)
    # vMCP 앞단이 아직 status 를 쓰지 않았다
    assert _pending(client, server) == [handover_warning("Pending")]
    assert _pending(client, server)[0].endswith("(reason: Pending)")
    assert handover_warning("Not ready!").endswith("(reason: Unknown)")
    # Ready 인데 그 CR 이 본 백엔드가 라우팅하지 않는 상태면 그 상태다
    status = {"phase": "Ready", "discoveredBackends": [{"name": FRONT, "status": "unknown"}]}
    fake.set_status(GV, "VirtualMCPServer", FRONT, DEFAULT_NS, status)
    assert _pending(client, server) == [handover_warning("unknown")]
    # 이름을 남이 쥐었으면 그 까닭이다
    key = entry_claim_id(cid, DEFAULT_NS, FRONT)
    mongo[ENTRY_NAMES].update_one({"_id": key}, {"$set": {"resource_id": "gw-other"}})
    assert _pending(client, server) == [handover_warning("name_taken")]
    mongo[ENTRY_NAMES].update_one({"_id": key}, {"$set": {"resource_id": server["id"]}})
    fake.delete(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    assert _pending(client, server) == [handover_warning("Missing")]
    # front_phase 는 Ingress 가 가리키는 옛 앞단의 것이다
    fake.set_status(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS, {"phase": "Ready"})
    st = client.get(f"{MS}/{server['id']}").json()["status"]
    assert st["exposure"]["front_phase"] == "Ready"
    # Ingress 를 vMCP 앞단으로 돌린 뒤에는 경고하지 않는다
    _point_ingress(fake, server, VMCP_SERVICE, 4483, **{SWITCHED_AT_ANNOTATION: iso_utc(utcnow())})
    assert _pending(client, server) == []
    st = client.get(f"{MS}/{server['id']}").json()["status"]
    assert st["exposure"]["front_phase"] == "Missing"


def test_an_invalid_front_authz_config_is_the_reason(client, fake_clusters):
    server, _cid, fake = _handing_over(client, fake_clusters)
    authz = fake.get(GV, "MCPAuthzConfig", f"{FRONT}-authz", DEFAULT_NS)
    authz["status"]["conditions"] = [{"type": "Valid", "status": "False", "message": "bad"}]
    fake.set_status(GV, "MCPAuthzConfig", f"{FRONT}-authz", DEFAULT_NS, authz["status"])
    assert _pending(client, server) == [handover_warning("authz_invalid")]


def test_one_status_read_reads_the_ingress_once(client, fake_clusters, monkeypatch):
    """노출 상태와 앞단의 경고는 상태 조회 하나에서 Ingress 를 한 번 읽어 함께 쓴다 (P54 7·11절)."""
    server, _cid, fake = _handing_over(client, fake_clusters)
    reads: list[str] = []
    get = fake.get

    def counted(api_version, kind, name, namespace=None):
        reads.append(kind)
        return get(api_version, kind, name, namespace)

    monkeypatch.setattr(fake, "get", counted)
    assert _pending(client, server) == [handover_warning("Pending")]
    assert reads.count("Ingress") == 1


def test_the_manifest_answers_while_the_cluster_is_unreachable(client, fake_clusters):
    """클러스터를 읽지 못해도 manifest 는 서버 문서와 두 앞단이 함께 있는 노출 묶음을 보인다."""
    server, _cid, fake = _handing_over(client, fake_clusters)
    fake.unreachable = True
    r = client.get(f"{MS}/{server['id']}/manifest")
    assert r.status_code == 200, r.text
    kinds = [d["kind"] for d in r.json()["documents"]]
    assert kinds[0] == "MCPServer"
    assert {"MCPRemoteProxy", "VirtualMCPServer", "Ingress"} <= set(kinds)


def test_a_fresh_vmcp_front_never_warns(client, fake_clusters):
    server, _cid, _fake = exposed_server(client, fake_clusters)
    assert _pending(client, server) == []


def test_the_data_plane_status_counts_handovers_per_step_and_cluster(client, fake_clusters):
    server, cid, fake = _handing_over(client, fake_clusters)
    run_data_plane_auth(client)
    body = client.get("/settings/data-plane-auth").json()
    assert body["handovers"] == {cid: {"wait": 1}}
    _ready_front(fake)
    body = client.get("/settings/data-plane-auth").json()
    assert body["handovers"] == {cid: {"switch": 1}}
    fake.delete(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    assert client.get("/settings/data-plane-auth").json()["handovers"] == {}
    assert server["id"]


def test_rotating_the_front_token_rolls_both_fronts_during_a_handover(client, fake_clusters):
    """두 앞단은 토큰 Secret 하나를 함께 쓴다. 회전은 Ingress 가 가리키는 옛 앞단과 vMCP 앞단을
    같은 판으로 굴린다 (P54 5절)."""
    server, _cid, fake = _handing_over(client, fake_clusters)
    sweeper = client.app.state.reconciler.svc_token_sweeper
    tenant_only = client.app.state.internal_issuer.mint(sub="svc:x", tenant="default", groups=[])
    plant_key(fake, "svc-expose-mcp-calc-1", "token", tenant_only)
    assert client.portal.call(lambda: sweeper.run())["renewed"] == [server["id"]]
    token = fake.get("v1", "Secret", "svc-expose-mcp-calc-1", DEFAULT_NS)["stringData"]["token"]
    rev = claims_of(token)["jti"][:8]
    proxy = fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS)
    vmcp = fake.get(GV, "VirtualMCPServer", FRONT, DEFAULT_NS)
    for cr in (proxy, vmcp):
        assert cr["spec"]["podTemplateSpec"]["metadata"]["annotations"][SVC_TOKEN_REV] == rev


def test_the_rolling_rules_warning_reads_the_front_the_ingress_names(local_admin, fake_clusters):
    """규칙이 굴러가는 경고는 Ingress 가 가리키는 앞단의 Deployment 를 본다 (P54 7절)."""
    _cid, bp, _ds, mp = cluster_with_ingress(local_admin)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    backend = {
        "backend_profile_id": bp["id"],
        "spec_version": 1,
        "tool_rules": {"Add": {"callers": {"roles": ["developer"]}}},
    }
    r = local_admin.put(f"{MS}/{mp['id']}", json={"name": mp["name"], "backends": [backend]})
    assert r.status_code == 200, r.text
    r = clone_and_deploy(local_admin, r.json(), exposure=EXPOSURE)
    assert r.status_code == 201, r.text
    server = r.json()
    _ready_front(fake)
    deployment = fake.get("apps/v1", "Deployment", FRONT, DEFAULT_NS)
    deployment["status"]["updatedReplicas"] = 0
    fake.set_status("apps/v1", "Deployment", FRONT, DEFAULT_NS, deployment["status"])
    warnings = local_admin.get(f"{MS}/{server['id']}").json()["status"]["warnings"]
    assert any(w.startswith("rules_front_rolling") for w in warnings)
