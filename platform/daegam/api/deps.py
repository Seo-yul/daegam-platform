"""요청 의존성: 인증 주체, 테넌트 결정, scope 검사, 저장소, 스펙 읽기 토큰."""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from daegam.api._common import check_tenant, error, forbidden, unauthorized
from daegam.auth import rbac
from daegam.auth.oidc import AuthError, Claims, OIDCVerifier
from daegam.auth.principal import Membership, Principal, header_principal, token_username
from daegam.auth.recent import REAUTH_DETAIL, REAUTH_REQUIRED, signed_in_recently
from daegam.auth.service_token import is_person_sub
from daegam.config import Settings
from daegam.store.backend_apis import BackendAPIStore, BackendAPIToken
from daegam.store.base import match_token
from daegam.store.files import SpecFiles


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> BackendAPIStore:
    return request.app.state.backend_apis


def get_files(request: Request) -> SpecFiles:
    return request.app.state.files


# -- 인증 ------------------------------------------------------------------------

# local 모드에서 must_change_password 인 사람이 지날 수 있는 경로 (D40)
PASSWORD_CHANGE_ALLOWED = frozenset({"/api/auth/password", "/api/auth/logout", "/api/me"})
# oidc 모드에서 오래 가는 자격을 만드는 요청이 액세스 토큰 곁에 싣는 UI 의 ID 토큰 (P53b 6.4절).
# 그 `auth_time` 이 최근 로그인(`auth/recent.py`)이어야 한다
ID_TOKEN_HEADER = "X-Daegam-Id-Token"


def _oidc_unauthorized(code: str, detail: str, status: int = 401) -> HTTPException:
    """OIDC 검증 실패. 코드가 여러 가지(token_expired·invalid_token…)라 챌린지에 그것을 싣는다.
    403 처럼 401 이 아닌 상태로도 오므로 그때는 챌린지가 없다."""
    if status != 401:
        return error(status, code, detail)
    return unauthorized(detail, code=code, challenge=f'Bearer error="{code}"')


def bearer_value(request: Request) -> str | None:
    """Authorization 헤더의 Bearer 값. 없거나 비었으면 None 이다.

    실패 챌린지는 부르는 쪽이 정한다."""
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _memberships(rows: list[Any]) -> list[Membership]:
    return [Membership(m.tenant, m.role, m.source) for m in rows]


async def _local_principal(request: Request, token: str) -> Principal:
    """플랫폼이 낸 토큰이다 (D40). 서명 뒤에 사용자 문서를 읽어 tv·disabled 를 보고, 소속은
    저장된 manual 소속 그대로다. 그룹 동기화는 없다."""
    try:
        user, claims = await request.app.state.local_auth.authenticate(token)
    except AuthError as e:
        raise _oidc_unauthorized(e.code, e.detail, e.status) from e
    # 비밀번호를 바꿔야 하는 사람은 바꾸는 문과 나가는 문, 자기가 누구인지 보는 문만 지난다.
    # UI 만 유도하면 배포 파이프라인에 박힌 부트스트랩 비밀번호가 curl 한 번으로 살아 있는
    # platform-admin 자격이 된다 (D40)
    if user.must_change_password and request.url.path not in PASSWORD_CHANGE_ALLOWED:
        raise forbidden(
            "password change required before using the API",
            code="password_change_required",
        )
    rows = await request.app.state.memberships.list_for_sub(user.id)
    return Principal(sub=user.id, email=user.email, memberships=_memberships(rows), raw=claims)


