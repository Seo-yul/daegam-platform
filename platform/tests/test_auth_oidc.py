"""P6 F1: 플랫폼 API OIDC 인증. mock OIDC 를 인프로세스에 붙여(conftest) discovery 로 JWKS 를 받고
kid 로 키를 골라 검증하는 경로를 그대로 돈다. 테넌트 소속은 F2 라 여기서는 platform-admin 그룹으로
통과시킨다."""

from __future__ import annotations

import asyncio
import time
from types import ModuleType
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from fastapi import HTTPException

import daegam.auth.oidc as platform_oidc
from daegam.api.deps import check_recent_sign_in
from daegam.auth.oidc import AuthError, OIDCVerifier
from daegam.config import Settings
from daegam.wiring import oidc_verifier_from_settings
from tests.helpers.auth import bearer, user_token
from tests.helpers.consts import OIDC_AUDIENCE, OIDC_ISSUER, PLATFORM_JWKS
from tests.helpers.http import ROOT_URL, api_client, detail_code, make_app, startup_warnings
from tests.helpers.oidc import Signer
from tests.helpers.source import AGENT_RUNTIME, runtime_module

ADMIN = ["daegam-admins"]


def admin_token(mock, **kw) -> str:
    return user_token(mock, groups=ADMIN, **kw)


def signed(signer: Signer, **claims) -> str:
    now = int(time.time())
    base = {"iss": OIDC_ISSUER, "aud": OIDC_AUDIENCE, "sub": "u1", "iat": now, "exp": now + 60}
    return signer.sign({**base, **claims})


# -- 게이트: 토큰 없음 401 / 만료 401 / aud 불일치 401 / 다른 발급자 401 ---------------------


def test_expired_token_is_401(oidc, signer):
    now = int(time.time())
    tok = signed(signer, iat=now - 600, exp=now - 120)
    r = oidc.get("/clusters", headers=bearer(tok))
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "token_expired"
    assert 'error="token_expired"' in r.headers["WWW-Authenticate"]


def test_audience_mismatch_is_401(oidc, mock_oidc):
    tok = admin_token(mock_oidc[1], aud="daegam-mcp")
    r = oidc.get("/clusters", headers=bearer(tok))
    assert detail_code(r) == (401, "invalid_token")


def test_wrong_issuer_is_401(oidc, signer):
    tok = signed(signer, iss="http://other.test")
    assert oidc.get("/clusters", headers=bearer(tok)).status_code == 401


def test_malformed_and_unsigned_tokens_are_401(oidc):
    """토큰이 없거나 Bearer 가 아닌 스킴도 401 이다. healthz 는 인증 없이 열린다."""
    r = oidc.get("/clusters")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "unauthorized"
    assert r.headers["WWW-Authenticate"].startswith("Bearer")
    assert oidc.get("/clusters", headers={"Authorization": "Basic abc"}).status_code == 401
    assert oidc.get(f"{ROOT_URL}/healthz").status_code == 200
    assert oidc.get("/clusters", headers=bearer("not.a.jwt")).status_code == 401
    now = int(time.time())
    none_tok = pyjwt.encode(
        {"iss": OIDC_ISSUER, "aud": OIDC_AUDIENCE, "sub": "u1", "exp": now + 60},
        key=None,
        algorithm="none",
    )
    assert oidc.get("/clusters", headers=bearer(none_tok)).status_code == 401


def test_idp_unavailable_is_503(oidc_settings, mongo, fake_clusters, fake_secrets, signer):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    app = make_app(oidc_settings, fake_clusters, fake_secrets, oidc_http_client=http)
    with api_client(app) as c:
        r = c.get("/clusters", headers=bearer(signed(signer)))
        assert detail_code(r) == (503, "idp_unavailable")
        assert "WWW-Authenticate" not in r.headers


# -- OIDCVerifier 단위 ------------------------------------------------------------


