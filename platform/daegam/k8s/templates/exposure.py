"""P8 (D22) 외부 노출. 앞단 프록시·NetworkPolicy 이름 규칙, Ingress, NetworkPolicy 렌더."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from daegam.k8s.labels import LABEL_APP_INSTANCE, LABEL_APP_NAME, daegam_labels
from daegam.k8s.templates.common import with_owner
from daegam.store.exposure import Exposure, tls_secret_name_for

INGRESS_API = "networking.k8s.io/v1"
CERT_MANAGER_ISSUER_ANNOTATION = "cert-manager.io/cluster-issuer"
# 노출한 MCP Server·Gateway 의 Ingress 가 여는 경로 (P50 5절). MCP 엔드포인트와 RFC 9728 메타데이터
# (`.../mcp` 처럼 뒤에 경로가 붙는 꼴 포함)뿐이다. vMCP 의 /status·/api/backends/health·/health 는
# 토큰 없이 백엔드 이름과 인증 방식, 상태를 답하므로 밖에 열지 않는다(F0 실측)
MCP_EXPOSE_PATHS: tuple[str, ...] = ("/mcp", "/.well-known/oauth-protected-resource")
# MCP Server 의 옛 노출 앞단 MCPRemoteProxy 이름의 앞머리. 재적용 틱이 나열한 옛 앞단의 이름에서
# 서버의 k8s 이름을 읽는다 (P54 6절)
EXPOSE_PROXY_PREFIX = "expose-"


def expose_proxy_name(k8s_name: str) -> str:
    """MCP Server 노출 앞단 MCPRemoteProxy 이름."""
    return f"{EXPOSE_PROXY_PREFIX}{k8s_name}"


def expose_policy_name(k8s_name: str) -> str:
    """노출 인스턴스의 NetworkPolicy 이름 (컨트롤러 네임스페이스에서 앞단 파드로의 ingress 허용)."""
    return f"daegam-expose-{k8s_name}"


def ingress_tls(exposure: Exposure, k8s_name: str) -> tuple[str | None, str | None]:
    """docs/guide/exposure.md 1절 TLS 규칙의 (tls secretName, cert-manager cluster-issuer).
    secret 이면 그 이름, cert-manager 면 <k8s.name>-tls 와 issuer, none 이면 둘 다 None."""
    if exposure.tls.mode == "secret":
        return exposure.tls.secret_name, None
    if exposure.tls.mode == "cert-manager":
        return tls_secret_name_for(k8s_name), exposure.tls.issuer
    return None, None


def render_ingress(
    *,
    tenant: str,
    instance_id: str,
    resource: str,
    name: str,
    namespace: str,
    host: str,
    ingress_class: str,
    service_name: str,
    port: int,
    tls_secret: str | None = None,
    cluster_issuer: str | None = None,
    annotations: dict[str, str] | None = None,
    owner: dict[str, Any] | None = None,
    paths: Sequence[str],
) -> dict[str, Any]:
    """networking.k8s.io/v1 Ingress (docs/guide/exposure.md 1절 렌더링 규칙). Cluster
    ingress.annotations 를 먼저 깔고 cert-manager annotation 을 얹는다. tls 는 secret 이면 그
    이름, cert-manager 면 <k8s.name>-tls. paths 는 같은 backend 로 가는 Prefix 경로 목록이다.
    MCP Server·Gateway 는 `MCP_EXPOSE_PATHS`, Agent 는 그 API 경로다."""
    meta: dict[str, Any] = {
        "name": name,
        "namespace": namespace,
        "labels": daegam_labels(tenant=tenant, resource=resource, id=instance_id),
    }
    merged = dict(annotations or {})
    if cluster_issuer:
        merged[CERT_MANAGER_ISSUER_ANNOTATION] = cluster_issuer
    if merged:
        meta["annotations"] = merged
    backend = {"service": {"name": service_name, "port": {"number": port}}}
    spec: dict[str, Any] = {
        "ingressClassName": ingress_class,
        "rules": [
            {
                "host": host,
                "http": {
                    "paths": [
                        {"path": path, "pathType": "Prefix", "backend": backend} for path in paths
                    ]
                },
            }
        ],
    }
    if tls_secret:
        spec["tls"] = [{"hosts": [host], "secretName": tls_secret}]
    return with_owner(
        {"apiVersion": INGRESS_API, "kind": "Ingress", "metadata": meta, "spec": spec}, owner
    )


def render_expose_policy(
    *,
    tenant: str,
    instance_id: str,
    resource: str,
    name: str,
    namespace: str,
    controller_namespace: str | None,
    controller_ip_blocks: list[str] | None = None,
    owner: dict[str, Any] | None = None,
    pod_selector: dict[str, str] | None = None,
    fronts: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Ingress 컨트롤러 네임스페이스(와 hostNetwork 컨트롤러의 ipBlock)에서 앞단 파드로 오는
    ingress 만 추가 허용한다. 테넌트 정책과 union 이라 충돌하지 않는다. podSelector 는
    pod_selector 가 있으면 그것(에이전트 파드의 daegam.devops.ai.kr/* 라벨)이고, 없으면 ToolHive
    앞단 파드의 app.kubernetes.io/instance·name 이다. fronts 는 (앞단 이름, 종류) 여럿이다. 종류는
    파드 라벨의 mcpremoteproxy 나 virtualmcpserver 이고, 앞단을 넘겨받는 MCP Server 는 두 종류의
    파드를 함께 고른다 (P54 5절)."""
    if pod_selector is not None:
        selector: dict[str, Any] = {"matchLabels": dict(pod_selector)}
    elif fronts:
        selector = front_pods_selector(fronts)
    else:
        raise ValueError("render_expose_policy needs pod_selector or fronts")
    sources: list[dict[str, Any]] = []
    if controller_namespace:
        sources.append(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": controller_namespace}
                }
            }
        )
    for cidr in controller_ip_blocks or []:
        sources.append({"ipBlock": {"cidr": cidr}})
    return with_owner(
        {
            "apiVersion": INGRESS_API,
            "kind": "NetworkPolicy",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": daegam_labels(tenant=tenant, resource=resource, id=instance_id),
            },
            "spec": {
                "podSelector": selector,
                "policyTypes": ["Ingress"],
                "ingress": [{"from": sources}] if sources else [],
            },
        },
        owner,
    )


def front_pods_selector(fronts: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """앞단 파드의 podSelector. (앞단 이름, 종류) 하나면 두 라벨의 matchLabels 이고, 여럿이면 두
    라벨이 각각 그 값들 가운데 하나인 matchExpressions 다. 이름의 앞머리(`expose-`, `front-`)가
    종류마다 달라 다른 짝은 실제로 없다."""
    if len(fronts) == 1:
        ((name, kind),) = fronts
        return {"matchLabels": {LABEL_APP_INSTANCE: name, LABEL_APP_NAME: kind}}
    return {
        "matchExpressions": [
            {"key": LABEL_APP_INSTANCE, "operator": "In", "values": sorted({n for n, _ in fronts})},
            {"key": LABEL_APP_NAME, "operator": "In", "values": sorted({k for _, k in fronts})},
        ]
    }
