"""P8 F5: 인스턴스 외부 노출 (docs/guide/exposure.md, resource-model.md 11절, D22).

fake 렌더 스냅샷: MCP Instance http / secret / cert-manager (vMCP 앞단 front-<k8s.name> +
Ingress + NetworkPolicy, P54), Gateway secret (Ingress 대상 vmcp +
incomingAuth.oidcConfigRef.resourceUrl), header 모드 경고, allow_unauthenticated false 422,
검증 오류 코드, 삭제·재배포 정리, 상태 필드. host 유일성은 모든 클러스터를 넘고, 기동이 그 전에 생긴
중복을 알린다 (P33 6절).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from daegam.k8s.templates.common import proxy_resources
from daegam.services.exposure import (
    UNAUTHENTICATED_WARNING,
    ExposureError,
    ExposureInput,
    _resolve_host,
    normalize_host,
)
from daegam.store.base import utcnow
from daegam.store.clusters import ClusterIngress
from daegam.store.exposure import Exposure
from tests.helpers.backend_apis import CALC_ENDPOINT, uploaded_backend
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import DEFAULT_NS, GV, GW, MS, NP_API
from tests.helpers.docs import seed_agent, seed_gateway, seed_server
from tests.helpers.exposure import (
    INGRESS,
    cluster_secret,
    cluster_with_ingress,
    ingress_class,
    prep_ingress,
)
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.gateways import cedar_policies, create_gateway, deploy_gateway, vmcp_of
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.hub import hub_env
from tests.helpers.mcp_servers import clone_and_deploy


def inst_doc(client, inst_id: str) -> dict:
    return client.get(f"{MS}/{inst_id}").json()


# -- 단위 ----------------------------------------------------------------------------------


def test_host_normalization_and_external_url():
    assert normalize_host(" GW-A.E2E.Local. ") == "gw-a.e2e.local"
    for bad in ("", "-a.b", "a_b.c", "a..b", "x" * 64 + ".com"):
        try:
            normalize_host(bad)
        except Exception as e:  # noqa: BLE001
            assert getattr(e, "code", None) == "invalid_host"
        else:
            raise AssertionError(bad)
    assert Exposure().external_url is None and Exposure().scheme == "http"


def test_reserved_hosts_match_in_normal_form_without_validation():
    """예약 호스트는 요청 호스트와 같은 꼴로 맞춰 본다(P74 2.3절). 끝 점·공백·대문자로 적힌 항목도
    걸리고, 잘못 적힌 항목은 아무것도 막지 않는다. 다른 배포가 쓰는 host 는 409 host_conflict 다."""
    ing = ClusterIngress(enabled=True, reserved_hosts=[" Daegam.Example. ", "not a host!"])

    def resolve(host: str, taken: frozenset[str] = frozenset()) -> str:
        inp = ExposureInput(enabled=True, host=host)
        return _resolve_host(inp, tenant="t", name="n", ing=ing, taken=set(taken))

    with pytest.raises(ExposureError) as reserved:
        resolve("daegam.example")
    assert (reserved.value.status, reserved.value.code) == (422, "host_reserved")
    assert resolve("calc.example") == "calc.example"
    with pytest.raises(ExposureError) as taken:
        resolve("calc.example", frozenset({"calc.example"}))
    assert (taken.value.status, taken.value.code) == (409, "host_conflict")
    e = Exposure(enabled=True, host="gw.local", ingress_class="traefik")
    assert e.external_url == "http://gw.local/mcp"
    e = Exposure(enabled=True, host="gw.local", tls={"mode": "secret", "secret_name": "s"})
    assert e.external_url == "https://gw.local/mcp"


# -- MCP Instance: http (header 모드, 무인증 경고) -----------------------------------------------


def test_mcp_instance_http_exposure_renders_vmcp_front_ingress_policy(client, fake_clusters, mongo):
    cid, bp, ds, mp = cluster_with_ingress(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    r = clone_and_deploy(client, mp, exposure={"enabled": True, "host": "Calc.E2E.Local"})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"] == {
        "enabled": True,
        "host": "calc.e2e.local",
        "ingress_class": "traefik",
        "tls": {"mode": "none", "secret_name": None, "issuer": None},
    }
    assert inst["status"]["external_url"] == "http://calc.e2e.local/mcp"
    assert UNAUTHENTICATED_WARNING in inst["warnings"]
    assert UNAUTHENTICATED_WARNING in inst["status"]["warnings"]
    assert inst["k8s_uid"] == fake.get(GV, "MCPServer", "mcp-calc-1", DEFAULT_NS)["metadata"]["uid"]
    owner = {
        "apiVersion": GV,
        "kind": "MCPServer",
        "name": "mcp-calc-1",
        "uid": inst["k8s_uid"],
        "blockOwnerDeletion": False,
        "controller": False,
    }
    # 새 노출의 앞단은 vMCP 다 (P54 5절). 옛 앞단 MCPRemoteProxy 는 만들지 않는다
    assert fake.get(GV, "MCPRemoteProxy", "expose-mcp-calc-1", DEFAULT_NS) is None
    front = fake.get(GV, "VirtualMCPServer", "front-mcp-calc-1", DEFAULT_NS)
    rev = front["spec"]["podTemplateSpec"]["metadata"]["annotations"][
        "daegam.devops.ai.kr/svc-token-rev"
    ]
    assert len(rev) == 8 and rev == inst_doc(client, inst["id"])["svc_token_rev"]
    # header 모드: anonymous 와 permit-all 을 든 설정의 참조. Entry 하나를 거르지 않고 접두사 없이
    # 내고, 서버의 backend 타임아웃(30초)에 5초를 더한다
    assert front["spec"]["incomingAuth"] == {
        "type": "anonymous",
        "authzConfigRef": {"name": "front-mcp-calc-1-authz"},
    }
    assert cedar_policies(front, fake=fake) == ["permit(principal, action, resource);"]
    assert front["spec"]["outgoingAuth"] == {
        "source": "inline",
        "backends": {
            "front-mcp-calc-1": {
                "type": "externalAuthConfigRef",
                "externalAuthConfigRef": {"name": "front-mcp-calc-1"},
            }
        },
    }
    config = front["spec"]["config"]
    assert config["aggregation"] == {
        "conflictResolution": "priority",
        "conflictResolutionConfig": {"priorityOrder": ["front-mcp-calc-1"]},
        "defaultToolVisibility": "deny",
        "tools": [{"workload": "front-mcp-calc-1"}],
    }
    assert config["operational"]["timeouts"] == {"perWorkload": {"front-mcp-calc-1": "35s"}}
    assert config["audit"]["enabled"] is True
    assert front["spec"]["groupRef"] == {"name": "front-mcp-calc-1"}
    assert front["spec"]["podTemplateSpec"]["spec"]["containers"] == [
        {"name": "vmcp", "resources": proxy_resources()}
    ]
    labels = front["metadata"]["labels"]
    assert labels["daegam.devops.ai.kr/resource"] == "mcp-instance-front"
    assert labels["daegam.devops.ai.kr/id"] == inst["id"]
    assert labels["daegam.devops.ai.kr/profile-revision"] == "1"
    assert "daegam.devops.ai.kr/gateway-profile" not in labels
    assert front["metadata"]["ownerReferences"] == [owner]
    # Entry 는 서버의 프록시 Service 를 감싸고 헤더 주입 설정을 단다
    entry = fake.get(GV, "MCPServerEntry", "front-mcp-calc-1", DEFAULT_NS)
    assert entry["spec"] == {
        "remoteUrl": "http://mcp-mcp-calc-1-proxy.daegam-default.svc.cluster.local:8080/mcp",
        "transport": "streamable-http",
        "allowPrivateEndpoint": True,
        "groupRef": {"name": "front-mcp-calc-1"},
        "externalAuthConfigRef": {"name": "front-mcp-calc-1"},
    }
    assert "daegam.devops.ai.kr/hub-item" not in entry["metadata"]["labels"]
    group = fake.get(GV, "MCPGroup", "front-mcp-calc-1", DEFAULT_NS)
    assert group["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "mcp-instance-front"
    # 토큰 Secret 은 옛 앞단의 것 그대로이고, vMCP 앞단은 그 authorization 키를 헤더로 싣는다
    secret = fake.get("v1", "Secret", "svc-expose-mcp-calc-1", DEFAULT_NS)
    assert secret["metadata"]["ownerReferences"] == [owner]
    assert secret["stringData"]["authorization"] == "Bearer " + secret["stringData"]["token"]
    assert secret["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "mcp-expose-svc-token"
    ext = fake.get(GV, "MCPExternalAuthConfig", "front-mcp-calc-1", DEFAULT_NS)
    assert ext["spec"] == {
        "type": "headerInjection",
        "headerInjection": {
            "headerName": "Authorization",
            "valueSecretRef": {"name": "svc-expose-mcp-calc-1", "key": "authorization"},
        },
    }
    assert ext["metadata"]["ownerReferences"] == [owner]
    assert fake.get(GV, "MCPExternalAuthConfig", "svc-expose-mcp-calc-1", DEFAULT_NS) is None
    # Entry 이름은 이 서버가 선점한다 (D82)
    assert [d["resource_id"] for d in mongo["gateway_entry_names"].find()] == [inst["id"]]
    # Ingress
    ing = fake.get(NP_API, "Ingress", "mcp-calc-1", DEFAULT_NS)
    backend = {"service": {"name": "vmcp-front-mcp-calc-1", "port": {"number": 4483}}}
    # MCP 엔드포인트와 RFC 9728 메타데이터만 연다. 앞단의 /health 같은 경로는 밖에 없다 (P50 5절)
    assert ing["spec"] == {
        "ingressClassName": "traefik",
        "rules": [
            {
                "host": "calc.e2e.local",
                "http": {
                    "paths": [
                        {"path": "/mcp", "pathType": "Prefix", "backend": backend},
                        {
                            "path": "/.well-known/oauth-protected-resource",
                            "pathType": "Prefix",
                            "backend": backend,
                        },
                    ]
                },
            }
        ],
    }
    assert ing["metadata"]["annotations"] == {"nginx.ingress.kubernetes.io/proxy-buffering": "off"}
    assert ing["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "mcp-instance-ingress"
    assert ing["metadata"]["ownerReferences"] == [owner]
    # NetworkPolicy
    # NetworkPolicy 는 두 종류의 앞단 파드를 함께 고른다 (P54 5절)
    np = fake.get(NP_API, "NetworkPolicy", "daegam-expose-mcp-calc-1", DEFAULT_NS)
    assert np["spec"] == {
        "podSelector": {
            "matchExpressions": [
                {
                    "key": "app.kubernetes.io/instance",
                    "operator": "In",
                    "values": ["expose-mcp-calc-1", "front-mcp-calc-1"],
                },
                {
                    "key": "app.kubernetes.io/name",
                    "operator": "In",
                    "values": ["mcpremoteproxy", "virtualmcpserver"],
                },
            ]
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "traefik"}
                        }
                    }
                ]
            }
        ],
    }
    assert np["metadata"]["ownerReferences"] == [owner]
    # 적용 순서: MCPServer → 토큰 Secret → 그룹 → 헤더 설정 → Entry → Cedar 설정 → vMCP →
    # Ingress → NetworkPolicy
    kinds = [o["kind"] for o in fake.applied]
    assert kinds[-9:] == [
        "MCPServer",
        "Secret",
        "MCPGroup",
        "MCPExternalAuthConfig",
        "MCPServerEntry",
        "MCPAuthzConfig",
        "VirtualMCPServer",
        "Ingress",
        "NetworkPolicy",
    ]
    # 상태. 앞단의 phase 는 Ingress 가 가리키는 vMCP 앞단의 것이다
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert st["external_url"] == "http://calc.e2e.local/mcp"
    assert st["exposure"] == {
        "front_phase": None,
        "address_assigned": False,
        "tls_secret_present": None,
        "certificate_ready": None,
    }
    fake.set_status(GV, "VirtualMCPServer", "front-mcp-calc-1", DEFAULT_NS, {"phase": "Ready"})
    fake.set_status(
        NP_API,
        "Ingress",
        "mcp-calc-1",
        DEFAULT_NS,
        {"loadBalancer": {"ingress": [{"ip": "10.0.0.9"}]}},
    )
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert st["exposure"]["front_phase"] == "Ready" and st["exposure"]["address_assigned"] is True
    # 문서 묶음: MCPServer + 테넌트 울타리 + vMCP 앞단 다섯과 Ingress·NetworkPolicy(토큰 Secret 은
    # 미리보기에 없다). vMCP 앞단은 노출한 vMCP 의 잠금이고 나머지는 전부 잠긴다 (P54 11절)
    docs = client.get(f"{MS}/{inst['id']}/manifest").json()["documents"]
    assert docs.pop(1)["kind"] == "MCPAuthzConfig"
    assert [d["kind"] for d in docs] == [
        "MCPServer",
        "MCPGroup",
        "MCPExternalAuthConfig",
        "MCPServerEntry",
        "MCPAuthzConfig",
        "VirtualMCPServer",
        "Ingress",
        "NetworkPolicy",
    ]
    vmcp = docs[5]
    assert vmcp["live"] is not None and vmcp["overrides"] == {}
    assert {
        "spec.incomingAuth.oidcConfigRef",
        "spec.incomingAuth.authzConfigRef",
        "spec.outgoingAuth",
        "spec.config.audit",
        "spec.config.operational.timeouts",
    } <= set(vmcp["locked"])
    assert all(d["locked"] == ["*"] for i, d in enumerate(docs) if i not in (0, 5))
    assert (
        mongo["mcp_servers"].find_one({"_id": inst["id"]})["exposure"]["host"] == "calc.e2e.local"
    )
    # 같은 host 는 다른 테넌트에서도 409 host_conflict, 플랫폼 host 는 422 host_reserved 다
    r = clone_and_deploy(
        client, mp, name="dup", exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert detail_code(r) == (409, "host_conflict")
    r = clone_and_deploy(client, mp, name="plat", exposure={"enabled": True, "host": "daegam.com"})
    assert detail_code(r) == (422, "host_reserved")
    # 삭제하면 앞단과 Ingress·NetworkPolicy 가 사라지고 Entry 이름을 놓는다
    assert client.delete(f"{MS}/{inst['id']}").status_code == 200
    for kind in ("VirtualMCPServer", "MCPServerEntry", "MCPGroup", "MCPExternalAuthConfig"):
        assert fake.get(GV, kind, "front-mcp-calc-1", DEFAULT_NS) is None, kind
    assert fake.get(GV, "MCPAuthzConfig", "front-mcp-calc-1-authz", DEFAULT_NS) is None
    assert fake.get(NP_API, "Ingress", "mcp-calc-1", DEFAULT_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-mcp-calc-1", DEFAULT_NS) is None
    assert mongo["gateway_entry_names"].count_documents({}) == 0
    # host 가 풀렸다
    r = clone_and_deploy(
        client, mp, name="again", exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert r.status_code == 201, r.text


def test_mcp_instance_secret_and_cert_manager_tls(client, fake_clusters):
    cid, bp, ds, mp = cluster_with_ingress(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    # secret 모드
    r = clone_and_deploy(
        client,
        mp,
        name="sec",
        exposure={"enabled": True, "tls": {"mode": "secret", "secret_name": "site-tls"}},
    )
    assert r.status_code == 201, r.text
    inst = r.json()
    # host 기본값 <name>.<tenant>.<domain>
    assert inst["exposure"]["host"] == "sec.default.mcp.example.com"
    assert inst["status"]["external_url"] == "https://sec.default.mcp.example.com/mcp"
    ing = fake.get(NP_API, "Ingress", "mcp-sec", DEFAULT_NS)
    assert ing["spec"]["tls"] == [
        {"hosts": ["sec.default.mcp.example.com"], "secretName": "site-tls"}
    ]
    assert "cert-manager.io/cluster-issuer" not in ing["metadata"]["annotations"]
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert (
        st["exposure"]["tls_secret_present"] is True and st["exposure"]["certificate_ready"] is None
    )
    # Secret 이 없거나 type 이 다르면 422 tls_secret_invalid
    for name in ("missing", "opaque"):
        r = clone_and_deploy(
            client,
            mp,
            name=f"bad-{name}",
            exposure={"enabled": True, "tls": {"mode": "secret", "secret_name": name}},
        )
        assert detail_code(r) == (422, "tls_secret_invalid"), r.text
    # cert-manager: CRD 없으면 422, 있으면 annotation + <k8s.name>-tls
    r = clone_and_deploy(
        client,
        mp,
        name="cm",
        exposure={"enabled": True, "tls": {"mode": "cert-manager", "issuer": "le"}},
    )
    assert detail_code(r) == (422, "cert_manager_unavailable")
    fake.extra_discovery["cert-manager.io/v1"] = ["clusterissuers", "certificates"]
    r = clone_and_deploy(
        client,
        mp,
        name="cm",
        exposure={"enabled": True, "tls": {"mode": "cert-manager", "issuer": "le"}},
    )
    assert detail_code(r) == (422, "unknown_issuer")
    fake.apply(
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "le"}}
    )
    r = clone_and_deploy(
        client,
        mp,
        name="cm",
        exposure={"enabled": True, "tls": {"mode": "cert-manager", "issuer": "le"}},
    )
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"]["tls"] == {"mode": "cert-manager", "secret_name": None, "issuer": "le"}
    ing = fake.get(NP_API, "Ingress", "mcp-cm", DEFAULT_NS)
    assert ing["spec"]["tls"] == [
        {"hosts": ["cm.default.mcp.example.com"], "secretName": "mcp-cm-tls"}
    ]
    assert ing["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "le"
    assert ing["metadata"]["annotations"]["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert st["exposure"]["tls_secret_present"] is False
    assert st["exposure"]["certificate_ready"] is False
    fake.apply(
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "mcp-cm-tls", "namespace": DEFAULT_NS},
            "spec": {},
        }
    )
    fake.set_status(
        "cert-manager.io/v1",
        "Certificate",
        "mcp-cm-tls",
        DEFAULT_NS,
        {"conditions": [{"type": "Ready", "status": "True"}]},
    )
    cluster_secret(fake, DEFAULT_NS, "mcp-cm-tls", tls=True)
    st = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert st["exposure"]["tls_secret_present"] is True
    assert st["exposure"]["certificate_ready"] is True


def test_mcp_instance_edge_tls(client, fake_clusters):
    """앞단 프록시가 TLS 를 끝내는 클러스터(기본값 edge)는 edge 만 받는다. Ingress 에 tls 가
    없고 주소는 https 다. LoadBalancer 가 없어 Ingress 상태에 주소가 쓰이지 않으므로 주소 판정은
    하지 않는다(D54)."""
    cid, bp, ds, mp = cluster_with_ingress(client, tls="edge")
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    # 입력이 없으면 클러스터 기본값 edge 다
    r = clone_and_deploy(client, mp, name="ed", exposure={"enabled": True})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["exposure"]["tls"]["mode"] == "edge"
    assert inst["status"]["external_url"] == "https://ed.default.mcp.example.com/mcp"
    ing = fake.get(NP_API, "Ingress", "mcp-ed", DEFAULT_NS)
    assert "tls" not in ing["spec"]
    assert "cert-manager.io/cluster-issuer" not in (ing["metadata"].get("annotations") or {})
    st = client.get(f"{MS}/{inst['id']}").json()["status"]["exposure"]
    assert st["address_assigned"] is None and st["tls_secret_present"] is None
    # edge 를 적어 보내도 된다
    r = clone_and_deploy(
        client, mp, name="ed2", exposure={"enabled": True, "tls": {"mode": "edge"}}
    )
    assert r.status_code == 201, r.text
    # 앞단이 https 로 돌려보내는 곳에 http 주소를 내지 않는다
    for mode in ({"mode": "none"}, {"mode": "secret", "secret_name": "site-tls"}):
        r = clone_and_deploy(client, mp, name="nn", exposure={"enabled": True, "tls": mode})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "tls_mode_not_allowed"
        assert r.json()["detail"]["allowed"] == ["edge"]


def test_exposure_validation_errors(client, fake_clusters):
    cid, bp, ds, mp = cluster_with_ingress(client, allowed_classes=["traefik"])
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)

    def err(name, exposure, **extra):
        r = clone_and_deploy(client, mp, name=name, exposure=exposure, **extra)
        assert r.status_code == 422, r.text
        return r.json()["detail"]

    assert err("h", {"enabled": True, "host": "bad_host"})["code"] == "invalid_host"
    d = err("c", {"enabled": True, "ingress_class": "nginx"})
    assert d["code"] == "ingress_class_not_allowed" and d["allowed"] == ["traefik"]
    # 허용 목록을 비우면 존재 여부만 본다
    client.patch(f"/clusters/{cid}", json={"ingress": {**INGRESS, "allowed_classes": []}})
    assert (
        err("u", {"enabled": True, "ingress_class": "haproxy"})["code"] == "unknown_ingress_class"
    )
    assert err("n", {"enabled": True}, namespace="nope")["code"] == "namespace_not_allowed"
    # 노출을 끈 클러스터
    client.patch(f"/clusters/{cid}", json={"ingress": {**INGRESS, "enabled": False}})
    assert err("d", {"enabled": True})["code"] == "ingress_disabled"
    # header 모드 + allow_unauthenticated false → 422 unauthenticated_exposure
    client.patch(f"/clusters/{cid}", json={"ingress": {**INGRESS, "allow_unauthenticated": False}})
    assert err("a", {"enabled": True})["code"] == "unauthenticated_exposure"
    # 도메인이 없고 host 도 없으면 invalid_host
    client.patch(f"/clusters/{cid}", json={"ingress": {**INGRESS, "domain": None}})
    assert err("nh", {"enabled": True})["code"] == "invalid_host"
    # 노출을 끄면 아무 검증도 없고 문서도 없다
    r = clone_and_deploy(client, mp, name="off", exposure={"enabled": False, "host": "bad_host"})
    assert r.status_code == 201 and r.json()["status"]["external_url"] is None
    assert r.json()["status"]["exposure"] is None and r.json()["warnings"] == [
        w for w in r.json()["warnings"] if w != UNAUTHENTICATED_WARNING
    ]
    assert fake.get(NP_API, "Ingress", "mcp-off", DEFAULT_NS) is None
    assert [
        d["kind"] for d in client.get(f"{MS}/{r.json()['id']}/manifest").json()["documents"]
    ] == ["MCPServer", "MCPAuthzConfig"]


def test_mcp_instance_namespace_and_basic_patch_exposure(client, fake_clusters):
    cid, bp, ds, mp = cluster_with_ingress(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    assert (
        client.patch(
            "/tenants/default/settings", json={"allowed_namespaces": ["team-a"]}
        ).status_code
        == 200
    )
    ingress_class(fake, "traefik")
    r = clone_and_deploy(client, mp, name="ns", namespace="team-a")
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["k8s"]["namespace"] == "team-a"
    assert inst["effective_config"]["namespace"]["name"] == "team-a"
    assert fake.get(GV, "MCPServer", "mcp-ns", "team-a") is not None
    assert fake.get("v1", "Secret", inst["k8s"]["token_secret"], "team-a") is not None
    # Basic 편집으로 노출을 켜고(프로필 리비전은 그대로) 재배포하면 Ingress 가 team-a 에 생긴다
    r = client.patch(f"{MS}/{inst['id']}", json={"exposure": {"enabled": True, "host": "ns.local"}})
    assert r.status_code == 200, r.text
    assert r.json()["deployed_revision"] == 1
    assert r.json()["status"]["external_url"] == "http://ns.local/mcp"
    assert fake.get(NP_API, "Ingress", "mcp-ns", "team-a")["spec"]["rules"][0]["host"] == "ns.local"
    assert fake.get(GV, "VirtualMCPServer", "front-mcp-ns", "team-a") is not None
    # 같은 host 로 자기 자신을 다시 저장하는 것은 된다 (자기 host 는 제외)
    r = client.patch(f"{MS}/{inst['id']}", json={"exposure": {"enabled": True, "host": "ns.local"}})
    assert r.status_code == 200, r.text
    # 끄면 앞단과 Ingress·NetworkPolicy 가 사라진다
    r = client.patch(f"{MS}/{inst['id']}", json={"exposure": {"enabled": False}})
    assert r.status_code == 200 and r.json()["status"]["external_url"] is None
    assert fake.get(NP_API, "Ingress", "mcp-ns", "team-a") is None
    assert fake.get(GV, "VirtualMCPServer", "front-mcp-ns", "team-a") is None
    assert fake.get(GV, "MCPServerEntry", "front-mcp-ns", "team-a") is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-mcp-ns", "team-a") is None
    # 재배포는 노출 스냅샷을 유지한다
    r = client.patch(f"{MS}/{inst['id']}", json={"exposure": {"enabled": True, "host": "ns.local"}})
    assert r.status_code == 200
    assert client.post(f"{MS}/{inst['id']}/redeploy").status_code == 200
    assert fake.get(NP_API, "Ingress", "mcp-ns", "team-a") is not None


def test_mcp_instance_oidc_vmcp_front(oidc_client, oidc_headers, fake_clusters):
    h = oidc_headers
    fake_clusters.by_name["edge"] = FakeClusterClient()
    fake = fake_clusters.by_name["edge"]
    ingress_class(fake, "traefik")
    cluster_secret(fake, "daegam-acme", "site-tls", tls=True)
    default_cluster(oidc_client, headers=h, ingress={**INGRESS, "allow_unauthenticated": False})
    oidc_client.patch(
        "/tenants/acme/settings",
        json={"mcp_group": "okta-acme", "agent_client_ids": ["agent-1"]},
        headers=h,
    )
    bp = uploaded_backend(oidc_client, endpoint=CALC_ENDPOINT, headers=h)
    mp = oidc_client.post(
        MS,
        json={"name": "calc-mcp", "backend_profile_id": bp["id"], "spec_version": 1},
        headers=h,
    ).json()
    r = clone_and_deploy(
        oidc_client,
        mp,
        name="calc-1",
        headers=h,
        exposure={"enabled": True, "tls": {"mode": "secret", "secret_name": "site-tls"}},
    )
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["status"]["external_url"] == "https://calc-1.acme.mcp.example.com/mcp"
    assert UNAUTHENTICATED_WARNING not in inst["warnings"]
    front = fake.get(GV, "VirtualMCPServer", "front-mcp-calc-1", "daegam-acme")
    assert front["spec"]["incomingAuth"] == {
        "type": "oidc",
        "oidcConfigRef": {
            "name": "daegam-oidc",
            "audience": "daegam-mcp",
            "resourceUrl": "https://calc-1.acme.mcp.example.com/mcp",
        },
        # 앞단의 Cedar 는 앞단이 참조하는 MCPAuthzConfig 에 있다 (P50 8절)
        "authzConfigRef": {"name": "front-mcp-calc-1-authz"},
    }
    assert front["spec"]["config"]["audit"]["enabled"] is True
    policies = cedar_policies(front, fake=fake)
    assert any('THVGroup::"okta-acme"' in p for p in policies)
    assert any('claim_cid == "agent-1"' in p for p in policies)
    # MCPServer 자체는 플랫폼 발급자의 서비스 토큰을 그 서버의 audience 로 검증한다 (D41, P50 9절).
    # 새로 만든 서버라 처음부터 자기 audience 다. Cedar 는 없다
    inner = fake.get(GV, "MCPServer", "mcp-calc-1", "daegam-acme")["spec"]
    assert inner["oidcConfigRef"] == {
        "name": "daegam-internal-oidc",
        "audience": f"daegam-internal:acme:{inst['id']}",
        "resourceUrl": "http://mcp-mcp-calc-1-proxy.daegam-acme.svc.cluster.local:8080/mcp",
    }
    assert "authzConfig" not in inner and "trustProxyHeaders" not in inner


# -- Gateway Instance -------------------------------------------------------------------------


def test_gateway_secret_exposure_renders_ingress_policy(client, fake_clusters):
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    client.patch(f"/clusters/{cid}", json={"ingress": INGRESS})
    prep_ingress(fake)
    p = create_gateway(client, items, name="a").json()
    r = client.post(
        f"{GW}/{p['id']}/deploy",
        json={
            "exposure": {
                "enabled": True,
                "host": "gw-a.e2e.local",
                "tls": {"mode": "secret", "secret_name": "site-tls"},
            },
        },
    )
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["status"]["external_url"] == "https://gw-a.e2e.local/mcp"
    assert UNAUTHENTICATED_WARNING in inst["warnings"]
    uid = vmcp_of(fake, "gw-a")["metadata"]["uid"]
    owner = {
        "apiVersion": GV,
        "kind": "VirtualMCPServer",
        "name": "gw-a",
        "uid": uid,
        "blockOwnerDeletion": False,
        "controller": False,
    }
    ing = fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS)
    paths = ing["spec"]["rules"][0]["http"]["paths"]
    # vMCP 의 /status·/api/backends/health·/health 는 밖에 열지 않는다 (P50 5절)
    assert [p["path"] for p in paths] == ["/mcp", "/.well-known/oauth-protected-resource"]
    backend = {"service": {"name": "vmcp-gw-a", "port": {"number": 4483}}}
    assert all(p["backend"] == backend for p in paths)
    assert ing["spec"]["tls"] == [{"hosts": ["gw-a.e2e.local"], "secretName": "site-tls"}]
    assert ing["metadata"]["ownerReferences"] == [owner]
    assert ing["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "gateway-instance-ingress"
    np = fake.get(NP_API, "NetworkPolicy", "daegam-expose-gw-a", DEFAULT_NS)
    assert np["spec"]["podSelector"] == {
        "matchLabels": {
            "app.kubernetes.io/instance": "gw-a",
            "app.kubernetes.io/name": "virtualmcpserver",
        }
    }
    assert np["metadata"]["ownerReferences"] == [owner]
    # header 모드: vMCP 는 anonymous 그대로 (앞단 프록시 없음)
    vm = vmcp_of(fake, "gw-a")
    assert vm["spec"]["incomingAuth"]["type"] == "anonymous"
    assert fake.find("MCPRemoteProxy", DEFAULT_NS) == [
        x for x in fake.find("MCPRemoteProxy", DEFAULT_NS) if x["metadata"]["name"] == "hub-spike"
    ]
    # 문서 묶음 + 상태
    docs = client.get(f"{GW}/{inst['id']}/manifest").json()["documents"]
    # 토큰을 받는 Entry 둘(calculator, orders)이 저마다 토큰 설정을 갖는다 (P50 9절)
    assert [d["kind"] for d in docs] == [
        "MCPGroup",
        "MCPExternalAuthConfig",
        "MCPExternalAuthConfig",
        "MCPServerEntry",
        "MCPServerEntry",
        "MCPAuthzConfig",
        "VirtualMCPServer",
        "Ingress",
        "NetworkPolicy",
    ]
    assert all(docs[i]["locked"] == ["*"] for i in (5, 7, 8))
    st = client.get(f"{GW}/{inst['id']}").json()["status"]
    assert st["exposure"] == {
        "front_phase": None,
        "address_assigned": False,
        "tls_secret_present": True,
        "certificate_ready": None,
    }
    # 같은 클러스터의 다른 Gateway 가 같은 host 를 쓰면 409 host_conflict
    pb = create_gateway(client, items, name="b").json()
    r = deploy_gateway(client, pb["id"], exposure={"enabled": True, "host": "gw-a.e2e.local"})
    assert detail_code(r) == (409, "host_conflict")
    # PATCH 로 끄면 Ingress·NetworkPolicy 가 사라지고 host 가 풀린다
    r = client.patch(f"{GW}/{inst['id']}", json={"exposure": {"enabled": False}})
    assert r.status_code == 200, r.text
    assert r.json()["status"]["external_url"] is None
    assert fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-gw-a", DEFAULT_NS) is None
    assert client.patch(f"{GW}/{inst['id']}", json={}).status_code == 422
    r = client.patch(
        f"{GW}/{inst['id']}", json={"exposure": {"enabled": True, "host": "gw-a.e2e.local"}}
    )
    assert r.status_code == 200 and fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is not None
    # 삭제가 정리한다
    assert client.delete(f"{GW}/{inst['id']}").status_code == 200
    assert fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is None
    assert fake.get(NP_API, "NetworkPolicy", "daegam-expose-gw-a", DEFAULT_NS) is None


# -- RBAC: IngressClass·ClusterIssuer 존재 확인은 get 이 아니라 list 다 (F7 실측 403) --------------


def test_exposure_validation_needs_only_list_on_cluster_scoped_kinds(client, fake_clusters):
    """rbac.yaml 은 ingressclasses·clusterissuers 에 list 만 준다. get 이 403 이어도 노출 생성이
    되고, 없는 이름은 여전히 422 다."""
    cid, bp, ds, mp = cluster_with_ingress(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    fake.extra_discovery["cert-manager.io/v1"] = ["clusterissuers"]
    fake.apply(
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "le"}}
    )
    fake.denied.add(("networking.k8s.io", "ingressclasses", "get"))
    fake.denied.add(("cert-manager.io", "clusterissuers", "get"))
    r = clone_and_deploy(client, mp, name="http", exposure={"enabled": True, "host": "h.local"})
    assert r.status_code == 201, r.text
    r = clone_and_deploy(
        client,
        mp,
        name="cm",
        exposure={"enabled": True, "tls": {"mode": "cert-manager", "issuer": "le"}},
    )
    assert r.status_code == 201, r.text
    r = clone_and_deploy(
        client, mp, name="bad", exposure={"enabled": True, "ingress_class": "haproxy"}
    )
    assert detail_code(r) == (422, "unknown_ingress_class")
    r = clone_and_deploy(
        client,
        mp,
        name="bad2",
        exposure={"enabled": True, "tls": {"mode": "cert-manager", "issuer": "nope"}},
    )
    assert detail_code(r) == (422, "unknown_issuer")
    # list 가 막히면 403 + 빠진 verb (500 이 아니다)
    fake.denied.add(("networking.k8s.io", "ingressclasses", "list"))
    r = clone_and_deploy(client, mp, name="denied", exposure={"enabled": True, "host": "d.local"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["missing"] == ["ingressclasses:list"]


# -- 플랫폼 전체의 host 유일성 (P33 6절) ---------------------------------------------------------
#
# 다른 클러스터의 살아 있는 노출은 tests/helpers/docs.py 의 `seed_*` 가 Mongo 에 바로 넣는다(테넌트
# acme). 앞단이 TLS 를 끝내는 노출이라 공개 주소가 https 다. OAuth 테스트도 그 도우미로 대상을
# 만든다.


def test_host_conflict_spans_every_cluster(client, fake_clusters, mongo):
    """다른 클러스터의 살아 있는 MCP Server·Gateway·Agent 가 쓰는 host 도 409 host_conflict 다.
    OAuth 의 resource 가 테넌트 하나로 풀리는 전제다."""
    cid, bp, ds, mp = cluster_with_ingress(client)
    prep_ingress(fake_clusters.by_name["edge"])
    seed_server(mongo, "far-mcp", "far-mcp.example", cluster="elsewhere")
    seed_gateway(mongo, "far-gw", "far-gw.example", cluster="elsewhere")
    seed_agent(mongo, "far-bot", "far-bot.example", cluster="elsewhere")
    for n, host in enumerate(("far-mcp.example", "far-gw.example", "far-bot.example")):
        r = clone_and_deploy(client, mp, name=f"dup{n}", exposure={"enabled": True, "host": host})
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {
            "code": "host_conflict",
            "detail": f"host {host} is already exposed on this platform",
        }
    # 노출을 끈 것은 host 를 잡지 않는다
    mongo["agents"].update_one({"_id": "far-bot"}, {"$set": {"exposure.enabled": False}})
    r = clone_and_deploy(
        client, mp, name="free", exposure={"enabled": True, "host": "far-bot.example"}
    )
    assert r.status_code == 201, r.text


def test_startup_claims_live_hosts_and_leaves_stale_claims(
    settings, mongo, fake_clusters, fake_secrets
):
    """기동은 기록 없는 살아 있는 노출에 host 선점을 준다. 기동은 리컨실 리스 밖이라 선점 기록을
    주기만 하고 지우지 않는다."""
    seed_gateway(mongo, "gw-c", "solo.example", cluster="c1")
    seed_server(mongo, "mcp-d", "other.example", cluster="c2")
    stale = {"kind": "mcp_server", "resource_id": "nobody", "tenant": "default", "nonce": "n0"}
    old = utcnow() - timedelta(minutes=10)
    mongo["exposure_hosts"].insert_one({"_id": "stale.example", **stale, "claimed_at": old})
    with api_client(make_app(settings, fake_clusters, fake_secrets)):
        pass
    claims = {d["_id"]: d["resource_id"] for d in mongo["exposure_hosts"].find()}
    assert claims == {"solo.example": "gw-c", "other.example": "mcp-d", "stale.example": "nobody"}


def test_turning_a_gateway_exposure_on_checks_the_stored_overrides_first(
    client, fake_clusters, mongo
):
    """노출 여부가 잠금 표를 고른다. 노출을 켤 때 저장된 overrides 를 새 표로 먼저 보지 않으면
    노출만 저장된 채 재배포가 422 로 멈춘다. 검사가 먼저이므로 저장된 노출 값은 그대로다."""
    cid, items, calc, orders, remote, fake = hub_env(client, fake_clusters)
    client.patch(f"/clusters/{cid}", json={"ingress": INGRESS})
    prep_ingress(fake)
    p = create_gateway(client, items, name="a").json()
    assert deploy_gateway(client, p["id"]).status_code == 201
    # 노출하지 않은 동안에는 oidcConfigRef 의 resourceUrl 이 열려 있다 (P50 8절)
    patch = {"spec": {"incomingAuth": {"oidcConfigRef": {"resourceUrl": "https://x.example/mcp"}}}}
    assert client.put(f"{GW}/{p['id']}/overrides", json={"patch": patch}).status_code == 200

    r = client.patch(
        f"{GW}/{p['id']}", json={"exposure": {"enabled": True, "host": "gw-a.e2e.local"}}
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "locked_field"
    assert r.json()["detail"]["path"] == "spec.incomingAuth.oidcConfigRef.resourceUrl"
    saved = mongo["gateways"].find_one({"_id": p["id"]})
    assert saved["exposure"]["enabled"] is False and saved["state"] == "deployed"
    assert fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is None
    # overrides 에서 그 자리를 빼면 노출이 켜진다
    assert client.put(f"{GW}/{p['id']}/overrides", json={"patch": {}}).status_code == 200
    r = client.patch(
        f"{GW}/{p['id']}", json={"exposure": {"enabled": True, "host": "gw-a.e2e.local"}}
    )
    assert r.status_code == 200, r.text
    assert fake.get(NP_API, "Ingress", "gw-a", DEFAULT_NS) is not None
