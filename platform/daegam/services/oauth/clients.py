"""MCP 클라이언트의 등록과 확인 (P33 4절).

두 갈래가 있고 어느 쪽도 인가 전에 저장하지 않는다(3절).

- **DCR.** `POST /api/oauth/register` 가 등록 값을 정리해 `client_id` 자체에 서명해 싣는다
  (`auth/oauth_keys.py`). 정규화한 값을 봉인하므로 같은 등록은 같은 `client_id` 를 받고, 연결할
  때마다 다시 등록해도 쌓이는 것이 없다. 비밀은 내지 않는다(`token_endpoint_auth_method: none`).
- **CIMD.** `client_id` 가 허용 목록(`DAEGAM_OAUTH_CIMD_CLIENTS`)의 URL 과 글자 그대로 같으면 그
  문서를 `fetch_url` 로 받는다. 리다이렉트를 따르지 않고 5KB·5초 상한이다. 레플리카마다 1시간
  캐시하고 실패는 60초 기억한다. 조회는 레플리카 안에서 한 번에 하나라 캐시가 빈 순간 요청이 몰려도
  URL 하나를 한 번 받는다.

돌아갈 곳은 두 갈래 모두 loopback 만 남긴다(`auth/oauth_redirect.py`). `grant_types` 는 지원하는
둘과의 교집합이고 `authorization_code` 가 빠지면 받지 않는다. VS Code 의 CIMD 문서처럼 device
flow 를 함께 적은 클라이언트도 그 둘로 쓴다. 이름은 클라이언트가 스스로 밝힌 것이라 정리한 뒤에도
신원이 아니다. 동의 화면이 `name_collides` 로 알려진 이름의 흉내를 경고한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from daegam import urlfetch
from daegam.auth.oauth_keys import DCR_PREFIX, OAuthKeys, oauth_keys
from daegam.auth.oauth_redirect import loopback_only, redirect_matches
from daegam.auth.service_token import InternalIssuer
from daegam.services.oauth.errors import (
    CLIENT_METADATA_UNAVAILABLE,
    INVALID_CLIENT,
    INVALID_CLIENT_METADATA,
    INVALID_REDIRECT_URI,
    OAuthError,
)
from daegam.store.base import hash_token
from daegam.store.oauth_grants import CIMD, DCR, ClientKind
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.oauth.clients")

AUTHORIZATION_CODE = "authorization_code"
REFRESH_TOKEN = "refresh_token"
SUPPORTED_GRANTS = (AUTHORIZATION_CODE, REFRESH_TOKEN)
# 받는 response_type. 등록 응답과 인가 요청의 검증, 인가 서버 메타데이터가 같은 값을 쓴다 (2절)
RESPONSE_TYPE_CODE = "code"
# RFC 7591 2절의 기본값. CIMD 문서가 grant_types 를 적지 않으면 이것이다
CIMD_DEFAULT_GRANTS = (AUTHORIZATION_CODE,)
PUBLIC_CLIENT_AUTH = "none"
DCR_VERSION = 1
DCR_ID_PREFIX = f"{DCR_PREFIX}."
# 17절의 코드 상수. 남용 방어의 재료라 설정으로 흔들지 않는다
MAX_CLIENT_NAME_CHARS = 64
MAX_DCR_REDIRECT_URIS = 4
CIMD_MAX_BYTES = 5 * 1024
CIMD_TIMEOUT_SECONDS = 5.0
CIMD_CACHE_SECONDS = 3600.0
CIMD_FAILURE_SECONDS = 60.0
UNNAMED_CLIENT = "이름 없는 클라이언트"
# 이름에서 지우는 Unicode 범주. 제어 문자(Cc), 폭 없는 공백과 bidi 재정렬 같은 서식 문자(Cf), 짝
# 없는 대리 문자(Cs)다. 대리 문자는 UTF-8 로 적을 수 없어 응답을 깨뜨린다
STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
# name_collides 가 보는 알려진 클라이언트의 이름. 소문자에 공백을 지운 꼴이다 (7절)
KNOWN_CLIENT_NAMES = ("claudecode", "visualstudiocode", "vscode", "codex", "cursor")
# client_ref 의 DCR 꼴에 싣는 client_id sha256 의 앞 글자 수
CLIENT_REF_HASH_CHARS = 16


@dataclass(frozen=True)
class OAuthClient:
    """확인한 클라이언트. `redirect_uris` 는 loopback 만 남긴 것이다."""

    client_id: str
    kind: ClientKind
    name: str
    redirect_uris: tuple[str, ...]
    grant_types: tuple[str, ...]

    @property
    def ref(self) -> str:
        return client_ref(self.client_id)

    @property
    def host(self) -> str | None:
        return published_host(self.kind, self.client_id)

    @property
    def collides(self) -> bool:
        """DCR 이름이 알려진 클라이언트의 이름을 흉내 내는가 (7절). CIMD 는 스스로 밝힌 이름이
        아니라 문서를 공개한 호스트를 말하므로 보지 않는다."""
        return self.kind == DCR and name_collides(self.name)

    def accepts(self, redirect_uri: str) -> bool:
        return redirect_matches(self.redirect_uris, redirect_uri)


def published_host(kind: ClientKind, client_id: str) -> str | None:
    """CIMD 문서를 공개한 호스트. DCR 이면 None 이다. 화면은 신원이 아니라 이 호스트만 말한다
    (16절). 동의 화면과 연결 목록이 쓴다."""
    return urlsplit(client_id).hostname if kind == CIMD else None


def client_ref(client_id: str) -> str:
    """화면과 감사에 쓰는 짧은 이름. CIMD 는 그 URL, DCR 은 `dcr:` 와 client_id sha256 앞 16자다."""
    if client_id.startswith(DCR_ID_PREFIX):
        return f"{DCR}:{hash_token(client_id)[:CLIENT_REF_HASH_CHARS]}"
    return client_id


def clean_client_name(raw: str | None) -> str:
    """NFKC 정규화, Cc·Cf·Cs 제거, 공백 연속을 하나로, 64자. 비면 "이름 없는 클라이언트" 다."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = "".join(c for c in text if unicodedata.category(c) not in STRIPPED_CATEGORIES)
    return " ".join(text.split())[:MAX_CLIENT_NAME_CHARS].strip() or UNNAMED_CLIENT