def test_verifier_claims_and_caching(mock_oidc, token):
    calls: list[str] = []

    async def spy(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return await httpx.ASGITransport(app=mock_oidc[0]).handle_async_request(request)

    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(spy)),
            insecure_http=True,
        )
        user = token("u1", "daegam-a-dev", email="u1@example.com")
        c = await v.verify(user)
        assert (c.sub, c.email, c.groups, c.client_id) == (
            "u1",
            "u1@example.com",
            ["daegam-a-dev"],
            None,
        )
        # 서비스 앱 토큰: groups 없고 cid 있음
        svc = token("svc-1", extra={"cid": "svc-1", "scp": ["daegam.api"]})
        c2 = await v.verify(svc)
        assert c2.client_id == "svc-1" and c2.groups == [] and c2.raw["scp"] == ["daegam.api"]
        # discovery·JWKS 는 첫 검증에서만 한 번
        assert calls == ["/.well-known/openid-configuration", "/jwks"]
        # 모르는 kid: JWKS 한 번 재조회 후 실패
        with pytest.raises(AuthError) as ei:
            await v.verify(signed(Signer(None)))
        assert ei.value.status == 401 and calls.count("/jwks") == 2
        await v.aclose()

    asyncio.run(run())


def test_verifier_custom_claim_names(mock_oidc, token):
    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_oidc[0])),
            groups_claim="roles",
            client_id_claim="azp",
            insecure_http=True,
        )
        tok = token("u2", "ignored", extra={"roles": "single", "azp": "app-1"})
        c = await v.verify(tok)
        assert c.groups == ["single"] and c.client_id == "app-1"

    asyncio.run(run())


def test_keycloak_shaped_claims(mock_oidc, token):
    """Keycloak 은 realm 역할을 `realm_access.roles` 에 중첩해 내고, 그룹 매퍼는 `/parent/child`
    경로를 낸다. 점 경로로 내려가고 앞의 슬래시를 벗겨 매핑 표의 이름과 맞춘다."""

    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_oidc[0])),
            groups_claim="realm_access.roles",
            insecure_http=True,
        )
        tok = token(
            "kc",
            "ignored",
            extra={"realm_access": {"roles": ["/daegam-a-dev", "offline_access", "/"]}},
        )
        c = await v.verify(tok)
        assert c.groups == ["daegam-a-dev", "offline_access"] and c.groups_present
        # 클레임 이름에 점이 있어도 그 이름이 그대로 있으면 그것을 먼저 본다
        tok2 = token("kc2", extra={"realm_access.roles": ["flat"]})
        assert (await v.verify(tok2)).groups == ["flat"]
        await v.aclose()

    asyncio.run(run())


def test_groups_claim_absent_is_not_empty(mock_oidc, signer, token):
    """groups 클레임이 없는 토큰(Okta 기본 access token)은 빈 그룹이 아니라 "모른다"다."""

    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_oidc[0])),
            insecure_http=True,
        )
        now = int(time.time())
        absent = signer.sign(
            {"iss": OIDC_ISSUER, "aud": OIDC_AUDIENCE, "sub": "u3", "iat": now, "exp": now + 60}
        )
        c = await v.verify(absent)
        assert c.groups == [] and c.groups_present is False
        empty = token("u4")
        assert (await v.verify(empty)).groups_present is True
        await v.aclose()

    asyncio.run(run())


def test_id_tokens_are_rejected(mock_oidc, signer, token):
    """ID 토큰은 누구인가의 증명이지 API 접근 권한이 아니다. audience 를 UI client_id 로 잡은
    배포에서 ID 토큰이 통과하지 않도록 헤더 typ=ID 와 at_hash 로 가른다."""

    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_oidc[0])),
            insecure_http=True,
        )
        with_hash = token("u5", extra={"at_hash": "abc"})
        with pytest.raises(AuthError, match="ID tokens"):
            await v.verify(with_hash)
        now = int(time.time())
        typ_id = pyjwt.encode(
            {"iss": OIDC_ISSUER, "aud": OIDC_AUDIENCE, "sub": "u6", "iat": now, "exp": now + 60},
            signer.private_key,
            algorithm="RS256",
            headers={"kid": signer.kid, "typ": "ID"},
        )
        with pytest.raises(AuthError, match="ID tokens"):
            await v.verify(typ_id)
        await v.aclose()

    asyncio.run(run())


UI_CLIENT = "daegam-ui"


def _mock_verifier(mock_oidc) -> OIDCVerifier:
    return OIDCVerifier(
        OIDC_ISSUER,
        OIDC_AUDIENCE,
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_oidc[0])),
        insecure_http=True,
    )


