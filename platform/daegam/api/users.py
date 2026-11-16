"""local 모드의 사용자 관리 API (D40). platform-admin(`users:write`)만 쓴다.

GET    /users                목록 (users:write)
GET    /users/directory      어느 테넌트든 `members:write` 가 있으면 id·username·display_name 만
                             준다(멤버 화면에서 사람을 고른다)
POST   /users                만들기. username 은 만든 뒤 못 바꾼다
GET    /users/{id}
PATCH  /users/{id}           email·display_name·disabled·platform_admin. 막으면 개인 토큰이 회수된다
POST   /users/{id}/password  관리자 재설정. 다음 로그인에 본인이 다시 바꾼다. 개인 토큰이 회수된다
DELETE /users/{id}           소속도 지운다. 개인 토큰이 회수된다. 응답은 {id, deleted}

마지막 활성 platform-admin 은 막거나 지우거나 내리지 못한다(422 last_admin). oidc 모드에는 이
라우터가 없다. 사용자는 IdP 가 관리한다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from daegam.api._common import forbidden, or_404, patched_fields
from daegam.api.deps import User, require_local_mode, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.users import (
    UserCreateIn,
    UserDeleteResponse,
    UserOut,
    UserPasswordIn,
    UserPatchIn,
    UserSummaryOut,
)
from daegam.auth.principal import Principal
from daegam.services.local_auth import LocalAuthService
from daegam.store.users import User as LocalUser

router = APIRouter(
    responses=ERROR_RESPONSES, tags=["users"], dependencies=[Depends(require_local_mode)]
)

UsersWrite = Annotated[Principal, Depends(require_scope("users:write"))]


def _service(request: Request) -> LocalAuthService:
    return request.app.state.local_auth


async def _out(svc: LocalAuthService, user: LocalUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "disabled": user.disabled,
        "must_change_password": user.must_change_password,
        "platform_admin": await svc.is_platform_admin(user),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login_at": user.last_login_at,
    }


def _summary(user: LocalUser) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


async def require_users_read(principal: User) -> Principal:
    """목록 읽기. `users:write` 거나 어느 테넌트든 `members:write` 가 있으면 된다.

    `require_scope` 는 "어느 테넌트든" 을 표현하지 못하고(테넌트 헤더 없이 소속이 여럿이면 400)
    두 scope 의 합도 못 만든다.
    테넌트 관리자는 이 길로 플랫폼 전체 사용자 이름을 본다(D40)."""
    scopes = principal.all_scopes
    if "users:write" in scopes or "members:write" in scopes:
        return principal
    raise forbidden("scope users:write or members:write required", scope="users:write")


async def user_or_404(svc: LocalAuthService, user_id: str) -> LocalUser:
    """경로의 사용자. 없으면 404 다. 관리자의 개인 토큰 라우트(api/personal_tokens.py)도 쓴다."""
    return or_404(await svc.users.get(user_id), "user", user_id)


@router.get("/users", response_model=list[UserOut])
async def list_users(request: Request, _: UsersWrite) -> list[dict[str, Any]]:
    svc = _service(request)
    return [await _out(svc, u) for u in await svc.users.list()]


@router.get("/users/directory", response_model=list[UserSummaryOut])
async def user_directory(
    request: Request, _: Annotated[Principal, Depends(require_users_read)]
) -> list[dict[str, Any]]:
    """멤버 화면에서 사람을 고르는 목록. 계정 상태는 없다."""
    svc = _service(request)
    return [_summary(u) for u in await svc.users.list()]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(request: Request, body: UserCreateIn, _: UsersWrite) -> dict[str, Any]:
    svc = _service(request)
    user = await svc.create_user(
        username=body.username,
        password=body.password,
        email=body.email,
        display_name=body.display_name,
        platform_admin=body.platform_admin,
    )
    return await _out(svc, user)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(request: Request, user_id: str, _: UsersWrite) -> dict[str, Any]:
    svc = _service(request)
    return await _out(svc, await user_or_404(svc, user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    request: Request, user_id: str, body: UserPatchIn, principal: UsersWrite
) -> dict[str, Any]:
    svc = _service(request)
    user = await user_or_404(svc, user_id)
    sent = patched_fields(body)
    if "platform_admin" in sent and body.platform_admin is not None:
        await svc.set_platform_admin(user, body.platform_admin)
    if "disabled" in sent and body.disabled is not None:
        user = await svc.set_disabled(user, body.disabled, by=principal.sub)
    profile = {k: getattr(body, k) for k in ("email", "display_name") if k in sent}
    if profile:
        updated = await svc.users.update(user.id, profile)
        user = updated or user
    return await _out(svc, user)


@router.post("/users/{user_id}/password", response_model=UserOut)
async def reset_password(
    request: Request, user_id: str, body: UserPasswordIn, principal: UsersWrite
) -> dict[str, Any]:
    svc = _service(request)
    user = await user_or_404(svc, user_id)
    updated = await svc.reset_password(user, body.password, by=principal.sub)
    return await _out(svc, updated)


@router.delete("/users/{user_id}", response_model=UserDeleteResponse)
async def delete_user(request: Request, user_id: str, principal: UsersWrite) -> dict[str, Any]:
    svc = _service(request)
    user = await user_or_404(svc, user_id)
    await svc.delete_user(user, by=principal.sub)
    return {"id": user.id, "deleted": True}