def name_collides(name: str) -> bool:
    """정리한 이름을 소문자로 바꾸고 공백을 지운 값이 알려진 클라이언트의 이름을 품는가 (7절)."""
    compact = "".join(name.lower().split())
    return any(known in compact for known in KNOWN_CLIENT_NAMES)


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _grants(requested: Any, default: tuple[str, ...]) -> tuple[str, ...] | None:
    """지원하는 grant 와의 교집합. 없으면 default 이고, 목록이 아니거나 authorization_code 가
    빠지면 None 이다."""
    if requested is None:
        return default
    if not _str_list(requested):
        return None
    kept = tuple(g for g in SUPPORTED_GRANTS if g in requested)
    return kept if AUTHORIZATION_CODE in kept else None


def _invalid_client(description: str = "unknown client_id") -> OAuthError:
    return OAuthError(INVALID_CLIENT, description, status=401)


def _dcr_client(client_id: str, payload: dict[str, Any]) -> OAuthClient | None:
    """봉인을 푼 등록 값. 판이 다르거나 꼴이 틀리면 None 이다."""
    name, uris, grants = payload.get("n"), payload.get("r"), payload.get("g")
    if payload.get("v") != DCR_VERSION or not isinstance(name, str):
        return None
    if not (_str_list(uris) and _str_list(grants)):
        return None
    return OAuthClient(client_id, DCR, name, tuple(uris), tuple(grants))


def _cimd_problem(url: str, doc: Any) -> str | None:
    """CIMD 문서가 받을 수 없는 까닭. 받을 수 있으면 None 이다 (4절)."""
    if not isinstance(doc, dict):
        return "the client metadata document is not a JSON object"
    if doc.get("client_id") != url:
        return "the document client_id does not match its URL"
    uris = doc.get("redirect_uris")
    if not (_str_list(uris) and uris):
        return "the document redirect_uris is not a non-empty list of strings"
    if doc.get("token_endpoint_auth_method") not in (None, PUBLIC_CLIENT_AUTH):
        return "the client is not a public client"
    if "client_secret" in doc:
        return "the document carries a client secret"
    return None


def cimd_client(url: str, doc: Any) -> OAuthClient:
    """받은 CIMD 문서를 확인해 클라이언트로 만든다. 틀리면 401 invalid_client 다."""
    problem = _cimd_problem(url, doc)
    if problem is not None:
        raise _invalid_client(problem)
    grants = _grants(doc.get("grant_types"), CIMD_DEFAULT_GRANTS)
    if grants is None:
        raise _invalid_client("the document grant_types does not include authorization_code")
    name = doc.get("client_name")
    return OAuthClient(
        client_id=url,
        kind=CIMD,
        name=clean_client_name(name if isinstance(name, str) else None),
        redirect_uris=tuple(loopback_only(doc["redirect_uris"])),
        grant_types=grants,
    )


