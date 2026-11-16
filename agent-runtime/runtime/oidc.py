"""OIDC 토큰 검증. issuer discovery(캐시) → JWKS(kid 캐시) → PyJWT 검증.

Okta 와 mock OIDC(deploy/dev/mock-oidc) 둘 다 같은 경로다. discovery 문서는 10분 캐시하고,
JWKS 는 kid 별로 들고 있다가 모르는 kid 를 만나면 다시 받는다(키 롤오버). 모르는 kid 로 인한
재조회는 최소 간격(기본 30초, env DAEGAM_JWKS_REFRESH_MIN_SECONDS)을 지켜 임의 kid 를 단 쓰레기
토큰이 요청마다 IdP 조회를 일으키지 못하게 한다.

JWKS 를 통째로 받은 지 최대 나이(기본 5분)가 지나면 다음 검증 앞에 다시 받는다. 발급자가 JWKS
에서 뺀 키(개인 토큰 회수, P32 3절)의 토큰을 파드 수명 내내 받지 않고 최대 나이 안에 거절하려는
것이다. 다시 받지 못하면 옛 키로 계속 검증하고 최소 간격 뒤 다시 시도한다. 다시 받은 JWKS 에서
들고 있던 것과 같은 JWK 는 다시 파싱하지 않는다. jwks_url 을 주면 discovery 없이 그 주소에서
받는다. 플랫폼이 서명한 토큰을 클러스터 안 주소에서 검증하는 노출 에이전트가 그렇다(P32 1절).

`verify_connector` 는 Teams 봇의 받는 경로가 쓰는 Bot Connector 토큰 검증이다 (P47 6.2절). 키 받기와
캐시는 같고 규칙은 따로다. RS256 만, 300초 여유, `iss`·`aud`·`exp`·`nbf`·`serviceurl` 이 모두
있어야 하고, 서명을 검증한 키의 JWK 원문을 같은 스냅샷에서 함께 돌려준다. 그 키의 `endorsements`
는 부르는 쪽이 본다. 런타임은 부르지 않는다.

이 파일은 플랫폼(daegam.auth.oidc)과 agent-runtime(runtime.oidc)이 바이트 동일하게 나눠 쓴다(D27).
httpx 와 PyJWT 말고는 아무것도 끌어오지 않는다. 플랫폼 Settings 에서 조립하는 일은 wiring 이 한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

logger = logging.getLogger("daegam.auth.oidc")

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
DISCOVERY_PATH = "/.well-known/openid-configuration"
DEFAULT_DISCOVERY_TTL = 600.0  # 10분
DEFAULT_JWKS_REFRESH_MIN_SECONDS = 30.0
DEFAULT_JWKS_MAX_AGE = 300.0  # 5분. 회수 한도의 재료라 설정으로 흔들지 않는다 (P32 3절)
JWKS_REFRESH_MIN_ENV = "DAEGAM_JWKS_REFRESH_MIN_SECONDS"
LEEWAY_SECONDS = 30
# Bot Connector 토큰의 규칙 (P47 6.2절). 클레임 이름은 SDK 가 읽는 이름이다(F0.1)
CONNECTOR_ALGORITHMS = ["RS256"]
CONNECTOR_LEEWAY_SECONDS = 300
CONNECTOR_SERVICE_URL_CLAIM = "serviceurl"
CONNECTOR_REQUIRED = ["iss", "aud", "exp", "nbf", CONNECTOR_SERVICE_URL_CLAIM]


def jwks_refresh_min_seconds() -> float:
    """모르는 kid 재조회의 최소 간격. env 가 숫자가 아니면 기본값이다."""
    raw = os.environ.get(JWKS_REFRESH_MIN_ENV)
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("%s=%r is not a number; using default", JWKS_REFRESH_MIN_ENV, raw)
    return DEFAULT_JWKS_REFRESH_MIN_SECONDS


class AuthError(Exception):
    """토큰 검증 실패. status 는 401(토큰 문제) 또는 503(IdP 접근 불가)."""

    def __init__(self, detail: str, *, code: str = "invalid_token", status: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Claims:
    sub: str
    email: str | None
    groups: list[str]
    client_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    # groups 클레임이 토큰에 있었는가. 없는 것과 빈 것은 다르다. 없으면 IdP 가 그룹을 넣지 않은
    # 토큰(Okta 기본 access token 등)이라 저장된 소속을 건드리면 안 된다
    groups_present: bool = True
    # 발급 시각과 만료(epoch 초). 플랫폼은 iat 로 IdP 가 그 사람을 마지막으로 보증한 때를 적는다
    # (P53b 6.3절). 없거나 숫자가 아니면 None 이다
    iat: int | None = None
    exp: int | None = None
    # 사람이 IdP 에서 마지막으로 로그인한 때. ID 토큰이 `max_age` 요청 뒤에 싣는다 (6.4절)
    auth_time: int | None = None


@dataclass(frozen=True)
class ConnectorToken:
    """검증한 Bot Connector 토큰. 클레임과, 서명을 검증한 키의 JWK 원문이다."""

    claims: dict[str, Any]
    jwk: dict[str, Any]

    @property
    def service_url(self) -> str:
        return str(self.claims[CONNECTOR_SERVICE_URL_CLAIM])

    @property
    def endorsements(self) -> list[str]:
        """서명 키가 보증받은 채널 id. 배열이 아니면 없는 것이다."""
        raw = self.jwk.get("endorsements")
        return [str(e) for e in raw] if isinstance(raw, list) else []


class OIDCVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        groups_claim: str = "groups",
        client_id_claim: str = "cid",
        discovery_ttl: float = DEFAULT_DISCOVERY_TTL,
        jwks_refresh_min: float | None = None,
        timeout: float = 5.0,
        insecure_http: bool = False,
        jwks_url: str | None = None,
        jwks_max_age: float = DEFAULT_JWKS_MAX_AGE,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        self.issuer = issuer.rstrip("/")
        _require_https("issuer", self.issuer, insecure_http)
        # 설정으로 받은 JWKS 주소. 원격 문서가 심은 값이 아니므로 discovery 의 origin 검사 대상이
        # 아니지만 평문 규칙은 같다
        self.jwks_url = jwks_url or None
        if self.jwks_url:
            _require_https("jwks_url", self.jwks_url, insecure_http)
        self.insecure_http = insecure_http
        self.audience = audience
        self.groups_claim = groups_claim
        self.client_id_claim = client_id_claim
        self.discovery_ttl = discovery_ttl
        self.jwks_refresh_min = (
            jwks_refresh_min_seconds() if jwks_refresh_min is None else jwks_refresh_min
        )
        self.jwks_max_age = jwks_max_age
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._keys: dict[str, jwt.PyJWK] = {}
        # 파싱한 키의 원래 JWK. 다시 받은 JWKS 에서 같은 JWK 를 알아보는 데 쓴다
        self._jwk_docs: dict[str, dict[str, Any]] = {}
        # JWKS 를 통째로 받은 마지막 시각. 최대 나이는 여기서 센다
        self._keys_at: float | None = None
        # 첫 로드가 아닌 JWKS 다시 받기를 마지막으로 시도한 시각(모르는 kid, 최대 나이)
        self._refetch_at: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- discovery / JWKS ----------------------------------------------------

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        try:
            r = await self._client.get(url)
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("oidc fetch failed: %s: %s", url, e)
            raise AuthError(
                f"identity provider unavailable: {url}", code="idp_unavailable", status=503
            ) from e
        if not isinstance(body, dict):
            raise AuthError(f"unexpected response from {url}", code="idp_unavailable", status=503)
        return body

    async def discovery(self, *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        if (
            not force
            and self._discovery is not None
            and now - self._discovery_at < self.discovery_ttl
        ):
            return self._discovery
        doc = await self._fetch_json(self.issuer + DISCOVERY_PATH)
        if doc.get("issuer", "").rstrip("/") != self.issuer:
            raise AuthError(
                f"discovery issuer {doc.get('issuer')!r} does not match {self.issuer!r}",
                code="idp_unavailable",
                status=503,
            )
        if "jwks_uri" not in doc:
            raise AuthError("discovery has no jwks_uri", code="idp_unavailable", status=503)
        # 서명 키는 발급자와 같은 origin 에서만 받는다. 디스커버리 문서 한 줄로 남의 키를 심는 길을
        # 막는다. Okta·Keycloak·Auth0·Entra 모두 같은 host 에서 낸다. insecure_http 는 평문 스킴만
        # 풀고 이 검사는 풀지 않는다. 발급자가 평문일수록 이 검사가 더 필요하다
        if _origin(str(doc["jwks_uri"])) != _origin(self.issuer):
            raise AuthError(
                f"discovery jwks_uri {doc['jwks_uri']!r} is not on the issuer's origin",
                code="idp_unavailable",
                status=503,
            )
        self._discovery = doc
        self._discovery_at = now
        return doc

    async def _refresh_keys(self) -> None:
        url = self.jwks_url or (await self.discovery())["jwks_uri"]
        keys, docs = self._parse_keys(await self._fetch_json(url))
        if not keys:
            raise AuthError("JWKS has no usable signing key", code="idp_unavailable", status=503)
        self._keys, self._jwk_docs = keys, docs
        self._keys_at = self._clock()

    def _parse_keys(
        self, jwks: dict[str, Any]
    ) -> tuple[dict[str, jwt.PyJWK], dict[str, dict[str, Any]]]:
        """서명 키를 kid 별로 푼다. 들고 있는 것과 글자까지 같은 JWK 는 다시 파싱하지 않고,
        JWKS 에서 빠진 kid 는 버린다. 키 수천 개를 최대 나이마다 요청 경로에서 다시 풀지 않으려는
        것이다."""
        keys: dict[str, jwt.PyJWK] = {}
        docs: dict[str, dict[str, Any]] = {}
        for k in jwks.get("keys", []):
            if not isinstance(k, dict) or k.get("use", "sig") != "sig":
                continue
            kid = k.get("kid") or ""
            known = self._keys.get(kid)
            if known is None or self._jwk_docs.get(kid) != k:
                # 모양이 틀린 JWK 에 PyJWT 는 PyJWKError 말고 InvalidKeyError·KeyError·
                # ValueError(base64)·TypeError 도 던진다. 키 하나가 AuthError 밖의 예외로 새어
                # 요청을 500 으로 만들지 않게 그 키만 건너뛴다
                try:
                    known = jwt.PyJWK.from_dict(k)
                except (jwt.PyJWTError, KeyError, ValueError, TypeError) as e:
                    logger.debug("skipping jwk: %s", e)
                    continue
            keys[kid], docs[kid] = known, k
        return keys, docs

    async def key_for(self, kid: str | None) -> jwt.PyJWK:
        """kid 로 서명 키 선택. 들고 있는 JWKS 가 최대 나이를 넘었으면 먼저 다시 받는다. 캐시에 없는
        kid 면 JWKS 를 다시 받되 재조회 최소 간격을 지킨다."""
        return (await self.key_entry_for(kid))[0]

    async def key_entry_for(self, kid: str | None) -> tuple[jwt.PyJWK, dict[str, Any]]:
        """`key_for` 와 같고 그 키의 JWK 원문을 함께 돌려준다. 둘은 같은 스냅샷에서 읽는다. 다시
        받기가 키와 원문을 함께 바꾸므로 사이에 await 가 없는 한 번의 읽기가 짝을 지킨다."""
        if self._stale():
            await self._refresh_stale()
        entry = self._lookup(kid)
        if entry is not None:
            return entry
        async with self._lock:
            if self._lookup(kid) is None and self._may_refetch():
                await self._refresh_keys()
        entry = self._lookup(kid)
        if entry is not None:
            return entry
        raise AuthError("unknown signing key (kid)")

    def _stale(self) -> bool:
        return self._keys_at is not None and self._clock() - self._keys_at >= self.jwks_max_age

    async def _refresh_stale(self) -> None:
        """최대 나이를 넘긴 JWKS 를 다시 받는다. 받지 못하면 옛 키로 계속 검증한다. 발급자 장애가
        곧 모든 토큰의 거절이 되지 않게 하려는 것이고, 다음 시도는 재조회 최소 간격 뒤다."""
        async with self._lock:
            if not self._stale() or not self._may_refetch():
                return
            try:
                await self._refresh_keys()
            except AuthError as e:
                logger.warning(
                    "jwks refresh failed, keeping %d cached keys: %s", len(self._keys), e
                )

    def _may_refetch(self) -> bool:
        """키가 아직 없으면(첫 로드) 언제나 받는다. 있으면 마지막 재조회에서 최소 간격이 지나야
        한다. 시각은 시도 전에 찍어 IdP 가 죽어 있을 때도 재시도가 간격을 지키게 한다."""
        if not self._keys:
            return True
        now = self._clock()
        if self._refetch_at is not None and now - self._refetch_at < self.jwks_refresh_min:
            return False
        self._refetch_at = now
        return True

    def _lookup(self, kid: str | None) -> tuple[jwt.PyJWK, dict[str, Any]] | None:
        keys, docs = self._keys, self._jwk_docs
        if kid is None:
            # kid 없는 토큰은 키가 하나일 때만 받는다
            if len(keys) != 1:
                return None
            kid = next(iter(keys))
        key = keys.get(kid)
        return None if key is None else (key, docs[kid])

    # -- verify ------------------------------------------------------------

    async def verify(self, token: str) -> Claims:
        header = _header(token)
        # ID 토큰은 "누구인가"의 증명이지 API 접근 권한이 아니다. audience 를 UI client_id 로 잡은
        # 배포에서 ID 토큰이 그대로 통과하는 일을 막는다. Keycloak 은 헤더 typ=ID, 나머지는
        # ID 토큰에만 있는 at_hash 로 가른다
        if header.get("typ") == "ID":
            raise AuthError("ID tokens are not accepted; send the access token")
        payload = await self._decode(token, header, self.audience, ["exp", "sub"])
        if "at_hash" in payload:
            raise AuthError("ID tokens are not accepted; send the access token")
        return self.claims_from(payload)

    async def verify_id_token(self, token: str, client_id: str) -> Claims:
        """UI 가 액세스 토큰 곁에 보낸 ID 토큰을 검증한다. 최근 로그인의 증명에만 쓴다 (P53b
        6.4절). 발급자와 서명 키는 액세스 토큰과 같고 audience 는 UI 의 client id 다. `aud` 가
        여럿이면 `azp` 가 그 client id 여야 한다(OpenID Connect Core 3.1.3.7). UI 를 audience 의
        하나로 실은 다른 클라이언트의 토큰을 받지 않으려는 것이다. 로그인이 얼마나 최근이어야
        하는지(`auth_time`)와 액세스 토큰과 같은 사람인지는 부르는 쪽이 본다."""
        payload = await self._decode(token, _header(token), client_id, ["exp", "sub", "iat"])
        audiences = payload.get("aud")
        if isinstance(audiences, list) and len(audiences) > 1 and payload.get("azp") != client_id:
            raise AuthError("the ID token was issued to another client")
        return self.claims_from(payload)

    async def verify_connector(self, token: str) -> ConnectorToken:
        """Bot Connector 가 봇에 보낸 토큰 (P47 6.2절). issuer 는 `https://api.botframework.com`,
        audience 는 봇의 App ID 로 만든 검증기에서 부른다. RS256 만 받고 여유는 300초다.
        `iss`·`aud`·`exp`·`nbf`·`serviceurl` 가운데 하나라도 없으면 401 이다. Entra 가 낸 토큰은
        issuer 가 달라 401 이다. 키 묶음을 받지 못하면 503 이다. 서명 키의 `endorsements` 와
        활동의 `serviceUrl` 은 부르는 쪽이 견준다."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") not in CONNECTOR_ALGORITHMS:
            raise AuthError(f"unsupported alg {header.get('alg')!r}")
        key, doc = await self.key_entry_for(header.get("kid"))
        claims = self._decode_with(
            token,
            key,
            self.audience,
            CONNECTOR_REQUIRED,
            algorithms=CONNECTOR_ALGORITHMS,
            leeway=CONNECTOR_LEEWAY_SECONDS,
        )
        return ConnectorToken(claims=claims, jwk=doc)

    async def _decode(
        self, token: str, header: dict[str, Any], audience: str, required: list[str]
    ) -> dict[str, Any]:
        key = await self.key_for(header.get("kid"))
        return self._decode_with(token, key, audience, required)

    def _decode_with(
        self,
        token: str,
        key: jwt.PyJWK,
        audience: str,
        required: list[str],
        *,
        algorithms: list[str] = ALGORITHMS,
        leeway: int = LEEWAY_SECONDS,
    ) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                key.key,
                algorithms=algorithms,
                audience=audience,
                issuer=self.issuer,
                leeway=leeway,
                options={"require": required},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired", code="token_expired") from e
        except jwt.InvalidAudienceError as e:
            raise AuthError("audience mismatch") from e
        except jwt.InvalidIssuerError as e:
            raise AuthError("issuer mismatch") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e

    def claims_from(self, payload: dict[str, Any]) -> Claims:
        groups_raw = _claim(payload, self.groups_claim)
        present = groups_raw is not None
        if groups_raw is None:
            groups_raw = []
        if isinstance(groups_raw, str):
            groups_raw = [groups_raw]
        # Keycloak 의 그룹 경로는 "/parent/child" 다. 앞의 슬래시만 벗겨 매핑 표의 이름과 맞춘다
        groups = [_strip_slash(str(g)) for g in groups_raw if str(g).strip("/")]
        client_id = _claim(payload, self.client_id_claim)
        email = payload.get("email")
        return Claims(
            sub=str(payload["sub"]),
            email=str(email) if email else None,
            groups=groups,
            client_id=str(client_id) if client_id else None,
            raw=payload,
            groups_present=present,
            iat=_epoch(payload, "iat"),
            exp=_epoch(payload, "exp"),
            auth_time=_epoch(payload, "auth_time"),
        )


