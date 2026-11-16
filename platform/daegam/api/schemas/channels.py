"""Teams 채널의 요청·응답 (P47 6.9절). 코드 평문은 `LinkCodeOut` 에만 있다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from daegam.store.base import UtcDatetime
from daegam.store.channels import Language


class TeamsSecretIn(BaseModel):
    """봇의 Client Secret. 관리 Secret 에만 들어가고 응답·감사·Mongo 에는 남지 않는다."""

    model_config = ConfigDict(extra="forbid")

    client_secret: str = Field(min_length=1, max_length=1024)


class TeamsSecretOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_set: bool


class TeamsLinkOut(BaseModel):
    """연결 하나. 계정 메뉴와 Platform Settings 가 같은 칸을 읽는다."""

    model_config = ConfigDict(extra="forbid")

    link_id: str
    username: str
    teams_name: str
    address: str
    linked_at: UtcDatetime
    ends_at: UtcDatetime
    last_turn_at: UtcDatetime
    language: Language
    # 이 연결이 바꾼 같은 사람의 앞 Teams 계정 이름과 그때
    replaced_name: str | None
    replaced_at: UtcDatetime | None


class TeamsPendingOut(BaseModel):
    """코드를 보낸 Teams 계정. 사람이 확인하기를 기다린다."""

    model_config = ConfigDict(extra="forbid")

    teams_name: str
    address: str
    expires_at: UtcDatetime


class TeamsTakenOut(BaseModel):
    """이 사람이 연결한 Teams 계정을 다른 대감 계정이 가져가 연결이 끊겼다는 표시. 다음 연결이나
    연결 끊기가 지운다."""

    model_config = ConfigDict(extra="forbid")

    teams_name: str
    at: UtcDatetime


class MyTeamsOut(BaseModel):
    """계정 메뉴의 Teams 연결. 열린 코드가 있으면 그 기한이다."""

    model_config = ConfigDict(extra="forbid")

    link: TeamsLinkOut | None
    pending: TeamsPendingOut | None
    code_expires_at: UtcDatetime | None
    taken: TeamsTakenOut | None


class MyChannelsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teams: MyTeamsOut


class LinkCodeIn(BaseModel):
    """local 모드는 지금 비밀번호를 받고 없으면 422 다. oidc 모드는 `X-Daegam-Id-Token` 의 최근
    로그인을 본다 (6.3절)."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, min_length=1, max_length=256)


class LinkCodeOut(BaseModel):
    """코드의 평문은 이 응답에만 있다."""

    model_config = ConfigDict(extra="forbid")

    code: str
    expires_at: UtcDatetime


class LinkConfirmIn(BaseModel):
    """확인을 기다리는 Teams 계정을 받는가(true) 버리는가(false)."""

    model_config = ConfigDict(extra="forbid")

    accept: bool


class TeamsLanguageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language


class TeamsUnlinkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unlinked: bool


class TeamsLinksCutOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cut: int


__all__ = [
    "LinkCodeIn",
    "LinkCodeOut",
    "LinkConfirmIn",
    "MyChannelsOut",
    "MyTeamsOut",
    "TeamsLanguageIn",
    "TeamsLinkOut",
    "TeamsLinksCutOut",
    "TeamsPendingOut",
    "TeamsSecretIn",
    "TeamsSecretOut",
    "TeamsTakenOut",
    "TeamsUnlinkOut",
]