def test_claims_carry_the_idp_times(mock_oidc, token):
    """플랫폼은 iat 로 IdP 가 사람을 마지막으로 보증한 때를 적는다 (P53b 6.3절). 숫자가 아닌 값은
    없는 것이다."""

    async def run():
        v = _mock_verifier(mock_oidc)
        c = await v.verify(token("u7", extra={"auth_time": 1_700_000_000}))
        assert c.exp is not None and c.iat is not None and c.exp - c.iat == 600
        assert c.auth_time == 1_700_000_000
        odd = await v.verify(token("u8", extra={"auth_time": "yesterday"}))
        assert odd.auth_time is None
        await v.aclose()

    asyncio.run(run())


def test_an_id_token_is_verified_only_for_the_ui_client(mock_oidc, token):
    """최근 로그인의 증명은 UI 의 client id 를 audience 로 한 ID 토큰이다. 액세스 토큰의 audience
    로는 지나지 않고, 액세스 토큰 검증은 여전히 ID 토큰을 거절한다."""

    async def run():
        v = _mock_verifier(mock_oidc)
        id_token = token("u9", aud=UI_CLIENT, extra={"auth_time": 1_700_000_000, "at_hash": "x"})
        c = await v.verify_id_token(id_token, UI_CLIENT)
        assert (c.sub, c.auth_time) == ("u9", 1_700_000_000)
        with pytest.raises(AuthError, match="audience"):
            await v.verify_id_token(token("u9"), UI_CLIENT)
        with pytest.raises(AuthError, match="ID tokens|audience"):
            await v.verify(id_token)
        # aud 가 여럿이면 azp 가 UI 의 client id 여야 한다 (OpenID Connect Core 3.1.3.7)
        both = [UI_CLIENT, "other-client"]
        shared = token("u9", extra={"aud": both, "azp": "other-client", "auth_time": 1})
        with pytest.raises(AuthError, match="another client"):
            await v.verify_id_token(shared, UI_CLIENT)
        ours = token("u9", extra={"aud": both, "azp": UI_CLIENT, "auth_time": 1})
        assert (await v.verify_id_token(ours, UI_CLIENT)).sub == "u9"
        await v.aclose()

    asyncio.run(run())


def test_a_recent_sign_in_needs_a_fresh_auth_time_of_the_same_person(mock_oidc, token):
    """ID 토큰의 auth_time 이 10분(시계 여유 30초) 안이고 요청한 사람의 것이어야 한다. 없거나
    오래됐거나 남의 것이면 403 reauth_required 다 (P53b 6.4절)."""
    now = time.time()

    def id_token(sub: str = "u9", auth_time: float | None = now - 60) -> str:
        extra = {} if auth_time is None else {"auth_time": int(auth_time)}
        return token(sub, aud=UI_CLIENT, extra=extra)

    async def run():
        v = _mock_verifier(mock_oidc)

        async def code(presented: str | None, client: str | None = UI_CLIENT) -> Any:
            try:
                return await check_recent_sign_in(v, client, presented, "u9", now=now)
            except HTTPException as e:
                return e.status_code, e.detail["code"]

        assert await code(id_token()) == int(now - 60)
        assert await code(id_token(auth_time=now - 620)) == int(now - 620)
        refused = (403, "reauth_required")
        assert await code(id_token(auth_time=now - 640)) == refused
        assert await code(id_token(auth_time=None)) == refused
        assert await code(id_token(sub="someone-else")) == refused
        assert await code(token("u9", extra={"auth_time": int(now)})) == refused
        assert await code(None) == refused
        assert await code(id_token(), client=None) == refused
        await v.aclose()

    asyncio.run(run())


