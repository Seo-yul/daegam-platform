"""Teams 에서 오는 활동의 토큰과 모양 (P47 6.2절 "The token", "The activity").

- **토큰.** Bot Connector 의 토큰을 공유 검증기
  (`daegam/auth/oidc.py` `OIDCVerifier.verify_connector`)로 본다. 발급자
  `https://api.botframework.com`, audience 는 봇의 App ID, 키 묶음은 고정 주소
  `https://login.botframework.com/v1/.well-known/keys` 다. 검증기는 (App ID, CA 묶음)마다 하나를
  레플리카에 둔다. 키 묶음이 크므로(F0.1, 232개 808 KB) 통째로 다시 받는 간격은 한 시간이고,
  Microsoft 의 24시간 규칙 안이다. 모르는 kid 의 다시 받기는 1분에 한 번이다. 서명 키의
  `endorsements` 에 `msteams` 가 있어야 하고(아니면 403), 토큰의 `serviceurl` 이 활동의
  `serviceUrl` 과 같아야 한다(아니면 401). 끝의 `/` 하나는 가리지 않는다. Teams SDK 가 그렇게
  견준다(F0.1). F0.2 가 실제 토큰으로 확인한다.
- **활동.** 본문은 256 KiB 까지 읽고 토큰을 본 뒤에 푼다. 글은 사람이 친 글에서 마크업을 뗀 것이다.
  `quotedReply` 엔티티나 `<quoted …/>` 자리표시자, 옛 인용 마크업이 있으면 인용으로 본다(F0.1).
  전달한 메시지는 공개 예시가 없어 F0.2 가 잰다. `text/html` 사본은 첨부가 아니고, 그 밖의 첨부는
  첨부다.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx

from daegam.auth.oidc import OIDCVerifier
from daegam.store.channels import TeamsAccount
from daegam.urlfetch import tls_verify

CONNECTOR_ISSUER = "https://api.botframework.com"
CONNECTOR_KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
TEAMS_CHANNEL = "msteams"
# Teams 에서 봇의 id 는 이 앞머리와 App ID 다 (F0.1)
BOT_ID_PREFIX = "28:"
PERSONAL = "personal"
PLAIN = "plain"
BODY_CAP = 256 * 1024
KEYS_MAX_AGE = 3600.0
KEYS_REFETCH_MIN = 60.0
KEYS_TIMEOUT = 10.0
HTML_COPY = "text/html"
# 인용의 표시 (F0.1). 엔티티와 SDK 가 글에 넣는 자리표시자, 옛 Skype 인용 마크업이다
QUOTED_ENTITY = "quotedReply"
_QUOTED_MARKUP = re.compile(r"<quoted\b|schema\.skype\.com/Reply", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
MAX_TEXT = 64 * 1024


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "p", "div", "li"):
            self.parts.append(" ")


def typed_text(raw: str, text_format: str = "") -> str:
    """사람이 친 글. `plain` 이면 그대로, 아니면 마크업을 뗀다. 공백은 하나로 줄인다."""
    raw = raw[:MAX_TEXT]
    if text_format != PLAIN:
        parser = _Text()
        parser.feed(raw)
        parser.close()
        raw = html.unescape("".join(parser.parts))
    return _SPACES.sub(" ", raw).strip()


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Activity:
    """받는 경로가 읽는 활동의 칸."""

    type: str
    id: str
    channel_id: str
    service_url: str
    entra_tenant_id: str
    conversation_id: str
    conversation_type: str
    from_id: str
    aad_object_id: str
    from_name: str
    recipient_id: str
    text: str = ""
    quoted: bool = False
    attachments: bool = False
    name: str = ""
    action: str = ""
    members_added: list[str] = field(default_factory=list)

    def account(self, app_id: str) -> TeamsAccount:
        """이 활동의 Teams 쪽 값. 이름과 주소는 멤버 읽기가 채운다."""
        return TeamsAccount(
            app_id=app_id,
            entra_tenant_id=self.entra_tenant_id,
            aad_object_id=self.aad_object_id,
            from_id=self.from_id,
            conversation_id=self.conversation_id,
            service_url=self.service_url,
            teams_name=self.from_name,
        )


def parse_activity(body: bytes) -> Activity:
    """본문을 활동으로 읽는다. JSON 객체가 아니면 ValueError 다."""
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise ValueError("the activity is not a JSON object")
    sender = _obj(doc.get("from"))
    conversation = _obj(doc.get("conversation"))
    tenant = _obj(_obj(doc.get("channelData")).get("tenant"))
    raw_text = _str(doc.get("text"))
    attachments = [a for a in doc.get("attachments") or [] if isinstance(a, dict)]
    entities = [e for e in doc.get("entities") or [] if isinstance(e, dict)]
    html_copies = [_str(a.get("content")) for a in attachments if a.get("contentType") == HTML_COPY]
    quoted = any(e.get("type") == QUOTED_ENTITY for e in entities) or any(
        _QUOTED_MARKUP.search(s) for s in [raw_text, *html_copies]
    )
    return Activity(
        type=_str(doc.get("type")),
        id=_str(doc.get("id")),
        channel_id=_str(doc.get("channelId")),
        service_url=_str(doc.get("serviceUrl")),
        entra_tenant_id=_str(tenant.get("id")),
        conversation_id=_str(conversation.get("id")),
        conversation_type=_str(conversation.get("conversationType")),
        from_id=_str(sender.get("id")),
        aad_object_id=_str(sender.get("aadObjectId")),
        from_name=_str(sender.get("name")),
        recipient_id=_str(_obj(doc.get("recipient")).get("id")),
        text=typed_text(raw_text, _str(doc.get("textFormat"))),
        quoted=quoted,
        attachments=any(a.get("contentType") != HTML_COPY for a in attachments),
        name=_str(doc.get("name")),
        action=_str(doc.get("action")),
        members_added=[
            _str(m.get("id")) for m in doc.get("membersAdded") or [] if isinstance(m, dict)
        ],
    )


def bot_id(app_id: str) -> str:
    return f"{BOT_ID_PREFIX}{app_id}"


def same_service_url(claimed: str, activity: str) -> bool:
    """토큰의 `serviceurl` 과 활동의 `serviceUrl` 이 같은가. 끝의 `/` 는 가리지 않는다."""
    return bool(claimed) and claimed.rstrip("/") == activity.rstrip("/")


class ConnectorVerifiers:
    """(App ID, CA 묶음)마다의 Connector 토큰 검증기. 값이 바뀌면 앞 검증기를 닫고 새로 만든다."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key: tuple[str, str | None] | None = None
        self._verifier: OIDCVerifier | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # 테스트가 httpx MockTransport 를 꽂는 자리. 운영에서는 없다
        self.transport = transport

    async def get(self, app_id: str, ca_bundle: str | None) -> OIDCVerifier:
        key = (app_id, ca_bundle)
        async with self._lock:
            if self._verifier is not None and self._key == key:
                return self._verifier
            await self._close()
            self._client = httpx.AsyncClient(
                timeout=KEYS_TIMEOUT,
                verify=tls_verify(ca_bundle),
                follow_redirects=False,
                transport=self.transport,
            )
            self._verifier = OIDCVerifier(
                CONNECTOR_ISSUER,
                app_id,
                http_client=self._client,
                jwks_url=CONNECTOR_KEYS_URL,
                jwks_max_age=KEYS_MAX_AGE,
                jwks_refresh_min=KEYS_REFETCH_MIN,
            )
            self._key = key
            return self._verifier

    async def aclose(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client, self._verifier, self._key = None, None, None


__all__ = [
    "BODY_CAP",
    "BOT_ID_PREFIX",
    "CONNECTOR_ISSUER",
    "CONNECTOR_KEYS_URL",
    "PERSONAL",
    "TEAMS_CHANNEL",
    "Activity",
    "ConnectorVerifiers",
    "bot_id",
    "parse_activity",
    "same_service_url",
    "typed_text",
]
