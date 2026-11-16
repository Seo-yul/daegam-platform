"""플랫폼 공개 주소 DAEGAM_PUBLIC_URL (D58). origin 만 받고 틀리면 기동을 거절한다. local 에서는
그 주소가 사람 발급자다. 발급자와 JWKS 주소에 따른 평문 플래그와 기동 경고도 여기서 본다
(P32 1절)."""

from __future__ import annotations

from dataclasses import replace

import jwt
import pytest

from daegam.auth.signer import JWKS_PATH
from daegam.config import Settings, settings_from_env
from daegam.services.data_plane_auth import consumer_auth
from daegam.store.tenants import TenantSettings
from tests.helpers.auth import admin_headers
from tests.helpers.consts import ISSUER, PLATFORM_JWKS, PUBLIC_URL
from tests.helpers.http import api_client, make_app, startup_warnings


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://daegam.example.com", "https://daegam.example.com"),
        (" \thttps://daegam.example.com/\n", "https://daegam.example.com"),
        ("HTTPS://Daegam.Example.COM:8443//", "https://daegam.example.com:8443"),
        ("http://daegam.local", "http://daegam.local"),
        ("", None),
        ("  ", None),
    ],
)
def test_the_value_is_a_normalized_origin(bare_env, raw, want):
    """앞뒤 ASCII 공백과 끝의 / 를 지우고 호스트까지 소문자로 맞춘다. 토큰의 iss 로 글자 그대로
    맞대므로 같은 주소가 두 글자로 갈리지 않게 한다."""
    bare_env.setenv("DAEGAM_PUBLIC_URL", raw)
    assert settings_from_env().public_url == want


@pytest.mark.parametrize(
    "raw",
    [
        "https://daegam.example.com/ui",
        "https://daegam.example.com/?x=1",
        "https://daegam.example.com#top",
        "https://daegam.example.com/\nhttps://evil.example.com",
        "https://user@daegam.example.com",
        "https://daegam.example.com:",
        "daegam.example.com",
        "ftp://daegam.example.com",
        "https://daegam.example.com\xa0",
    ],
)
def test_anything_but_an_origin_refuses_to_start(bare_env, raw):
    bare_env.setenv("DAEGAM_PUBLIC_URL", raw)
    with pytest.raises(ValueError, match="DAEGAM_PUBLIC_URL"):
        settings_from_env()


# -- 두 발급자 (P32 1절) ---------------------------------------------------------------------------


def test_the_public_url_is_the_issuer_of_the_tokens_the_platform_mints_for_people():
    """local 의 사람 발급자는 공개 주소이고 비었으면 서비스 발급자다. oidc 는 IdP, header 는 없다.
    서비스 발급자와 두 JWKS 주소는 공개 주소와 무관하다. 디스커버리 문서의 issuer 인
    platform_human_issuer 는 플랫폼이 사람 토큰을 내는 local 과 oidc 에서 공개 주소다 (P53b
    6.1절)."""
    local = Settings(auth_mode="local", platform_issuer=ISSUER, public_url=PUBLIC_URL)
    assert local.issuer == local.platform_human_issuer == PUBLIC_URL
    assert (local.platform_issuer_url, local.human_jwks_url) == (ISSUER, PLATFORM_JWKS)
    assert local.service_jwks_url == f"{PLATFORM_JWKS}?use=service"
    bare = replace(local, public_url=None)
    assert bare.issuer == bare.platform_human_issuer == ISSUER
    oidc = replace(local, auth_mode="oidc", oidc_issuer="https://idp.example")
    assert (oidc.issuer, oidc.platform_human_issuer) == ("https://idp.example", PUBLIC_URL)
    header = replace(local, auth_mode="header")
    assert (header.issuer, header.platform_human_issuer) == (None, ISSUER)


def test_local_human_tokens_carry_the_public_url(
    local_settings, mongo, fake_clusters, fake_secrets
):
    """제어면 토큰과 데이터 플레인 토큰은 공개 주소를 iss 로 싣고 제어면 검증기가 그것을 받는다.
    밖의 클라이언트가 읽는 디스커버리 문서도 공개 주소의 것이다. 내부 홉 토큰은 서비스 발급자
    그대로라 공개 주소가 바뀌어도 살아남는다."""
    app = make_app(local_settings, fake_clusters, fake_secrets, public_url=PUBLIC_URL)

    def iss(token: str) -> str:
        return jwt.decode(token, options={"verify_signature": False})["iss"]

    with api_client(app) as c:
        h = admin_headers(c, mongo)
        assert iss(h["Authorization"].removeprefix("Bearer ")) == PUBLIC_URL
        assert c.get("/me", headers=h).status_code == 200
        assert iss(c.post("/auth/token", headers=h).json()["access_token"]) == PUBLIC_URL
        assert c.get("/auth/config").json()["issuer"] == PUBLIC_URL
        doc = c.get(f"{PUBLIC_URL}/.well-known/openid-configuration").json()
        assert (doc["issuer"], doc["jwks_uri"]) == (PUBLIC_URL, PUBLIC_URL + JWKS_PATH)
        internal = app.state.internal_issuer.mint(sub="svc:vmcp:g", tenant="t", groups=[])
        assert iss(internal) == ISSUER