def test_jwks_must_live_on_the_issuer_origin(mock_oidc):
    """디스커버리 문서 한 줄로 남의 서명 키를 심는 길을 막는다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200, json={"issuer": OIDC_ISSUER, "jwks_uri": "http://evil.test/jwks"}
            )
        return await httpx.ASGITransport(app=mock_oidc[0]).handle_async_request(request)

    async def run():
        v = OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            insecure_http=True,
        )
        # insecure_http 는 평문 스킴만 풀고 origin 검사는 풀지 않는다. 발급자가 평문일수록 더
        # 필요하다
        with pytest.raises(AuthError, match="issuer's origin") as ei:
            await v.discovery()
        assert ei.value.status == 503
        await v.aclose()

    asyncio.run(run())


def test_settings_validation():
    with pytest.raises(ValueError):
        Settings(auth_mode="basic")
    with pytest.raises(RuntimeError):
        oidc_verifier_from_settings(Settings(auth_mode="oidc"))
    # 평문 http 발급자는 insecure_http 없이는 만들어지지 않는다
    with pytest.raises(ValueError, match="https"):
        oidc_verifier_from_settings(
            Settings(auth_mode="oidc", oidc_issuer=OIDC_ISSUER + "/", oidc_audience=OIDC_AUDIENCE)
        )
    v = oidc_verifier_from_settings(
        Settings(
            auth_mode="oidc",
            oidc_issuer=OIDC_ISSUER + "/",
            oidc_audience=OIDC_AUDIENCE,
            oidc_insecure_http=True,
        )
    )
    assert v.issuer == OIDC_ISSUER
    assert (
        oidc_verifier_from_settings(
            Settings(
                auth_mode="oidc", oidc_issuer="https://idp.example/realms/x", oidc_audience="a"
            )
        ).insecure_http
        is False
    )


# -- JWKS 주소와 최대 나이 (P32 1·3절). 런타임 복사본도 같은 테스트를 돈다 -------------------------

# 검증기는 플랫폼 JWKS 의 클러스터 안 주소(PLATFORM_JWKS)를 받는다. 발급자와 origin 이 달라도
# 설정으로 준 주소는 그대로 쓴다


@pytest.fixture(params=["platform", "runtime"])
def oidc_mod(request) -> ModuleType:
    """두 벌(D27). 바이트가 같은지는 test_oidc_copy.py 가 따로 본다."""
    return (
        platform_oidc if request.param == "platform" else runtime_module(AGENT_RUNTIME / "oidc.py")
    )


class Clock:
    """검증기의 가짜 단조 시계. 테스트가 now 를 옮긴다."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class JwksServer:
    """고칠 수 있는 JWKS 하나. status 가 0 이면 연결을 끊고, 받은 요청의 주소를 적는다. extra 는
    서명 키 앞에 그대로 싣는 JWK 다."""

    def __init__(self, *signers: Signer) -> None:
        self.signers = list(signers)
        self.extra: list[dict] = []
        self.status = 200
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.status == 0:
            raise httpx.ConnectError("jwks down", request=request)
        if self.status != 200:
            return httpx.Response(self.status)
        keys = [*self.extra, *(k for s in self.signers for k in _jwks(s))]
        return httpx.Response(200, json={"keys": keys})

    def verifier(self, mod: ModuleType, clock: Clock, jwks_url: str = PLATFORM_JWKS):
        return mod.OIDCVerifier(
            OIDC_ISSUER,
            OIDC_AUDIENCE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            insecure_http=True,
            jwks_url=jwks_url,
            jwks_refresh_min=30,
            clock=clock,
        )


def _jwks(signer: Signer) -> list[dict]:
    return signer.jwks()["keys"]


async def _accepts(v, signer: Signer) -> bool:
    """토큰이 통과하면 참, 모르는 kid 로 401 이면 거짓이다. 그 밖의 실패는 테스트를 깨뜨린다."""
    try:
        await v.verify(signed(signer))
    except Exception as e:  # noqa: BLE001 - 두 벌의 AuthError 는 다른 클래스다
        assert getattr(e, "status", None) == 401 and "kid" in str(e), e
        return False
    return True


def test_jwks_url_skips_discovery(oidc_mod):
    """설정으로 준 주소에서 바로 받는다. 디스커버리를 읽지 않고, 쿼리도 그대로 보낸다. 발급자의
    kid 를 달고 다른 키로 서명한 위조 토큰은 서명이 맞지 않아 401 이다."""
    a = Signer(None)
    server = JwksServer(a)

    async def run():
        v = server.verifier(oidc_mod, Clock(), jwks_url=PLATFORM_JWKS + "?use=service")
        assert (await v.verify(signed(a))).sub == "u1"
        assert server.urls == [PLATFORM_JWKS + "?use=service"]
        forged = Signer(None)
        forged.kid = a.kid
        with pytest.raises(Exception, match="invalid token") as refused:
            await v.verify(signed(forged))
        assert getattr(refused.value, "status", None) == 401
        await v.aclose()

    asyncio.run(run())


