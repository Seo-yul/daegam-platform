"""UI 로그인 연동용 인증 API (P6 F1 보강, P7 인증 행).

GET /auth/config  인증 없이. UI 가 OIDC(PKCE) 로그인을 구성하는 데 쓴다
GET /me           require_user. 주체·소속·테넌트별 scope
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from daegam.api.deps import Settings_, User
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.tenancy import AuthConfig, MeOut
from daegam.auth import rbac
from daegam.auth.principal import Principal
from daegam.auth.recent import RECENT_AUTH

router = APIRouter(responses=ERROR_RESPONSES, tags=["auth"])

OIDC_SCOPES = ["openid", "profile", "email", "groups"]


@router.get("/auth/config", response_model=AuthConfig, response_model_exclude_unset=True)
async def auth_config(settings: Settings_) -> dict[str, Any]:
    if settings.auth_mode == "header":
        return {"auth_mode": "header"}
    # 개인 토큰과 OAuth 인가 서버는 플랫폼이 사람 토큰을 내는 모드(local, oidc)에 있다 (P53b 6.5절)
    minted = {
        "personal_tokens": {"max_days": settings.pat_max_days},
        "oauth": {"enabled": settings.oauth_enabled, "dormant": settings.oauth_dormant},
    }
    if settings.auth_mode == "local":
        # 브라우저는 발급자 주소를 쓰지 않는다. 로그인은 같은 origin 의 /api/auth/login 이다 (D40)
        return {
            "auth_mode": "local",
            "issuer": settings.issuer,
            "mcp_audience": settings.mcp_audience,
            "token_ttl": settings.local_token_ttl,
            **minted,
        }
    return {
        "auth_mode": "oidc",
        "issuer": settings.oidc_issuer,
        "client_id": settings.oidc_client_id,
        "audience": settings.oidc_audience,
        "scopes": OIDC_SCOPES,
        "groups_claim": settings.oidc_groups_claim,
        "mcp_audience": settings.mcp_audience,
        **minted,
        # 토큰 화면이 발급자를 IdP 로 둔 동안 Gateway 가 그 토큰을 아직 받지 않는다고 말하고, 긴
        # 한도와 최근 로그인의 시간으로 문구와 다시 로그인할지를 정한다 (P53b 6.4절, 13절)
        "data_plane_issuer": "platform" if settings.data_plane_trusts_platform else "idp",
        "idp_confirm_seconds": settings.idp_confirm_seconds,
        "recent_sign_in_seconds": int(RECENT_AUTH.total_seconds()),
    }


def display_name(p: Principal) -> str | None:
    """화면이 사람을 부를 때 쓰는 이름 (D45). 토큰의 `name`, 없으면 `preferred_username`, 없으면
    이메일의 `@` 앞부분이고 셋 다 없으면 None 이다. `sub` 는 쓰지 않는다. local 은 uuid 이고
    header 모드는 "header" 라 인사가 될 수 없다."""
    for claim in ("name", "preferred_username"):
        value = p.raw.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if p.email and "@" in p.email:
        return p.email.split("@", 1)[0] or None
    return p.email or None


def principal_out(p: Principal) -> dict[str, Any]:
    tenants = {m.tenant for m in p.memberships}
    return {
        "sub": p.sub,
        "name": display_name(p),
        "email": p.email,
        "groups": list(p.groups),
        "client_id": p.client_id,
        "subject_type": p.subject_type,
        "memberships": [
            {"tenant": m.tenant, "role": m.role, "source": m.source}
            for m in sorted(p.memberships, key=lambda m: (m.tenant != rbac.ALL_TENANTS, m.tenant))
        ],
        "is_platform_admin": p.is_platform_admin,
        "scopes_by_tenant": {t: sorted(p.scopes_in(t)) for t in sorted(tenants)},
    }


@router.get("/me", response_model=MeOut)
async def me(principal: User) -> dict[str, Any]:
    return principal_out(principal)
