"""개인 MCP 토큰 요청·응답 (P32 6절, P53b 6.5절). api/personal_tokens.py 의 `token_out` 과 같은
필드다."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from daegam.store.base import UtcDatetime
from daegam.store.personal_tokens import Revocation, TokenStatus


class PersonalTokenIssueIn(BaseModel):
    """이름의 길이(1~64자)와 만료의 범위는 서비스가 422 invalid_value 로 본다. 여기 길이는 본문의
    상한일 뿐이다. password 는 local 모드에서만 받고 그때는 없으면 422 다. oidc 모드는 비밀번호
    대신 `X-Daegam-Id-Token` 의 최근 로그인을 본다 (P53b 6.4절)."""

    model_config = ConfigDict(extra="forbid")

    tenant: str = Field(min_length=1, max_length=64)
    label: str = Field(max_length=256)
    expires_at: datetime
    password: str | None = Field(default=None, min_length=1, max_length=256)


class PersonalTokenOut(BaseModel):
    """`status` 는 active·pending(ready_at 전)·revoking·revoked·expired 다. 토큰 값은 없다.
    `revoke_reason` 은 2.0.0-rc1 이 모르는 까닭(`unconfirmed` 등)이 있으면 그것이다 (P53b
    6.5절)."""

    model_config = ConfigDict(extra="forbid")

    token_id: str
    label: str
    tenant: str
    group: str
    created_at: UtcDatetime
    expires_at: UtcDatetime
    ready_at: UtcDatetime | None
    revoked_at: UtcDatetime | None
    revoke_reason: Revocation | None
    status: TokenStatus


class IssuedPersonalToken(PersonalTokenOut):
    """발급 응답. 평문 토큰은 여기에만 있다."""

    token: str


__all__ = ["IssuedPersonalToken", "PersonalTokenIssueIn", "PersonalTokenOut"]