async def _oidc_principal(request: Request, settings: Settings, token: str) -> Principal:
    verifier: OIDCVerifier = request.app.state.oidc
    try:
        claims = await verifier.verify(token)
    except AuthError as e:
        raise _oidc_unauthorized(e.code, e.detail, e.status) from e
    # 로그인(첫 요청)마다 그룹 → (tenant, role) 매핑을 풀어 okta_group 소속을 맞춘다. 토큰에
    # groups 클레임 자체가 없으면 맞출 근거가 없는 것이라 저장된 소속을 그대로 쓴다. 지우면
    # 그룹 클레임이 빠진 토큰 한 장이 그 사람의 모든 소속을 없앤다
    memberships_store = request.app.state.memberships
    if claims.groups_present:
        rows = await memberships_store.sync_groups(claims.sub, claims.groups)
    else:
        rows = await memberships_store.list_for_sub(claims.sub)
    memberships = _memberships(rows)
    await _observe_person(request, claims)
    # 부트스트랩: 최초 관리자용. 매핑 표에 platform-admin 이 하나도 없을 때만 이 그룹으로
    # platform-admin 이 된다 (저장하지 않는다). 첫 매핑을 저장하면 힘을 잃는다. 그렇지 않으면
    # 환경 변수 하나가 매핑 화면에 보이지도, 거둘 수도 없는 관리자를 영원히 만든다
    boot = settings.bootstrap_admin_group
    if (
        boot
        and boot in claims.groups
        and not any(rbac.is_platform_admin_row(m.tenant, m.role) for m in memberships)
        and not await memberships_store.has_platform_admin_mapping()
    ):
        memberships.append(Membership(rbac.ALL_TENANTS, rbac.PLATFORM_ADMIN, "bootstrap"))
    return Principal(
        sub=claims.sub,
        email=claims.email,
        groups=claims.groups,
        client_id=claims.client_id,
        memberships=memberships,
        raw=claims.raw,
    )


async def _observe_person(request: Request, claims: Claims) -> None:
    """사람 기록에 이 토큰을 적는다 (P53b 6.3절). 쓰기 규칙은 `store/idp_people.py` 가 정한다.
    groups 클레임이 없는 토큰은 저장된 그룹을 건드리지 않는다."""
    await request.app.state.idp_people.observe(
        claims.sub,
        iat=claims.iat,
        exp=claims.exp,
        username=token_username(claims.raw, claims.email),
        email=claims.email,
        groups=claims.groups if claims.groups_present else None,
    )


