"""OAuth 동의 화면과 연결 목록의 요청·응답 (P33 7절·12절). 동의는 api/oauth.py 의 `_consent_out`,
연결은 api/oauth_grants.py 의 `_out` 과 같은 필드다.

MCP 클라이언트가 부르는 라우트(register, authorize, token, revoke)는 명세의 모양이라 OpenAPI 에 싣지
않고, 화면이 부르는 동의·연결 라우트만 여기 모델로 싣는다.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.store.base import UtcDatetime
from daegam.store.oauth_grants import ClientKind, GrantRevocation, GrantStatus, ResourceKind

# 요청 핸들의 본문 상한. 핸들은 수 KB 까지 커진다
MAX_HANDLE_CHARS = 8192


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=MAX_HANDLE_CHARS)


class ConsentClientOut(BaseModel):
    """클라이언트는 스스로 밝힌 것이다. `host` 는 CIMD 문서를 공개한 호스트이고 DCR 이면 null 이다.
    `name_collides` 는 DCR 이름이 알려진 클라이언트의 이름을 품을 때 참이다."""

    model_config = REQUIRED_DEFAULTS

    name: str
    ref: str
    kind: ClientKind
    host: str | None
    name_collides: bool


class ConsentRedirectOut(BaseModel):
    """돌아갈 곳. 언제나 이 컴퓨터(loopback)다."""

    model_config = REQUIRED_DEFAULTS

    host: str
    port: int | None


class ConsentResourceOut(BaseModel):
    """대상. 요청한 사람이 그 테넌트의 소속이 아니면 공개 주소만 싣고 나머지는 null 이다."""

    model_config = REQUIRED_DEFAULTS

    url: str
    kind: ResourceKind | None
    name: str | None
    display_name: str | None
    tenant: str | None


class ConsentOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    client: ConsentClientOut
    redirect: ConsentRedirectOut
    member: bool
    resource: ConsentResourceOut
    scope: list[str]
    expires_at: UtcDatetime
    access_token_seconds: int
    grant_days: int


class ConsentDecisionOut(BaseModel):
    """화면이 `window.location.assign` 으로 가는 주소. 허용이면 코드가, 거절이면 오류가 실린다."""

    model_config = REQUIRED_DEFAULTS

    redirect_to: str


class OAuthGrantOut(BaseModel):
    """연결 하나. 토큰 값도 해시도 없다. `client_host` 는 CIMD 문서를 공개한 호스트이고 DCR 이면
    null 이다. DCR 의 `client_name` 은 클라이언트가 스스로 밝힌 이름이다. `status` 는 active·
    expired(끝남)·revoked(끊김)이고 끝나거나 끊긴 연결은 30일 동안 남는다. `revoked_reason` 은
    2.0.0-rc1 이 모르는 까닭(`unconfirmed` 등)이 있으면 그것이다 (P53b 6.5절)."""

    model_config = REQUIRED_DEFAULTS

    grant_id: str
    client_name: str
    client_ref: str
    client_kind: ClientKind
    client_host: str | None
    tenant: str
    resource: str
    resource_kind: ResourceKind
    resource_id: str
    scope: list[str]
    created_at: UtcDatetime
    last_refresh_at: UtcDatetime
    expires_at: UtcDatetime
    revoked_at: UtcDatetime | None
    revoked_reason: GrantRevocation | None
    status: GrantStatus


class OAuthGrantRevokeOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    grant_id: str
    revoked: bool


__all__ = [
    "ConsentClientOut",
    "ConsentDecisionOut",
    "ConsentIn",
    "ConsentOut",
    "ConsentRedirectOut",
    "ConsentResourceOut",
    "OAuthGrantOut",
    "OAuthGrantRevokeOut",
]
