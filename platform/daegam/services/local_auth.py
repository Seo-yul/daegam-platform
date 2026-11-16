"""local 모드의 인증 서비스 (D40). 로그인·토큰 발급·비밀번호·사용자 관리의 규칙이 여기 있다.

라우터(`api/local_auth.py`, `api/users.py`)는 HTTP 모양만 맡고, `require_user` 는 `authenticate` 만
부른다. 규칙은 다음이다.

- 로그인 실패는 사용자 이름별로 세고 5번이면 15분 잠긴다. 없는 사용자에게도 같은 시간(더미 해시)을
  쓴다. 비밀번호 변경과 개인 토큰 발급의 현재 비밀번호도 같은 확인(`confirm_password`)으로 같은
  잠금에 센다 (P32 6절).
- 계정 막기·삭제·비밀번호 변경은 저장한 뒤 그 사람의 개인 토큰 회수 훅을 부른다 (P32 7절).
- 토큰은 서명·iss·aud·exp 뒤에 사용자 문서를 한 번 읽어 `tv` 와 `disabled` 를 본다.
- platform-admin 은 memberships 의 `*` 줄 하나다. 마지막 활성 platform-admin 은 막거나 지우거나
  내릴 수 없다.
- users 가 비어 있을 때 한 번, 설정의 첫 관리자를 만든다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.auth import rbac
from daegam.auth.local import DATA_PLANE_TOKEN_TTL, LocalVerifier, access_claims
from daegam.auth.oidc import AuthError
from daegam.auth.password import (
    DUMMY_HASH,
    PasswordPolicyError,
    check_policy,
    hash_password,
    verify_password,
)
from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import CONTROL_PLANE_AUDIENCE, PlatformSigner
from daegam.config import Settings
from daegam.errors import ServiceError, retry_after_header
from daegam.services.access_revocation import AccessRevoker
from daegam.store.base import iso_utc, utcnow
from daegam.store.login_throttle import MongoLoginThrottle
from daegam.store.memberships import MongoMembershipStore
from daegam.store.personal_tokens import DELETED, DISABLED, PASSWORD
from daegam.store.tenants import MongoTenantSettingsStore, human_mcp_group
from daegam.store.users import USERNAME_RE, MongoUserStore, User, normalize_username

logger = logging.getLogger("daegam.local_auth")


class LocalAuthError(ServiceError):
    """라우터가 그대로 응답으로 바꾸는 오류. status·code·detail 을 갖는다."""


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_in: int
    audience: str


class LocalAuthService:
    """서명기는 플랫폼 공용 발급자(`InternalIssuer`)의 것이다. 키는 home 클러스터의 Secret 에 있어
    `Services.start()` 가 `bootstrap_home()` 뒤에 붙이고, 그 뒤 `start()` 가 검증기를 만든다."""

    def __init__(
        self,
        settings: Settings,
        users: MongoUserStore,
        memberships: MongoMembershipStore,
        throttle: MongoLoginThrottle,
        issuer: InternalIssuer,
        tenant_settings: MongoTenantSettingsStore,
        revoker: AccessRevoker,
    ):
        self.settings = settings
        self.users = users
        self.memberships = memberships
        self.throttle = throttle
        self.tenant_settings = tenant_settings
        self.revoker = revoker
        self._issuer = issuer
        self.issuer = settings.issuer or ""
        self._verifier: LocalVerifier | None = None

    # -- 기동 ---------------------------------------------------------------------

    async def start(self) -> None:
        if not self._issuer.loaded:
            raise RuntimeError("auth_mode=local needs the platform signing key (no home cluster?)")
        self._verifier = LocalVerifier(
            self.signer, issuer=self.issuer, audience=CONTROL_PLANE_AUDIENCE
        )
        await self.bootstrap_admin()

    @property
    def signer(self) -> PlatformSigner:
        return self._issuer.signer

    @property
    def verifier(self) -> LocalVerifier:
        if self._verifier is None:
            raise RuntimeError("local auth is not started")
        return self._verifier

    async def bootstrap_admin(self) -> None:
        """users 가 비어 있을 때만 첫 platform-admin 을 만든다.

        하나라도 있으면 설정을 읽지 않는다."""
        if await self.users.count() > 0:
            return
        password = self.settings.local_bootstrap_password
        username = self.settings.local_bootstrap_username
        if not password:
            logger.warning(
                "auth_mode=local with no users and no DAEGAM_LOCAL_BOOTSTRAP_PASSWORD: "
                "nobody can sign in until it is set"
            )
            return
        try:
            check_policy(password, username=username)
        except PasswordPolicyError as e:
            logger.error("bootstrap admin password rejected: %s", e.detail)
            return
        try:
            user = await self.create_user(
                username=username,
                password=password,
                email=None,
                display_name=None,
                platform_admin=True,
                must_change_password=True,
            )
        except LocalAuthError as e:
            # 다른 레플리카가 먼저 만들었다
            logger.info("bootstrap admin not created: %s", e.detail)
            return
        logger.warning(
            "bootstrap platform-admin %r created; it must change its password at first sign-in",
            user.username,
        )

    # -- 로그인·토큰 ------------------------------------------------------------------

    async def login(self, username: str, password: str) -> tuple[User, IssuedToken]:
        user = await self.confirm_password(username, password)
        if user.disabled:
            raise LocalAuthError(401, "account_disabled", "this account is disabled")
        await self.users.record_login(user.id)
        return user, self.issue(user, CONTROL_PLANE_AUDIENCE, self.settings.local_token_ttl)

    async def confirm_password(
        self,
        username: str,
        password: str,
        *,
        status: int = 401,
        detail: str = "username or password is wrong",
    ) -> User:
        """로그인, 비밀번호 변경, 개인 토큰 발급이 함께 쓰는 비밀번호 확인이다. 사용자 이름 키의
        잠금을 먼저 보고 잠겼으면 검증하지 않는다(429 login_locked). 틀리면 같은 키로 한 번 세고,
        맞으면 그 기록을 지운 뒤 방금 읽은 사용자 문서를 돌려준다. 틀린 비밀번호는 `status` 의
        invalid_credentials 다. 훔친 제어면 토큰으로 비밀번호를 맞춰 보는 길을 로그인과 같은
        잠금이 막는다."""
        name = normalize_username(username)
        until = await self.throttle.locked_until(name)
        if until is not None:
            raise _locked(until)
        user = await self.users.get_by_username(name) if USERNAME_RE.fullmatch(name) else None
        stored = user.password_hash if user else DUMMY_HASH
        ok = await run_in_threadpool(verify_password, password, stored)
        if user is None or not ok:
            locked = await self.throttle.record_failure(name)
            if locked is not None:
                raise _locked(locked)
            raise LocalAuthError(status, "invalid_credentials", detail)
        await self.throttle.clear(name)
        return user

    def issue(
        self,
        user: User,
        audience: str,
        ttl: int,
        *,
        groups: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> IssuedToken:
        claims = access_claims(
            issuer=self.issuer,
            audience=audience,
            sub=user.id,
            ttl=ttl,
            token_version=user.token_version,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
        )
        if groups is not None:
            claims["groups"] = groups
        if roles:
            claims[rbac.ROLES_CLAIM] = roles
        return IssuedToken(self.signer.sign(claims), ttl, audience)

    async def data_plane_token(self, user: User) -> IssuedToken:
        """제어면 토큰을 가진 사람이 노출된 에이전트·MCP 에 붙을 토큰. aud 는 mcp_audience 하나다.

        vMCP 와 노출 앞단의 Cedar 정책은 토큰의 groups 클레임을 THVGroup 으로 본다. 그래서 이 토큰만
        groups 를 싣는다. 값은 그 사람이 속한 테넌트들의 MCP 그룹이고 local 모드라
        daegam-<tenant> 로 고정이다(`human_mcp_group`). platform-admin 의 `*` 소속은 펼치지 않는다.
        데이터 플레인에 들어가려면 테넌트 소속이 있어야 한다. 제어면 토큰과 Principal.groups 는
        비어 있다. 소속은 memberships 에서 오지 그룹에서 오지 않기 때문이다. 발급 시점의 소속을
        15분 들고 다닌다. 회수가 없는 토큰이라 이 수명이 끊기는 한도다 (P32 6절)."""
        groups: list[str] = []
        roles: list[str] = []
        for m in await self.memberships.list_for_sub(user.id):
            if m.tenant == rbac.ALL_TENANTS:
                continue
            ts = await self.tenant_settings.get(m.tenant)
            groups.append(human_mcp_group(self.settings, ts))
            # 그룹을 싣는 테넌트마다 역할 하나다 (P52 3절)
            roles.append(rbac.role_group(m.tenant, m.role))
        return self.issue(
            user,
            self.settings.mcp_audience,
            DATA_PLANE_TOKEN_TTL,
            groups=sorted(set(groups)),
            roles=sorted(set(roles)),
        )

    async def authenticate(self, token: str) -> tuple[User, dict]:
        """Bearer 토큰 → 살아 있는 사용자. 서명·iss·aud·exp 뒤에 사용자 문서를 읽는다."""
        claims = self.verifier.verify(token)
        user = await self.users.get(str(claims["sub"]))
        if user is None or user.disabled:
            raise AuthError("invalid token: unknown or disabled user")
        if claims.get("tv") != user.token_version:
            raise AuthError("invalid token: superseded by a password or account change")
        return user, claims

    # -- 비밀번호 ----------------------------------------------------------------------

    async def change_password(self, user: User, current: str, new: str) -> tuple[User, IssuedToken]:
        """본인이 바꾼다. 현재 비밀번호를 다시 받고 틀리면 로그인 잠금에 센다.

        다른 세션은 tv 로 끝나고 이 세션은 새 토큰을 받는다."""
        user = await self.confirm_password(
            user.username, current, detail="current password is wrong"
        )
        updated = await self._set_password(user, new, must_change=False, by=user.id)
        return updated, self.issue(updated, CONTROL_PLANE_AUDIENCE, self.settings.local_token_ttl)

    async def reset_password(self, user: User, new: str, *, by: str) -> User:
        """관리자가 바꾼다. 다음 로그인에 본인이 다시 바꾸게 한다."""
        return await self._set_password(user, new, must_change=True, by=by)

    async def _hash_new_password(self, password: str, *, username: str) -> str:
        """정책을 통과한 새 비밀번호의 해시. 통과하지 못하면 422 weak_password 다."""
        try:
            check_policy(password, username=username)
        except PasswordPolicyError as e:
            raise LocalAuthError(422, "weak_password", e.detail) from e
        return await run_in_threadpool(hash_password, password)

    async def _set_password(self, user: User, new: str, *, must_change: bool, by: str) -> User:
        """비밀번호를 바꾸면 그 사람의 개인 토큰이 모두 회수된다. 계정 도용이 의심될 때 사람과
        관리자가 쥔 손잡이가 비밀번호 하나이기 때문이다 (P32 7절)."""
        digest = await self._hash_new_password(new, username=user.username)
        updated = await self.users.set_password(user.id, digest, must_change=must_change)
        if updated is None:
            raise LocalAuthError(404, "not_found", f"user {user.id} not found")
        await self.revoker.revoke_for(user.id, PASSWORD, by=by)
        return updated

    # -- 사용자 관리 -------------------------------------------------------------------

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        email: str | None,
        display_name: str | None,
        platform_admin: bool,
        must_change_password: bool = True,
    ) -> User:
        name = normalize_username(username)
        if not USERNAME_RE.fullmatch(name):
            raise LocalAuthError(
                422,
                "invalid_value",
                "username must be lowercase letters, digits, '.', '_' or '-' (2 to 64 chars)",
            )
        digest = await self._hash_new_password(password, username=name)
        user = User(
            username=name,
            email=email or None,
            display_name=display_name or None,
            password_hash=digest,
            must_change_password=must_change_password,
        )
        try:
            await self.users.create(user)
        except DuplicateKeyError as e:
            raise LocalAuthError(409, "name_conflict", f"username {name!r} is taken") from e
        if platform_admin:
            await self.memberships.set_platform_admin(user.id, True)
        return user

    async def is_platform_admin(self, user: User) -> bool:
        return any(
            rbac.is_platform_admin_row(m.tenant, m.role)
            for m in await self.memberships.list_for_sub(user.id)
        )

    async def set_platform_admin(self, user: User, on: bool) -> None:
        if not on:
            await self._guard_last_admin(user, "demote")
        await self.memberships.set_platform_admin(user.id, on)

    async def set_disabled(self, user: User, disabled: bool, *, by: str) -> User:
        """막으면 개인 토큰이 모두 회수된다. 다시 켜도 회수한 토큰은 살아나지 않는다."""
        if disabled:
            await self._guard_last_admin(user, "disable")
        updated = await self.users.set_disabled(user.id, disabled)
        if updated is None:
            raise LocalAuthError(404, "not_found", f"user {user.id} not found")
        if disabled:
            await self.revoker.revoke_for(user.id, DISABLED, by=by)
        return updated

    async def delete_user(self, user: User, *, by: str) -> None:
        await self._guard_last_admin(user, "delete")
        await self.users.delete(user.id)
        await self.memberships.delete_all_for_sub(user.id)
        await self.revoker.revoke_for(user.id, DELETED, by=by)

    async def _guard_last_admin(self, user: User, verb: str) -> None:
        """user 가 마지막 활성 platform-admin 이면 그 일을 못 한다.

        모두를 잠그는 길이기 때문이다."""
        if not await self.is_platform_admin(user):
            return
        others = await self.memberships.list_for_tenant(rbac.ALL_TENANTS)
        for m in others:
            if m.sub == user.id or m.role != rbac.PLATFORM_ADMIN:
                continue
            other = await self.users.get(m.sub)
            if other is not None and not other.disabled:
                return
        raise LocalAuthError(
            422, "last_admin", f"cannot {verb} the last active platform-admin {user.username!r}"
        )


def _locked(until: datetime) -> LocalAuthError:
    return LocalAuthError(
        429,
        "login_locked",
        f"too many failed attempts; locked until {iso_utc(until)}",
        headers=retry_after_header(until - utcnow()),
    )
