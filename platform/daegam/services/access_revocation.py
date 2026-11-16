"""회수 훅과 접근 정리 (P32 7절).

개인 토큰의 회수는 세 걸음이다. 토큰 문서에 요청(사유·시각·누가)을 적고, 슬롯 문서를 지워 JWKS
에서 키를 빼고, `revoked_at` 을 찍는다. 슬롯 삭제가 "회수됨" 보다 늘 먼저라 화면은 회수됨인데 키가
JWKS 에 남는 일이 없다. 둘째나 셋째 걸음이 실패하면 문서는 `revoking` 으로 남고 접근 정리가 다음
틱에 끝낸다.

OAuth 연결(P33 12절)의 회수는 한 걸음이다. 연결 문서에 `revoked_at` 과 사유를 찍고 갱신 토큰의 두
해시를 지운다. 갱신이 그 자리에서 막히고 이미 나간 접근 토큰은 15분 안에 만료한다.

**훅.** 계정 막기·삭제·비밀번호 변경(`LocalAuthService`)과 멤버 교체(`MembershipService`)가 사용자와
소속을 저장한 뒤 부른다. 그 사람의 개인 토큰과 OAuth 연결을 함께 회수하고, 그 사람에게 묶인
백그라운드 실행의 소스를 멈추며 그 사람의 미리 보기를 끝낸다(`on_lost`, P41 6절, P44b 10절).
역할이 낮아진 사람은 `agent:deploy` 를 잃었을 때 미리 보기를 끝낸다(`on_role_lowered`). 빠른 길일
뿐이라 예외로 끝나면 경고 로그만 남기고 삼킨다. 계정 사건은 이미 일어났고 한도의 보장은 접근 정리와
연결의 갱신이 맡는다. 갱신은 같은 규칙(`grant_reason`)으로 사람과 소속을 늘 다시 본다.

**접근 정리.** 리컨실 틱마다(60초, 리스 안) 돈다. 리컨실은 기동하자마자 첫 틱을 돌므로
(`services/periodic.py`) 기동 때의 정리를 따로 부르지 않는다.

1. 회수 요청이 없는 활성 토큰을 그 사람(local 은 사용자 문서: 없으면 `deleted`, 막혔으면
   `disabled`, `token_version` 이 발급 때와 다르면 `password`. oidc 는 사람 기록: 없거나 비웠거나
   마지막 IdP 확인이 긴 한도를 넘었으면 `unconfirmed`)과 그 테넌트의 소속 줄(`membership`), 토큰의
   그룹이 테넌트의 고정 그룹인지(`wrong_group`), 지금 사람 발급자(`issuer_changed`),
   슬롯(`master` 가 지금 kid 가 아니면 `key_rotated`, 없으면 `key_missing`), 역할(`role_changed`),
   토큰이 실은 IdP 그룹(`groups_changed`)에 맞대 회수한다. 서비스 어카운트의 토큰(`sa:`)은 사람
   대신 계정에 맞댄다. 없으면 `deleted`, 역할이 내려갔으면 `role_changed`, 서비스 어카운트를 껐으면
   `accounts_off` 다. 활성 OAuth 연결도 사람과 소속, 발급자, 연결을 만든 서명 키(`key_rotated`),
   인가 서버가 꺼졌는지(`oauth_disabled`, 잠든 서버는 아니다), CIMD 클라이언트가 허용 목록에서
   빠졌는지(`client_disallowed`)에 맞대 회수한다. 사람과 소속은 sub 마다 한 번 읽는다. 발급과 계정
   사건이 엇갈려 훅이 놓친 토큰이 여기서 잡힌다.
2. `revoking` 에 머문 문서의 회수를 끝낸다.
3. 슬롯을 정리한다. 토큰 문서가 회수된 bound 슬롯과, 문서가 없고 잡힌 지 `ORPHAN_GRACE` 가 지난
   bound 슬롯을 지운다. 그 유예는 발급이 슬롯을 잡고 문서를 넣기 전의 틈이다. `master` 가 지금
   kid 가 아닌 슬롯과 플랫폼이 사람 토큰을 내지 않는 header 모드의 pool 슬롯도 지운다. 그래서 header
   모드의 JWKS 는 RSA 키 하나다.
4. 회수가 있었으면 사유마다 로그 한 줄 `access_revoked reason=<r> tokens=<n> grants=<n>` 을, 사유와
   테넌트마다 시스템 주체의 감사 행 하나를 남긴다. 감사 행의 resource 는 개인 토큰이면
   `personal-tokens`, 연결이면 `oauth-grants` 다.

사람 발급자와 pool 은 플랫폼이 사람 토큰을 내는지(`Settings.platform_mints_human_tokens`, local 과
oidc)를 따른다. 데이터 플레인 발급자를 바꾸어도 아무것도 회수하거나 지우지 않는다 (P53b 6.1절).
2.0.0-rc1 이 모르는 까닭(`role_changed`, `unconfirmed`, `groups_changed`, `wrong_group`,
`accounts_off`)은 문서의 새 필드에 두고 rc1 이 아는 사유를 함께 적는다
(`store/personal_tokens.py` `stored_reason`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from daegam.audit.store import MongoAuditStore, record_system_event
from daegam.auth import rbac
from daegam.auth.service_token import InternalIssuer, account_id_of, is_account
from daegam.config import SERVICE_ACCOUNTS_OFF, Settings
from daegam.store.base import utcnow
from daegam.store.idp_people import IdpPerson, MongoIdpPeopleStore
from daegam.store.memberships import Membership, MongoMembershipStore
from daegam.store.oauth_grants import (
    CIMD,
    CLIENT_DISALLOWED,
    OAUTH_DISABLED,
    GrantRevocation,
    MongoOAuthGrantStore,
    OAuthGrant,
)
from daegam.store.pat_keys import MongoPatKeyStore, PatKeySlot
from daegam.store.personal_tokens import (
    ACCOUNTS_OFF,
    DELETED,
    DISABLED,
    GROUPS_CHANGED,
    ISSUER_CHANGED,
    KEY_MISSING,
    KEY_ROTATED,
    MEMBERSHIP,
    PASSWORD,
    ROLE_CHANGED,
    UNCONFIRMED,
    WRONG_GROUP,
    MongoPersonalTokenStore,
    PersonalToken,
    Revocation,
)
from daegam.store.tenants import default_mcp_group
from daegam.store.users import MongoUserStore, User

logger = logging.getLogger("daegam.access_revocation")

# 요청 없이 일어난 회수의 주체. 토큰 문서의 revoked_by 와 감사 행의 sub 다
SWEEP_SUB = "system:access-sweep"
# 발급이 슬롯을 잡고 토큰 문서를 넣기 전의 틈. 이보다 오래된 주인 없는 bound 슬롯만 지운다
ORPHAN_GRACE = timedelta(minutes=5)
# 요청 없는 회수의 감사 행이 싣는 resource
PERSONAL_TOKENS_RESOURCE = "personal-tokens"
OAUTH_GRANTS_RESOURCE = "oauth-grants"


class _Held(Protocol):
    """사람이 한 테넌트에 쥔 접근. 개인 토큰과 OAuth 연결과 인가 코드가 이 모양이다."""

    sub: str
    tv: int
    tenant: str


@dataclass(frozen=True)
class HeldAccess:
    """문서 없이 `lost_access` 에 넘기는 접근이다. 턴 위임과 백그라운드 실행의 사람 확인이 쓴다."""

    sub: str
    tv: int
    tenant: str

    @classmethod
    def of(cls, sub: str, tv: int | None, tenant: str) -> HeldAccess:
        """tv 를 모르면 어떤 사용자 문서와도 맞지 않는 값이다. 비밀번호를 바꾼 것으로 본다."""
        return cls(sub=sub, tv=tv if tv is not None else -1, tenant=tenant)


# 훅이 회수와 함께 부르는 것. (sub, 까닭, 테넌트) 이고 테넌트가 None 이면 그 사람의 모든 테넌트다
LostAccessHook = Callable[[str, Revocation, str | None], Awaitable[object]]
# 역할이 낮아진 사람의 훅. (sub, 테넌트) 다. 미리 보기가 그 사람이 `agent:deploy` 를 잃었는지 본다
RoleLoweredHook = Callable[[str, str], Awaitable[object]]


class ServiceAccountRoles(Protocol):
    """서비스 어카운트의 지금 역할 (P53b 6.7절). 계정이 그 테넌트에 없으면 None 이다. 접근 정리가
    `sa:` 토큰을 사람 대신 이것에 맞댄다."""

    async def role_of(self, account_id: str, tenant: str) -> str | None: ...


@dataclass(frozen=True)
class Holder:
    """sub 하나가 지금 가진 것. 접근 정리가 sub 마다 한 번 읽는다. local 과 header 모드는 사용자
    문서를, oidc 모드는 사람 기록을 읽고, 서비스 어카운트는 계정의 역할만 읽는다."""

    members: list[Membership] = field(default_factory=list)
    user: User | None = None
    person: IdpPerson | None = None
    account_role: str | None = None

    @property
    def tenants(self) -> list[str]:
        return [m.tenant for m in self.members]

    def role_in(self, tenant: str) -> str | None:
        return next((m.role for m in self.members if m.tenant == tenant), None)

    @property
    def idp_groups(self) -> list[str] | None:
        """oidc 모드의 사람 기록이 든 IdP 그룹. 기록이 없거나 그룹을 모르면 None 이다."""
        return self.person.groups if self.person is not None else None

    @property
    def username(self) -> str | None:
        """사람의 이름. local 은 사용자 문서, oidc 는 사람 기록의 것이고 둘 다 없으면 None 이다."""
        named = self.user or self.person
        return named.username if named is not None else None


def _first[R](checks: Iterable[tuple[bool, R]]) -> R | None:
    return next((reason for hit, reason in checks if hit), None)


def account_reason(user: User | None, tv: int) -> Revocation | None:
    """local 계정이 바뀌어 접근을 잃은 사유. 사용자 문서가 없으면 `deleted`, 막혔으면 `disabled`,
    `token_version` 이 tv 와 다르면 `password` 다. Teams 연결(P47 6.3절)도 이 판정을 쓴다."""
    return _first(
        (
            (user is None, DELETED),
            (user is not None and user.disabled, DISABLED),
            (user is not None and user.token_version != tv, PASSWORD),
        )
    )


def lost_access(held: _Held, user: User | None, tenants: list[str]) -> Revocation | None:
    """사람과 소속이 바뀌어 접근을 잃은 사유. 계정의 사유(`account_reason`)이거나, 그 테넌트의
    소속 줄이 없으면 `membership` 이다. local 모드의 개인 토큰과 OAuth 연결, 인가 코드의 교환이 이
    판정 하나를 쓴다."""
    return account_reason(user, held.tv) or _first(
        ((not rbac.holds_tenant(tenants, held.tenant), MEMBERSHIP),)
    )


def lost_idp_access(
    held: _Held, person: IdpPerson | None, tenants: list[str], *, oldest: datetime
) -> Revocation | None:
    """oidc 모드에서 사람이 접근을 잃은 사유 (P53b 6.3절). 사람 기록이 없거나 비웠거나 마지막 IdP
    확인(`confirmed_at`)이 oldest 보다 오래면 `unconfirmed`, 그 테넌트의 소속 줄이 없으면
    `membership` 이다. 지운 계정, 막은 계정, 바뀐 비밀번호는 oidc 모드에 없고 한도가 그 자리를
    갈음한다. 접근 정리, 턴 위임, 실행의 사람 확인이 이 판정 하나를 쓴다."""
    return _first(
        (
            (person is None or not person.confirmed_since(oldest), UNCONFIRMED),
            (not rbac.holds_tenant(tenants, held.tenant), MEMBERSHIP),
        )
    )


def role_reason(token: PersonalToken, now: str | None) -> Revocation | None:
    """토큰이 발급 때보다 낮은 역할(now)이 되었으면 `role_changed` 다 (P52 3절). 사람은 그 테넌트
    소속 줄의 역할이고 서비스 어카운트는 계정의 역할이다. 역할을 남기지 않은 토큰(이 기능 전에 낸
    것)은 건너뛴다. 그 토큰은 역할 규칙을 지나지 못한다. 올라간 역할은 토큰을 다시 만들 때 닿는다.
    역할이 없으면(소속을 잃었거나 계정이 없다) 앞의 판정이 먼저 잡는다."""
    if token.role is None or now is None:
        return None
    return ROLE_CHANGED if rbac.role_below(now, token.role) else None


def groups_reason(token: PersonalToken, person: IdpPerson | None) -> Revocation | None:
    """토큰이 실은 IdP 그룹 가운데 사람 기록에 없는 것이 있으면 `groups_changed` 다 (P53b 6.6절).
    기록이 그룹을 모르면(None) 보지 않는다. 나중에 더해진 그룹은 토큰을 다시 만들 때 닿는다."""
    if not token.idp_groups or person is None or person.groups is None:
        return None
    return GROUPS_CHANGED if set(token.idp_groups) - set(person.groups) else None


def _orphaned(slot: PatKeySlot, revoked: dict[str, bool], cutoff: datetime) -> bool:
    """주인 없는 bound 슬롯인가. 토큰 문서가 있으면 그 토큰이 회수됐을 때이고, 문서가 없으면 잡힌
    지 `ORPHAN_GRACE` 가 지났을 때다. 그 전에는 발급이 아직 문서를 넣는 중일 수 있다."""
    if slot.token_id is not None and slot.token_id in revoked:
        return revoked[slot.token_id]
    return slot.bound_at is not None and slot.bound_at < cutoff


class AccessRevoker:
    def __init__(
        self,
        tokens: MongoPersonalTokenStore,
        slots: MongoPatKeyStore,
        grants: MongoOAuthGrantStore,
        users: MongoUserStore,
        memberships: MongoMembershipStore,
        audit: MongoAuditStore,
        issuer: InternalIssuer,
        *,
        settings: Settings,
        people: MongoIdpPeopleStore,
        accounts: ServiceAccountRoles | None = None,
        on_lost: LostAccessHook | None = None,
        on_role_lowered: RoleLoweredHook | None = None,
    ):
        self.tokens = tokens
        self.slots = slots
        self.grants = grants
        self.users = users
        self.memberships = memberships
        self.audit = audit
        self.issuer = issuer
        # 사람 기록 (P53b 6.3절). oidc 모드의 사람 판정이 읽는다
        self.people = people
        # 서비스 어카운트의 역할. 없으면 `sa:` 토큰은 계정이 없는 것이다
        self.accounts = accounts
        # 아래 다섯은 설정에서 읽는다. 시험이 바꿔 본다
        self.mints = settings.platform_mints_human_tokens
        self.oidc = settings.auth_mode == "oidc"
        self.long_bound = settings.idp_confirm_bound
        self.accounts_off = settings.service_accounts == SERVICE_ACCOUNTS_OFF
        # 인가 서버가 꺼졌는가. 잠든 서버(P53b 6.5절)는 꺼진 것이 아니라 연결을 두고 본다
        self.oauth_off = settings.oauth_off_reason is not None
        # 지금의 CIMD 허용 목록(DAEGAM_OAUTH_CIMD_CLIENTS). 빠진 client_id 의 연결은 끊긴다
        self.cimd_clients = frozenset(settings.oauth_cimd_clients)
        self.on_lost = on_lost
        # 역할이 낮아져 `agent:deploy` 를 잃은 사람의 미리 보기를 끝낸다 (P44b 10절)
        self.on_role_lowered = on_role_lowered

    @property
    def human_issuer(self) -> str | None:
        """지금 사람 발급자. 플랫폼이 사람 토큰을 내지 않는 header 모드면 없고, 그때는 모든 사람
        토큰의 발급자가 바뀐 것이다."""
        return self.issuer.human_issuer if self.mints else None

    # -- 회수 ---------------------------------------------------------------------------

    async def revoke(self, ids: list[str], reason: Revocation, *, by: str) -> list[PersonalToken]:
        """ids 의 토큰을 세 걸음으로 회수하고 이번에 다룬 토큰을 돌려준다. 이미 회수된 것은
        빠진다."""
        if not ids:
            return []
        tokens = await self.tokens.request_revoke(ids, reason=reason, by=by, now=utcnow())
        await self._finish(tokens)
        return tokens

    async def _finish(self, tokens: list[PersonalToken]) -> None:
        """회수의 둘째·셋째 걸음. 슬롯을 지운 뒤에만 `revoked_at` 을 찍는다."""
        await self.slots.remove_kids([t.kid for t in tokens])
        for token in tokens:
            await self.tokens.mark_revoked(token.id)

    async def revoke_for(
        self, sub: str, reason: Revocation, *, by: str, tenant: str | None = None
    ) -> None:
        """훅. sub 의 활성 토큰과 OAuth 연결을, tenant 를 주면 그 테넌트의 것만 회수하고 `on_lost`
        를 부른다. 회수가 실패해도 `on_lost` 는 부른다. 예외는 삼킨다."""
        try:
            now = utcnow()
            await self.revoke(await self.tokens.live_ids(sub, now, tenant=tenant), reason, by=by)
            await self.grants.revoke_many(
                await self.grants.live_ids(sub, now, tenant=tenant), reason
            )
        except Exception as e:  # noqa: BLE001 - 계정 사건은 이미 일어났고 한도는 접근 정리가 지킨다
            logger.warning("access revocation hook failed (sub=%s reason=%s): %s", sub, reason, e)
        if self.on_lost is None:
            return
        try:
            await self.on_lost(sub, reason, tenant)
        except Exception as e:  # noqa: BLE001 - 실행 소스는 시작 워커가 시작마다 다시 본다
            logger.warning("lost access hook failed (sub=%s reason=%s): %s", sub, reason, e)

    async def revoke_tokens_for(
        self, sub: str, reason: Revocation, *, by: str, tenant: str
    ) -> None:
        """훅. sub 의 그 테넌트 개인 토큰만 회수한다. 역할이 낮아진 사람의 것이다 (P52 3절). OAuth
        연결과 실행 소스는 건드리지 않는다. 접근 토큰은 15분 수명이 한도이고 갱신이 역할을 새로
        싣기 때문이다. 그 뒤 `on_role_lowered` 를 부른다. `agent:deploy` 를 잃었으면 그 사람의
        미리 보기가 끝난다 (P44b 10절). 예외는 삼킨다."""
        try:
            ids = await self.tokens.live_ids(sub, utcnow(), tenant=tenant)
            await self.revoke(ids, reason, by=by)
        except Exception as e:  # noqa: BLE001 - 한도는 접근 정리가 지킨다
            logger.warning("access revocation hook failed (sub=%s reason=%s): %s", sub, reason, e)
        if self.on_role_lowered is None:
            return
        try:
            await self.on_role_lowered(sub, tenant)
        except Exception as e:  # noqa: BLE001 - 미리 보기는 요청마다의 scope 검사가 막는다
            logger.warning("role lowered hook failed (sub=%s): %s", sub, e)

    # -- 판정 ---------------------------------------------------------------------------

    async def holder(self, sub: str, tenant: str) -> Holder:
        """sub 가 지금 가진 것을 읽는다. 서비스 어카운트는 tenant 의 계정 역할이다."""
        if is_account(sub):
            account_id = account_id_of(sub)
            role = await self.accounts.role_of(account_id, tenant) if self.accounts else None
            return Holder(account_role=role)
        members = await self.memberships.list_for_sub(sub)
        if self.oidc:
            return Holder(members, person=await self.people.get(sub))
        return Holder(members, user=await self.users.get(sub))

    def holder_lost(self, held: _Held, holder: Holder, now: datetime) -> Revocation | None:
        """그 사람(또는 서비스 어카운트)이 held 의 테넌트에 대한 접근을 잃은 사유. oidc 모드는 긴
        한도로 사람 기록을 보고 다른 모드는 사용자 문서를 본다."""
        if is_account(held.sub):
            if self.accounts_off:
                return ACCOUNTS_OFF
            return DELETED if holder.account_role is None else None
        if self.oidc:
            oldest = now - self.long_bound
            return lost_idp_access(held, holder.person, holder.tenants, oldest=oldest)
        return lost_access(held, holder.user, holder.tenants)

    def grant_reason(
        self, grant: OAuthGrant, holder: Holder, now: datetime
    ) -> GrantRevocation | None:
        """연결이 접근을 잃은 사유. 아직 살아 있으면 None 이다. 접근 정리와 연결의 갱신이 함께
        쓴다 (P33 11절·12절). 잠든 인가 서버는 연결을 끊지 않는다 (P53b 6.5절)."""
        disallowed = grant.client_kind == CIMD and grant.client_id not in self.cimd_clients
        return self.holder_lost(grant, holder, now) or _first(
            (
                (grant.iss != self.human_issuer, ISSUER_CHANGED),
                (grant.signer_kid != self.issuer.kid, KEY_ROTATED),
                (self.oauth_off, OAUTH_DISABLED),
                (disallowed, CLIENT_DISALLOWED),
            )
        )

    def token_reason(
        self, token: PersonalToken, holder: Holder, master: str | None, now: datetime
    ) -> Revocation | None:
        """토큰이 접근을 잃은 사유. 아직 살아 있으면 None 이다. 모듈 머리글의 순서대로 본다. 토큰의
        그룹은 언제나 그 테넌트의 고정 그룹이어야 한다 (P53b 6.1절)."""
        role = holder.account_role if is_account(token.sub) else holder.role_in(token.tenant)
        return (
            self.holder_lost(token, holder, now)
            or _first(
                (
                    (token.group != default_mcp_group(token.tenant), WRONG_GROUP),
                    (token.iss != self.human_issuer, ISSUER_CHANGED),
                    (master is not None and master != self.issuer.kid, KEY_ROTATED),
                    (master is None, KEY_MISSING),
                )
            )
            or role_reason(token, role)
            or groups_reason(token, holder.person)
        )

    # -- 접근 정리 ------------------------------------------------------------------------

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """접근 정리 한 번. 리컨실 단계이고 결과 키는 `revoked`(이번에 회수한 토큰), `grants`(이번에
        회수한 OAuth 연결), `finished`(회수 중에 머물다 끝난 토큰), `removed`(지운 슬롯의 kid)다.
        서명 키가 없으면 지금 kid 를 모르므로 건너뛴다."""
        if not self.issuer.loaded:
            return {}
        revoked, grants = await self._revoke_lost_access(now)
        finished = await self.tokens.revoking()
        await self._finish(finished)
        return {
            "revoked": revoked,
            "grants": grants,
            "finished": [t.id for t in finished],
            "removed": await self._prune_slots(now),
        }

    async def _revoke_lost_access(self, now: datetime) -> tuple[list[str], list[str]]:
        """접근을 잃은 토큰과 연결을 회수하고 사유마다 기록한다. (토큰 id, 연결 id) 다."""
        lost_tokens = await self._lost_tokens(await self.tokens.live(now), now)
        lost_grants = await self._lost(
            await self.grants.live(now),
            lambda grant, holder: self.grant_reason(grant, holder, now),
        )
        for reason, tokens in lost_tokens.items():
            await self.revoke([t.id for t in tokens], reason, by=SWEEP_SUB)
        for reason, grants in lost_grants.items():
            await self.grants.revoke_many([g.id for g in grants], reason)
        for reason in sorted(lost_tokens.keys() | lost_grants.keys()):
            await self._record(reason, lost_tokens.get(reason, []), lost_grants.get(reason, []))
        return (
            [t.id for tokens in lost_tokens.values() for t in tokens],
            [g.id for grants in lost_grants.values() for g in grants],
        )

    async def _lost_tokens(
        self, tokens: list[PersonalToken], now: datetime
    ) -> dict[Revocation, list[PersonalToken]]:
        """사유 → 접근을 잃은 토큰. 슬롯의 master 는 한 번에 읽는다."""
        masters = await self.slots.masters([t.kid for t in tokens])
        return await self._lost(
            tokens,
            lambda token, holder: self.token_reason(token, holder, masters.get(token.kid), now),
        )

    async def _lost[T: _Held, R](
        self, held: list[T], reason_of: Callable[[T, Holder], R | None]
    ) -> dict[R, list[T]]:
        """사유 → 접근을 잃은 것. 그 sub 가 가진 것은 sub 마다 한 번 읽는다. 서비스 어카운트의
        토큰은 모두 그 계정의 테넌트 하나의 것이다."""
        by_sub: dict[str, list[T]] = defaultdict(list)
        for item in held:
            by_sub[item.sub].append(item)
        lost: dict[R, list[T]] = defaultdict(list)
        for sub, owned in by_sub.items():
            holder = await self.holder(sub, owned[0].tenant)
            for item in owned:
                reason = reason_of(item, holder)
                if reason is not None:
                    lost[reason].append(item)
        return lost

    async def _prune_slots(self, now: datetime) -> list[str]:
        """주인 없는 bound 슬롯, 옛 서명 키의 슬롯, 플랫폼이 사람 토큰을 내지 않는 header 모드의
        pool 슬롯을 지운다."""
        bound = await self.slots.bound()
        revoked = await self.tokens.revoked_flags([s.token_id for s in bound if s.token_id])
        orphans = [s.id for s in bound if _orphaned(s, revoked, now - ORPHAN_GRACE)]
        await self.slots.remove_kids(orphans)
        rotated = await self.slots.remove_other_masters(self.issuer.kid)
        pruned = [] if self.mints else await self.slots.remove_pool()
        if orphans or rotated or pruned:
            logger.info(
                "pat key slots removed: %d orphaned, %d of an older signing key, "
                "%d pool where the platform issues no person tokens",
                len(orphans),
                len(rotated),
                len(pruned),
            )
        return orphans + [slot.id for slot in rotated + pruned]

    async def _record(
        self, reason: str, tokens: list[PersonalToken], grants: list[OAuthGrant]
    ) -> None:
        """요청 없는 회수의 기록. 로그 한 줄과, 종류와 테넌트마다 시스템 주체의 감사 행 하나다
        (P32 12절, P33 12절)."""
        logger.info(
            "access_revoked reason=%s tokens=%d grants=%d", reason, len(tokens), len(grants)
        )
        tenants = (
            (PERSONAL_TOKENS_RESOURCE, {t.tenant for t in tokens}),
            (OAUTH_GRANTS_RESOURCE, {g.tenant for g in grants}),
        )
        for resource, names in tenants:
            for tenant in sorted(names):
                await record_system_event(
                    self.audit,
                    sub=SWEEP_SUB,
                    tenant=tenant,
                    action="access_revoked",
                    resource=resource,
                    path="reconcile",
                    error_code=reason,
                )
