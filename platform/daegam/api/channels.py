"""Teams 채널의 라우트 (P47 6.9절).

POST   /channels/teams/messages                 Bot Connector 의 활동. OpenAPI 밖이고 판단은
                                                `services/channels/bot.py` 에 있다
PUT    /settings/channels/teams/client-secret   봇의 Client Secret 을 관리 Secret 에 넣는다
GET    /settings/channels/teams/app-package     Teams 앱 패키지(zip)
GET    /settings/channels/teams/links           연결 목록
DELETE /settings/channels/teams/links           모든 연결 끊기
DELETE /settings/channels/teams/links/{link_id} 연결 하나 끊기
GET    /me/channels                             내 연결, 확인 대기, 열린 코드의 기한
POST   /me/channels/teams/link-code             새 연결 코드. 개인 토큰과 같은 확인(oidc 는 최근
                                                로그인, local 은 지금 비밀번호)으로 403 이 같다
POST   /me/channels/teams/confirm               확인 대기의 받기와 버리기
PATCH  /me/channels/teams                       봇의 언어
DELETE /me/channels/teams                       연결 끊기

`DAEGAM_CHANNELS_ENABLED` 를 껐거나 사람을 확인하지 않는 header 모드면 모두 404 다. 설정 라우트는
platform-admin(`settings:write`)의 것이다. 쓰기는 감사 미들웨어가 남기고, 메시지 엔드포인트는
경로가 스스로 쓴다. 코드와 Client Secret 은 응답 하나에만 있거나(코드) 어디에도 없다(Secret).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from daegam.api._common import error, not_found, one_line, require_secret_store
from daegam.api._download import download_openapi, download_response
from daegam.api.deps import (
    Settings_,
    User,
    bearer_value,
    require_person,
    require_recent_sign_in,
    require_scope,
)
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.channels import (
    LinkCodeIn,
    LinkCodeOut,
    LinkConfirmIn,
    MyChannelsOut,
    MyTeamsOut,
    TeamsLanguageIn,
    TeamsLinkOut,
    TeamsLinksCutOut,
    TeamsSecretIn,
    TeamsSecretOut,
    TeamsUnlinkOut,
)
from daegam.config import Settings
from daegam.request_body import body_upto
from daegam.services.channels.bot import TeamsBot
from daegam.services.channels.connector import SECRET_KEY, SECRET_NAME
from daegam.services.channels.inbound import BODY_CAP
from daegam.services.channels.links import ChannelLinks
from daegam.services.channels.package import app_package
from daegam.store.channels import ChannelLink, ChannelLinkCode, LinkNotice
from daegam.urls import TEAMS_MESSAGES_ROUTE

PACKAGE_RESPONSES = download_openapi("Teams 앱 패키지(zip). 내려받기 헤더로 온다")
PACKAGE_NAME = "daegam-teams.zip"
ZIP = "application/zip"


def require_channels(request: Request) -> None:
    """채널을 껐거나 사람을 확인하지 않는 모드면 라우트가 없는 것처럼 404 다 (6.1절)."""
    settings: Settings = request.app.state.settings
    if not settings.channels_enabled or not settings.token_auth:
        raise not_found("channels are not enabled")


def _links(request: Request) -> ChannelLinks:
    return request.app.state.channel_links


inbound_router = APIRouter(include_in_schema=False, dependencies=[Depends(require_channels)])
settings_router = APIRouter(
    responses=ERROR_RESPONSES,
    prefix="/settings/channels/teams",
    tags=["settings"],
    dependencies=[Depends(require_channels), Depends(require_scope("settings:write"))],
)
me_router = APIRouter(
    responses=ERROR_RESPONSES,
    tags=["channels"],
    dependencies=[Depends(require_channels), Depends(require_person)],
)


@inbound_router.post(TEAMS_MESSAGES_ROUTE)
async def receive_teams_activity(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """Bot Connector 의 활동 하나. 받아 적으면 200 이고 본문은 비어 있다. 멤버 읽기와 고정 줄은
    답한 뒤에 한다."""
    bot: TeamsBot = request.app.state.teams_bot
    follow = await bot.receive(bearer_value(request), lambda: body_upto(request, BODY_CAP))
    if follow is not None:
        background.add_task(follow)
    return {}


# -- Platform Settings --------------------------------------------------------------------------


@settings_router.put("/client-secret", response_model=TeamsSecretOut)
async def put_teams_client_secret(
    request: Request, body: TeamsSecretIn, user: User
) -> dict[str, Any]:
    """봇의 Client Secret 을 플랫폼 네임스페이스의 관리 Secret(`daegam-channel-teams`, 항목
    `client_secret`)에 넣고 `channels.teams.secret_set` 을 켠다 (D56). 값은 응답·감사·Mongo 에
    남지 않는다. 표시는 그 칸만 그 자리에서 쓰므로 다른 저장과 부딪혀 Secret 만 남는 일이 없다.
    받아 둔 봇 토큰을 버린다."""
    secret = one_line(body.client_secret)
    if secret is None:
        raise error(422, "invalid_value", "the client secret must be one line without spaces")
    secrets = require_secret_store(request, "teams")
    namespace = request.app.state.settings.namespace
    await run_in_threadpool(secrets.put, SECRET_NAME, {SECRET_KEY: secret}, namespace=namespace)
    await request.app.state.platform_settings.mark_teams_secret(updated_by=user.sub)
    request.app.state.teams_connector.forget_tokens()
    return {"secret_set": True}


@settings_router.get("/app-package", response_class=Response, responses=PACKAGE_RESPONSES)
async def get_teams_app_package(request: Request, settings: Settings_) -> Response:
    """Teams 관리 센터에 올릴 앱 패키지. App ID 와 https 공개 주소가 있어야 한다."""
    teams = (await request.app.state.platform_settings.get()).channels.teams
    public = settings.public_url or ""
    host = urlsplit(public).hostname
    if not teams.app_id:
        raise error(422, "teams_not_configured", "enter the bot's App ID first")
    if not public.startswith("https://") or not host:
        raise error(
            422,
            "teams_not_configured",
            "the platform needs an https public URL (DAEGAM_PUBLIC_URL)",
        )
    body = app_package(teams.app_id, public.rstrip("/"), host)
    return download_response(body, PACKAGE_NAME, media_type=ZIP)


@settings_router.get("/links", response_model=list[TeamsLinkOut])
async def list_teams_links(request: Request) -> list[dict[str, Any]]:
    return [link_out(link) for link in await _links(request).links()]


@settings_router.delete("/links", response_model=TeamsLinksCutOut)
async def cut_all_teams_links(request: Request) -> dict[str, Any]:
    """모든 연결과 열린 코드를 지운다. 연결된 모든 사람이 다시 연결해야 한다."""
    return {"cut": await _links(request).cut_all()}


@settings_router.delete("/links/{link_id}", response_model=TeamsLinksCutOut)
async def cut_teams_link(request: Request, link_id: str) -> dict[str, Any]:
    link = await _links(request).cut(link_id)
    if link is None:
        raise not_found(f"teams link {link_id} not found")
    return {"cut": 1}


# -- 계정 메뉴 -------------------------------------------------------------------------------


def link_out(link: ChannelLink) -> dict[str, Any]:
    return {
        "link_id": link.id,
        "username": link.username,
        "teams_name": link.teams.teams_name,
        "address": link.teams.address,
        "linked_at": link.linked_at,
        "ends_at": link.ends_at,
        "last_turn_at": link.last_turn_at,
        "language": link.language,
        "replaced_name": link.replaced_name,
        "replaced_at": link.replaced_at,
    }


def _mine(
    link: ChannelLink | None, code: ChannelLinkCode | None, notice: LinkNotice | None
) -> dict[str, Any]:
    pending = None
    if code is not None and code.pending is not None:
        pending = {
            "teams_name": code.pending.teams_name,
            "address": code.pending.address,
            "expires_at": code.expires_at,
        }
    return {
        "link": link_out(link) if link is not None else None,
        "pending": pending,
        "code_expires_at": code.expires_at if code is not None and pending is None else None,
        "taken": {"teams_name": notice.teams_name, "at": notice.at} if notice else None,
    }


async def _my_teams(request: Request, sub: str) -> dict[str, Any]:
    return _mine(*await _links(request).view(sub))


@me_router.get("/me/channels", response_model=MyChannelsOut)
async def get_my_channels(request: Request, principal: User) -> dict[str, Any]:
    return {"teams": await _my_teams(request, principal.sub)}


@me_router.post("/me/channels/teams/link-code", response_model=LinkCodeOut, status_code=201)
async def make_teams_link_code(
    request: Request, body: LinkCodeIn, principal: User, settings: Settings_
) -> dict[str, Any]:
    """Teams 에서 보낼 연결 코드 (6.3절). 10분 살고 앞 코드를 바꾼다. 평문은 이 응답에만 있다."""
    await require_recent_sign_in(request, settings, principal)
    # local 은 지금 비밀번호, oidc 는 긴 한도 안의 사람 기록이다. 개인 토큰과 같은 403 이다
    person = await request.app.state.personal_tokens.confirm_person(principal.sub, body.password)
    code, expires_at = await _links(request).make_code(person.sub, person.name, person.tv)
    return {"code": code, "expires_at": expires_at}


@me_router.post("/me/channels/teams/confirm", response_model=MyTeamsOut)
async def confirm_teams_link(
    request: Request, body: LinkConfirmIn, principal: User
) -> dict[str, Any]:
    """코드를 보낸 Teams 계정을 받거나 버린다. 기다리는 계정이 없으면 404 다. 받으면 그 대화에
    연결을 알린다(`ChannelLinks`)."""
    if await _links(request).confirm(principal.sub, body.accept) is None:
        raise not_found("no Teams account is waiting for confirmation")
    return await _my_teams(request, principal.sub)


@me_router.patch("/me/channels/teams", response_model=MyTeamsOut)
async def set_teams_language(
    request: Request, body: TeamsLanguageIn, principal: User
) -> dict[str, Any]:
    """봇의 언어. 연결이 없으면 404 다."""
    link = await _links(request).set_language(principal.sub, body.language)
    if link is None:
        raise not_found("no Teams link")
    return await _my_teams(request, principal.sub)


@me_router.delete("/me/channels/teams", response_model=TeamsUnlinkOut)
async def unlink_teams(request: Request, principal: User) -> dict[str, Any]:
    """연결, 열린 코드, 가져간 연결의 표시를 지운다."""
    link = await _links(request).unlink(principal.sub)
    return {"unlinked": link is not None}


__all__ = ["inbound_router", "link_out", "me_router", "require_channels", "settings_router"]
