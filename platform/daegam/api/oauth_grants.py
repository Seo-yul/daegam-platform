"""OAuth 연결의 목록과 끊기 (P33 12절). OAuth 가 켜졌거나 잠들었을 때 있고(P53b 6.5절) 꺼지면
404 다.

GET    /me/oauth-grants                          본인의 연결, 새것부터
DELETE /me/oauth-grants/{grant_id}               본인이 끊는다(revoked_reason=user). 남의 연결이나
                                                 없는 연결은 404
GET    /users/{user_id}/oauth-grants             관리자(users:write)가 그 사람의 연결을 본다.
                                                 local 만
DELETE /users/{user_id}/oauth-grants/{grant_id}  관리자가 끊는다(revoked_reason=admin). local 만

규칙은 services/oauth/tokens.py 다. 끊으면 갱신이 그 자리에서 막히고 이미 나간 접근 토큰은 15분
안에 만료한다. 감사는 미들웨어가 남긴다. 관리자 라우트의 경로에는 테넌트가 없으므로 핸들러가 연결의
테넌트와 연결 id 를 `request.state` 에 둔다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from daegam.api._common import audit_target, or_404
from daegam.api.deps import LOCAL_ONLY, User, require_oauth, require_person
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.oauth import OAuthGrantOut, OAuthGrantRevokeOut
from daegam.api.users import UsersWrite, user_or_404
from daegam.services.oauth.clients import published_host
from daegam.services.oauth.tokens import OAuthTokens
from daegam.store.base import utcnow
from daegam.store.oauth_grants import GrantRevokeReason, OAuthGrant
from daegam.store.personal_tokens import ADMIN, USER

router = APIRouter(
    responses=ERROR_RESPONSES,
    tags=["oauth"],
    dependencies=[Depends(require_oauth), Depends(require_person)],
)

WHAT = "oauth grant"


def _service(request: Request) -> OAuthTokens:
    return request.app.state.oauth_tokens


def _out(grant: OAuthGrant) -> dict[str, Any]:
    return {
        "grant_id": grant.id,
        "client_name": grant.client_name,
        "client_ref": grant.client_ref,
        "client_kind": grant.client_kind,
        "client_host": published_host(grant.client_kind, grant.client_id),
        "tenant": grant.tenant,
        "resource": grant.resource,
        "resource_kind": grant.resource_kind,
        "resource_id": grant.resource_id,
        "scope": grant.scope.split(),
        "created_at": grant.created_at,
        "last_refresh_at": grant.last_refresh_at,
        "expires_at": grant.expires_at,
        "revoked_at": grant.revoked_at,
        "revoked_reason": grant.shown_reason,
        "status": grant.status(utcnow()),
    }


async def _list(svc: OAuthTokens, sub: str) -> list[dict[str, Any]]:
    return [_out(g) for g in await svc.list_for(sub)]


async def _disconnect(
    request: Request, sub: str, grant_id: str, reason: GrantRevokeReason
) -> dict[str, Any]:
    svc = _service(request)
    grant = or_404(await svc.owned(sub, grant_id), WHAT, grant_id)
    audit_target(request, grant.id, grant.tenant)
    await svc.disconnect(grant, reason)
    return {"grant_id": grant.id, "revoked": True}


@router.get("/me/oauth-grants", response_model=list[OAuthGrantOut])
async def list_my_oauth_grants(request: Request, principal: User) -> list[dict[str, Any]]:
    return await _list(_service(request), principal.sub)


@router.delete("/me/oauth-grants/{grant_id}", response_model=OAuthGrantRevokeOut)
async def revoke_my_oauth_grant(request: Request, grant_id: str, principal: User) -> dict[str, Any]:
    return await _disconnect(request, principal.sub, grant_id, USER)


@router.get(
    "/users/{user_id}/oauth-grants", response_model=list[OAuthGrantOut], dependencies=LOCAL_ONLY
)
async def list_user_oauth_grants(
    request: Request, user_id: str, _: UsersWrite
) -> list[dict[str, Any]]:
    await user_or_404(request.app.state.local_auth, user_id)
    return await _list(_service(request), user_id)


@router.delete(
    "/users/{user_id}/oauth-grants/{grant_id}",
    response_model=OAuthGrantRevokeOut,
    dependencies=LOCAL_ONLY,
)
async def revoke_user_oauth_grant(
    request: Request, user_id: str, grant_id: str, _: UsersWrite
) -> dict[str, Any]:
    return await _disconnect(request, user_id, grant_id, ADMIN)
