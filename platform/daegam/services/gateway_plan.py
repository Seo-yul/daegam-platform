"""Gateway 의 이름 (P72 2.2절). 순수 함수만 있다. K8s 이름, 도구 이름 충돌 경고, 모르는 이름의
안내, 백엔드가 부르는 서버의 종류, Advanced 편집의 잠긴 경로와 표면을 막는 Cedar 다. 서비스와
스윕, 허브 도구 이름 해석이 함께 쓴다. 충돌 계획과 광고하는 도구 이름은
`services/advertised_names.py` 에 있다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from daegam.auth.cedar import forbid_action_policy
from daegam.auth.signer import FETCH_SERVER_ID, SEARCH_SERVER_ID
from daegam.errors import ServiceError
from daegam.k8s.merge import Path, vmcp_locked_paths
from daegam.k8s.templates import proxy_service_url
from daegam.k8s.templates.svc_token import TOKEN_BACKEND_KINDS
from daegam.services.platform_tools import is_fetch
from daegam.services.tenancy import k8s_name
from daegam.store.gateways import (
    Gateway,
    GatewayBackend,
    GatewayBackendSnapshot,
    GatewayRevision,
    GatewaySpec,
    GatewaySurface,
)
from daegam.store.hub import PLATFORM_KIND, HubItem, is_derived

PREFIX_WARNING = (
    "some backends have no tool_names; falling back to prefix mode "
    "(tool names become <workload>_<tool>)"
)

# Gateway 표면의 기능과 그것을 막는 ToolHive Cedar 액션. 0.51.2 의 vMCP 는 resources/read 를
# read_resource, prompts/get 을 get_prompt 로 인가하고 두 목록도 항목마다 같은 액션으로 거른다(F0).
# 열쇠는 `GatewaySurface` 의 필드 이름이다
SURFACE_ACTIONS: dict[str, str] = {"resources": "read_resource", "prompts": "get_prompt"}


def unknown_names_hint(kinds: Iterable[str | None]) -> str:
    """모르는 이름을 어떻게 채우는지. 종류에 따라 길이 다르다.

    `internal`·`published` 에는 도구 새로고침이 없다. 이름을 원본에서 계산하기 때문인데(D49)
    그 종류로 `refresh-tools` 를 부르면 422 다. 그런데도 "새로고침하라" 고만 적으면 없는 버튼을
    찾게 된다."""
    kinds = set(kinds)
    derived = any(is_derived(k) for k in kinds)
    other = any(not is_derived(k) for k in kinds)
    if derived and other:
        return "run refresh-tools, or redeploy the source for internal and published items"
    if derived:
        return "redeploy the source; internal and published items have no refresh-tools"
    return "run refresh-tools"


def priority_unknown_warning(backends: list[tuple[str, str | None]]) -> str:
    """tool_names 를 모르는 백엔드가 있는 priority 모드의 경고. 입력은 (이름, 종류) 목록이다.

    정의 검증(`validate_spec`)과 스냅샷 계획(`plan_conflicts`) 두 진입점이 같은 상황을 본다. 응답을
    만들 때 `_plan` 이 문자열이 같은 경고를 걸러 내므로 문구가 갈라지면 사용자에게 같은 경고가 두 번
    나간다. 그래서 한 곳에서만 만든다."""
    return (
        "priority mode with unknown tool_names for "
        + ", ".join(n for n, _ in backends)
        + ": conflicting tools from those backends cannot be renamed and the first workload "
        f"wins silently ({unknown_names_hint(k for _, k in backends)} or use prefix mode)"
    )


def gateway_k8s_name(name: str) -> str:
    """새 Gateway 의 VirtualMCPServer·MCPGroup 이름.

    MCP 의 `services.mcp_servers.k8s_name_for`·Agent 의 `store.agents.k8s_name_for` 와
    같은 자리다. 이름과 사는 모듈만 다르다."""
    return k8s_name(name, prefix="gw")


def entry_name(gateway_name: str, hub_item_name: str) -> str:
    """Gateway 그룹의 MCPServerEntry 이름 = vMCP workload = prefix 모드의 툴 접두사."""
    return k8s_name(f"{gateway_name}-{hub_item_name}")


def spec_of_revision(rev: GatewayRevision) -> GatewaySpec:
    """리비전에서 spec 필드만."""
    return GatewaySpec(**rev.model_dump(exclude={"revision", "created_at"}))


def vmcp_locked_paths_for(doc: Gateway) -> tuple[Path, ...]:
    """Advanced 편집의 잠긴 경로. 들어오는 인증은 모든 vMCP 에서 잠기고 노출했으면
    oidcConfigRef 전체까지다. ConfigMap 의 authzConfig 도 잠긴다. 플랫폼이 렌더하는 forbid
    (끈 표면 P50 13절, 묶이지 않은 위임 토큰 P53 3.1절)를 소유자의 ConfigMap 이 갈음하기
    때문이다. 잠기기 전에 저장한 것은 다시 렌더하기와 재배포를 locked_field 로 막고, 소유자가
    Advanced 저장으로 지울 때까지 vMCP 는 마지막으로 적용한 정책으로 돈다."""
    return vmcp_locked_paths(doc.exposure.enabled)


def surface_forbids(surface: GatewaySurface) -> list[str]:
    """끈 표면을 막는 Cedar forbid (P50 13절)."""
    return [
        forbid_action_policy(action)
        for key, action in SURFACE_ACTIONS.items()
        if not getattr(surface, key)
    ]


def point_at_clone(b: GatewayBackendSnapshot, name: str, namespace: str) -> None:
    """백엔드 스냅샷이 복제본 name 의 프록시를 가리키게 한다."""
    b.clone_name = name
    b.url = proxy_service_url(name, namespace, remote=b.kind == "remote")


def tool_names_of(item: HubItem, resolved: Mapping[str, Any]) -> list[str]:
    """해석된 이름이 있으면 그것, 없으면 저장된 값.

    해석 실패(`Origin.tool_names is None`)는 "모른다" 이고 "도구가 없다" 가 아니다 (D49). 그것을 빈
    목록으로 뭉개면 `plan_conflicts` 가 이름 규칙을 통째로 `prefix` 로 떨어뜨려 배포된 도구 이름이
    전부 `<workload>_<tool>` 로 바뀐다. 모를 때는 저장된 사본으로 떨어지는데, 배포 스냅샷을 만드는
    `_snapshot_backends` 가 이미 그렇게 한다. 같은 값을 두 경로가 다르게 읽으면 검증을 지난 정의가
    배포에서 다른 것이 된다."""
    origin = resolved.get(item.id)
    names = origin.tool_names if origin is not None else None
    return list(names if names is not None else item.tool_names)


def backend_server_id(item: HubItem, tenant: str, error_cls: type[ServiceError]) -> str | None:
    """Entry 의 토큰이 여는 우리 서버의 id (P50 9절). 이 테넌트의 항목이면 그 항목의 서버
    (`HubItem.server_id`)이고 토큰을 받지 않는 종류는 None 이다. 다른 테넌트의 image 는 이
    네임스페이스의 공유 복제본이 서버라 provision 이 그 기록의 id 를 적는다(`_apply_shared_clones`).
    다른 테넌트의 internal 에는 토큰을 내지 않고 422 cross_tenant_unsupported 다. 플랫폼 fetch 는
    fetch audience 의 열쇠(`FETCH_SERVER_ID`)이고 search 는 search audience 의
    열쇠(`SEARCH_SERVER_ID`)다 (P39b 12·13절)."""
    if item.kind == PLATFORM_KIND:
        return FETCH_SERVER_ID if is_fetch(item) else SEARCH_SERVER_ID
    if item.kind not in TOKEN_BACKEND_KINDS:
        return None
    if item.tenant == tenant:
        return item.server_id
    if item.kind == "image":
        return None
    raise error_cls(
        422,
        "cross_tenant_unsupported",
        f"hub item {item.name} reaches a server of tenant {item.tenant}",
    )


def backend_snapshot_of(
    gateway_name: str,
    backend: GatewayBackend,
    item: HubItem,
    resolved: Mapping[str, Any],
    *,
    tenant: str,
    error_cls: type[ServiceError],
    **deployed: Any,
) -> GatewayBackendSnapshot:
    """정의의 백엔드 하나를 스냅샷 행으로 옮긴다. 이름(`tool_names_of`), 필터, 종류, Entry
    이름(`entry_name`), 서버 id 를 정한다. 배포는 클러스터에서 읽은 칸(url, 복제본, 타임아웃,
    인증 종류)과 도달 확인으로 알게 된 이름을 deployed 로 덮고, 저장 때의 code mode 판정은 덮지
    않는다. 두 경로가 같은 이름으로 판정하게 하려는 것이다 (P56 6.2절)."""
    fields: dict[str, Any] = {
        "hub_item_id": item.id,
        "hub_item_name": item.name,
        "kind": item.kind,
        "workload": entry_name(gateway_name, item.name),
        "url": "",
        "tools": list(backend.tools),
        "tool_names": tool_names_of(item, resolved),
        "server_id": backend_server_id(item, tenant, error_cls),
    }
    return GatewayBackendSnapshot(**{**fields, **deployed})