async def _fetch_cimd(url: str) -> Any:
    """CIMD 문서를 받아 JSON 으로 푼다. 받지 못하면 503 client_metadata_unavailable 이다."""
    try:
        raw = await urlfetch.fetch_url(
            url, max_redirects=0, max_bytes=CIMD_MAX_BYTES, timeout=CIMD_TIMEOUT_SECONDS
        )
    except (urlfetch.BlockedURL, urlfetch.FetchError) as e:
        logger.warning("cimd: fetching %s failed: %s", url, e.code)
        raise OAuthError(
            CLIENT_METADATA_UNAVAILABLE,
            "the client metadata document could not be fetched",
            status=503,
        ) from e
    try:
        return json.loads(raw)
    except ValueError as e:
        raise _invalid_client("the client metadata document is not JSON") from e


class OAuthClients:
    """DCR 등록과 두 갈래의 `client_id` 풀기. 서명 키는 플랫폼 공용 발급자의 것이다."""

    def __init__(
        self,
        issuer: InternalIssuer,
        cimd_clients: Iterable[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.issuer = issuer
        self.cimd_clients = frozenset(cimd_clients)
        self._cimd_docs: TTLCache[str, OAuthClient] = TTLCache(CIMD_CACHE_SECONDS, clock=clock)
        self._cimd_failures: TTLCache[str, OAuthError] = TTLCache(CIMD_FAILURE_SECONDS, clock=clock)
        # CIMD 조회 하나만 돌게 한다. 기다린 요청은 앞선 조회가 채운 캐시를 읽는다
        self._fetching = asyncio.Lock()

    def _keys(self) -> OAuthKeys:
        return oauth_keys(self.issuer.signer)

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        """DCR 등록 (RFC 7591). 400 invalid_redirect_uri·invalid_client_metadata 다. 등록 값을
        바꿔 돌려주는 것은 RFC 7591 이 허락한다."""
        raw_name, uris = body.get("client_name"), body.get("redirect_uris")
        if not (raw_name is None or isinstance(raw_name, str)):
            raise OAuthError(INVALID_CLIENT_METADATA, "client_name must be a string")
        if not (uris is None or _str_list(uris)):
            raise OAuthError(INVALID_CLIENT_METADATA, "redirect_uris must be a list of strings")
        kept = loopback_only(uris or [])[:MAX_DCR_REDIRECT_URIS]
        if not kept:
            raise OAuthError(
                INVALID_REDIRECT_URI,
                "redirect_uris needs a loopback address (http://127.0.0.1, http://[::1] or "
                "http://localhost) without a fragment",
            )
        grants = _grants(body.get("grant_types"), SUPPORTED_GRANTS)
        if grants is None:
            raise OAuthError(INVALID_CLIENT_METADATA, "grant_types must include authorization_code")
        # 정규화는 목록 정렬이다(grants 는 이미 SUPPORTED_GRANTS 의 정렬된 순서다). 봉인이 키를
        # 정렬하므로 같은 등록은 같은 글자가 된다
        name, redirect_uris = clean_client_name(raw_name), tuple(sorted(kept))
        payload = {"v": DCR_VERSION, "n": name, "r": redirect_uris, "g": grants}
        client_id = self._keys().dcr.seal(payload)
        return _registration(OAuthClient(client_id, DCR, name, redirect_uris, grants))

    async def resolve(self, client_id: str) -> OAuthClient:
        """`client_id` 를 푼다. 모르거나 서명이 틀리면 401 invalid_client, CIMD 문서를 받지
        못하면 503 client_metadata_unavailable 이다."""
        if client_id.startswith(DCR_ID_PREFIX):
            payload = self._keys().dcr.unseal(client_id)
            client = _dcr_client(client_id, payload) if payload is not None else None
            if client is None:
                raise _invalid_client()
            return client
        if client_id in self.cimd_clients:
            return await self._resolve_cimd(client_id)
        raise _invalid_client()

    async def _resolve_cimd(self, url: str) -> OAuthClient:
        cached = self._cached_cimd(url)
        if cached is not None:
            return cached
        async with self._fetching:
            cached = self._cached_cimd(url)
            if cached is not None:
                return cached
            try:
                client = cimd_client(url, await _fetch_cimd(url))
            except OAuthError as e:
                self._cimd_failures.put(url, e)
                raise
            self._cimd_docs.put(url, client)
            return client

    def _cached_cimd(self, url: str) -> OAuthClient | None:
        """캐시한 문서. 60초 안에 실패했으면 그 오류를 다시 낸다."""
        failure = self._cimd_failures.get(url)
        if failure is not None:
            raise failure.with_traceback(None)
        return self._cimd_docs.get(url)


def _registration(client: OAuthClient) -> dict[str, Any]:
    """등록 응답의 본문. `client_id_issued_at` 은 저장하지 않으므로 응답한 시각이다."""
    return {
        "client_id": client.client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": client.name,
        "redirect_uris": list(client.redirect_uris),
        "grant_types": list(client.grant_types),
        "response_types": [RESPONSE_TYPE_CODE],
        "token_endpoint_auth_method": PUBLIC_CLIENT_AUTH,
    }
