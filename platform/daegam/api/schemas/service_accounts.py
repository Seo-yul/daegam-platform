"""서비스 어카운트 요청·응답 (P53b 6.7절). api/service_accounts.py 의 `_out` 과 같은 필드다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from daegam.api.schemas.personal_tokens import PersonalTokenOut
from daegam.auth.rbac import TenantRole
from daegam.store.base import UtcDatetime

# DAEGAM_SERVICE_ACCOUNTS 의 값 (`daegam.config` `SERVICE_ACCOUNTS_MODES`). 시험이 둘을 맞춰 본다
ServiceAccountsMode = Literal["tenant-admin", "platform-admin", "off"]


class ServiceAccountIn(BaseModel):
    """이름의 길이(1~64자)는 서비스가 422 invalid_value 로 본다. 여기 길이는 본문의 상한일
    뿐이다."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=256)
    role: TenantRole


class ServiceAccountRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: TenantRole


class ServiceAccountTokenIn(BaseModel):
    """expires_at 이 없으면 90일(DAEGAM_PAT_MAX_DAYS 가 더 짧으면 그것)이다. password 는 local
    모드에서만 받는 내는 사람의 비밀번호이고, oidc 는 `X-Daegam-Id-Token` 의 최근 로그인을 본다."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=256)
    expires_at: datetime | None = None
    password: str | None = Field(default=None, min_length=1, max_length=256)


class ServiceAccountOut(BaseModel):
    """계정 하나와 그 토큰(새것부터). 회수됐거나 만료한 토큰도 30일 동안 남는다."""

    model_config = ConfigDict(extra="forbid")

    account_id: str
    tenant: str
    name: str
    role: TenantRole
    created_by: str
    created_by_name: str | None
    created_at: UtcDatetime
    tokens: list[PersonalTokenOut]


class ServiceAccountsOut(BaseModel):
    """테넌트의 계정 목록. mode 는 DAEGAM_SERVICE_ACCOUNTS 이고, can_manage 는 이 사람이 계정을
    만들거나 역할을 바꾸거나 토큰을 낼 수 있는가다. 회수와 지우기는 늘 된다."""

    model_config = ConfigDict(extra="forbid")

    mode: ServiceAccountsMode
    can_manage: bool
    accounts: list[ServiceAccountOut]


class ServiceAccountDeleteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    deleted: bool


__all__ = [
    "ServiceAccountDeleteOut",
    "ServiceAccountIn",
    "ServiceAccountOut",
    "ServiceAccountRoleIn",
    "ServiceAccountTokenIn",
    "ServiceAccountsOut",
]
