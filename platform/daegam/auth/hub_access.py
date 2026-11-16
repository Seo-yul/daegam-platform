"""허브 항목과 Skill(D64)이 함께 쓰는 접근 정책 `access` 의 모델과 판정 (D23,
docs/guide/hub-gateway.md 7절).

세 동작 view·consume·manage 마다 `Subjects{tenants, roles, groups}` 다. `tenants` 는 경계이고
`roles` 와 `groups` 는 OR 로 묶인 주체 조건이다. 둘 다 비어 있으면 그 동작의 기본 최소 role
이상이다. platform-admin 은 테넌트 경계 안에서는 항상 통과하고 상위 동작은 하위를 포함한다
(manage ⊇ consume ⊇ view).
이 모듈은 라우터 scope 검사 뒤에서만 뜻이 있다. `access` 는 scope 를 좁힐 뿐 넓히지 않는다.
전부 순수 함수라 표로 테스트한다.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from daegam.auth import rbac
from daegam.auth.principal import Principal
from daegam.errors import ServiceError

Action = Literal["view", "consume", "manage"]
ACTIONS: tuple[Action, ...] = ("view", "consume", "manage")

# 둘 다 비어 있을 때의 기본 최소 role. view 는 테넌트 소속 전원, consume·manage 는 developer 이상
DEFAULT_MIN_ROLE: dict[str, str] = {
    "view": rbac.VIEWER,
    "consume": rbac.DEVELOPER,
    "manage": rbac.DEVELOPER,
}
# 상위 동작이 하위를 포함한다. 동작 a 는 여기 나열된 Subjects 중 하나라도 맞으면 된다
IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "view": ("view", "consume", "manage"),
    "consume": ("consume", "manage"),
    "manage": ("manage",),
}
ALL_TENANTS = "*"

# 다른 테넌트에 열 수 있는 kind. internal(MCP Server)은 서비스 토큰 audience 가 테넌트마다라
# 다른 테넌트의 소비자가 인증할 수 없고(D41), published(vMCP)는 호출자 토큰을 받을 수 없으며(D5),
# 둘 다 복제할 정의가 없다. 플랫폼 웹 Tool 은 주인이 없어 모든 테넌트가 다른 테넌트다. fetch 는
# 복제되고 search 는 어느 네임스페이스에서나 닿는다 (P39b 13절)
CROSS_TENANT_KINDS: tuple[str, ...] = ("remote", "entry", "image", "platform")


class Subjects(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenants: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)

    @property
    def empty_subjects(self) -> bool:
        return not self.roles and not self.groups


class Access(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: Subjects = Field(default_factory=Subjects)
    consume: Subjects = Field(default_factory=Subjects)
    manage: Subjects = Field(default_factory=Subjects)

    def subjects(self, action: str) -> Subjects:
        return getattr(self, action)


class Permissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: bool
    consume: bool
    manage: bool


class AccessError(ServiceError):
    """정책 검증 실패. 모양이 서비스 오류와 같아 놓친 자리도 전역 처리기가 같은 응답으로 올린다."""


class HasAccess(Protocol):
    """판정에 필요한 항목의 면. HubItem 과 Skill(D64)이 만족한다."""

    tenant: str
    access: Access


class NamedAccess(HasAccess, Protocol):
    """상태 경고에 이름으로 적히는 항목."""

    name: str


def _in_boundary(tenant: str, owner: str, s: Subjects) -> bool:
    return tenant == owner or ALL_TENANTS in s.tenants or tenant in s.tenants


def _subject_matches(principal: Principal, tenant: str, action: str, s: Subjects) -> bool:
    roles = principal.roles_in(tenant)
    if s.empty_subjects:
        need = DEFAULT_MIN_ROLE[action]
        return any(rbac.role_at_least(r, need) for r in roles)
    if any(rbac.role_at_least(r, need) for need in s.roles for r in roles):
        return True
    return bool(set(s.groups) & set(principal.groups))


def can(principal: Principal, tenant: str, item: HasAccess, action: Action) -> bool:
    """요청 테넌트 tenant 에서 principal 이 item 에 action 을 할 수 있는가
    (docs/guide/hub-gateway.md 7절 판정 규칙).
    platform-admin 은 주체 조건을 건너뛰지만 테넌트 경계는 그대로 본다. 경계 밖 테넌트 문맥에서
    다른 테넌트의 internal 항목을 백엔드로 고르면 서비스 토큰 audience 가 맞지 않아 인증할 수 없는
    게이트웨이가 생기고(D41), 항목을 다루려면 그 테넌트 문맥(X-Daegam-Tenant)으로 들어가면 되기
    때문이다."""
    if not item.tenant and action != "consume" and principal.is_platform_admin:
        # 주인 없는 항목(플랫폼 웹 Tool, P39b 13절)은 platform-admin 이 어느 테넌트에서나 보고
        # 관리한다. consume 은 누구에게나 경계를 따른다
        return True
    for a in IMPLIED_BY[action]:
        s = item.access.subjects(a)
        if not _in_boundary(tenant, item.tenant, s):
            continue
        if principal.is_platform_admin or _subject_matches(principal, tenant, a, s):
            return True
    return False


def visible[T: HasAccess](principal: Principal, tenant: str, items: Iterable[T]) -> list[T]:
    """items 가운데 principal 이 tenant 에서 view 할 수 있는 것이다. 목록은 저장소가 고른 후보를
    이것으로 거른다."""
    return [i for i in items if can(principal, tenant, i, "view")]


def permissions_for(principal: Principal, tenant: str, item: HasAccess) -> Permissions:
    return Permissions(
        view=can(principal, tenant, item, "view"),
        consume=can(principal, tenant, item, "consume"),
        manage=can(principal, tenant, item, "manage"),
    )


def permissions_dict(principal: Principal, tenant: str, item: HasAccess) -> dict[str, bool]:
    """요청 주체가 항목에 할 수 있는 동작. 허브 항목·MCP Server·Skill 응답의 `permissions` 다."""
    return permissions_for(principal, tenant, item).model_dump()


def tenant_in_boundary(tenant: str, item: HasAccess, action: Action) -> bool:
    """주체 없이 경계만 본다. 살아 있는 Gateway 의 경고와 공유 복제본 기록의 동결이 쓴다."""
    return any(
        _in_boundary(tenant, item.tenant, item.access.subjects(a)) for a in IMPLIED_BY[action]
    )


def revoked_lines(code: str, tenant: str, items: Iterable[NamedAccess]) -> list[str]:
    """배포가 붙든 항목 가운데 tenant 가 consume 경계 밖으로 빠진 것의 상태 경고(`code: 이름`)다.
    배포된 문서에는 주체가 없어 경계만 본다. 뜬 것은 건드리지 않고 다음 저장·재배포에서 422 다."""
    return [
        f"{code}: {item.name}" for item in items if not tenant_in_boundary(tenant, item, "consume")
    ]


def scope_of(item: HasAccess) -> Literal["tenant", "shared"]:
    """응답의 읽기 전용 scope. 다른 테넌트에 view 나 consume 이 열려 있으면 shared 다."""
    for a in ("view", "consume"):
        if any(t != item.tenant for t in item.access.subjects(a).tenants):
            return "shared"
    return "tenant"


def opens_other_tenants(access: Access, owner: str) -> bool:
    """정책이 어느 동작으로든 주인 테넌트 밖(다른 테넌트나 `*`)을 여는가. Secret env 를 든 허브
    항목은 그런 정책을 받지 않는다 (P55 5절)."""
    return any(_foreign(access.subjects(a).tenants, owner) for a in ACTIONS)


def _foreign(tenants: list[str], owner: str) -> set[str]:
    return {t for t in tenants if t != owner}


def _widens(new: list[str], current: list[str], owner: str) -> bool:
    """경계가 넓어지는가. current 에 `*` 가 있으면 어떤 목록도 넓어지지 않는다."""
    cur = _foreign(current, owner)
    if ALL_TENANTS in cur:
        return False
    return bool(_foreign(new, owner) - cur)


def widened_actions(before: Access, after: Access, owner: str) -> list[Action]:
    """경계가 넓어지는 동작. `validate_access` 가 이 동작에 hub:share 를 요구하고, 이미지가 거부된
    허브 항목은 하나라도 있으면 저장하지 않는다 (P55 6절)."""
    return [
        a for a in ACTIONS if _widens(after.subjects(a).tenants, before.subjects(a).tenants, owner)
    ]


def narrowed(before: Access, after: Access, owner: str, action: Action) -> bool:
    """action 의 경계에서 빠지는 다른 테넌트가 있는가 (`_widens` 의 반대). 경계는 그 동작을 품는
    동작 (`IMPLIED_BY`)의 테넌트를 합친 것이다. after 에 `*` 가 있으면 아무도 빠지지 않고, before
    에만 `*` 가 있으면 빠진다. `[a]` 를 `[*]` 로 넓히는 것은 빼는 것이 아니다."""

    def boundary(access: Access) -> set[str]:
        return {t for a in IMPLIED_BY[action] for t in _foreign(access.subjects(a).tenants, owner)}

    old, new = boundary(before), boundary(after)
    if ALL_TENANTS in new:
        return False
    return ALL_TENANTS in old or bool(old - new)


def validate_access(
    new: Access,
    *,
    current: Access,
    owner_tenant: str,
    cross_tenant: Collection[Action],
    share_scope: str,
    principal: Principal,
) -> Access:
    """저장할 정책을 검증해 정규화한 사본을 돌려준다.
    - roles 는 ROLES 안의 값 (422 invalid_role)
    - cross_tenant 는 다른 테넌트를 넣을 수 있는 동작이다. 그 밖의 동작에 다른 테넌트를 넣으면
      422 다(`_cross_tenant_error`)
    - 세 동작 중 하나라도 경계가 current 보다 넓어지면 platform-admin 과 share_scope (403 forbidden)
    허브는 kind 에 따라 세 동작 전부나 빈 목록을 hub:share 와 함께 넘기고, Skill 은 view·consume 을
    skill:share 와 함께 넘긴다. 생성 시 current 는 빈 정책이다. 경계를 유지하거나 좁히는 저장은
    manage 만으로 된다."""
    normalized = Access()
    for action in ACTIONS:
        s = new.subjects(action)
        tenants = _dedupe(s.tenants)
        roles = _dedupe(s.roles)
        groups = _dedupe(s.groups)
        bad = [r for r in roles if r not in rbac.ROLES]
        if bad:
            raise AccessError(
                422,
                "invalid_role",
                f"{action}.roles: unknown role(s) {', '.join(bad)}",
                action=action,
                roles=bad,
            )
        foreign = _foreign(tenants, owner_tenant)
        if foreign and action not in cross_tenant:
            raise _cross_tenant_error(action, cross_tenant, sorted(foreign))
        setattr(normalized, action, Subjects(tenants=tenants, roles=roles, groups=groups))
    wider = widened_actions(current, normalized, owner_tenant)
    if wider and not (principal.is_platform_admin and share_scope in principal.all_scopes):
        raise AccessError(
            403,
            "forbidden",
            "opening this item to other tenants needs platform-admin",
            scope=share_scope,
            actions=wider,
        )
    return normalized


# 일부 동작만 다른 테넌트에 여는 리소스가 그 밖의 동작에 다른 테넌트를 넣었을 때의 코드. 화면의
# 오류 표가 글자 그대로 찾으므로 조립하지 않는다
CROSS_TENANT_ACTION_CODES: dict[Action, str] = {
    "view": "cross_tenant_view_unsupported",
    "consume": "cross_tenant_consume_unsupported",
    "manage": "cross_tenant_manage_unsupported",
}


def _cross_tenant_error(
    action: Action, cross_tenant: Collection[Action], foreign: list[str]
) -> AccessError:
    """다른 테넌트에 열 수 없는 동작에 다른 테넌트를 넣었다. 어느 동작도 열지 못하는 항목(허브의
    internal·published)은 `cross_tenant_unsupported`, 일부만 여는 리소스(Skill 의 manage)는
    `CROSS_TENANT_ACTION_CODES` 의 동작별 코드다."""
    if not cross_tenant:
        detail = f"this item cannot be opened to other tenants ({action}.tenants)"
        return AccessError(422, "cross_tenant_unsupported", detail, action=action, tenants=foreign)
    opened = " or ".join(cross_tenant)
    return AccessError(
        422,
        CROSS_TENANT_ACTION_CODES[action],
        f"{action} stays in the owner tenant; open {opened} to other tenants instead",
        action=action,
        tenants=foreign,
    )


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out