def test_plain_jwks_url_needs_insecure_http(oidc_mod):
    """https 발급자에 클러스터 안 평문 JWKS 를 주려면 평문 플래그가 있어야 한다 (P32 1절)."""
    with pytest.raises(ValueError, match="jwks_url must use https"):
        oidc_mod.OIDCVerifier("https://daegam.example.com", OIDC_AUDIENCE, jwks_url=PLATFORM_JWKS)
    v = oidc_mod.OIDCVerifier(
        "https://daegam.example.com", OIDC_AUDIENCE, jwks_url=PLATFORM_JWKS, insecure_http=True
    )
    assert v.jwks_url == PLATFORM_JWKS
    tls = "https://daegam.example.com/.well-known/jwks.json"
    assert oidc_mod.OIDCVerifier("https://daegam.example.com", OIDC_AUDIENCE, jwks_url=tls).jwks_url


def test_removed_key_is_refused_within_six_minutes(oidc_mod):
    """JWKS 에서 뺀 키의 토큰은 통째로 받은 지 5분 뒤의 검증에서 거절된다. 그 직전의 다시 받기가
    실패했으면 최소 간격 30초가 더해진다. 플랫폼의 JWKS 캐시 30초를 더한 6분이 노출 에이전트의
    회수 한도다 (P32 3절)."""
    a, b = Signer(None), Signer(None)
    server, clock = JwksServer(a), Clock()

    async def run():
        v = server.verifier(oidc_mod, clock)
        assert await _accepts(v, a) and len(server.urls) == 1
        start = clock.now
        server.signers = [b]  # a 를 회수했다
        clock.now = start + 299
        assert await _accepts(v, a) and len(server.urls) == 1
        # 최대 나이 직전에 모르는 kid 가 다시 받기를 시도했는데 발급자가 답하지 못했다
        server.status = 503
        with pytest.raises(Exception, match="unavailable"):
            await v.verify(signed(Signer(None)))
        server.status = 200
        clock.now = start + 328
        assert await _accepts(v, a) and len(server.urls) == 2
        clock.now = start + 329
        assert not await _accepts(v, a) and len(server.urls) == 3
        assert await _accepts(v, b) and len(server.urls) == 3
        await v.aclose()

    asyncio.run(run())


def test_refresh_failure_keeps_the_old_keys(oidc_mod):
    """최대 나이의 다시 받기가 연결 실패나 503 이면 옛 키로 계속 검증하고 최소 간격 뒤 다시
    시도한다. 발급자 장애가 곧 모든 토큰의 거절이 되지 않는다."""
    a, b = Signer(None), Signer(None)
    server, clock = JwksServer(a), Clock()

    async def run():
        v = server.verifier(oidc_mod, clock)
        assert await _accepts(v, a)
        start = clock.now
        for status, at, fetches in ((0, 300, 2), (0, 310, 2), (503, 330, 3)):
            server.status = status
            clock.now = start + at
            assert await _accepts(v, a) and len(server.urls) == fetches, (status, at)
        server.status, server.signers = 200, [b]
        clock.now = start + 360
        assert not await _accepts(v, a) and await _accepts(v, b) and len(server.urls) == 4
        await v.aclose()

    asyncio.run(run())


# PyJWK.from_dict 가 PyJWKError 밖의 예외로 거절하는 JWK 들. 차례로 InvalidKeyError(kty 없음),
# ValueError(base64), InvalidKeyError(모르는 crv), KeyError(oct 에 k 없음), TypeError(숫자 n) 다
MALFORMED_JWKS = [
    {"kid": "no-kty"},
    {"kty": "RSA", "kid": "bad-n", "n": "x", "e": "AQAB"},
    {"kty": "EC", "kid": "bad-crv", "crv": "P-999", "x": "a", "y": "b"},
    {"kty": "oct", "kid": "no-k"},
    {"kty": "RSA", "kid": "int-n", "n": 123, "e": "AQAB"},
]


