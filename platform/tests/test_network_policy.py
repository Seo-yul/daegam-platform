"""P6 F3a: 테넌트 네임스페이스 NetworkPolicy(ingress 전용), 정책 컨트롤러 탐지, 경고 필드."""

from __future__ import annotations

from daegam.config.resolve import EffectiveNamespace
from daegam.k8s.preflight import (
    NETWORK_POLICY_WARNING,
    REQUIRED_VERBS,
    check_cluster,
    detect_policy_controller,
    preflight_warnings,
)
from daegam.services.tenancy import NETWORK_POLICY_NAME, ensure_namespace_objects
from daegam.store.clusters import Preflight
from tests.helpers.consts import NP_API
from tests.helpers.fake_cluster import FakeClusterClient
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env


def _expected_spec(platform_ns: str, tenant: str = "acme") -> dict:
    return {
        # 샌드박스 파드(P40 4절)와 MCP 백엔드 파드(T67)는 고르지 않는다. 앞의 것은 격리 정책만, 뒤의
        # 것은 백엔드 정책만 받는다
        "podSelector": {
            "matchExpressions": [
                {"key": "daegam.devops.ai.kr/resource", "operator": "NotIn", "values": ["sandbox"]},
                {"key": "daegam.devops.ai.kr/mcp-backend", "operator": "DoesNotExist"},
            ]
        },
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [
                    {"podSelector": {}},
                    {"namespaceSelector": {"matchLabels": {"daegam.devops.ai.kr/tenant": tenant}}},
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": platform_ns}
                        }
                    },
                ]
            }
        ],
    }


def _workload(kind: str, name: str, ns: str = "kube-system") -> dict:
    return {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": name, "namespace": ns}}


# -- 매니페스트 스냅샷 ------------------------------------------------------------


def test_ensure_namespace_objects_applies_policy_idempotently():
    fake = FakeClusterClient()
    ns = EffectiveNamespace(name="daegam-acme", quota={"pods": "5"})
    ensure_namespace_objects(fake, "acme", ns, platform_namespace="platform-ns")
    assert [o["kind"] for o in fake.applied] == [
        "Namespace",
        "NetworkPolicy",
        "NetworkPolicy",
        "NetworkPolicy",
        "ResourceQuota",
        "MCPGroup",
    ]
    # subject 를 주면 RoleBinding daegam-tls-reader 가 마지막에 붙는다 (D22)
    ensure_namespace_objects(
        fake,
        "acme",
        ns,
        platform_namespace="platform-ns",
        tls_reader_subject={
            "kind": "ServiceAccount",
            "name": "daegam-platform",
            "namespace": "daegam-system",
        },
    )
    rb = fake.get("rbac.authorization.k8s.io/v1", "RoleBinding", "daegam-tls-reader", "daegam-acme")
    assert rb["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "daegam-tls-reader",
    }
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": "daegam-platform", "namespace": "daegam-system"}
    ]
    assert rb["metadata"]["labels"]["daegam.devops.ai.kr/resource"] == "tenant-tls-reader"
    first = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, "daegam-acme")
    assert first["spec"] == _expected_spec("platform-ns")
    assert first["metadata"]["labels"] == {
        "daegam.devops.ai.kr/tenant": "acme",
        "daegam.devops.ai.kr/resource": "tenant-network-policy",
        "daegam.devops.ai.kr/id": "acme",
        "app.kubernetes.io/managed-by": "daegam",
    }
    # 두 번째 apply 는 같은 객체를 갱신한다 (SSA 멱등)
    ensure_namespace_objects(fake, "acme", ns, platform_namespace="platform-ns")
    # 테넌트 정책, 샌드박스 격리 정책(P40 4절), MCP 백엔드 정책(T67) 셋이다. egress 정책(T75)은
    # 클러스터의 스위치를 켤 때만 있다
    assert len(fake.find("NetworkPolicy")) == 3
    again = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, "daegam-acme")
    assert again["metadata"]["uid"] == first["metadata"]["uid"]
    # 위의 RoleBinding 포함 apply 까지 세 번 적용됐다
    assert again["metadata"]["generation"] == 3 and again["spec"] == first["spec"]


def test_api_provisioning_creates_policy_with_platform_namespace(client, fake_clusters):
    """Data Service·MCP Instance 생성 경로 둘 다 Settings.namespace 를 from 에 쓴다."""
    _, _, _, mp = mcp_server_env(client)
    fake: FakeClusterClient = fake_clusters.by_name["edge"]
    np = fake.get(NP_API, "NetworkPolicy", NETWORK_POLICY_NAME, "daegam-default")
    assert np is not None and np["spec"] == _expected_spec("daegam-system", "default")
    assert np["metadata"]["labels"]["daegam.devops.ai.kr/tenant"] == "default"
    assert clone_and_deploy(client, mp).status_code == 201
    assert len(fake.find("NetworkPolicy")) == 3
    kinds = [o["kind"] for o in fake.applied]
    assert kinds.index("Namespace") < kinds.index("NetworkPolicy") < kinds.index("MCPGroup")


# -- preflight: 정책 컨트롤러 탐지 ---------------------------------------------------


