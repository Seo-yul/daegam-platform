"""Tenant Settings. 테넌트는 경로로 받는다 (헤더보다 우선)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from daegam.api._common import check_tenant, error, forbidden, patched_fields
from daegam.api.deps import User, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.tenancy import TenantMembership, TenantSettingsOut
from daegam.auth import rbac
from daegam.errors import revision_conflict
from daegam.services.tenancy import TenancyService, check_namespace_name
from daegam.store.tenants import (
    AgentOAuth,
    Binding,
    MongoTenantSettingsStore,
    TenantNamespace,
    TenantSettings,
    default_mcp_group,
)

router = APIRouter(responses=ERROR_RESPONSES, prefix="/tenants", tags=["tenants"])


def get_tenant_store(request: Request) -> MongoTenantSettingsStore:
    return request.app.state.tenant_settings


TenantStore = Annotated[MongoTenantSettingsStore, Depends(get_tenant_store)]


class TenantSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_cluster_id: str | None = None
    allowed_cluster_ids: list[str] | None = None
    namespace: TenantNamespace | None = None
    quota: dict[str, Any] | None = None
    default_bindings: list[Binding] | None = None
    mcp_group: str | None = None
    agent_client_ids: list[str] | None = None
    agent_oauth: AgentOAuth | None = None
    # platform-admin settings:write 전용 (D5·D22). 저장 시점에 각 네임스페이스를 보장한다
    allowed_namespaces: list[str] | None = None


def _out(s: TenantSettings) -> dict[str, Any]:
    return s.model_dump(mode="json")


@router.get("", response_model=list[TenantMembership])
async def list_my_tenants(store: TenantStore, principal: User) -> list[dict[str, Any]]:
    """내 소속 테넌트 [{tenant, role}]. platform-admin 은 "*" 와 tenant_settings 의 테넌트 전부.
    UI 테넌트 선택기가 쓴다."""
    by_tenant: dict[str, str] = {}
    for m in principal.memberships:
        by_tenant[m.tenant] = rbac.merge_role(m.role, by_tenant.get(m.tenant))
    if principal.is_platform_admin:
        for t in await store.list_tenants():
            by_tenant.setdefault(t, rbac.PLATFORM_ADMIN)
    return [
        {"tenant": t, "role": by_tenant[t]}
        for t in sorted(by_tenant, key=lambda t: (t != rbac.ALL_TENANTS, t))
    ]


@router.get(
    "/{tenant}/settings",
    response_model=TenantSettingsOut,
    dependencies=[Depends(require_scope("settings:read", path_tenant=True))],
)
async def get_tenant_settings(store: TenantStore, tenant: str) -> dict[str, Any]:
    return _out(await store.get(check_tenant(tenant)))


@router.patch(
    "/{tenant}/settings",
    response_model=TenantSettingsOut,
    dependencies=[Depends(require_scope("members:write", path_tenant=True))],
)
async def patch_tenant_settings(
    store: TenantStore, tenant: str, patch: TenantSettingsPatch, request: Request, user: User
) -> dict[str, Any]:
    tenant = check_tenant(tenant)
    # 보낸 필드만 넣고 중첩 모델은 통째로 덤프한다. exclude_unset 은 중첩 모델 안의 보내지 않은
    # 필드까지 빼는데, store.update 는 얕게 합치므로 저장된 중첩 값이 바뀐다
    changes = patch.model_dump(include=patched_fields(patch))
    # local 모드의 MCP 그룹은 daegam-<tenant> 로 고정이다(human_mcp_group). 바꿀 수 있으면
    # 다른 테넌트의 그룹을 가져와 그 Gateway 를 열 수 있으므로 역할과 무관하게 받지 않는다 (P32 2절)
    if "mcp_group" in changes and request.app.state.settings.auth_mode == "local":
        raise error(422, "invalid_value", "mcp_group is fixed to daegam-<tenant> in local mode")
    # 플랫폼이 토큰에 싣는 역할 그룹(P52 3절)과 IdP 그룹(P53b 6.6절)의 접두사는 예약돼 있다
    if rbac.reserved_group(changes.get("mcp_group") or ""):
        raise error(
            422,
            "invalid_value",
            f"mcp_group cannot start with {rbac.RESERVED_GROUPS_TEXT}",
        )
    # 데이터 플레인이 플랫폼을 믿는 동안 다른 테넌트의 고정 그룹이나 그 밖의 `daegam-` 이름을 적으면
    # 다음 기동이 데이터 플레인을 idp 로 남긴다(`issuer_switch.held_settings`, P53b 6.10절)
    group = changes.get("mcp_group") or ""
    settings = request.app.state.settings
    if (
        settings.oidc_trusts_platform
        and group.startswith(rbac.PLATFORM_GROUP_HEAD)
        and group != default_mcp_group(tenant)
    ):
        raise error(
            422,
            "invalid_value",
            "while the data plane trusts the platform, mcp_group cannot name another tenant's "
            f"group or another {rbac.PLATFORM_GROUP_HEAD} name",
        )
    # UI 의 client id 는 모든 사람의 로그인 토큰이 싣는다. Agent client id 로 두면 Gateway 와 노출
    # 앞단의 client id 정책이 그룹과 상관없이 모든 사람을 받는다
    ui_client = request.app.state.settings.ui_client_id
    if ui_client and ui_client in (changes.get("agent_client_ids") or []):
        raise error(
            422,
            "agent_client_id_is_ui_client",
            "agent_client_ids must not hold the UI client id (DAEGAM_OIDC_CLIENT_ID), because "
            "every person's login token carries it",
        )

    clusters = request.app.state.clusters
    ids = set()
    if changes.get("default_cluster_id"):
        ids.add(changes["default_cluster_id"])
    ids.update(changes.get("allowed_cluster_ids") or [])
    for cid in sorted(ids):
        if await clusters.get(cid) is None:
            raise error(422, "unknown_cluster", cid)
    allowed = changes.get("allowed_cluster_ids")
    default = changes.get("default_cluster_id")
    if allowed is not None and default and default not in allowed:
        raise error(422, "invalid_value", "default_cluster_id must be in allowed_cluster_ids")
    # namespace 는 테넌트가 배포되는 자리를 옮기고, allowed_namespaces 는 테넌트 경계를 넓힌다.
    # 그래서 tenant-admin 의 members:write 로는 못 바꾼다. 클러스터 검사(422) 뒤에 두어야 오류가
    # 나는 순서가 예전과 같다
    if changes.keys() & {"namespace", "allowed_namespaces"} and (
        "settings:write" not in user.all_scopes
    ):
        raise forbidden("scope settings:write required", scope="settings:write", tenant=tenant)
    # 저장의 기준이 되는 읽기다. 라벨을 다는 동안 지나간 저장과도 부딪히도록 클러스터에 쓰기 전에
    # 읽는다. 저장이 지면 단 라벨은 남는다 (P68 2절)
    base = await store.get(tenant)
    tenancy: TenancyService = request.app.state.tenancy
    reserved = tenancy.cluster_service.settings.reserved_namespaces
    if "namespace" in changes:
        # 이름은 플랫폼·게이트웨이·k8s 네임스페이스가 아니어야 한다
        name = (changes["namespace"] or {}).get("name")
        if name:
            check_namespace_name(name, reserved=reserved)
    if "allowed_namespaces" in changes:
        namespaces = list(dict.fromkeys(changes["allowed_namespaces"] or []))
        for ns in namespaces:
            check_namespace_name(ns, reserved=reserved)
        changes["allowed_namespaces"] = namespaces
        cluster = await tenancy.resolve_cluster(tenant, changes.get("default_cluster_id"))
        for ns in namespaces:
            await tenancy.ensure_namespace(tenant, cluster, ns)
    try:
        saved = await store.update(tenant, changes, updated_by=user.sub, base=base)
    except ValidationError as e:
        raise error(422, "invalid_value", str(e)) from e
    if saved is None:
        raise revision_conflict(error, "tenant", f"{tenant} settings")
    return _out(saved)