# -- 평문 플래그와 기동 경고 (P32 1절) ------------------------------------------------------------


def test_plain_http_flags_follow_the_issuer_and_the_jwks_url():
    """사람 토큰 검증기(daegam-oidc, 노출 에이전트)의 평문 플래그는 발급자나 그 검증기가 받는 JWKS
    주소가 http 일 때 붙는다. oidc 의 검증기는 플랫폼 JWKS 를 받지 않으므로 IdP 만 본다. 노출
    에이전트 env 의 입력(consumer_auth)도 같은 판단이다."""
    ts = TenantSettings(tenant="t")
    local = Settings(auth_mode="local", platform_issuer=ISSUER)
    assert (local.human_jwks_url, local.insecure_human_http) == (PLATFORM_JWKS, True)
    auth = consumer_auth(local, ts)
    assert (auth.jwks_url, auth.insecure_http) == (PLATFORM_JWKS, True)
    tls = Settings(auth_mode="local", platform_issuer="https://daegam.example.com")
    assert tls.human_jwks_url == "https://daegam.example.com/.well-known/jwks.json"
    assert tls.insecure_human_http is False
    oidc = Settings(
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        platform_issuer=ISSUER,
        data_plane_issuer="idp",
    )
    assert (oidc.human_jwks_url, oidc.insecure_human_http) == (None, False)
    assert oidc.insecure_platform_issuer and oidc.service_jwks_url == f"{PLATFORM_JWKS}?use=service"
    assert (consumer_auth(oidc, ts).jwks_url, consumer_auth(oidc, ts).insecure_http) == (
        None,
        False,
    )
    # 공개 주소가 https 인 설치. 사람 발급자(공개 주소)는 https 이지만 JWKS 가 클러스터 안 http 라
    # 붙는다. 발급자만 http 여도 붙는다. 서비스 토큰 검증기의 플래그는 서비스 발급자만 따른다
    https = replace(local, public_url=PUBLIC_URL)
    assert (https.insecure_issuer, https.insecure_human_http) == (False, True)
    consumer = consumer_auth(https, ts)
    assert (consumer.issuer, consumer.insecure_http) == (PUBLIC_URL, True)
    plain_public = replace(tls, public_url="http://daegam.local")
    assert plain_public.insecure_human_http and not plain_public.insecure_platform_issuer
    assert replace(tls, public_url=PUBLIC_URL).insecure_human_http is False
    mock = Settings(
        auth_mode="oidc",
        oidc_issuer="http://mock.svc",
        oidc_insecure_http=True,
        data_plane_issuer="idp",
    )
    assert mock.insecure_human_http and mock.human_jwks_url is None
    header = Settings(auth_mode="header", platform_issuer=ISSUER)
    assert (consumer_auth(header, ts).jwks_url, consumer_auth(header, ts).insecure_http) == (
        None,
        False,
    )


def test_startup_warnings_tell_the_issuer_from_the_jwks_url(offline_settings, caplog):
    """평문인 쪽이 사람 토큰의 발급자인지 데이터 플레인이 받는 JWKS 주소인지 나누어 말한다."""
    jwks = f"platform JWKS {PLATFORM_JWKS} is plain http"
    issuer = f"issuer {ISSUER} is plain http"
    local = startup_warnings(offline_settings, caplog, auth_mode="local", platform_issuer=ISSUER)
    assert any(issuer in m for m in local) and any(jwks in m for m in local)
    oidc = startup_warnings(
        offline_settings,
        caplog,
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_audience="daegam-api",
        platform_issuer=ISSUER,
    )
    assert any(jwks in m for m in oidc) and not any("issuer" in m for m in oidc)
    tls = startup_warnings(
        offline_settings, caplog, auth_mode="local", platform_issuer="https://daegam.example.com"
    )
    assert not any("plain http" in m for m in tls)
    # 공개 주소가 https 인 설치는 사람 발급자가 https 라 JWKS 주소만 말한다. https 공개 주소는
    # 경고하지 않는다
    https = startup_warnings(
        offline_settings, caplog, auth_mode="local", platform_issuer=ISSUER, public_url=PUBLIC_URL
    )
    assert any(jwks in m for m in https) and not any("issuer" in m for m in https)
    assert not any("public url" in m for m in https)
    # 로컬 벌은 사람 발급자가 평문 공개 주소다. 공개 주소의 경고 한 줄이 그것을 말한다. http 는 로컬
    # 벌만 쓰므로 공개 서버에 놓이면 기동 로그에서 바로 보인다
    orb = startup_warnings(
        offline_settings,
        caplog,
        auth_mode="local",
        platform_issuer=ISSUER,
        public_url="http://daegam.local",
    )
    assert any("public url http://daegam.local is plain http" in m for m in orb)
    assert any(jwks in m for m in orb) and not any("issuer" in m for m in orb)
