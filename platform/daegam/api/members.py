"""테넌트 멤버와 Okta 그룹 매핑 API (P6 F2).

GET/PUT /tenants/{tenant}/members   tenant-admin 이상(members:write). PUT 은 manual 소속 전체
                                    교체이고 빠진 사람의 그 테넌트 개인 토큰이 회수된다
GET/PUT /group-mappings             platform-admin. PUT 은 매핑 전체 교체이고 사람들의 okta_group
                                    소속을 다시 풀어 빠진 사람의 토큰을 회수한다 (P53b 6.3절)
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._common import TENANT_RE, check_tenant, error
from daegam.api.deps import require_platform_admin, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.tenancy import MembershipOut
from daegam.auth import rbac
from daegam.auth.principal import Principal
from daegam.services.memberships import MembershipService
from daegam.store.memberships import GroupMapping, Membership, MongoMembershipStore

router = APIRouter(responses=ERROR_RESPONSES, tags=["members"])


def get_membership_store(request: Request) -> MongoMembershipStore:
    return request.app.state.memberships


def get_membership_service(request: Request) -> MembershipService:
    return request.app.state.membership_service


Memberships = Annotated[MongoMembershipStore, Depends(get_membership_store)]
MembersWrite = Annotated[Principal, Depends(require_scope("members:write", path_tenant=True))]


class MemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub: str = Field(min_length=1, max_length=256)
    role: str


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str = Field(min_length=1, max_length=256)
    tenant: str
    role: str


def _out(m: Membership) -> dict[str, Any]:
    return m.model_dump(mode="json")


@router.get(
    "/tenants/{tenant}/members",
    response_model=list[MembershipOut],
    dependencies=[Depends(require_scope("members:write", path_tenant=True))],
)
async def list_members(store: Memberships, tenant: str) -> list[dict[str, Any]]:
    return [_out(m) for m in await store.list_for_tenant(check_tenant(tenant))]


@router.put("/tenants/{tenant}/members", response_model=list[MembershipOut])
async def put_members(
    service: Annotated[MembershipService, Depends(get_membership_service)],
    principal: MembersWrite,
    tenant: str,
    body: list[MemberIn],
) -> list[dict[str, Any]]:
    tenant = check_tenant(tenant)
    seen: set[str] = set()
    for m in body:
        if m.role not in rbac.TENANT_ROLES:
            raise error(
                422,
                "invalid_role",
                f"role must be one of {', '.join(rbac.TENANT_ROLES)}",
                sub=m.sub,
            )
        if m.sub in seen:
            raise error(422, "duplicate_sub", f"sub {m.sub!r} listed twice")
        seen.add(m.sub)
    rows = await service.replace_manual(tenant, [(m.sub, m.role) for m in body], by=principal.sub)
    return [_out(m) for m in rows]


@router.get(
    "/group-mappings",
    response_model=list[GroupMapping],
    dependencies=[Depends(require_platform_admin)],
)
async def list_group_mappings(store: Memberships) -> list[dict[str, Any]]:
    return [m.model_dump() for m in await store.list_mappings()]


@router.put("/group-mappings", response_model=list[GroupMapping])
async def put_group_mappings(
    service: Annotated[MembershipService, Depends(get_membership_service)],
    principal: Annotated[Principal, Depends(require_platform_admin)],
    body: list[MappingIn],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    items: list[GroupMapping] = []
    for m in body:
        if not rbac.is_role(m.role):
            raise error(422, "invalid_role", f"role must be one of {', '.join(rbac.ROLES)}")
        if rbac.reserved_group(m.group):
            # 플랫폼이 내는 토큰의 역할 그룹(P52 3절)이나 IdP 그룹의 꼴(P53b 6.6절)과 겹치면 그
            # 그룹을 가진 IdP 토큰이 역할 규칙이나 그룹 규칙을 지난다
            raise error(
                422,
                "invalid_value",
                f"groups cannot start with {rbac.RESERVED_GROUPS_TEXT}",
                group=m.group,
            )
        if (m.role == rbac.PLATFORM_ADMIN) != (m.tenant == rbac.ALL_TENANTS):
            raise error(
                422,
                "invalid_value",
                "platform-admin maps to tenant '*' only, and '*' takes platform-admin only",
                group=m.group,
            )
        if m.tenant != rbac.ALL_TENANTS and not TENANT_RE.fullmatch(m.tenant):
            raise error(400, "invalid_tenant", m.tenant)
        key = (m.group, m.tenant)
        if key in seen:
            raise error(422, "duplicate_mapping", f"{m.group} -> {m.tenant} listed twice")
        seen.add(key)
        items.append(GroupMapping(group=m.group, tenant=m.tenant, role=m.role))
    return [m.model_dump() for m in await service.replace_mappings(items, by=principal.sub)]
