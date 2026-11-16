"""local 모드의 로그인·토큰·사용자 관리 스키마 (D40)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from daegam.store.base import UtcDatetime


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenOut(BaseModel):
    """`POST /auth/login`·`/auth/token`·`/auth/password` 의 응답.

    OAuth 토큰 응답과 같은 이름을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    audience: str
    must_change_password: bool = False


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    """비밀번호 해시는 절대 나가지 않는다. platform_admin 은 memberships 의 `*` 줄에서 온다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    email: str | None
    display_name: str | None
    disabled: bool
    must_change_password: bool
    platform_admin: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime
    last_login_at: UtcDatetime | None


class UserSummaryOut(BaseModel):
    """`members:write` 만 가진 사람이 보는 목록. 사람을 고르는 데 필요한 것만이다. 계정 상태는
    다른 테넌트 관리자가 알 일이 아니라 넣지 않는다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    display_name: str | None


class UserCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)
    platform_admin: bool = False


class UserPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)
    disabled: bool | None = None
    platform_admin: bool | None = None


class UserPasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


class UserDeleteResponse(BaseModel):
    """다른 리소스의 삭제 응답과 같은 꼴이다. 소속도 함께 지워졌다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    deleted: bool = True


__all__ = [
    "LoginIn",
    "PasswordChangeIn",
    "TokenOut",
    "UserCreateIn",
    "UserDeleteResponse",
    "UserOut",
    "UserPasswordIn",
    "UserPatchIn",
    "UserSummaryOut",
]
