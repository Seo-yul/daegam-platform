"""role → scope 표 (P6 F2). 코드 상수다. 테넌트 소속·role 은 memberships 컬렉션에 있다.

role: platform-admin(전체) > tenant-admin(테넌트 전체 + 멤버 관리)
      > developer(리소스 CRUD) > viewer(읽기).
cluster:write·hub:share·skill:share·settings:write 는 platform-admin 만, members:write 와
usage:read 는 tenant-admin 이상, audit:read 는 developer 이상(viewer 제외). gateway:* 는 Gateway
라우터(D23)다. agent:* 는 Agent 라우터(D26)다. knowledge:* 는 Knowledge Base 라우터(D32)다.
skill:* 는 Skill 라우터(D64)다.
viewer 는 read 와 `agent:chat` 을, developer 이상이 write·deploy·approve 를 갖는다.

`agent:chat` 이 viewer 에 있는 것은 홈 챗이 배포된 에이전트와 대화하는 자리이기 때문이다(D35).
그 scope 를 요구하는 라우터는 `/chat` 과 `/sessions/{sid}/decisions` 둘뿐이고, 결정도 여기 든다.
`agent:approve` 가 가르는 것은 자기 세션의 승인이 아니라 **남의 세션을 대신 결정하는 것**이다.
배포(`agent:deploy`)는 그대로 developer 위에 남는다.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Literal

PLATFORM_ADMIN = "platform-admin"
TENANT_ADMIN = "tenant-admin"
DEVELOPER = "developer"
VIEWER = "viewer"

# 모든 테넌트를 뜻하는 소속의 tenant 값. platform-admin 은 이 값과 짝지어서만 표현한다
ALL_TENANTS = "*"

ROLES: tuple[str, ...] = (PLATFORM_ADMIN, TENANT_ADMIN, DEVELOPER, VIEWER)
# 테넌트에 붙일 수 있는 role. platform-admin 은 tenant=ALL_TENANTS 로만 표현한다
TENANT_ROLES: tuple[str, ...] = (TENANT_ADMIN, DEVELOPER, VIEWER)
# 그 role 을 받는 API 칸의 타입. Tool 규칙과 서비스 어카운트가 쓴다
TenantRole = Literal["viewer", "developer", "tenant-admin"]
ROLE_RANK: dict[str, int] = {VIEWER: 0, DEVELOPER: 1, TENANT_ADMIN: 2, PLATFORM_ADMIN: 3}

# 플랫폼이 낸 사람의 토큰이 역할을 싣는 클레임과 그 값의 꼴 (P52 3절). 값은
# `daegam-role:<tenant>:<role>` 이고, ToolHive 가 role_claim_name 으로 읽어 THVGroup 으로 삼는다.
# Group Mapping 과 mcp_group 은 이 접두사를 쓸 수 없다
ROLES_CLAIM = "daegam_roles"
ROLE_GROUP_PREFIX = "daegam-role:"
# 플랫폼이 낸 사람의 토큰이 IdP 그룹을 싣는 꼴의 접두사 (P53b 6.6절). 값은
# `daegam-group:<tenant>:<group>` 이다. 역할 그룹처럼 Group Mapping 과 mcp_group 은 쓸 수 없다
IDP_GROUP_PREFIX = "daegam-group:"
RESERVED_GROUP_PREFIXES = (ROLE_GROUP_PREFIX, IDP_GROUP_PREFIX)
# 거절 응답이 대는 예약 접두사
RESERVED_GROUPS_TEXT = (
    f"{' or '.join(RESERVED_GROUP_PREFIXES)}, "
    "which are reserved for the groups the platform puts in its tokens"
)


def reserved_group(group: str) -> bool:
    """플랫폼이 토큰에 싣는 그룹의 꼴인가. 그런 이름의 IdP 그룹이나 mcp_group 은 그 그룹을 가진
    토큰이 역할 규칙이나 그룹 규칙을 지나게 한다."""
    return group.startswith(RESERVED_GROUP_PREFIXES)


def role_group(tenant: str, role: str) -> str:
    return f"{ROLE_GROUP_PREFIX}{tenant}:{role}"


# 플랫폼이 이름을 짓는 그룹(테넌트 그룹 `daegam-<tenant>`, 역할·IdP 그룹의 꼴)의 머리. 이것으로
# 시작하는 IdP 그룹은 플랫폼 토큰에 싣지 않는다 (P53b 6.6절)
PLATFORM_GROUP_HEAD = "daegam-"


def idp_group(tenant: str, group: str) -> str:
    """IdP 그룹 하나를 플랫폼 토큰에 싣는 꼴 `daegam-group:<tenant>:<group>` (P53b 6.6절). 테넌트
    이름에는 `:` 가 없으므로 다른 테넌트의 값과 겹치지 않는다."""
    return f"{IDP_GROUP_PREFIX}{tenant}:{group}"


def carried_idp_groups(groups: Iterable[str], named: Collection[str]) -> list[str]:
    """사람의 IdP 그룹 가운데 플랫폼 토큰이 싣는 것 (P53b 6.6절). 그 테넌트의 Tool 규칙이나 노출
    Agent 의 allowed_groups 가 이름 댄(named) 것만이고, `daegam-` 로 시작하는 것은 싣지 않는다. 원래
    이름을 정렬해 돌려준다. 토큰에는 `idp_group` 의 꼴로 싣는다."""
    return sorted({g for g in groups if g in named and not g.startswith(PLATFORM_GROUP_HEAD)})


SCOPES: frozenset[str] = frozenset(
    {
        "backend:read",
        "backend:write",
        "mcp:read",
        "mcp:write",
        "mcp:deploy",
        "hub:read",
        "hub:write",
        "hub:share",
        "gateway:read",
        "gateway:write",
        "gateway:deploy",
        "agent:read",
        "agent:write",
        "agent:deploy",
        "agent:chat",
        "agent:approve",
        "knowledge:read",
        "knowledge:write",
        "skill:read",
        "skill:write",
        "skill:share",
        "cluster:read",
        "cluster:write",
        "settings:read",
        "settings:write",
        "members:write",
        # local 모드의 사용자 관리 (D40). platform-admin 만. 목록 읽기는 members:write 도 된다
        "users:write",
        "audit:read",
        # 테넌트의 사용량과 비용, 사람별 표 (P37 14절). tenant-admin 이상
        "usage:read",
    }
)

_VIEWER = frozenset(
    {
        "backend:read",
        "mcp:read",
        "hub:read",
        "gateway:read",
        "agent:read",
        # 홈 챗에서 배포된 에이전트와 대화한다 (D35). 자기 세션의 결정도 여기 든다
        "agent:chat",
        "knowledge:read",
        "skill:read",
        "cluster:read",
        "settings:read",
    }
)
_DEVELOPER = _VIEWER | {
    "backend:write",
    "mcp:write",
    "mcp:deploy",
    "hub:write",
    "gateway:write",
    "gateway:deploy",
    "agent:write",
    "agent:deploy",
    "agent:approve",
    "knowledge:write",
    "skill:write",
    "audit:read",
}
_TENANT_ADMIN = _DEVELOPER | {"members:write", "usage:read"}

ROLE_SCOPES: dict[str, frozenset[str]] = {
    VIEWER: _VIEWER,
    DEVELOPER: _DEVELOPER,
    TENANT_ADMIN: _TENANT_ADMIN,
    PLATFORM_ADMIN: SCOPES,
}

# 테넌트 문맥 없이 검사하는 플랫폼 수준 리소스. 나머지 scope 는 테넌트 안에서의 role 로 판단한다
PLATFORM_RESOURCES: frozenset[str] = frozenset({"cluster", "settings", "users"})


def is_role(role: str) -> bool:
    return role in ROLE_SCOPES


def scopes_for_role(role: str) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())


def scopes_for_roles(roles: Iterable[str]) -> frozenset[str]:
    out: frozenset[str] = frozenset()
    for r in roles:
        out |= scopes_for_role(r)
    return out


def highest_role(roles: Iterable[str]) -> str | None:
    """알려진 role 중 가장 높은 것. 모르는 role 은 건너뛰고, 하나도 없으면 None 이다."""
    return max((r for r in roles if r in ROLE_RANK), key=ROLE_RANK.__getitem__, default=None)


def merge_role(new: str, cur: str | None) -> str:
    """같은 테넌트에 소속이 둘 이상이면 높은 role 을 남긴다. 둘 다 모르는 role 이면 new 를 쓴다."""
    if cur is None:
        return new
    return highest_role([new, cur]) or new


def is_platform_admin_row(tenant: str, role: str) -> bool:
    """소속 한 줄이 platform-admin 인가. ALL_TENANTS 에 붙은 platform-admin 만 그렇다."""
    return tenant == ALL_TENANTS and role == PLATFORM_ADMIN


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]


def role_below(role: str, than: str) -> bool:
    """role 이 than 보다 낮은가. 모르는 역할은 가장 낮다."""
    return ROLE_RANK.get(role, -1) < ROLE_RANK.get(than, -1)


def holds_tenant(tenants: Iterable[str], tenant: str) -> bool:
    """소속 줄의 테넌트들에 tenant 가 있는가. 역할은 묻지 않고 platform-admin 의 `*` 줄은 소속이
    아니다. 개인 토큰 발급과 접근 정리, P33 의 허용과 갱신이 이 판정 하나를 쓴다 (P32 2절)."""
    return tenant != ALL_TENANTS and tenant in set(tenants)