def test_refresh_skips_malformed_keys(oidc_mod):
    """다시 받은 JWKS 에 쓸 수 없는 JWK 가 섞여 있어도 그 키만 건너뛰고 옆의 좋은 키로 검증한다.
    PyJWT 의 다른 예외가 새면 최대 나이마다 모든 요청이 500 이 된다 (P32 3절)."""
    a, b = Signer(None), Signer(None)
    server, clock = JwksServer(a), Clock()

    async def run():
        v = server.verifier(oidc_mod, clock)
        assert await _accepts(v, a)
        server.signers, server.extra = [b], MALFORMED_JWKS
        clock.now += 300
        assert await _accepts(v, b) and len(server.urls) == 2
        assert not await _accepts(v, a)
        await v.aclose()

    asyncio.run(run())


def test_refresh_parses_only_new_or_changed_keys(oidc_mod, monkeypatch):
    """다시 받은 JWKS 에서 들고 있던 것과 같은 JWK 는 다시 파싱하지 않는다. 빠진 kid 는 버리고,
    같은 kid 라도 내용이 바뀌었으면 새로 푼다."""
    a, b, c = Signer(None), Signer(None), Signer(None)
    server, clock = JwksServer(a, b), Clock()
    parsed: list[str] = []
    real = pyjwt.PyJWK.from_dict

    def counting(obj, algorithm=None):
        parsed.append(obj.get("kid"))
        return real(obj, algorithm)

    monkeypatch.setattr(pyjwt.PyJWK, "from_dict", staticmethod(counting))

    async def run():
        v = server.verifier(oidc_mod, clock)
        assert await _accepts(v, a) and sorted(parsed) == sorted([a.kid, b.kid])
        parsed.clear()
        server.signers = [a, c]
        clock.now += 300
        assert await _accepts(v, c) and parsed == [c.kid]
        assert not await _accepts(v, b)
        # a 의 kid 에 다른 키를 실었다. 글자가 달라졌으므로 다시 풀고 옛 키의 서명은 맞지 않는다
        swapped = Signer(None)
        swapped.kid = a.kid
        server.signers = [swapped, c]
        parsed.clear()
        clock.now += 300
        assert (await v.verify(signed(swapped))).sub == "u1" and parsed == [a.kid]
        with pytest.raises(Exception, match="invalid token"):
            await v.verify(signed(a))
        await v.aclose()

    asyncio.run(run())


def test_unknown_kid_refetch_keeps_the_minimum_interval(oidc_mod):
    """모르는 kid 는 JWKS 를 다시 받게 하지만 30초에 한 번이다. 새 키는 그 안에 쓰인다."""
    a, b = Signer(None), Signer(None)
    server, clock = JwksServer(a), Clock()

    async def run():
        v = server.verifier(oidc_mod, clock)
        assert await _accepts(v, a)
        server.signers = [a, b]
        clock.now += 10
        assert await _accepts(v, b) and len(server.urls) == 2
        stranger = Signer(None)
        clock.now += 20
        assert not await _accepts(v, stranger) and len(server.urls) == 2
        clock.now += 10
        assert not await _accepts(v, stranger) and len(server.urls) == 3
        await v.aclose()

    asyncio.run(run())


def test_oidc_without_the_ui_client_warns_at_start(offline_settings, caplog):
    """UI 의 PKCE 클라이언트 없는 oidc 배포에서는 아무도 로그인하지 못하고 최근 로그인 검사가 늘
    403 이다. 차트는 렌더에서 막고, env 로 깐 배포는 기동 로그가 말한다 (P53b 6.4절)."""
    warning = "without DAEGAM_OIDC_CLIENT_ID"
    oidc = {"auth_mode": "oidc", "oidc_issuer": OIDC_ISSUER, "oidc_audience": OIDC_AUDIENCE}

    def warned(**overrides: Any) -> bool:
        return any(warning in m for m in startup_warnings(offline_settings, caplog, **overrides))

    assert warned(**oidc)
    assert not warned(**oidc, oidc_client_id="daegam-ui")
    assert not warned(auth_mode="local")
