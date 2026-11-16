"""MCP 클라이언트의 인가 요청 (P33 6절).

`GET /api/oauth/authorize` 는 요청을 검증하고, 아무것도 저장하지 않은 채 서명한 요청 핸들을 만들어
동의 화면(`/oauth/consent?request=<핸들>`)으로 보낸다(3절). 확인하지 않은 `client_id` 나
`redirect_uri` 로는 어떤 오류도 보내지 않는다. 그 둘의 오류는 동의 화면이 보이고, 둘을 맞댄 뒤의
오류는 `redirect_uri` 에 `error`·`state`·`iss` 를 붙여 보낸다. 거절한 요청은 GET 이라 감사하지 않고
로그 한 줄로 남긴다.

`resource` 는 노출한 Gateway·MCP Server 하나의 공개 주소여야 하고, 그 리소스의 테넌트가 요청의
테넌트다. 노출 host 는 플랫폼 전체에서 유일하므로(`hosts_in_use`) 일치는 하나뿐이다. 유일성이
넓어지기 전에 두 클러스터에 같은 host 로 노출해 둔 경우만 둘 이상이고, 그때는 테넌트를 하나로 고를
수 없어 거절한다. 동의와 허용도 핸들의 `resource` 를 같은 함수로 다시 푼다. 노출은 `resource` 의
host 로 인덱스를 타 찾는다. 인증 없는 인가 요청이 노출 문서 전체를 읽지 않게 하려는 것이다.

인가 서버가 잠들었으면(P53b 6.5절, `Settings.oauth_dormant`) `client_id` 와 `redirect_uri` 를 맞댄
뒤 `redirect_uri` 에 `temporarily_unavailable` 을 실어 보내고 핸들을 만들지 않는다. 잠든 동안
ToolHive 는 IdP 를 인가 서버로 알린다. 동의와 토큰 엔드포인트도 이 표시(`OAuthAuthorizer.dormant`)를
본다.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from daegam.auth.oauth_keys import Seal, oauth_keys
from daegam.auth.oauth_redirect import redirect_with
from daegam.auth.signer import SCOPE_MCP
from daegam.services.data_plane_auth import KIND_AGENT
from daegam.services.exposure import LiveExposure
from daegam.services.oauth.clients import (
    RESPONSE_TYPE_CODE,
    OAuthClient,
    OAuthClients,
    client_ref,
)
from daegam.services.oauth.errors import (
    INTERACTION_REQUIRED,
    INVALID_REDIRECT_URI,
    INVALID_REQUEST,
    INVALID_SCOPE,
    INVALID_TARGET,
    TEMPORARILY_UNAVAILABLE,
    UNSUPPORTED_RESPONSE_TYPE,
    OAuthError,
)
from daegam.store.oauth_grants import ResourceKind
from daegam.urls import DEFAULT_PORTS, url_parts

logger = logging.getLogger("daegam.oauth.authorize")

# 동의 화면. UI 경로라 nginx 가 index.html 을 낸다
CONSENT_PATH = "/oauth/consent"
# 17절의 코드 상수. 남용 방어의 재료라 설정으로 흔들지 않는다
REQUEST_TTL = timedelta(minutes=10)
MAX_STATE_CHARS = 512
# 요청 핸들의 jti. 128비트 난수의 hex 다 (3절)
HANDLE_JTI_BYTES = 16
# 받는 PKCE 방법. 인가 서버 메타데이터가 같은 값을 광고한다 (2절)
S256 = "S256"
# S256 challenge 는 SHA-256 32바이트의 패딩 없는 base64url 이라 43자다 (RFC 7636)
_CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
SCOPE_OFFLINE = "offline_access"
# 받고 버린다. id_token 을 내지 않는다 (13절)
SCOPE_OPENID = "openid"
_KNOWN_SCOPES = frozenset({SCOPE_MCP, SCOPE_OFFLINE, SCOPE_OPENID})
# 거절 로그에 싣는 client_ref 의 길이. 모르는 client_id 는 요청이 보낸 글자 그대로다
_LOGGED_REF_CHARS = 200

Params = Mapping[str, list[str]]


def normalize_resource(raw: str) -> str | None:
    """RFC 8707 resource 를 맞대는 꼴로 바꾼다. 스킴·호스트는 소문자, 기본 포트는 빼고 끝 `/` 는
    지운다. 쿼리나 조각이나 사용자 정보가 있거나 http(s) 주소가 아니면 None 이다."""
    resolved = url_parts(raw) if not any(c in raw for c in "?#") else None
    parts = urlsplit(raw)
    if resolved is None or "@" in parts.netloc:
        return None
    scheme, host, port, _ = resolved
    netloc = host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


# 핸들 JSON 의 짧은 키. 핸들이 URL 에 실리므로 필드 이름을 줄인다
_HANDLE_KEYS = {
    "jti": "jti",
    "client_id": "cid",
    "redirect_uri": "ru",
    "state": "st",
    "code_challenge": "cc",
    "scope": "sc",
    "resource": "rs",
    "iat": "iat",
    "exp": "exp",
}


@dataclass(frozen=True)
class ConsentRequest:
    """요청 핸들에 실린 인가 요청 (3절). 서버에 두지 않고 핸들 글자 안에만 있다. `iat`·`exp` 는
    epoch 초다."""

    jti: str
    client_id: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    scope: str
    resource: str
    iat: int
    exp: int

    def seal(self, seal: Seal) -> str:
        return seal.seal({key: getattr(self, name) for name, key in _HANDLE_KEYS.items()})

    @classmethod
    def unseal(cls, seal: Seal, handle: str) -> ConsentRequest | None:
        """서명이 맞는 핸들의 요청. 서명이 틀리거나 꼴이 다르면 None 이다."""
        payload = seal.unseal(handle)
        if payload is None or set(payload) != set(_HANDLE_KEYS.values()):
            return None
        return cls(**{name: payload[key] for name, key in _HANDLE_KEYS.items()})


@dataclass(frozen=True)
class OAuthResource:
    """resource 가 가리키는 노출 리소스. 요청의 테넌트가 여기서 나온다."""

    url: str  # 정규화한 공개 주소. 핸들과 코드가 이 글자를 싣는다
    kind: ResourceKind
    id: str
    tenant: str
    name: str
    display_name: str

    @classmethod
    def of(cls, url: str, exposure: LiveExposure) -> OAuthResource:
        doc = exposure.resource
        return cls(url, exposure.kind, doc.id, doc.tenant, doc.name, doc.display_name)


def consent_url(**params: str) -> str:
    """동의 화면의 주소. UI 와 API 가 같은 origin 이라 절대 경로다."""
    return f"{CONSENT_PATH}?{urlencode(params)}"


def _one(params: Params, name: str) -> str | None:
    """인자 하나. 없으면 None 이고 둘 이상이면 invalid_request 다 (RFC 6749 3.1)."""
    values = params.get(name, [])
    if len(values) > 1:
        raise OAuthError(INVALID_REQUEST, f"{name} appears more than once")
    return values[0] if values else None


def _echo_state(params: Params) -> str | None:
    """오류 응답에 되돌려 줄 state. 하나이고 상한 안일 때만 되돌린다."""
    values = params.get("state", [])
    return values[0] if len(values) == 1 and len(values[0]) <= MAX_STATE_CHARS else None


def _pkce_challenge(params: Params) -> str:
    """4단계. S256 과 43자 base64url challenge 만 받는다. plain 은 받지 않는다."""
    challenge = _one(params, "code_challenge") or ""
    if _one(params, "code_challenge_method") != S256 or not _CODE_CHALLENGE.fullmatch(challenge):
        raise OAuthError(INVALID_REQUEST, "PKCE with code_challenge_method=S256 is required")
    return challenge


def scope_string(offline: bool) -> str:
    """연결과 토큰 응답의 scope 글자. mcp 는 늘 들고 offline_access 는 offline 일 때만 든다."""
    return " ".join([SCOPE_MCP, *([SCOPE_OFFLINE] if offline else [])])


def granted_scope(raw: str | None) -> str:
    """5단계. 모르는 scope 는 invalid_scope 이고 openid 는 버린다. offline_access 는 요청에 있을
    때만 든다. 갱신 요청의 scope 도 이것으로 읽는다."""
    asked = set((raw or "").split())
    if asked - _KNOWN_SCOPES:
        raise OAuthError(INVALID_SCOPE, "only mcp and offline_access are supported")
    return scope_string(SCOPE_OFFLINE in asked)


def _check_request_shape(params: Params) -> None:
    """7·8단계. request object 와 prompt=none 을 받지 않고 state 는 512자까지다. redirect_uri 의
    길이는 2단계의 맞대기가 이미 본다."""
    if "request" in params or "request_uri" in params:
        raise OAuthError(INVALID_REQUEST, "request objects are not supported")
    if "none" in (_one(params, "prompt") or "").split():
        raise OAuthError(INTERACTION_REQUIRED, "consent always needs the person")
    state = _one(params, "state")
    if state is not None and len(state) > MAX_STATE_CHARS:
        raise OAuthError(INVALID_REQUEST, f"state is longer than {MAX_STATE_CHARS} characters")


def _log_rejected(client_id: str, code: str) -> None:
    logger.info(
        "oauth authorize rejected: client=%r error=%s",
        client_ref(client_id)[:_LOGGED_REF_CHARS],
        code,
    )


class OAuthAuthorizer:
    """인가 요청을 검증해 요청 핸들을 만들고, 핸들을 풀고, resource 를 리소스로 푼다. exposures 는
    host 하나를 받아 플랫폼 전체에서 그 host 의 살아 있는 노출을 준다
    (`ExposureValidationMixin.platform_exposures`). dormant 는 인가 서버가 잠들었는가다."""

    def __init__(
        self,
        clients: OAuthClients,
        exposures: Callable[[str], Awaitable[list[LiveExposure]]],
        *,
        dormant: bool = False,
        clock: Callable[[], float] = time.time,
    ):
        self.clients = clients
        self.exposures = exposures
        self.dormant = dormant
        self.clock = clock

    @property
    def iss(self) -> str:
        """인가 응답과 오류 응답의 `iss` (RFC 9207). 사람 발급자다."""
        return self.clients.issuer.human_issuer

    def _seal(self) -> Seal:
        return oauth_keys(self.clients.issuer.signer).request

    def open_request(self, handle: str) -> ConsentRequest | None:
        return ConsentRequest.unseal(self._seal(), handle)

    async def authorize(self, params: Params) -> str:
        """302 의 Location. 검증 순서는 6절 그대로다."""
        try:
            client, redirect_uri = await self._client_and_redirect(params)
        except OAuthError as e:
            _log_rejected(params.get("client_id", [""])[0], e.code)
            return consent_url(error=e.code)
        state = _echo_state(params)
        if self.dormant:
            _log_rejected(client.client_id, TEMPORARILY_UNAVAILABLE)
            answer = {"error": TEMPORARILY_UNAVAILABLE, "state": state, "iss": self.iss}
            return redirect_with(redirect_uri, answer)
        try:
            request = await self._request(params, client, redirect_uri, state)
        except OAuthError as e:
            _log_rejected(client.client_id, e.code)
            return redirect_with(redirect_uri, {"error": e.code, "state": state, "iss": self.iss})
        return consent_url(request=request.seal(self._seal()))

    async def _client_and_redirect(self, params: Params) -> tuple[OAuthClient, str]:
        """1·2단계. 여기의 오류는 동의 화면이 보인다."""
        client = await self.clients.resolve(_one(params, "client_id") or "")
        redirect_uri = _one(params, "redirect_uri")
        if redirect_uri is None or not client.accepts(redirect_uri):
            raise OAuthError(
                INVALID_REDIRECT_URI, "redirect_uri does not match a registered loopback address"
            )
        return client, redirect_uri

    async def _request(
        self, params: Params, client: OAuthClient, redirect_uri: str, state: str | None
    ) -> ConsentRequest:
        """3~8단계를 지난 요청."""
        if _one(params, "response_type") != RESPONSE_TYPE_CODE:
            raise OAuthError(UNSUPPORTED_RESPONSE_TYPE, "response_type must be code")
        challenge = _pkce_challenge(params)
        scope = granted_scope(_one(params, "scope"))
        resource = await self.resource(params.get("resource", []))
        _check_request_shape(params)
        now = int(self.clock())
        return ConsentRequest(
            jti=secrets.token_hex(HANDLE_JTI_BYTES),
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            scope=scope,
            resource=resource.url,
            iat=now,
            exp=now + int(REQUEST_TTL.total_seconds()),
        )

    async def resource(self, values: Sequence[str]) -> OAuthResource:
        """6단계. resource 하나를 노출한 Gateway·MCP Server 하나로 푼다. 아니면 invalid_target
        이다."""
        target = normalize_resource(values[0]) if len(values) == 1 else None
        if target is None:
            raise OAuthError(
                INVALID_TARGET, "exactly one resource URL without a query or fragment is required"
            )
        found = await self.exposures(urlsplit(target).hostname or "")
        matches = [e for e in found if normalize_resource(e.external_url) == target]
        if len(matches) > 1:
            _warn_ambiguous(target, matches)
        if len(matches) != 1 or matches[0].kind == KIND_AGENT:
            raise OAuthError(INVALID_TARGET, "the resource is not an exposed Gateway or MCP Server")
        return OAuthResource.of(target, matches[0])


def _warn_ambiguous(target: str, matches: list[LiveExposure]) -> None:
    ids = ", ".join(sorted(e.resource.id for e in matches))
    logger.warning(
        "oauth resource %s matches %d live exposures (%s); rejected as invalid_target",
        target,
        len(matches),
        ids,
    )
