"""Teams 사용자와 사람의 연결 (P47 6.3절).

- **코드.** local 과 oidc 모드의 로그인한 사람이 계정 메뉴에서 만든다. 라우트가 개인 토큰과 같은
  확인을 한다(oidc 는 최근 로그인, local 은 지금 비밀번호). 8글자이고 헷갈리는 글자(I, L, O, 0, 1)
  가 없으며 10분 산다. 사람마다 열린 코드 하나이고, 플랫폼 서명 키에서 HKDF 로 만든 키의
  HMAC-SHA256 으로 둔다. 그래서 DB 를 읽은 사람도 살아 있는 코드를 거꾸로 찾지 못한다. local 은
  만들 때의 `tv` 를 함께 둔다.
- **보내기.** 사람이 Teams 의 1:1 채팅에 `연결 <코드>` 를 보낸다. 받는 경로가 먼저 봇 토큰으로 그
  `from.id` 가 그 대화의 멤버인지 읽고, 연결의 `aadObjectId` 와 틀린 코드의 잠금 열쇠는 그 멤버
  읽기의 값이다. 코드는 한 번만 쓰인다. 멤버의 `userPrincipalName` 이나 `email` 이 사람의
  이메일(local `User.email`, oidc `IdpPerson.email`)과 같으면 곧바로 연결하고, 아니면 그 Teams
  계정을 코드에 두고 사람이 계정 메뉴에서 10분 안에 확인할 때까지 기다린다. 한 Teams 사용자가 한
  시간에 다섯 번 틀리면 한 시간 동안 코드를 받지 않는다(`login_failures` 의 `teams-code:` 열쇠).
- **하나와 하나.** 새 연결은 그 사람의 앞 연결과 그 Teams 사용자의 앞 연결을 바꾼다. 같은 사람이
  다른 Teams 계정으로 옮기면 앞 대화에 `bot.replaced` 를 올리고 새 연결이 앞 계정의 이름을 든다.
  Teams 사용자가 다른 사람에게 옮겨 가면, 연결을 잃은 사람에게 표시(`channel_link_notices`)를 남겨
  계정 메뉴가 보이고, 감사 행(`teams_links_cut`, `replaced`)을 쓰며, 앞 대화가 새 대화와 다르면 그
  대화에 `bot.relinked` 를 올린다. 새 연결의 대화에는 `bot.linked` 를 올린다.
- **끝.** 연결할 때 `DAEGAM_PAT_MAX_DAYS` 뒤로 끝을 정하고 쓰여도 밀지 않는다. 사람(계정 메뉴나
  Teams 의 `연결 끊기`), platform-admin(하나나 모두), App ID 나 Entra 테넌트 id 의 변경, 실행을
  멈추는 계정 훅(local 계정 삭제·막기, `tv` 가 바뀐 비밀번호), 리컨실의 정리(끝이 지남, 90일 동안
  Teams 턴 없음, local 계정이 없거나 막혔거나 `tv` 가 다름, oidc 사람 기록이 없거나 비워짐, 다른 App
  ID 나 테넌트의 연결)가 끊는다. 끝이나 쓰지 않아 끊긴 연결의 대화에는 한 줄을 올린다.

감사는 Teams 에서 한 일(연결, 확인 대기, 연결 끊기)과 요청 없이 끊은 일을 여기서 쓴다. 화면의 요청은
감사 미들웨어가 남긴다. 코드와 메시지 글은 어디에도 남기지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from daegam.audit.store import AuditEvent, MongoAuditStore, record_system_event
from daegam.auth.service_token import InternalIssuer
from daegam.config import Settings
from daegam.services.access_revocation import account_reason
from daegam.services.channels.errors import ChannelError
from daegam.services.channels.voice import TeamsVoice
from daegam.services.reconcile import interval_due
from daegam.store.base import new_id, utcnow
from daegam.store.channels import (
    ChannelLink,
    ChannelLinkCode,
    Language,
    LinkNotice,
    MongoChannelStore,
    TeamsAccount,
)
from daegam.store.idp_people import IdpPerson, MongoIdpPeopleStore
from daegam.store.login_throttle import MongoLoginThrottle
from daegam.store.personal_tokens import DELETED, DISABLED, PASSWORD, Revocation
from daegam.store.settings import MongoPlatformSettingsStore, TeamsSettings
from daegam.store.users import MongoUserStore, User
from daegam.urls import TEAMS_MESSAGES_PATH

logger = logging.getLogger("daegam.channels")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL = timedelta(minutes=10)
# 코드의 HMAC 키를 서명 키에서 만드는 HKDF 의 salt 와 info
CODE_KEY_SALT = b"daegam-channels-v1"
CODE_KEY_INFO = b"link-code"
CODE_KEY_BYTES = 32
# 틀린 코드의 잠금 (6.3절). 열쇠는 Teams 사용자다
CODE_FAILURES = 5
CODE_WINDOW = timedelta(hours=1)
CODE_LOCK = timedelta(hours=1)
CODE_THROTTLE_PREFIX = "teams-code:"
# Teams 턴이 이만큼 없으면 연결이 끊긴다 (6.3절)
IDLE_CUT = timedelta(days=90)
# 정리가 사람(계정, 사람 기록)을 다시 보는 간격. 끝과 쓰지 않음은 틱마다 본다
PERSON_CHECK_SECONDS = 3600.0
# 감사 행. 받는 경로의 거절과 요청 없이 끊은 일의 주체가 SYSTEM_SUB 다
AUDIT_RESOURCE = "channels"
SYSTEM_SUB = "system:teams"
VIA = "teams"
# 연결이 끊긴 까닭 (감사 행의 error_code)
CUT_ACCOUNT = "account"
CUT_BOT_CHANGED = "bot_changed"
CUT_ENDED = "ended"
CUT_IDLE = "idle"
CUT_REPLACED = "replaced"
# 계정 훅 가운데 연결을 끊는 사건. 테넌트 하나의 소속을 잃은 것은 끊지 않는다. 턴마다 소속을 본다
_ACCOUNT_EVENTS: frozenset[str] = frozenset({DELETED, DISABLED, PASSWORD})

Outcome = Literal["invalid", "blocked", "pending", "linked"]


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str | None:
    """사람이 보낸 코드. 대소문자, 사이의 `-`·공백, 안내 글을 따라 친 꺾쇠(`<코드>`)를 가리지
    않는다. 꼴이 아니면 None 이다."""
    code = "".join(raw.split()).replace("-", "").strip("<>").upper()
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


def throttle_key(account: TeamsAccount) -> str:
    return f"{CODE_THROTTLE_PREFIX}{account.entra_tenant_id}:{account.aad_object_id}"


def same_bot(account: TeamsAccount, teams: TeamsSettings) -> bool:
    """연결이 지금 봇(App ID 와 Entra 테넌트)의 것인가."""
    return (account.app_id, account.entra_tenant_id) == (teams.app_id, teams.entra_tenant_id)


@dataclass
class Redeemed:
    """코드를 보낸 결과. linked 면 새 연결이다."""

    outcome: Outcome
    link: ChannelLink | None = None


@dataclass(frozen=True)
class Person:
    """코드를 만든 사람의 지금. ok 가 거짓이면 그 코드로 연결하지 않는다."""

    ok: bool
    email: str | None = None


def _still_valid(user: User | None, record: IdpPerson | None, mode: str, tv: int) -> bool:
    """연결이나 코드의 사람이 아직 그대로인가. local 은 계정의 사유(`account_reason`)가 없어야
    하고, oidc 는 사람 기록이 있고 비워지지 않아야 한다. header 모드는 연결이 없다."""
    if mode == "local":
        return account_reason(user, tv) is None
    if mode == "oidc":
        return record is not None and record.cleared_at is None
    return False


class ChannelLinks:
    def __init__(
        self,
        store: MongoChannelStore,
        users: MongoUserStore,
        people: MongoIdpPeopleStore,
        throttle: MongoLoginThrottle,
        voice: TeamsVoice,
        platform_settings: MongoPlatformSettingsStore,
        audit: MongoAuditStore,
        issuer: InternalIssuer,
        settings: Settings,
    ):
        self.store = store
        self.users = users
        self.people = people
        self.throttle = throttle
        self.voice = voice
        self.platform_settings = platform_settings
        self.audit = audit
        self.issuer = issuer
        self.settings = settings
        self._person_checked: datetime | None = None
        self._code_key: tuple[str, bytes] | None = None

    # -- 코드 ----------------------------------------------------------------------------------

    def _digest(self, code: str) -> str:
        """코드의 HMAC. 키는 서명 키(kid)마다 한 번 만든다. 서명 키가 아직 없으면 503 이다."""
        if not self.issuer.loaded:
            raise ChannelError(503, "signer_unavailable", "the platform signing key is not loaded")
        signer = self.issuer.signer
        if self._code_key is None or self._code_key[0] != signer.kid:
            key = signer.derive(CODE_KEY_SALT, CODE_KEY_INFO, CODE_KEY_BYTES)
            self._code_key = (signer.kid, key)
        return hmac.new(self._code_key[1], code.encode("ascii"), hashlib.sha256).hexdigest()

    async def make_code(self, sub: str, username: str, tv: int) -> tuple[str, datetime]:
        """새 코드와 그 기한. 앞 코드와 그 확인 대기는 사라진다."""
        code = new_code()
        now = utcnow()
        await self.store.put_code(
            ChannelLinkCode(
                id=sub,
                code_hmac=self._digest(code),
                username=username,
                tv=tv,
                created_at=now,
                expires_at=now + CODE_TTL,
            )
        )
        return code, now + CODE_TTL

    async def redeem(
        self, raw_code: str, account: TeamsAccount, addresses: Iterable[str]
    ) -> Redeemed:
        """Teams 에서 온 코드. account 는 멤버 읽기로 확인한 Teams 계정이고 addresses 는 멤버의
        `userPrincipalName` 과 `email` 이다. 연결하면 새 대화에 `bot.linked` 를 올린다."""
        key = throttle_key(account)
        if await self.throttle.locked_until(key) is not None:
            return Redeemed("blocked")
        now = utcnow()
        code = normalize_code(raw_code)
        taken = await self.store.take_code(self._digest(code), now) if code else None
        if taken is None:
            locked = await self.throttle.record_failure(key)
            return Redeemed("blocked" if locked is not None else "invalid")
        await self.throttle.clear(key)
        person = await self._person(taken.id, taken.tv)
        if not person.ok:
            return Redeemed("invalid")
        if _same_address(person.email, addresses):
            link = await self._link(taken, account, now)
            await self._audit("teams_linked", taken.id, resource_id=link.id)
            return Redeemed("linked", link)
        taken.pending = account
        await self.store.hold_code(taken)
        await self._audit("teams_link_pending", taken.id)
        return Redeemed("pending")

    async def confirm(self, sub: str, accept: bool) -> Redeemed | None:
        """계정 메뉴의 확인. 확인을 기다리는 코드가 없으면 None 이다. 받으면 연결하고, 거절하거나
        그사이 사람이 접근을 잃었으면 대기만 지운다."""
        now = utcnow()
        taken = await self.store.take_pending(sub, now)
        if taken is None or taken.pending is None:
            return None
        if not accept or not (await self._person(taken.id, taken.tv)).ok:
            return Redeemed("invalid")
        return Redeemed("linked", await self._link(taken, taken.pending, now))

    async def _person(self, sub: str, tv: int) -> Person:
        """코드를 만든 사람이 아직 그대로인가."""
        mode = self.settings.auth_mode
        user = await self.users.get(sub) if mode == "local" else None
        record = await self.people.get(sub) if mode == "oidc" else None
        named = user or record
        return Person(_still_valid(user, record, mode, tv), named.email if named else None)

    async def _link(
        self, code: ChannelLinkCode, account: TeamsAccount, now: datetime
    ) -> ChannelLink:
        """연결을 넣고 앞 연결을 바꾼다. 앞 연결의 사람과 대화에 알린다(모듈 머리글)."""
        before = await self.store.link_for_person(code.id)
        moved = before is not None and (
            before.teams.entra_tenant_id,
            before.teams.aad_object_id,
        ) != (account.entra_tenant_id, account.aad_object_id)
        link = ChannelLink(
            id=new_id(),
            sub=code.id,
            username=code.username,
            tv=code.tv,
            teams=account,
            language=before.language if before is not None else "ko",
            linked_at=now,
            ends_at=now + timedelta(days=self.settings.pat_max_days),
            last_turn_at=now,
            replaced_name=before.teams.teams_name if moved and before is not None else None,
            replaced_at=now if moved else None,
        )
        removed = await self.store.replace_link(link)
        await self.store.delete_code(code.id)
        await self.store.delete_notice(code.id)
        for old in removed:
            if old.sub != link.sub:
                await self._taken(old, link, now)
            elif moved:
                await self.voice.say_to(old.teams, "bot.replaced", old.language)
        await self.voice.say_to(account, "bot.linked", link.language, username=link.username)
        return link

    async def _taken(self, old: ChannelLink, link: ChannelLink, now: datetime) -> None:
        """다른 사람이 가져간 연결. 그 사람의 표시, 감사 행, 앞 대화의 한 줄이다."""
        await self.store.put_notice(old.sub, old.teams.teams_name, now)
        await self._system_audit(CUT_REPLACED, resource_id=old.id)
        if old.teams.conversation_id != link.teams.conversation_id:
            await self.voice.say_to(old.teams, "bot.relinked", old.language)

    # -- 조회와 끊기 ----------------------------------------------------------------------------

    async def match(self, account: TeamsAccount) -> ChannelLink | None:
        """활동의 네 값(테넌트, `aadObjectId`, `from.id`, 대화)과 봇이 모두 같은 연결. 끝이 지난
        연결은 None 이다."""
        link = await self.store.link_for_teams(account.entra_tenant_id, account.aad_object_id)
        if link is None or link.ends_at <= utcnow():
            return None
        found = link.teams
        same = (found.from_id, found.conversation_id, found.app_id, found.entra_tenant_id) == (
            account.from_id,
            account.conversation_id,
            account.app_id,
            account.entra_tenant_id,
        )
        return link if same else None

    async def view(
        self, sub: str
    ) -> tuple[ChannelLink | None, ChannelLinkCode | None, LinkNotice | None]:
        """계정 메뉴가 보는 것. 그 사람의 연결, 열린 코드, 가져간 연결의 표시다."""
        now = utcnow()
        return (
            await self.store.link_for_person(sub),
            await self.store.code_for(sub, now),
            await self.store.notice_for(sub, now),
        )

    async def links(self) -> list[ChannelLink]:
        return await self.store.list_links()

    async def set_language(self, sub: str, language: Language) -> ChannelLink | None:
        return await self.store.set_language(sub, language)

    async def unlink(self, sub: str) -> ChannelLink | None:
        """그 사람의 연결, 열린 코드, 가져간 연결의 표시를 지운다. 지운 연결을 돌려준다."""
        link = await self.store.delete_link_for_person(sub)
        await self.store.delete_code(sub)
        await self.store.delete_notice(sub)
        return link

    async def unlink_from_teams(self, link: ChannelLink) -> None:
        """Teams 의 `연결 끊기`. 감사 행을 쓴다."""
        if await self.store.delete_link(link.id) is not None:
            await self._audit("teams_unlinked", link.sub, resource_id=link.id)

    async def cut(self, link_id: str) -> ChannelLink | None:
        """platform-admin 의 연결 하나 끊기. 화면의 요청이라 감사 미들웨어가 남긴다."""
        return await self.store.delete_link(link_id)

    async def cut_all(self, reason: str | None = None) -> int:
        """모든 연결과 열린 코드를 지운다. reason 이 있으면(App ID 나 테넌트의 변경) 시스템 감사
        행을 쓴다. 화면의 요청은 감사 미들웨어가 남긴다."""
        links = await self.store.list_links()
        count = await self.store.delete_links([link.id for link in links])
        await self.store.delete_all_codes()
        if reason is not None and count:
            await self._system_audit(reason)
        return count

    async def on_lost_access(self, sub: str, reason: Revocation, tenant: str | None) -> None:
        """실행을 멈추는 계정 훅 (6.3절). local 계정의 삭제·막기와 비밀번호 바꾸기면 연결을 끊는다.
        테넌트 하나의 소속을 잃은 것은 끊지 않는다."""
        if tenant is not None or reason not in _ACCOUNT_EVENTS:
            return
        link = await self.unlink(sub)
        if link is not None:
            await self._system_audit(CUT_ACCOUNT, resource_id=link.id)

    # -- 정리 ----------------------------------------------------------------------------------

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """리컨실 틱의 단계. 끝이 지났거나 90일 동안 Teams 턴이 없는 연결을 끊고 그 대화에 한 줄을
        올린다. 한 시간에 한 번 사람과 봇을 다시 보아 접근을 잃은 사람과 다른 봇의 연결을 끊는다.
        결과 키 `cut` 은 끊은 연결의 id 다."""
        cut: dict[str, list[ChannelLink]] = {}
        for link in await self.store.due_links(now, now - IDLE_CUT):
            cut.setdefault(CUT_ENDED if link.ends_at <= now else CUT_IDLE, []).append(link)
        if interval_due(self._person_checked, now, PERSON_CHECK_SECONDS):
            self._person_checked = now
            done = {link.id for links in cut.values() for link in links}
            for reason, links in (await self._lost(done)).items():
                cut.setdefault(reason, []).extend(links)
        ids: list[str] = []
        for reason, links in sorted(cut.items(), key=lambda item: item[0]):
            await self.store.delete_links([link.id for link in links])
            ids += [link.id for link in links]
            logger.info("teams_links_cut reason=%s links=%d", reason, len(links))
            await self._system_audit(reason)
            if reason in (CUT_ENDED, CUT_IDLE):
                for link in links:
                    await self.voice.say_to(link.teams, "bot.linkEnded", link.language)
        return {"cut": ids} if ids else {}

    async def _lost(self, skip: set[str]) -> dict[str, list[ChannelLink]]:
        """사람이나 봇이 바뀐 연결. 까닭 → 연결이다."""
        links = [link for link in await self.store.list_links() if link.id not in skip]
        if not links:
            return {}
        teams = (await self.platform_settings.get()).channels.teams
        mode = self.settings.auth_mode
        subs = {link.sub for link in links}
        users = await self.users.get_many(subs) if mode == "local" else {}
        people = await self.people.get_many(subs) if mode == "oidc" else {}
        lost: dict[str, list[ChannelLink]] = {}
        for link in links:
            if not same_bot(link.teams, teams):
                lost.setdefault(CUT_BOT_CHANGED, []).append(link)
            elif not _still_valid(users.get(link.sub), people.get(link.sub), mode, link.tv):
                lost.setdefault(CUT_ACCOUNT, []).append(link)
        return lost

    async def _audit(self, action: str, sub: str, *, resource_id: str | None = None) -> None:
        """사람이 Teams 에서 한 일의 감사 행. 막혀도 하는 일은 같으므로 경고만 남긴다."""
        event = AuditEvent(
            sub=sub,
            tenant=None,
            action=action,
            resource=AUDIT_RESOURCE,
            resource_id=resource_id,
            method="POST",
            path=TEAMS_MESSAGES_PATH,
            request_id=new_id(),
            status=200,
            result="ok",
            via=VIA,
        )
        try:
            await self.audit.record(event)
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 연결은 그대로다
            logger.warning("channel audit row %s not written: %s", action, e)

    async def _system_audit(self, reason: str, *, resource_id: str | None = None) -> None:
        """요청 없이 끊은 연결의 감사 행 `teams_links_cut`. error_code 가 까닭이다."""
        try:
            await record_system_event(
                self.audit,
                sub=SYSTEM_SUB,
                tenant=None,
                action="teams_links_cut",
                resource=AUDIT_RESOURCE,
                path=TEAMS_MESSAGES_PATH,
                resource_id=resource_id,
                error_code=reason,
            )
        except Exception as e:  # noqa: BLE001 - 감사가 막혀도 연결은 그대로다
            logger.warning("channel audit row teams_links_cut not written: %s", e)


def _same_address(email: str | None, addresses: Iterable[str]) -> bool:
    """사람의 이메일이 멤버의 주소 가운데 하나와 같은가. 대소문자를 가리지 않는다."""
    if not email:
        return False
    want = email.strip().casefold()
    return any(a.strip().casefold() == want for a in addresses if a)


__all__ = [
    "AUDIT_RESOURCE",
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL",
    "CUT_BOT_CHANGED",
    "SYSTEM_SUB",
    "ChannelLinks",
    "Redeemed",
    "new_code",
    "normalize_code",
    "same_bot",
]
