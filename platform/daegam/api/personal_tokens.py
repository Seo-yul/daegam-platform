"""개인 MCP 토큰 API (P32 6절, P53b 6.5절). 플랫폼이 사람 토큰을 내는 모드(local, oidc)에 있고
header 모드에서는 404 다.

GET    /me/tokens                         본인의 목록
POST   /me/tokens                         {tenant, label, expires_at, password?} → 201. 평문은
                                          이 응답에만 있다. local 은 password 를, oidc 는
                                          `X-Daegam-Id-Token` 의 최근 로그인을 본다. 403
                                          invalid_credentials·not_a_member·reauth_required,
                                          422 invalid_value, 429 login_locked·token_limit
DELETE /me/tokens/{token_id}              본인 회수. 남의 토큰이나 없는 토큰은 404
GET    /users/{user_id}/tokens            관리자(users:write)가 그 사람의 목록을 본다. local 만
DELETE /users/{user_id}/tokens/{token_id} 관리자 회수(revoke_reason=admin). local 만

규칙은 services/personal_tokens.py 다. 감사는 미들웨어가 남긴다. 발급에는 경로 인자가 없고 관리자
회수의 경로에는 테넌트가 없으므로 핸들러가 `request.state` 에 토큰 id 와 토큰의 테넌트를 둔다.
그래야 테넌트 감사 화면에 잡힌다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from daegam.api._common import audit_target, or_404
from daegam.api.deps import (
    LOCAL_ONLY,
    Settings_,
    User,
    require_minting,
    require_person,
    require_recent_sign_in,
    require_scope,
)
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.backend_apis import TokenRevokeResponse
from daegam.api.schemas.personal_tokens import (
    IssuedPersonalToken,
    PersonalTokenIssueIn,
    PersonalTokenOut,
)
from daegam.api.users import user_or_404
from daegam.auth.principal import Principal
from daegam.services.personal_tokens import PersonalTokenService
from daegam.store.base import utcnow
from daegam.store.personal_tokens import ADMIN, USER, PersonalToken, RevokeReason

router = APIRouter(
    responses=ERROR_RESPONSES,
    tags=["personal-tokens"],
    dependencies=[Depends(require_minting), Depends(require_person)],
)

UsersWrite = Annotated[Principal, Depends(require_scope("users:write"))]
WHAT = "personal token"


def _service(request: Request) -> PersonalTokenService:
    return request.app.state.personal_tokens


def token_out(token: PersonalToken) -> dict[str, Any]:
    """토큰 하나의 응답. 서비스 어카운트의 토큰 목록(api/service_accounts.py)도 쓴다."""
    return {
        "token_id": token.id,
        "label": token.label,
        "tenant": token.tenant,
        "group": token.group,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "ready_at": token.ready_at,
        "revoked_at": token.revoked_at,
        "revoke_reason": token.shown_reason,
        "status": token.status(utcnow()),
    }


async def _list(svc: PersonalTokenService, sub: str) -> list[dict[str, Any]]:
    return [token_out(t) for t in await svc.list_for(sub)]


async def _revoke(
    request: Request, sub: str, token_id: str, reason: RevokeReason, *, by: str
) -> dict[str, Any]:
    svc = _service(request)
    token = or_404(await svc.owned(sub, token_id), WHAT, token_id)
    audit_target(request, token.id, token.tenant)
    await svc.revoke(token, reason, by=by)
    return {"token_id": token.id, "revoked": True}


@router.get("/me/tokens", response_model=list[PersonalTokenOut])
async def list_my_tokens(request: Request, principal: User) -> list[dict[str, Any]]:
    return await _list(_service(request), principal.sub)


@router.post("/me/tokens", response_model=IssuedPersonalToken, status_code=201)
async def issue_my_token(
    request: Request, body: PersonalTokenIssueIn, principal: User, settings: Settings_
) -> dict[str, Any]:
    await require_recent_sign_in(request, settings, principal)
    svc = _service(request)
    token, plain = await svc.issue(
        principal.sub,
        tenant=body.tenant,
        label=body.label,
        expires_at=body.expires_at,
        password=body.password,
    )
    audit_target(request, token.id, token.tenant)
    return {**token_out(token), "token": plain}


@router.delete("/me/tokens/{token_id}", response_model=TokenRevokeResponse)
async def revoke_my_token(request: Request, token_id: str, principal: User) -> dict[str, Any]:
    return await _revoke(request, principal.sub, token_id, USER, by=principal.sub)


@router.get(
    "/users/{user_id}/tokens", response_model=list[PersonalTokenOut], dependencies=LOCAL_ONLY
)
async def list_user_tokens(request: Request, user_id: str, _: UsersWrite) -> list[dict[str, Any]]:
    svc = _service(request)
    await user_or_404(svc.local_auth, user_id)
    return await _list(svc, user_id)


@router.delete(
    "/users/{user_id}/tokens/{token_id}",
    response_model=TokenRevokeResponse,
    dependencies=LOCAL_ONLY,
)
async def revoke_user_token(
    request: Request, user_id: str, token_id: str, principal: UsersWrite
) -> dict[str, Any]:
    return await _revoke(request, user_id, token_id, ADMIN, by=principal.sub)
