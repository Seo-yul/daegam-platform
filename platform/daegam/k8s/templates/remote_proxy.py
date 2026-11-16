"""MCPRemoteProxy 렌더. 허브 remote 항목(P4)과 MCP Server 노출 앞단(P8, D22)이 같이 쓴다."""

from __future__ import annotations

from typing import Any

from daegam.k8s.labels import daegam_labels
from daegam.k8s.templates.common import (
    PROXY_PORT,
    TOOLHIVE_API,
    hub_labels,
    is_private_endpoint,
    proxy_resources,
    with_owner,
)


def render_remote_proxy(
    *,
    tenant: str,
    item_id: str,
    name: str,
    namespace: str,
    url: str,
    transport: str,
    resource: str | None = None,
    oidc_config_ref: dict[str, Any] | None = None,
    authz_config: dict[str, Any] | None = None,
    authz_config_ref: dict[str, Any] | None = None,
    trust_proxy_headers: bool = False,
    audit: bool = False,
    owner: dict[str, Any] | None = None,
    external_auth_config: str | None = None,
    pod_annotations: dict[str, str] | None = None,
    service_account: str | None = None,
) -> dict[str, Any]:
    """MCPRemoteProxy. 허브 remote 항목(기본)과 노출 앞단 `expose-<k8s.name>`(resource 를 주면
    대감 공통 라벨 + 그 resource, D22)이 같은 렌더러를 쓴다. groupRef 는 넣지 않는다 (그룹 소속은
    Gateway 그룹의 Entry 래퍼가 담당). 같은 그룹에 게시 프록시가 들어가면 vMCP 가 자기 자신을
    백엔드로 발견해 무한 재귀한다 (실측). 앞단은 oidcConfigRef(resourceUrl 포함)·Cedar authzConfig·
    trustProxyHeaders·audit 를 켠다. external_auth_config(bearerToken MCPExternalAuthConfig)는
    앞단이 안쪽 MCPServer 에 낼 서비스 토큰이고(D41), pod_annotations 는 그 토큰의 판이라 바뀌면
    앞단이 굴러간다. service_account 를 주면(P50 10절, G10) 오퍼레이터가 프록시마다 만드는 Role 없이
    역할 없는 ServiceAccount 로 돈다."""
    labels = (
        daegam_labels(tenant=tenant, resource=resource, id=item_id)
        if resource
        else hub_labels(tenant, item_id, "remote")
    )
    spec: dict[str, Any] = {
        "remoteUrl": url,
        "transport": transport,
        "allowPrivateEndpoint": is_private_endpoint(url),
        "proxyPort": PROXY_PORT,
        # 비어 있으면 ToolHive 는 프록시 컨테이너에 자원을 넣지 않는다 (D54)
        "resources": proxy_resources(),
    }
    if oidc_config_ref:
        spec["oidcConfigRef"] = dict(oidc_config_ref)
    if authz_config:
        spec["authzConfig"] = authz_config
    if authz_config_ref:
        # Cedar 를 든 MCPAuthzConfig 참조 (P50 8절). CRD 는 authzConfig 와 함께 받지 않는다
        spec["authzConfigRef"] = dict(authz_config_ref)
    if trust_proxy_headers:
        spec["trustProxyHeaders"] = True
    if audit:
        spec["audit"] = {"enabled": True}
    if external_auth_config:
        spec["externalAuthConfigRef"] = {"name": external_auth_config}
    if pod_annotations:
        spec["podTemplateSpec"] = {"metadata": {"annotations": dict(pod_annotations)}}
    if service_account:
        spec["serviceAccount"] = service_account
    return with_owner(
        {
            "apiVersion": TOOLHIVE_API,
            "kind": "MCPRemoteProxy",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": spec,
        },
        owner,
    )


def runner_binding_name(name: str) -> str:
    """오퍼레이터가 MCPRemoteProxy 마다 만드는 RoleBinding 이름. spec.serviceAccount 를 주어도
    오퍼레이터는 전에 만든 것을 지우지 않는다 (P50 10절)."""
    return f"{name}-remote-proxy-runner"
