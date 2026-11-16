"""Teams 봇의 받는 경로 (P47 6.2절·6.3절, 1단계 A1).

`POST /api/channels/teams/messages` 를 Microsoft 의 Bot Connector 가 부른다. 이 차례로 본다.

1. 봇이 설정되어 있어야 한다(App ID 와 Entra 테넌트 id). 아니면 404 다.
2. 토큰. `Authorization: Bearer` 를 `inbound.py` 의 검증기로 본다. 막히면 401(토큰), 서명 키에
   `msteams` 보증이 없으면 403, 키 묶음을 받지 못하면 503 이다. 막힌 호출은 레플리카마다의 실패
   버킷에서 세고 버킷이 비면 429 `hook_rate_limited` 이며, 1분에 한 행을 두고 감사에 센다. 토큰을
   보기 전에는 본문을 읽지 않고 아무것도 세지 않으므로 Microsoft 의 토큰이 429 를 받는 일은 없다.
3. 본문을 256 KiB 까지 읽고 푼다. 토큰의 `serviceurl` 이 활동의 `serviceUrl` 과 다르면(끝의 `/`
   하나는 가리지 않는다) 401, `channelId` 가 `msteams` 가 아니거나 `channelData.tenant.id` 가 설정한
   테넌트가 아니거나 `recipient.id` 가 이 봇(`28:` 과 App ID)이 아니면 403 이다.
4. 메시지와 봇 추가는 Teams 사용자(`from.id`)마다의 속도(1분에 20번, 레플리카마다)를 넘으면
   버린다. 메시지는 받은 기록(대화, `from.id`, 활동 id)을 넣고, 이미 있으면 버린다. 그다음 200 으로
   답하고, 멤버 읽기와 고정 줄 올리기는 답한 뒤에 한다(`receive` 가 돌려주는 뒷일). 그 사이
   레플리카가 죽으면 고정 줄 하나를 잃는다.

메시지의 처리. 1:1 대화가 아니면 `bot.personalOnly` 한 줄이다. 활동의 네 값(테넌트, `aadObjectId`,
`from.id`, 대화)과 봇이 모두 같은 확인된 연결이 없으면 연결 전이다. 그때는 `연결 <코드>` 와 `도움말`
만 받고 그 밖에는 연결 안내(`bot.welcome`)를 대화마다 10분에 한 번 보낸다. 연결 전의 글은 봇
토큰으로 `from.id` 가 그 대화의 멤버임을 읽은 뒤에만 그 대화에 보낸다. 멤버 읽기는 비지 않은
`aadObjectId` 와 테넌트를 주어야 하고 둘 다 활동의 것과 같아야 한다. 기억한 답을 쓸 때도 견준다.
읽기는 레플리카마다 1분에 120번까지이고, 멤버가 아니거나 읽지 못한 답은 1분 기억한다. 연결이 있으면
인용·전달은 `bot.quoted`, 첨부는 `bot.attachments`, `연결 끊기` 는 연결을 끊고 `bot.unlinked`,
`도움말` 은 `bot.help` 이다. 이 단계에는 Teams 를 켠 Agent 가 없으므로 그 밖의 메시지는
`bot.noAgents` 다. 연결이 있을 때의 글은 연결의 대화에만 간다.

봇이 대화에 추가되면(`conversationUpdate`) 연결 안내를, 봇을 지우면(`installationUpdate` remove)
대화의 기록을 지운다. 그 밖의 활동은 200 으로 버린다. 토큰, 코드, 메시지 글은 로그와 감사에 남기지
않는다.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, NoReturn

import httpx

from daegam.audit.store import AuditEvent, MongoAuditStore
from daegam.auth.oidc import AuthError, ConnectorToken
from daegam.services.channels.connector import (
    REFUSED_CLOUD_HOST,
    ConnectorFailed,
    Member,
    ServiceUrlRefused,
    TeamsConnector,
)
from daegam.services.channels.errors import ChannelError
from daegam.services.channels.inbound import (
    PERSONAL,
    TEAMS_CHANNEL,
    Activity,
    ConnectorVerifiers,
    bot_id,
    parse_activity,
    same_service_url,
)
from daegam.services.channels.links import AUDIT_RESOURCE, SYSTEM_SUB, ChannelLinks
from daegam.services.channels.voice import TeamsVoice
from daegam.services.telemetry.ingest import KeyedBuckets
from daegam.store.base import new_id, utcnow
from daegam.store.channels import ChannelLink, MongoChannelStore
from daegam.store.settings import MongoPlatformSettingsStore, PlatformSettings
from daegam.ttl_cache import TTLCache
from daegam.urls import TEAMS_MESSAGES_PATH

logger = logging.getLogger("daegam.channels")

# 막힌 토큰의 버킷(레플리카마다 하나), Teams 사용자마다의 메시지 속도, 레플리카마다의 멤버 읽기
REFUSED_PER_MINUTE = 60.0
MESSAGES_PER_MINUTE = 20.0
MEMBER_READS_PER_MINUTE = 120.0
LIMITS_ENTRIES = 10_000
LIMITS_IDLE_SECONDS = 600.0
# `bot.rateLimited` 를 한 사람에게 다시 보내기까지
TOLD_SECONDS = 60.0
# 멤버 읽기의 답을 기억하는 동안. 멤버가 아니거나 읽지 못한 답은 짧게 기억한다
MEMBER_SECONDS = 600.0
NOT_MEMBER_SECONDS = 60.0
# 연결 없는 대화에 고정 줄을 다시 보내기까지 (6.3절)
NOTICE_EVERY = timedelta(minutes=10)

Reader = Callable[[], Awaitable[bytes | None]]
# 200 을 답한 뒤에 할 일
FollowUp = Callable[[], Awaitable[None]]
CommandKind = Literal["link", "unlink", "help", "other"]
MemberKey = tuple[str, str, str]

_UNLINK = re.compile(r"^(?:연결\s*끊기|unlink)$", re.IGNORECASE)
_LINK = re.compile(r"^(?:연결|link)\s+(.+)$", re.IGNORECASE)
_HELP = re.compile(r"^(?:도움말|help)$", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    code: str = ""


def parse_command(text: str) -> Command:
    """메시지 전체가 명령인가 (6.4절). 대소문자를 가리지 않고 한국어와 영어를 받는다."""
    text = text.strip()
    if _UNLINK.match(text):
        return Command("unlink")
    if _HELP.match(text):
        return Command("help")
    found = _LINK.match(text)
    if found:
        return Command("link", found.group(1))
    return Command("other")


def _member_key(a: Activity) -> MemberKey:
    return (a.service_url, a.conversation_id, a.from_id)


def _later(what: str, work: Callable[[], Awaitable[object]]) -> FollowUp:
    """답한 뒤의 일. 실패는 로그 한 줄로 접는다. 받는 경로의 답은 이미 나갔다."""

    async def run() -> None:
        try:
            await work()
        except Exception as e:  # noqa: BLE001 - 답한 뒤의 실패를 올릴 곳이 없다
            logger.warning("teams %s failed after the answer: %s", what, type(e).__name__)

    return run


class TeamsBot:
    def __init__(
        self,
        store: MongoChannelStore,
        links: ChannelLinks,
        voice: TeamsVoice,
        connector: TeamsConnector,
        platform_settings: MongoPlatformSettingsStore,
        audit: MongoAuditStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.store = store
        self.links = links
        self.voice = voice
        self.connector = connector
        self.platform_settings = platform_settings
        self.audit = audit
        self.verifiers = ConnectorVerifiers(transport)

        def buckets(per_minute: float, entries: int) -> KeyedBuckets:
            return KeyedBuckets(
                per_minute / 60,
                per_minute,
                idle_seconds=LIMITS_IDLE_SECONDS,
                max_entries=entries,
                clock=clock,
            )

        self._refused = buckets(REFUSED_PER_MINUTE, 1)
        self._messages = buckets(MESSAGES_PER_MINUTE, LIMITS_ENTRIES)
        self._member_reads = buckets(MEMBER_READS_PER_MINUTE, 1)
        self._told: TTLCache[str, bool] = TTLCache(
            TOLD_SECONDS, clock=clock, max_entries=LIMITS_ENTRIES
        )
        self._members: TTLCache[MemberKey, Member] = TTLCache(
            MEMBER_SECONDS, clock=clock, max_entries=LIMITS_ENTRIES
        )
        self._not_members: TTLCache[MemberKey, bool] = TTLCache(
            NOT_MEMBER_SECONDS, clock=clock, max_entries=LIMITS_ENTRIES
        )

    async def aclose(self) -> None:
        await self.verifiers.aclose()
        await self.connector.aclose()

    # -- 받기 --------------------------------------------------------------------------------

    async def receive(self, token: str | None, read_body: Reader) -> FollowUp | None:
        """활동 하나를 받는다. 막으면 ChannelError 다. token 은 `Authorization: Bearer` 의 값이고
        read_body 는 상한을 넘으면 None 인 본문이며 토큰을 본 뒤에만 부른다. 200 을 답한 뒤에 할
        일이 있으면 돌려준다."""
        settings = await self.platform_settings.get()
        teams = settings.channels.teams
        if not teams.app_id or not teams.entra_tenant_id:
            raise ChannelError(404, "not_found", "no Teams bot is configured")
        verified = await self._verify(token, teams.app_id, settings)
        body = await read_body()
        if body is None:
            raise ChannelError(413, "payload_too_large", "the activity is larger than 256 KiB")
        try:
            activity = parse_activity(body)
        except (ValueError, RecursionError):
            raise ChannelError(422, "invalid_value", "the activity is not a JSON object") from None
        if not same_service_url(verified.service_url, activity.service_url):
            await self._refuse(401, "invalid_token", "the token is for another service url")
        if (
            activity.channel_id != TEAMS_CHANNEL
            or activity.entra_tenant_id != teams.entra_tenant_id
            or activity.recipient_id != bot_id(teams.app_id)
        ):
            await self._refuse(403, "forbidden", "the activity is not for this bot and tenant")
        return await self._accept(activity, settings)

    async def _verify(
        self, token: str | None, app_id: str, settings: PlatformSettings
    ) -> ConnectorToken:
        if not token:
            await self._refuse(401, "invalid_token", "a Bearer token is required")
        verifier = await self.verifiers.get(app_id, settings.network.ca_bundle)
        try:
            verified = await verifier.verify_connector(token)
        except AuthError as e:
            if e.status == 503:
                raise ChannelError(
                    503, "idp_unavailable", "the Bot Connector key set cannot be fetched"
                ) from e
            await self._refuse(401, e.code, e.detail)
        if TEAMS_CHANNEL not in verified.endorsements:
            await self._refuse(403, "forbidden", "the signing key is not endorsed for Teams")
        return verified

    async def _refuse(self, status: int, code: str, detail: str) -> NoReturn:
        """막은 호출. 실패 버킷이 비었으면 감사에 쓰지 않고 429 다. 아니면 1분에 한 행에 센다."""
        if not self._refused.take("refused"):
            raise ChannelError(
                429,
                "hook_rate_limited",
                "too many refused calls",
                headers={"Retry-After": "1"},
            )
        minute = utcnow().replace(second=0, microsecond=0)
        event = AuditEvent(
            id=f"teams-refused:{minute.isoformat()}",
            ts=minute,
            sub=SYSTEM_SUB,
            subject_type="system",
            action="teams_refused",
            resource=AUDIT_RESOURCE,
            method="POST",
            path=TEAMS_MESSAGES_PATH,
            request_id=new_id(),
            status=status,
            result="error",
            error_code=code,
        )
        try:
            await self.audit.count(event)
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 답은 같다
            logger.warning("teams refused audit row not written: %s", e)
        raise ChannelError(status, code, detail)

    # -- 활동 --------------------------------------------------------------------------------

    async def _accept(self, a: Activity, settings: PlatformSettings) -> FollowUp | None:
        """받은 활동의 기록과 속도. 답한 뒤에 할 일을 돌려준다."""
        app_id = settings.channels.teams.app_id or ""
        if a.type == "installationUpdate" and a.action == "remove":
            await self.store.forget_conversation(a.conversation_id)
            return None
        added = a.type == "conversationUpdate" and bot_id(app_id) in a.members_added
        if not (a.type == "message" or added) or not (a.from_id and a.conversation_id):
            return None
        if not self._messages.take(a.from_id):
            return _later("rate line", lambda: self._tell_rate_limited(a, settings))
        if added:
            key = "bot.welcome" if a.conversation_type == PERSONAL else "bot.personalOnly"
            return _later("welcome", lambda: self._notice(a, settings, key))
        if not (a.id and a.aad_object_id):
            return None
        if not await self.store.record_activity(a.conversation_id, a.from_id, a.id, utcnow()):
            return None
        return _later("message", lambda: self._message(a, settings))

    async def _message(self, a: Activity, settings: PlatformSettings) -> None:
        if a.conversation_type != PERSONAL:
            await self._notice(a, settings, "bot.personalOnly")
            return
        link = await self.links.match(a.account(settings.channels.teams.app_id or ""))
        command = parse_command(a.text)
        if link is None:
            await self._unlinked(a, settings, command)
            return
        await self._linked(a, settings, link, command)

    async def _unlinked(self, a: Activity, settings: PlatformSettings, command: Command) -> None:
        if command.kind == "link":
            await self._redeem(a, settings, command.code)
        elif command.kind == "help":
            if await self._member(a, settings) is not None:
                await self.voice.say(
                    a.service_url, a.conversation_id, "bot.help", settings=settings
                )
        else:
            await self._notice(a, settings, "bot.welcome")

    async def _linked(
        self, a: Activity, settings: PlatformSettings, link: ChannelLink, command: Command
    ) -> None:
        if a.quoted:
            key = "bot.quoted"
        elif a.attachments:
            key = "bot.attachments"
        elif command.kind == "link":
            await self._redeem(a, settings, command.code)
            return
        elif command.kind == "unlink":
            await self.links.unlink_from_teams(link)
            key = "bot.unlinked"
        elif command.kind == "help":
            key = "bot.help"
        else:
            # Teams 를 켠 Agent 는 2단계(A2)부터 있다
            key = "bot.noAgents"
        await self.voice.say_to(link.teams, key, link.language)

    async def _tell_rate_limited(self, a: Activity, settings: PlatformSettings) -> None:
        """속도를 넘은 메시지. 연결된 사람에게만 1분에 한 번 알린다."""
        if a.type != "message" or self._told.get(a.from_id):
            return
        link = await self.links.match(a.account(settings.channels.teams.app_id or ""))
        if link is None:
            return
        self._told.put(a.from_id, True)
        await self.voice.say_to(link.teams, "bot.rateLimited", link.language)

    async def _notice(self, a: Activity, settings: PlatformSettings, key: str) -> None:
        """연결 없는 대화의 고정 줄. 멤버 확인을 지난 대화에 10분에 한 번이다."""
        if await self._member(a, settings) is None:
            return
        if not await self.store.claim_notice(a.conversation_id, utcnow(), NOTICE_EVERY):
            return
        await self.voice.say(a.service_url, a.conversation_id, key, settings=settings)

    async def _redeem(self, a: Activity, settings: PlatformSettings, code: str) -> None:
        """`연결 <코드>`. 연결의 `aadObjectId` 와 잠금 열쇠는 멤버 읽기의 값이다. 연결되면
        `ChannelLinks` 가 새 대화에 `bot.linked` 를 올린다."""
        member = await self._member(a, settings)
        if member is None or member.aad_object_id is None:
            return
        addresses = [x for x in (member.user_principal_name, member.email) if x]
        account = a.account(settings.channels.teams.app_id or "").model_copy(
            update={
                "aad_object_id": member.aad_object_id,
                "teams_name": member.name or a.from_name,
                "address": addresses[0] if addresses else "",
            }
        )
        result = await self.links.redeem(code, account, addresses)
        if result.outcome == "linked":
            return
        key = {
            "blocked": "bot.codeBlocked",
            "pending": "bot.pendingConfirm",
        }.get(result.outcome, "bot.codeInvalid")
        await self.voice.say(a.service_url, a.conversation_id, key, settings=settings)

    async def _member(self, a: Activity, settings: PlatformSettings) -> Member | None:
        """`from.id` 가 그 대화의 멤버인가 (6.3절). 맞으면 멤버이고, 아니거나 읽지 못하면 None 이다.
        멤버는 비지 않은 `aadObjectId` 와 테넌트를 주어야 하고 둘 다 활동의 것과 같아야 한다.
        기억한 답도 활동마다 다시 견준다."""
        teams = settings.channels.teams
        key = _member_key(a)
        member = self._members.get(key)
        if member is None:
            if self._not_members.get(key) or not self._member_reads.take("reads"):
                return None
            member = await self._read_member(a, settings)
            if member is None:
                self._not_members.put(key, True)
                return None
            self._members.put(key, member)
        same = (
            member.id == a.from_id
            and member.aad_object_id
            and member.aad_object_id == a.aad_object_id
            and member.tenant_id
            and member.tenant_id == teams.entra_tenant_id
        )
        return member if same else None

    async def _read_member(self, a: Activity, settings: PlatformSettings) -> Member | None:
        teams = settings.channels.teams
        if teams.app_id is None or teams.entra_tenant_id is None:
            return None
        try:
            return await self.connector.member(
                a.service_url,
                a.conversation_id,
                a.from_id,
                tenant=teams.entra_tenant_id,
                app_id=teams.app_id,
                ca_bundle=settings.network.ca_bundle,
            )
        except ServiceUrlRefused as e:
            if e.reason == REFUSED_CLOUD_HOST:
                logger.warning("teams activity from an unsupported Microsoft cloud")
            return None
        except (ConnectorFailed, ChannelError) as e:
            logger.warning("teams member read failed: %s", type(e).__name__)
            return None


__all__ = ["Command", "FollowUp", "TeamsBot", "parse_command"]
