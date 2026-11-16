"""테넌트 네임스페이스마다 두는 NetworkPolicy 가운데 MCP 백엔드와 나가는 길의 것 (T67, T75).

정책은 합집합이다. 어느 정책이 고른 파드는 그 정책들이 연 것만 열리고, 어느 정책도 고르지 않은
파드는 다 열린다. 그래서 좁히려는 파드는 넓은 정책이 고르지 않게 빼고, 뺀 파드는 좁은 정책이 늘
고르게 둔다.

- 백엔드 정책 `daegam-mcp-backend`: MCPServer 의 백엔드 파드(`LABEL_MCP_BACKEND`)를 고르고, 같은
  네임스페이스의 MCPServer 프록시 파드에서 오는 것만 연다. 프록시 파드는 오퍼레이터가 Deployment
  selector 로 다는 `app.kubernetes.io/name: mcpserver` 가 있고 백엔드 라벨이 없는 파드다. 백엔드
  파드는 그 라벨을 늘 달아 맞지 않고, vMCP·MCPRemoteProxy 파드는 이름 값이 달라 맞지 않는다. 테넌트
  ingress 정책은 백엔드 파드를 빼므로(`tenant_ingress_selector`) 테넌트의 다른 파드와 플랫폼
  네임스페이스는 백엔드에 닿지 못한다. 노드에서 오는 kubelet 프로브는 NetworkPolicy 가 막지 않는다.
  Advanced 편집이 두 라벨을 모두 잠근다(`k8s/merge.py`). 프록시마다 제 백엔드로만 좁히지 않는 것은
  그것이 더하는 것이 없어서다. 프록시 러너의 역할은 StatefulSet 을 만들 수 있어 그 네임스페이스에
  아무 라벨의 파드나 띄운다.
- egress 정책 `daegam-tenant-egress`: 인스턴스 메타데이터 주소(`METADATA_EXCEPT`)만 빼고 다 연다.
  클러스터의 스위치(`Cluster.block_metadata`)가 켜져 있고 정책 컨트롤러가 확인된 것일 때만 둔다
  (`egress_policy_wanted`). 나가는 길을 이미 좁힌 파드는 고르지 않는다. 고르면 합집합이 그 파드의
  나가는 길을 연다. 샌드박스(격리 정책)와 웹 fetch 복제본(`daegam-web-fetch`)이 그것이다. 클러스터
  안의 파드는 namespaceSelector 로 따로 연다. ipBlock 이 파드 주소에 맞는지는 CNI 마다 다르기
  때문이다. 다른 링크로컬 주소(NodeLocal DNS 의 169.254.20.10 같은 것)는 그대로 열린다.
"""

from __future__ import annotations

from typing import Any

from daegam.k8s.labels import APP_MCPSERVER, LABEL_APP_NAME, LABEL_MCP_BACKEND, ensure_labels
from daegam.k8s.preflight import K3S_POLICY_CONTROLLER
from daegam.k8s.sandbox_targets import METADATA
from daegam.k8s.templates.common import NETWORK_POLICY_API
from daegam.k8s.templates.sandbox import tenant_policy_selector
from daegam.k8s.templates.web_tools import FETCH_LABEL
from daegam.store.clusters import Cluster, Preflight

BACKEND_POLICY_NAME = "daegam-mcp-backend"
RESOURCE_BACKEND_POLICY = "tenant-mcp-backend-policy"
EGRESS_POLICY_NAME = "daegam-tenant-egress"
RESOURCE_EGRESS_POLICY = "tenant-egress-policy"
# 인스턴스 메타데이터 주소. IPv4 는 OCI·AWS·GCP·Azure 가 같은 주소이고, IPv6 는 AWS Nitro 의 것이다
METADATA_EXCEPT = (f"{METADATA[0]}/32", "fd00:ec2::254/128")
# ipBlock 을 파드 밖 모든 주소(노드 주소 포함)에 맞추는 것으로 확인한 정책 컨트롤러의 워크로드
# 이름. 다른 컨트롤러(Cilium, GKE Dataplane V2 의 anetd)는 노드 주소를 ipBlock 으로 맞추지 않아 이
# 정책을 받은 프록시 러너와 vMCP 가 API 서버를 잃을 수 있다. k3s 의 내장 kube-router 와 DaemonSet
# kube-router 는 같은 컨트롤러다
EGRESS_CONTROLLERS = ("calico-node", "antrea-agent", "kube-router")
# 스위치를 켰는데 컨트롤러가 확인된 것이 아니어서 egress 정책을 두지 않은 클러스터의 경고 코드
METADATA_BLOCK_UNSUPPORTED = "metadata_block_unsupported"
# 관리자의 egress 정책이 있어 egress 정책을 두지 않은 네임스페이스가 있는 클러스터의 경고 코드
METADATA_BLOCK_SKIPPED = "metadata_block_skipped"


def _exists(key: str) -> dict[str, Any]:
    return {"key": key, "operator": "Exists"}


def _absent(key: str) -> dict[str, Any]:
    return {"key": key, "operator": "DoesNotExist"}


def tenant_ingress_selector() -> dict[str, Any]:
    """테넌트 ingress 정책(`daegam-tenant-ingress`)의 podSelector. 샌드박스
    (`tenant_policy_selector`)와 MCP 백엔드 파드를 뺀 모든 파드다. 백엔드 파드는 백엔드 정책만
    고른다."""
    expressions = tenant_policy_selector()["matchExpressions"]
    return {"matchExpressions": [*expressions, _absent(LABEL_MCP_BACKEND)]}


