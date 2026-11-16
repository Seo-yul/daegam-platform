"""MCPAuthzConfig 렌더 (P50 8절, G8).

vMCP 와 노출 앞단은 Cedar 정책을 인라인으로 싣지 않고 `<CR 이름>-authz` 라는 MCPAuthzConfig 를
참조한다. 이름이 CR 마다 달라 Gateway·앞단·서버의 설정이 이름을 나누지 않는다. 정책만 바뀌면 그 설정
하나만 바뀌고 오퍼레이터가 참조하는 CR 을 스스로 굴린다(F0 실측).

내부 MCPServer(MCP Server, 허브 image 항목, image 복제본)는 테넌트 울타리를 같은 이름의 설정에
싣는다(`tenant_fence`, P50 11절).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from daegam.auth.cedar import render_authz_inline, tenant_fence_policies
from daegam.k8s.gates import Feature
from daegam.k8s.labels import ensure_labels
from daegam.k8s.templates.common import TOOLHIVE_API, with_owner

AUTHZ_CONFIG_KIND = "MCPAuthzConfig"


def authz_config_name(cr_name: str) -> str:
    return f"{cr_name}-authz"


def authz_config_ref(cr_name: str) -> dict[str, str]:
    """CR 의 `authzConfigRef` (vMCP 는 `incomingAuth.authzConfigRef`)."""
    return {"name": authz_config_name(cr_name)}


def render_authz_config(
    *,
    cr_name: str,
    namespace: str,
    policies: list[str],
    tenant: str,
    resource: str,
    id: str,
    groups_claim: str | None,
    owner: dict[str, Any] | None = None,
    roles_claim: str | None = None,
) -> dict[str, Any]:
    """`type: cedarv1` 과 `config.policies` (F0). 참조하는 CR 이 소유한다. 그 CR 이 있는 동안 지우면
    Terminating 으로 남는다. groups_claim 이 있으면 ToolHive 가 THVGroup 을 그 클레임에서 읽는다
    (`config.group_claim_name`)."""
    config: dict[str, Any] = {"policies": list(policies)}
    if groups_claim:
        config["group_claim_name"] = groups_claim
    if roles_claim:
        # 역할 절이 있는 객체만이다 (P52 3절, F0 2번)
        config["role_claim_name"] = roles_claim
    return with_owner(
        ensure_labels(
            {
                "apiVersion": TOOLHIVE_API,
                "kind": AUTHZ_CONFIG_KIND,
                "metadata": {"name": authz_config_name(cr_name), "namespace": namespace},
                "spec": {"type": "cedarv1", "config": config},
            },
            tenant=tenant,
            resource=resource,
            id=id,
        ),
        owner,
    )


# 테넌트 울타리 설정의 라벨 daegam.devops.ai.kr/resource (P50 11절)
RESOURCE_TENANT_FENCE = "tenant-fence"


@dataclass(frozen=True)
class TenantFence:
    """내부 MCPServer 의 테넌트 울타리. config 는 서버보다 먼저 적용하고 받아들여지기를 기다릴
    MCPAuthzConfig 이고(참조일 때), spec 은 MCPServer spec 에 더할 칸이다."""

    config: dict[str, Any] | None
    spec: dict[str, Any]


def tenant_fence(
    features: frozenset[Feature],
    *,
    cr_name: str,
    namespace: str,
    tenant: str,
    id: str,
    rules: Sequence[str] = (),
) -> TenantFence | None:
    """클러스터가 울타리 게이트(0.52)를 지나면 그 울타리, 아니면 None 이다(그 앞의 판에서 Cedar 를
    걸지 않는 까닭은 docs/guide/auth.md 5절). G8 게이트도 지나면 `<CR 이름>-authz` 를 참조하고,
    지나지 못하면 인라인이다(CRD 는 둘을 함께 받지 않는다). 설정에는 소유자를 달지 않는다. 해제·
    삭제·복제본 정리가 이름으로 지우므로, 낡은 uid 로 단 소유자 때문에 GC 되는 일이 없다.
    rules 는 그 서버의 Tool 규칙 forbid 다(P52 2절). 울타리 뒤에 붙고, 없으면 렌더는 오늘과 같다."""
    if Feature.TENANT_FENCE not in features:
        return None
    policies = [*tenant_fence_policies(tenant), *rules]
    if Feature.AUTHZ_REF not in features:
        return TenantFence(None, {"authzConfig": render_authz_inline(policies)})
    config = render_authz_config(
        cr_name=cr_name,
        namespace=namespace,
        policies=policies,
        tenant=tenant,
        resource=RESOURCE_TENANT_FENCE,
        id=id,
        # 내부 서버에는 서비스 토큰만 오므로 그룹 클레임을 싣지 않는다
        groups_claim=None,
    )
    return TenantFence(config, {"authzConfigRef": authz_config_ref(cr_name)})
