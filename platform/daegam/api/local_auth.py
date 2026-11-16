"""local 모드의 로그인·토큰·비밀번호 API (D40).

라우터는 HTTP 모양만 맡고 규칙은 services/local_auth.py 다.

POST /auth/login     {username, password} → 토큰.
                     401 invalid_credentials·account_disabled, 429 login_locked
POST /auth/token     (본문 없음, require_user) → 데이터 플레인 토큰(aud=mcp_audience, groups 포함,
                     15분). 스크립트가 노출된 에이전트·MCP 를 밖에서 부를 때 쓴다
POST /auth/password  {current_password, new_password} (require_user) → 새 토큰.
                     다른 세션은 tv 로 끝나고 개인 토큰은 회수된다. 틀린 현재 비밀번호는 로그인
                     잠금에 세고 잠긴 동안은 429 login_locked 다
POST /auth/logout    204. 토큰은 상태가 없으므로 UI 가 지운다
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from daegam.api._common import unauthorized
from daegam.api.deps import User, require_local_mode
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.users import LoginIn, PasswordChangeIn, TokenOut
from daegam.auth.principal import Principal
from daegam.services.local_auth import IssuedToken, LocalAuthError, LocalAuthService
from daegam.store.users import User as LocalUser
from daegam.store.users import normalize_username

router = APIRouter(
    responses=ERROR_RESPONSES, tags=["auth"], dependencies=[Depends(require_local_mode)]
)


def _service(request: Request) -> LocalAuthService:
    return request.app.state.local_auth


def _raise(e: LocalAuthError) -> HTTPException:
    """오류에 401 챌린지를 더해 올린다. 429 의 Retry-After 는 오류가 싣고 온다."""
    headers = dict(e.headers or {})
    if e.status == 401:
        headers["WWW-Authenticate"] = f'Bearer error="{e.code}"'
    return HTTPException(e.status, e.to_dict(), headers=headers or None)


def _token_out(issued: IssuedToken, *, must_change: bool = False) -> dict[str, Any]:
    return {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": issued.expires_in,
        "audience": issued.audience,
        "must_change_password": must_change,
    }


@router.post("/auth/login", response_model=TokenOut)
async def login(request: Request, body: LoginIn) -> dict[str, Any]:
    """감사는 미들웨어가 남긴다. 시도한 이름을 state 에 두어 실패한 시도에도 남게 하고, 성공하면
    주체를 두어 sub 가 사용자 id 로 남게 한다. 핸들러가 따로 기록하면 한 시도에 이벤트가 둘이
    된다."""
    svc = _service(request)
    request.state.login_username = normalize_username(body.username)
    try:
        user, issued = await svc.login(body.username, body.password)
    except LocalAuthError as e:
        raise _raise(e) from e
    request.state.principal = Principal(sub=user.id, email=user.email, memberships=[])
    return _token_out(issued, must_change=user.must_change_password)


@router.post("/auth/token", response_model=TokenOut)
async def data_plane_token(request: Request, principal: User) -> dict[str, Any]:
    """데이터 플레인 토큰. audience 는 서버가 정한다(mcp_audience 하나뿐이다). 본문은 없다."""
    svc = _service(request)
    user = await local_user(svc, principal)
    return _token_out(await svc.data_plane_token(user))


@router.post("/auth/password", response_model=TokenOut)
async def change_password(
    request: Request, body: PasswordChangeIn, principal: User
) -> dict[str, Any]:
    svc = _service(request)
    user = await local_user(svc, principal)
    try:
        updated, issued = await svc.change_password(user, body.current_password, body.new_password)
    except LocalAuthError as e:
        raise _raise(e) from e
    return _token_out(issued, must_change=updated.must_change_password)


@router.post("/auth/logout", status_code=204, response_class=Response)
async def logout(principal: User) -> Response:
    return Response(status_code=204)


async def local_user(svc: LocalAuthService, principal: Principal) -> LocalUser:
    """요청 주체의 사용자 문서. 토큰 검증 뒤 지워졌으면 401 이다."""
    user = await svc.users.get(principal.sub)
    if user is None:
        # 다른 local 401 과 같은 챌린지를 싣는다
        raise unauthorized(
            "unknown user", code="invalid_token", challenge='Bearer error="invalid_token"'
        )
    return user
