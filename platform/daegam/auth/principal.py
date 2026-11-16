"""요청 주체. `require_user()` 가 만들고 라우터·서비스가 읽는다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from daegam.auth import rbac


def token_username(raw: Mapping[str, Any], email: str | None) -> str:
    """토큰이 대는 사람의 이름. `preferred_username`, 없으면 이메일, 둘 다 없으면 빈 글자다. 턴
    위임의 `preferred_username` 과 oidc 모드의 사람 기록이 이것을 쓴다."""
    return str(raw.get("preferred_username") or email or "")


@dataclass(frozen=True)
class Membership:
    """테넌트 소속. platform-admin 은 tenant=ALL_TENANTS 하나로 표현한다."""

    tenant: str
    role: str
    source: str = "manual"  # okta_group | manual


@dataclass
class Principal:
    sub: str
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    client_id: str | None = None  # client credentials 토큰이면 cid
    memberships: list[Membership] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def subject_type(self) -> str:
        return "client" if self.client_id else "user"

    @property
    def is_platform_admin(self) -> bool:
        return any(rbac.is_platform_admin_row(m.tenant, m.role) for m in self.memberships)

    @property
    def tenants(self) -> list[str]:
        """소속 테넌트 (와일드카드 제외, 정렬)."""
        return sorted({m.tenant for m in self.memberships if m.tenant != rbac.ALL_TENANTS})

    def is_member(self, tenant: str) -> bool:
        return self.is_platform_admin or any(m.tenant == tenant for m in self.memberships)

    def roles_in(self, tenant: str) -> set[str]:
        return {m.role for m in self.memberships if m.tenant in (tenant, rbac.ALL_TENANTS)}

    def is_tenant_admin(self, tenant: str) -> bool:
        """그 테넌트에서 tenant-admin 이상인가. platform-admin 은 tenant="*" 로 함께 걸린다."""
        role = rbac.highest_role(self.roles_in(tenant))
        return role is not None and rbac.role_at_least(role, rbac.TENANT_ADMIN)

    def scopes_in(self, tenant: str) -> frozenset[str]:
        return rbac.scopes_for_roles(self.roles_in(tenant))

    @property
    def all_scopes(self) -> frozenset[str]:
        """어느 테넌트에서든 가진 scope 의 합집합. 플랫폼 수준 리소스(cluster·settings) 검사용."""
        return rbac.scopes_for_roles(m.role for m in self.memberships)


def header_principal() -> Principal:
    """header 모드(로컬·테스트) 의 고정 주체. 전체 scope 를 가진 platform-admin 이다."""
    return Principal(
        sub="header",
        memberships=[
            Membership(tenant=rbac.ALL_TENANTS, role=rbac.PLATFORM_ADMIN, source="manual")
        ],
    )