def test_preflight_unknown_without_controller():
    pf = check_cluster(FakeClusterClient())[0]
    assert pf.ok is True  # 경고일 뿐 실패가 아니다
    assert pf.network_policy == "unknown" and pf.policy_controller is None
    assert preflight_warnings(pf) == [NETWORK_POLICY_WARNING]
    # preflight 가 아직 없는 클러스터도 경고하고, 집행이 보이면 경고하지 않는다
    assert preflight_warnings(None) == [NETWORK_POLICY_WARNING]
    assert preflight_warnings(Preflight(ok=True, network_policy="enforced")) == []
    fake = FakeClusterClient()
    fake.apply(_workload("DaemonSet", "kube-proxy"))
    fake.apply(_workload("Deployment", "coredns"))
    assert detect_policy_controller(fake) is None


def test_preflight_detects_daemonset_and_deployment_controllers():
    pf = check_cluster(FakeClusterClient(policy_controller="calico-node"))[0]
    assert pf.network_policy == "enforced"
    assert pf.policy_controller == "kube-system/calico-node"

    cilium = FakeClusterClient()
    cilium.apply(_workload("DaemonSet", "cilium", ns="cilium"))
    assert detect_policy_controller(cilium) == "cilium/cilium"

    kr = FakeClusterClient()
    kr.apply(_workload("Deployment", "kube-router"))
    assert check_cluster(kr)[0].network_policy == "enforced"

    antrea = FakeClusterClient()
    antrea.apply(_workload("DaemonSet", "antrea-agent-windows"))
    assert detect_policy_controller(antrea) == "kube-system/antrea-agent-windows"


def test_preflight_survives_list_failure():
    class Flaky(FakeClusterClient):
        def list(self, *a, **kw):  # noqa: D401
            raise RuntimeError("forbidden")

    assert detect_policy_controller(Flaky()) is None
    assert check_cluster(Flaky())[0].network_policy == "unknown"
    # k3s 여도 목록을 읽지 못했으면 다른 컨트롤러가 있는지 모르므로 내장 kube-router 로 추정하지
    # 않는다 (T75)
    pf, gates = check_cluster(Flaky(k8s_version="v1.36.5+k3s1"))
    assert pf.network_policy == "unknown" and pf.policy_controller is None
    assert gates.policy_scanned is False


def test_required_verbs_include_network_policy_and_daemonsets():
    need = {
        ("networking.k8s.io", "networkpolicies", "create"),
        ("networking.k8s.io", "networkpolicies", "patch"),
        ("networking.k8s.io", "networkpolicies", "get"),
        ("apps", "daemonsets", "get"),
        ("apps", "daemonsets", "list"),
        ("apps", "deployments", "list"),
    }
    assert need <= set(REQUIRED_VERBS)
    # P8: 노출·RoleBinding·SelfSubjectReview verb 와 이름 한정 bind 검사
    assert {
        ("networking.k8s.io", "ingresses", "create"),
        ("networking.k8s.io", "ingressclasses", "list"),
        ("networking.k8s.io", "networkpolicies", "delete"),
        ("rbac.authorization.k8s.io", "rolebindings", "create"),
        ("authentication.k8s.io", "selfsubjectreviews", "create"),
        ("rbac.authorization.k8s.io", "clusterroles", "bind", "daegam-tls-reader"),
    } <= set(REQUIRED_VERBS)
    pf = check_cluster(
        FakeClusterClient(
            denied={("rbac.authorization.k8s.io", "clusterroles", "bind", "daegam-tls-reader")}
        )
    )[0]
    assert pf.ok is False
    assert "rbac: rbac.authorization.k8s.io/clusterroles:bind[daegam-tls-reader]" in pf.missing
    named = [c for c in pf.rbac if c.name == "daegam-tls-reader"]
    assert named and named[0].allowed is False and named[0].verb == "bind"
    pf = check_cluster(
        FakeClusterClient(denied={("networking.k8s.io", "networkpolicies", "create")})
    )[0]
    assert pf.ok is False
    assert "rbac: networking.k8s.io/networkpolicies:create" in pf.missing


# -- 경고 필드 ------------------------------------------------------------------


def test_preflight_presumes_k3s_embedded_kube_router():
    """k3s 는 kube-router 정책 컨트롤러를 k3s 바이너리 안에서 돌려 워크로드로 보이지 않는다.
    gitVersion 의 +k3s 로 집행을 추정하고(presumed) 경고는 붙이지 않는다(D54).
    --disable-network-policy 일 수 있어 확정은 아니다. 확정은 home 의 DAEGAM_HOME_NETWORK_POLICY 가
    한다(test_clusters_api)."""
    pf = check_cluster(FakeClusterClient(k8s_version="v1.36.5+k3s1"))[0]
    assert pf.network_policy == "presumed"
    assert pf.policy_controller == "k3s (embedded kube-router)"
    assert preflight_warnings(pf) == []
    # 정책 컨트롤러 워크로드가 보이면 그쪽이 이긴다
    pf = check_cluster(
        FakeClusterClient(k8s_version="v1.36.5+k3s1", policy_controller="calico-node")
    )[0]
    assert pf.network_policy == "enforced"
    # k3s 가 아니면 지금처럼 unknown 과 경고다
    pf = check_cluster(FakeClusterClient(k8s_version="v1.36.5"))[0]
    assert pf.network_policy == "unknown" and preflight_warnings(pf) == [NETWORK_POLICY_WARNING]