def _header(token: str) -> dict[str, Any]:
    """서명을 보기 전에 읽는 헤더. 꼴이 틀렸거나 받지 않는 알고리즘이면 AuthError 다."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise AuthError("malformed token") from e
    if header.get("alg") not in ALGORITHMS:
        raise AuthError(f"unsupported alg {header.get('alg')!r}")
    return header


def _epoch(payload: dict[str, Any], name: str) -> int | None:
    """시각 클레임을 epoch 초 정수로 읽는다. 없거나 숫자가 아니면(bool 포함) None 이다."""
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _claim(payload: dict[str, Any], name: str) -> Any:
    """클레임 이름이 그대로 있으면 그것, 없으면 점 경로로 내려간다.

    Keycloak 의 realm 역할은 `realm_access.roles` 처럼 중첩돼 있다."""
    if name in payload:
        return payload[name]
    cur: Any = payload
    for seg in name.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _strip_slash(group: str) -> str:
    return group[1:] if group.startswith("/") else group


def _require_https(name: str, url: str, insecure_http: bool) -> None:
    """평문 http 주소는 디스커버리·JWKS 를 중간에서 바꿔 끼울 수 있다. 클러스터 안 주소(mock,
    플랫폼 자신)에만 insecure_http 로 연다."""
    if not url.startswith("https://") and not insecure_http:
        raise ValueError(f"{name} must use https (insecure_http is for an in-cluster address only)")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()