def egress_selector() -> dict[str, Any]:
    """egress 정책의 podSelector. 나가는 길을 이미 좁힌 파드(샌드박스, 웹 fetch 복제본)를 뺀
    모든 파드다."""
    expressions = tenant_policy_selector()["matchExpressions"]
    return {"matchExpressions": [*expressions, _absent(FETCH_LABEL)]}


def mcp_proxy_selector() -> dict[str, Any]:
    """MCPServer 프록시 파드를 고르는 셀렉터. 오퍼레이터의 종류 라벨이 `mcpserver` 이고 백엔드
    라벨이 없는 파드다."""
    return {
        "matchLabels": {LABEL_APP_NAME: APP_MCPSERVER},
        "matchExpressions": [_absent(LABEL_MCP_BACKEND)],
    }


def render_backend_policy(tenant: str, namespace: str) -> dict[str, Any]:
    """`daegam-mcp-backend`. MCP 백엔드 파드에 같은 네임스페이스의 MCPServer 프록시 파드에서 오는
    것만 연다. 포트는 가리지 않는다. 허브 image 항목의 포트가 저마다 다르다."""
    return ensure_labels(
        {
            "apiVersion": NETWORK_POLICY_API,
            "kind": "NetworkPolicy",
            "metadata": {"name": BACKEND_POLICY_NAME, "namespace": namespace},
            "spec": {
                "podSelector": {"matchExpressions": [_exists(LABEL_MCP_BACKEND)]},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": mcp_proxy_selector()}]}],
            },
        },
        tenant=tenant,
        resource=RESOURCE_BACKEND_POLICY,
        id=tenant,
    )


def render_egress_policy(tenant: str, namespace: str) -> dict[str, Any]:
    """`daegam-tenant-egress`. 메타데이터 주소만 빼고 나가는 길을 다 연다. 클러스터의 모든 파드,
    IPv4 전체, IPv6 전체에서 메타데이터 주소를 뺀 것이다. 포트는 가리지 않는다."""
    v4, v6 = METADATA_EXCEPT
    return ensure_labels(
        {
            "apiVersion": NETWORK_POLICY_API,
            "kind": "NetworkPolicy",
            "metadata": {"name": EGRESS_POLICY_NAME, "namespace": namespace},
            "spec": {
                "podSelector": egress_selector(),
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {"namespaceSelector": {}},
                            {"ipBlock": {"cidr": "0.0.0.0/0", "except": [v4]}},
                            {"ipBlock": {"cidr": "::/0", "except": [v6]}},
                        ]
                    }
                ],
            },
        },
        tenant=tenant,
        resource=RESOURCE_EGRESS_POLICY,
        id=tenant,
    )


def egress_controller_known(preflight: Preflight | None) -> bool:
    """preflight 가 탐지한 정책 컨트롤러가 이 egress 정책과 함께 쓸 수 있다고 확인된 것인가. k3s 의
    내장 kube-router 와 `EGRESS_CONTROLLERS` 의 워크로드다. 컨트롤러를 모르면 아니다. 집행 판정을
    DAEGAM_HOME_NETWORK_POLICY 로 못 박아도 컨트롤러는 탐지한 것이라 이 판정은 그대로다."""
    controller = preflight.policy_controller if preflight is not None else None
    if not controller:
        return False
    if controller == K3S_POLICY_CONTROLLER:
        return True
    name = controller.rpartition("/")[2]
    return any(name == c or name.startswith(c + "-") for c in EGRESS_CONTROLLERS)


def egress_policy_wanted(cluster: Cluster) -> bool:
    """이 클러스터의 테넌트 네임스페이스에 egress 정책을 두는가. 스위치가 켜져 있고 컨트롤러가
    확인된 것일 때다. 아니면 묶음이 그 정책을 빼고 남은 것을 지운다."""
    return cluster.block_metadata and egress_controller_known(cluster.status.preflight)


def metadata_block_warnings(cluster: Cluster) -> list[str]:
    """Cluster 응답의 경고. 스위치를 켰는데 컨트롤러가 확인된 것이 아니어서 egress 정책을 두지
    않으면 그 까닭이고, 관리자의 egress 정책이 있어 두지 않은 네임스페이스가 있으면 그 목록이다.
    목록은 문장의 끝 `: ` 뒤에 쉼표로 잇는다."""
    preflight = cluster.status.preflight
    if not cluster.block_metadata:
        return []
    if egress_controller_known(preflight):
        if not cluster.status.metadata_skipped:
            return []
        return [
            f"{METADATA_BLOCK_SKIPPED}: block_metadata is on, but these namespaces hold egress "
            "policies the platform did not make, so they get no metadata block: "
            + ", ".join(cluster.status.metadata_skipped)
        ]
    controller = (preflight.policy_controller if preflight is not None else None) or "none found"
    return [
        f"{METADATA_BLOCK_UNSUPPORTED}: block_metadata is on, but the NetworkPolicy controller "
        f"({controller}) is not one known to work with the metadata egress policy (kube-router, "
        "Calico, Antrea); on others it can cut pods off from the API server, so the "
        "platform applies none here. Block 169.254.169.254 on the nodes or with the "
        "controller's own policy"
    ]
