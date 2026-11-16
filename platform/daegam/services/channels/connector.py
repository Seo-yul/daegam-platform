"""Teams 봇이 Microsoft 로 나가는 호출 (P47 6.2절 "Sending", 7절).

봇 토큰은 Microsoft 의 두 곳에만 간다. 토큰을 받는 Entra(`login.microsoftonline.com`)와 활동을
올리는 Bot Connector(`smba.trafficmanager.net`)다. 두 호스트는 코드에 고정이고 설정으로 바꾸지
않는다.

- **올릴 주소.** 검증한 활동이 실은 `serviceUrl` 만 쓴다. https, 443 포트, 사용자 정보 없음,
  질의와 조각 없음, 호스트가 정확히 `smba.trafficmanager.net` 이어야 한다. 경로는 영문, 숫자, `.`,
  `_`, `-` 의 마디만 받는다(F0.1 의 `amer-client-ss.msg`). GCC, GCC High, DoD 의 Connector 호스트는
  따로 거절해 설정 카드가 그 까닭을 말할 수 있게 한다(`REFUSED_CLOUD`). 검사를 지난 뒤에만
  `Authorization` 을 싣는다.
- **다시 보내기 없음.** 클라이언트는 이 채널 것이고 리다이렉트를 따르지 않는다. 3xx 는 실패다.
  `daegam/urlfetch.py` 의 fetch 는 같은 호스트의 리다이렉트를 따르므로 쓰지 않는다. 사내 방화벽의
  CA 는 Platform Settings 의 `network.ca_bundle` 을 웹 검색과 같은 꼴로 더하고(`tls_verify`), HTTP
  클라이언트는 CA 묶음마다 하나를 레플리카에 둔다.
- **봇 토큰.** Client Secret 은 관리 Secret `daegam-channel-teams` 에 있다. client credentials 로
  받은 토큰을 레플리카마다 (테넌트, App ID) 열쇠로 만료 5분 전까지 둔다. Secret 을 읽지 못하면 503
  `teams_secret_unavailable` 이다.
- **멤버 읽기.** `GET <serviceUrl>v3/conversations/<id>/members/<from.id>` 가 그 사람이 그 대화의
  멤버인지 말한다. 404 와 403 은 멤버가 아닌 것으로 본다 [F0.2 가 확인한다]. Entra 객체 id 는
  `aadObjectId` 나 `objectId` 로 온다(F0.1).

토큰, Secret, 메시지 본문은 로그에 남기지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from daegam.secrets import SecretStore, read_secret_key
from daegam.services.channels.errors import ChannelError
from daegam.urlfetch import tls_verify

logger = logging.getLogger("daegam.channels")

CONNECTOR_HOST = "smba.trafficmanager.net"
# 이 판이 받지 않는 Microsoft 클라우드의 Connector 호스트 (P47 3절)
REFUSED_CLOUD = frozenset(
    {
        "smba.infra.gcc.teams.microsoft.com",
        "smba.infra.gov.teams.microsoft.us",
        "smba.infra.dod.teams.microsoft.us",
    }
)
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TOKEN_SCOPE = "https://api.botframework.com/.default"
# 봇의 Client Secret 을 두는 관리 Secret 과 그 키 (6.1절, D56)
SECRET_NAME = "daegam-channel-teams"
SECRET_KEY = "client_secret"
# 받은 토큰을 만료보다 이만큼 먼저 버린다
TOKEN_MARGIN_SECONDS = 300.0
TIMEOUT_SECONDS = 10.0
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# 올릴 주소를 거절한 까닭
REFUSED_HOST = "host"
REFUSED_CLOUD_HOST = "cloud"


class ServiceUrlRefused(ValueError):
    """올릴 주소가 규칙 밖이다. reason 은 `cloud`(지원하지 않는 Microsoft 클라우드)나 `host`다."""

    def __init__(self, reason: str):
        super().__init__(f"service url refused: {reason}")
        self.reason = reason


class ConnectorFailed(RuntimeError):
    """Connector 나 Entra 가 실패로 답했거나 닿지 않았다. status 는 받은 상태이고 닿지 않았으면
    None 이다."""

    def __init__(self, what: str, status: int | None):
        super().__init__(f"{what} failed with {status}")
        self.status = status


def connector_base(service_url: str) -> str:
    """규칙을 지난 `serviceUrl` 을 `/` 로 끝나게 돌려준다. 아니면 ServiceUrlRefused 다."""
    try:
        parts = urlsplit(service_url)
        port = parts.port
    except ValueError as e:
        raise ServiceUrlRefused(REFUSED_HOST) from e
    host = (parts.hostname or "").lower()
    if host in REFUSED_CLOUD:
        raise ServiceUrlRefused(REFUSED_CLOUD_HOST)
    segments = [s for s in parts.path.split("/") if s]
    if (
        parts.scheme != "https"
        or host != CONNECTOR_HOST
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(s in (".", "..") or not _PATH_SEGMENT.match(s) for s in segments)
    ):
        raise ServiceUrlRefused(REFUSED_HOST)
    path = "/".join(segments)
    return f"https://{CONNECTOR_HOST}/{path}/" if path else f"https://{CONNECTOR_HOST}/"


@dataclass(frozen=True)
class Member:
    """대화 멤버 읽기의 답 (6.3절)."""

    id: str
    aad_object_id: str | None
    user_principal_name: str | None
    email: str | None
    name: str | None
    tenant_id: str | None

    @classmethod
    def of(cls, body: dict[str, Any]) -> Member:
        def text(*keys: str) -> str | None:
            for key in keys:
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        given, surname = text("givenName"), text("surname")
        joined = " ".join(p for p in (given, surname) if p) or None
        return cls(
            id=text("id") or "",
            aad_object_id=text("aadObjectId", "objectId"),
            user_principal_name=text("userPrincipalName"),
            email=text("email"),
            name=text("name") or joined,
            tenant_id=text("tenantId"),
        )


@dataclass
class _Token:
    value: str
    until: float


class TeamsConnector:
    """봇 토큰을 받고 Connector 를 부르는 클라이언트. 레플리카마다 하나다."""

    def __init__(
        self,
        secret_store: Callable[[], SecretStore | None],
        *,
        namespace: str,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.secret_store = secret_store
        self.namespace = namespace
        self._clock = clock
        self._tokens: dict[tuple[str, str], _Token] = {}
        self._lock = asyncio.Lock()
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        # 테스트가 httpx MockTransport 를 꽂는 자리. 운영에서는 없다
        self.transport = transport

    def forget_tokens(self) -> None:
        """받아 둔 봇 토큰을 버린다. Client Secret 을 바꾼 뒤에 부른다."""
        self._tokens.clear()

    def _client(self, ca_bundle: str | None) -> httpx.AsyncClient:
        """CA 묶음의 HTTP 클라이언트. 묶음이 바뀌면 새 것을 만들고 앞 것은 `aclose` 가 닫는다."""
        client = self._clients.get(ca_bundle)
        if client is None:
            client = httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                transport=self.transport,
                verify=tls_verify(ca_bundle),
                follow_redirects=False,
            )
            self._clients[ca_bundle] = client
        return client

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    async def _read_secret(self) -> str:
        store = self.secret_store()
        if store is None:
            raise ChannelError(
                503, "teams_secret_unavailable", "the home cluster's Secret store is not available"
            )
        try:
            secret = await read_secret_key(store, SECRET_NAME, SECRET_KEY, namespace=self.namespace)
        except Exception as e:  # noqa: BLE001 - Secret 저장소의 어떤 실패든 같은 503 이다
            logger.warning("teams client secret could not be read: %s", type(e).__name__)
            raise ChannelError(
                503, "teams_secret_unavailable", "the Teams client secret cannot be read"
            ) from e
        if not secret:
            raise ChannelError(
                503, "teams_secret_unavailable", "the Teams client secret is not stored"
            )
        return secret

    async def bot_token(self, tenant: str, app_id: str, ca_bundle: str | None) -> str:
        """봇 토큰. 받아 둔 것이 만료 5분 전보다 이르면 그것이다."""
        key = (tenant, app_id)
        cached = self._tokens.get(key)
        if cached is not None and cached.until > self._clock():
            return cached.value
        async with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached.until > self._clock():
                return cached.value
            secret = await self._read_secret()
            form = {
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": secret,
                "scope": TOKEN_SCOPE,
            }
            url = TOKEN_URL.format(tenant=quote(tenant, safe=""))
            try:
                r = await self._client(ca_bundle).post(url, data=form)
            except httpx.HTTPError as e:
                raise ConnectorFailed("bot token", None) from e
            if r.status_code != 200:
                raise ConnectorFailed("bot token", r.status_code)
            try:
                body = r.json()
                value = str(body["access_token"])
                life = float(body.get("expires_in") or 0)
            except (ValueError, KeyError, TypeError) as e:
                raise ConnectorFailed("bot token", r.status_code) from e
            token = _Token(value, self._clock() + max(0.0, life - TOKEN_MARGIN_SECONDS))
            if token.until > self._clock():
                self._tokens[key] = token
            return value

    async def _call(
        self,
        method: str,
        url: str,
        token: str,
        ca_bundle: str | None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return await self._client(ca_bundle).request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as e:
            raise ConnectorFailed(method, None) from e

    async def post(
        self,
        service_url: str,
        conversation_id: str,
        activity: dict[str, Any],
        *,
        tenant: str,
        app_id: str,
        ca_bundle: str | None,
    ) -> None:
        """대화에 활동 하나를 올린다. 주소가 규칙 밖이면 ServiceUrlRefused, 실패면
        ConnectorFailed 다."""
        base = connector_base(service_url)
        url = f"{base}v3/conversations/{quote(conversation_id, safe='')}/activities"
        token = await self.bot_token(tenant, app_id, ca_bundle)
        r = await self._call("POST", url, token, ca_bundle, activity)
        if not 200 <= r.status_code < 300:
            raise ConnectorFailed("post", r.status_code)

    async def member(
        self,
        service_url: str,
        conversation_id: str,
        member_id: str,
        *,
        tenant: str,
        app_id: str,
        ca_bundle: str | None,
    ) -> Member | None:
        """대화의 멤버 하나. 멤버가 아니면(404, 403) None 이다."""
        base = connector_base(service_url)
        url = (
            f"{base}v3/conversations/{quote(conversation_id, safe='')}"
            f"/members/{quote(member_id, safe='')}"
        )
        token = await self.bot_token(tenant, app_id, ca_bundle)
        r = await self._call("GET", url, token, ca_bundle)
        if r.status_code in (403, 404):
            return None
        if r.status_code != 200:
            raise ConnectorFailed("member", r.status_code)
        try:
            body = r.json()
        except ValueError as e:
            raise ConnectorFailed("member", r.status_code) from e
        return Member.of(body) if isinstance(body, dict) else None


__all__ = [
    "CONNECTOR_HOST",
    "REFUSED_CLOUD",
    "REFUSED_CLOUD_HOST",
    "REFUSED_HOST",
    "SECRET_KEY",
    "SECRET_NAME",
    "ConnectorFailed",
    "Member",
    "ServiceUrlRefused",
    "TeamsConnector",
    "connector_base",
]