async def require_user(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Principal:
    """요청 주체. oidc 모드는 Bearer 토큰을 검증하고 groups 를 memberships 로 동기화한다.
    local 모드는 플랫폼이 낸 토큰을 검증한다. header 모드(로컬·테스트)는 전체 scope 를 가진
    고정 주체다."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    if settings.auth_mode == "header":
        principal = header_principal()
    else:
        token = bearer_value(request)
        if token is None:
            raise _oidc_unauthorized("unauthorized", "Bearer token required")
        if settings.auth_mode == "local":
            principal = await _local_principal(request, token)
        else:
            principal = await _oidc_principal(request, settings, token)
    request.state.principal = principal
    return principal


# -- 테넌트 결정 -----------------------------------------------------------------


def get_tenant(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_user)],
) -> str:
    """헤더 X-Daegam-Tenant 는 "선택" 이다. oidc 모드: 소속이 아니면 403 not_member, 헤더가 없으면
    소속이 하나일 때 그것, 여럿이면 400 tenant_required(소속 목록 포함). platform-admin 은 소속 없이
    헤더로 아무 테넌트나 고른다. header 모드는 예전처럼 헤더 또는 기본 테넌트다.
    결정한 테넌트는 request.state.tenant 에 남겨 감사 미들웨어가 읽는다."""
    tenant = _resolve_tenant(request, settings, principal)
    request.state.tenant = tenant
    return tenant


def _resolve_tenant(request: Request, settings: Settings, principal: Principal) -> str:
    raw = request.headers.get(settings.tenant_header)
    if settings.auth_mode == "header":
        return check_tenant((raw or settings.default_tenant).strip(), shown=raw)
    if raw is not None and raw.strip():
        tenant = check_tenant(raw.strip(), shown=raw)
        if not principal.is_member(tenant):
            raise forbidden(f"not a member of tenant {tenant}", code="not_member", tenant=tenant)
        return tenant
    tenants = principal.tenants
    if len(tenants) == 1:
        return tenants[0]
    if not tenants and not principal.is_platform_admin:
        raise forbidden("no tenant membership", code="not_member", tenants=[])
    raise error(
        400,
        "tenant_required",
        f"choose a tenant with the {settings.tenant_header} header",
        tenants=tenants,
    )


# -- scope 검사 ------------------------------------------------------------------

_Dep = Callable[..., Coroutine[Any, Any, Principal]]


def _check(principal: Principal, scope: str, scopes: frozenset[str], tenant: str | None) -> None:
    if scope not in scopes:
        extra: dict[str, Any] = {"scope": scope}
        if tenant is not None:
            extra["tenant"] = tenant
        raise forbidden(f"scope {scope} required", **extra)


def require_scope(scope: str, *, path_tenant: bool = False) -> _Dep:
    """`Depends(require_scope("mcp:deploy"))`. scope 의 테넌트 문맥은 리소스에 따라 정한다.
    - path_tenant=True: 경로 `{tenant}` 안에서의 role (tenants 라우터)
    - cluster·settings 처럼 플랫폼 수준 리소스: 어느 테넌트에서든 그 scope 가 있으면 통과
    - 나머지: 헤더/소속으로 결정한 테넌트(get_tenant) 안에서의 role
    header 모드의 주체는 platform-admin 이라 항상 통과한다."""
    if scope not in rbac.SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    resource = scope.split(":", 1)[0]

    if path_tenant:

        async def by_path(
            request: Request, principal: Annotated[Principal, Depends(require_user)]
        ) -> Principal:
            tenant = str(request.path_params.get("tenant", ""))
            _check(principal, scope, principal.scopes_in(tenant), tenant)
            return principal

        return by_path

    if resource in rbac.PLATFORM_RESOURCES:

        async def platform_level(
            principal: Annotated[Principal, Depends(require_user)],
        ) -> Principal:
            _check(principal, scope, principal.all_scopes, None)
            return principal

        return platform_level

    async def by_tenant(
        principal: Annotated[Principal, Depends(require_user)],
        tenant: Annotated[str, Depends(get_tenant)],
    ) -> Principal:
        _check(principal, scope, principal.scopes_in(tenant), tenant)
        return principal

    return by_tenant


def require_local_mode(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """local 모드에만 있는 라우트(D40). 라우터는 언제나 등록돼 OpenAPI 와 UI 타입에 보이지만,
    다른 모드에서는 404 다. oidc 사용자는 IdP 가 관리하고 header 모드는 사용자가 없다."""
    if settings.auth_mode != "local":
        raise error(404, "not_found", "local auth is not enabled (auth_mode is not local)")


def require_minting(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """플랫폼이 사람 토큰을 내는 모드(local, oidc)에만 있는 라우트 (P53b 6.5절). 개인 MCP 토큰이고
    header 모드에서는 404 다."""
    if not settings.platform_mints_human_tokens:
        raise error(404, "not_found", "the platform issues no person tokens in this auth mode")


def require_oauth(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """MCP 클라이언트의 OAuth 인가 서버 라우트 (P33 1절). 꺼졌으면(`Settings.oauth_off_reason`)
    404 다. 꺼진 까닭은 기동 로그가 말하고 응답은 말하지 않는다. 잠든 서버(P53b 6.5절)의 라우트는
    있고, 새 인가와 동의 허용과 토큰 엔드포인트는 서비스가 거절하며 회수와 연결 끊기는 그대로
    된다."""
    if settings.oauth_off_reason is not None:
        raise error(404, "not_found", "the OAuth authorization server is not enabled")


def require_oauth_awake(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """인가 서버가 일할 때만 내는 것(RFC 8414 메타데이터). 꺼졌거나 잠들었으면 404 다. 잠든 동안
    ToolHive 는 IdP 를 인가 서버로 알리므로 플랫폼의 메타데이터를 찾는 클라이언트가 없다."""
    if not settings.oauth_enabled:
        raise error(404, "not_found", "the OAuth authorization server is not enabled")


async def check_recent_sign_in(
    verifier: OIDCVerifier,
    client_id: str | None,
    id_token: str | None,
    sub: str,
    *,
    now: float,
) -> int:
    """oidc 모드의 최근 로그인 (P53b 6.4절). id_token 이 UI 의 client id 를 audience 로 IdP 가 낸
    ID 토큰이고, 요청한 사람(sub)의 것이고, `auth_time` 이 10분(시계 여유 30초) 안이면 그
    `auth_time` 이다. 아니면 403 `reauth_required` 이고 UI 는 IdP 에서 다시 로그인한다. ID 토큰은
    여기서만 받는다."""
    if not id_token or not client_id:
        raise forbidden(f"an ID token in {ID_TOKEN_HEADER} is required", code=REAUTH_REQUIRED)
    try:
        claims = await verifier.verify_id_token(id_token, client_id)
    except AuthError as e:
        if e.status != 401:
            raise _oidc_unauthorized(e.code, e.detail, e.status) from e
        raise forbidden(f"the ID token is not valid: {e.detail}", code=REAUTH_REQUIRED) from e
    if claims.sub != sub:
        raise forbidden("the ID token belongs to someone else", code=REAUTH_REQUIRED)
    if claims.auth_time is None or not signed_in_recently(claims.auth_time, now):
        raise forbidden(REAUTH_DETAIL, code=REAUTH_REQUIRED)
    return claims.auth_time


async def require_recent_sign_in(
    request: Request, settings: Settings, principal: Principal, *, now: float | None = None
) -> None:
    """오래 가는 자격(개인·서비스 어카운트 토큰, 로그인 연결)을 만들기 전의 oidc 최근 로그인
    (P53b 6.4절). oidc 모드에서 요청의 `X-Daegam-Id-Token` 을 `check_recent_sign_in` 으로 본다. 다른
    모드는 할 일이 없다. local 은 비밀번호나 제어면 토큰의 iat 를 따로 본다. now 를 주지 않으면
    지금이다."""
    if settings.auth_mode != "oidc":
        return
    await check_recent_sign_in(
        request.app.state.oidc,
        settings.oidc_client_id,
        request.headers.get(ID_TOKEN_HEADER),
        principal.sub,
        now=time.time() if now is None else now,
    )


async def require_person(principal: Annotated[Principal, Depends(require_user)]) -> None:
    """사람의 토큰과 연결을 다루는 라우트 (P53b 6.5절). `svc:` 나 `sa:` 로 시작하는 IdP sub 은
    사람이 아니라 403 `forbidden` 이다. 그런 sub 의 `/me/tokens` 가 서비스 어카운트의 토큰을 읽거나
    회수하지 못하게 한다."""
    if not is_person_sub(principal.sub):
        raise forbidden("this subject is not a person", code="forbidden")


# 다른 사람의 토큰과 연결을 보는 관리자 라우트는 사용자 문서가 있는 local 모드에만 있다 (P53b 3절)
LOCAL_ONLY = [Depends(require_local_mode)]


async def require_platform_admin(
    principal: Annotated[Principal, Depends(require_user)],
) -> Principal:
    if not principal.is_platform_admin:
        raise forbidden("platform-admin required", role=rbac.PLATFORM_ADMIN)
    return principal


# -- 스펙 읽기 토큰 (initContainer 용, OIDC 와 무관) ---------------------------------


def bearer_token(request: Request) -> str:
    token = bearer_value(request)
    if token is None:
        raise unauthorized("Bearer token required")
    return token


async def authenticate_api_token(
    store: BackendAPIStore, profile_id: str, presented: str
) -> BackendAPIToken:
    """토큰이 profile_id의 것이고 회수되지 않았을 때만 통과. 비교는 `match_token` 이 상수 시간으로
    한다."""
    matched = match_token(await store.list_tokens(profile_id, active_only=True), presented)
    if matched is None:
        raise unauthorized("invalid or revoked token")
    return matched


Tenant = Annotated[str, Depends(get_tenant)]
User = Annotated[Principal, Depends(require_user)]
BackendAPIs = Annotated[BackendAPIStore, Depends(get_store)]
Files = Annotated[SpecFiles, Depends(get_files)]
Settings_ = Annotated[Settings, Depends(get_settings)]
