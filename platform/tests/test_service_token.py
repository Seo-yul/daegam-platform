"""내부 MCP 홉의 서비스 토큰 발급자 (auth/service_token.py, D41)."""

from __future__ import annotations

import time

import jwt
import pytest
from starlette.testclient import TestClient

from daegam.auth.local import LocalVerifier
from daegam.auth.oidc import AuthError
from daegam.auth.service_token import (
    PLATFORM_CALL_TTL,
    PLATFORM_SUB,
    SERVICE_TOKEN_TTL,
    InternalIssuer,
    claims_current,
    service_sub,
)
from daegam.auth.signer import (
    CONTROL_PLANE_AUDIENCE,
    INTERNAL_AUDIENCE_PREFIX,
    TELEMETRY_AUDIENCE,
    PlatformSigner,
    internal_audience,
    jwks_url,
)
from daegam.config import Settings
from daegam.store.base import utcnow
from tests.helpers.auth import slot_keys
from tests.helpers.consts import ISSUER, PLATFORM_JWKS, PUBLIC_URL
from tests.helpers.http import make_app


def _issuer() -> InternalIssuer:
    iss = InternalIssuer(
        issuer=ISSUER + "/", human_issuer=PUBLIC_URL + "/", mcp_audience="daegam-mcp"
    )
    iss.attach(PlatformSigner.generate())
    return iss


def test_mint_carries_tenant_groups_and_a_svc_sub():
    iss = _issuer()
    tok = iss.mint(sub=service_sub("vmcp", "gw-a"), tenant="acme", groups=["daegam-acme"])
    assert jwt.get_unverified_header(tok)["typ"] == "at+jwt"
    claims = jwt.decode(
        tok,
        jwt.PyJWK.from_dict(iss.jwks()["keys"][0]).key,
        algorithms=["RS256"],
        audience="daegam-internal:acme",
        issuer=ISSUER,
    )
    assert claims["sub"] == "svc:vmcp:gw-a"
    assert internal_audience("acme") == "daegam-internal:acme"
    # 다른 테넌트의 audience 로는 검증되지 않는다. 그것이 테넌트 격리다
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            tok,
            jwt.PyJWK.from_dict(iss.jwks()["keys"][0]).key,
            algorithms=["RS256"],
            audience=internal_audience("other"),
        )
    assert claims["tenant"] == "acme" and claims["groups"] == ["daegam-acme"]
    assert "tv" not in claims and claims["jti"]
    assert SERVICE_TOKEN_TTL - 5 <= claims["exp"] - claims["iat"] <= SERVICE_TOKEN_TTL
    # 플랫폼 자신의 호출 토큰은 짧다
    short = iss.mint(sub=PLATFORM_SUB, tenant="acme", groups=[], ttl=PLATFORM_CALL_TTL)
    peek = InternalIssuer.peek(short)
    assert peek is not None and peek.sub == PLATFORM_SUB and peek.kid == iss.kid
    assert peek.exp is not None and peek.exp - int(time.time()) <= PLATFORM_CALL_TTL
    assert InternalIssuer.peek("not.a.jwt") is None


def test_gateway_token_has_the_data_plane_audience_and_the_human_issuer():
    """vMCP 의 daegam-oidc 는 사람 발급자 하나만 믿으므로 gateway_token 의 iss 는 사람 발급자다.
    내부 홉 토큰은 공개 주소가 바뀌어도 살아남도록 서비스 발급자 그대로다 (P32 1절)."""
    iss = _issuer()
    tok = iss.mint_gateway(sub=service_sub("agent", "ai-1"), tenant="acme", groups=["team-a"])
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert claims["aud"] == "daegam-mcp" and claims["groups"] == ["team-a"]
    assert claims["iss"] == PUBLIC_URL and InternalIssuer.peek(tok).groups == ("team-a",)
    internal = iss.mint(sub=service_sub("agent", "ai-1"), tenant="acme", groups=["team-a"])
    assert InternalIssuer.peek(internal).iss == ISSUER


def test_claims_current_compares_iss_aud_and_groups():
    """gateway_token 을 새로 낼지는 iss·aud·groups 셋을 원하는 값과 맞대 정한다. 하나만 달라도
    거짓이고, 없거나 JWT 가 아니어도 거짓이다 (P32 8절)."""
    iss = _issuer()
    tok = iss.mint_gateway(sub=service_sub("agent", "ai-1"), tenant="acme", groups=["team-a"])
    want = {"iss": PUBLIC_URL, "aud": "daegam-mcp", "groups": ["team-a"]}
    assert claims_current(tok, **want)
    for key, other in (("iss", "https://other"), ("aud", "daegam-api"), ("groups", ["team-b"])):
        assert not claims_current(tok, **{**want, key: other}), key
    assert not claims_current(None, **want) and not claims_current("garbage", **want)


def test_service_sub_prefix_and_tenant_are_enforced():
    iss = _issuer()
    with pytest.raises(ValueError, match="svc:"):
        iss.mint(sub="tenant:acme", tenant="acme", groups=[])
    with pytest.raises(ValueError, match="tenant"):
        iss.mint(sub="svc:vmcp:x", tenant="", groups=[])
    with pytest.raises(RuntimeError, match="not loaded"):
        InternalIssuer(issuer=ISSUER, human_issuer=ISSUER, mcp_audience="daegam-mcp").mint(
            sub="svc:vmcp:x", tenant="acme", groups=[]
        )


def test_control_plane_verifier_rejects_service_tokens_twice():
    """audience 가 다르고, 통과했더라도 tv 가 없다. 같은 키로 서명하는 이상 이 둘이 격리의 전부다"""
    iss = _issuer()
    verifier = LocalVerifier(iss.signer, issuer=ISSUER, audience=CONTROL_PLANE_AUDIENCE)
    with pytest.raises(AuthError):
        verifier.verify(iss.mint(sub="svc:vmcp:x", tenant="acme", groups=[]))
    # audience 만 맞춘 토큰도 tv 가 없어 거절된다
    forged = iss.signer.sign(
        {
            "iss": ISSUER,
            "aud": CONTROL_PLANE_AUDIENCE,
            "sub": "svc:vmcp:x",
            "exp": int(time.time()) + 60,
        }
    )
    with pytest.raises(AuthError, match="tv"):
        verifier.verify(forged)


def test_audiences_must_differ():
    with pytest.raises(ValueError, match="collides"):
        Settings(mcp_audience=INTERNAL_AUDIENCE_PREFIX + "acme")
    with pytest.raises(ValueError, match="collides"):
        Settings(mcp_audience=CONTROL_PLANE_AUDIENCE)
    with pytest.raises(ValueError, match="collides"):
        Settings(mcp_audience=TELEMETRY_AUDIENCE)
    assert Settings(mcp_audience="tools").mcp_audience == "tools"
    with pytest.raises(ValueError, match="tenant"):
        internal_audience("")


def test_platform_issuer_url_is_one_address_for_every_mode():
    s = Settings(auth_mode="header", in_cluster=True)
    assert s.platform_issuer_url == "http://daegam-platform.daegam-system.svc:9000"
    assert s.issuer is None and s.insecure_platform_issuer
    s = Settings(auth_mode="oidc", oidc_issuer="https://idp", platform_issuer="https://u.example/")
    assert s.issuer == "https://idp" and s.platform_issuer_url == "https://u.example"
    assert s.insecure_platform_issuer is False
    s = Settings(auth_mode="local", platform_issuer="https://u.example")
    assert s.issuer == "https://u.example" == s.platform_issuer_url


@pytest.mark.parametrize(
    ("issuer", "local"),
    [
        ("http://daegam-platform.daegam-system.svc:9000", True),
        ("http://daegam-platform.daegam-system.svc.cluster.local:9000", True),
        ("http://host.internal:9000", True),
        ("http://localhost:9000/", True),
        ("https://daegam.example.com", False),
        ("http://10.0.0.5:9000", False),
    ],
)
def test_cluster_local_platform_issuer_reads_the_url_host(issuer, local):
    assert Settings(platform_issuer=issuer).cluster_local_platform_issuer is local


def test_well_known_is_served_in_every_mode(settings, mongo, fake_clusters, fake_secrets):
    """디스커버리와 JWKS 는 header·oidc 에서도 열린다. 내부 MCPServer 가 그것으로 서비스 토큰을
    검증하기 때문이다(D41). local 만의 것이었던 D40 의 404 는 없다. 문서의 issuer 는 플랫폼이 사람
    토큰을 내는 oidc 모드에서 local 처럼 공개 주소이고(P53b 6.1절), header 모드는 공개 주소가 있어도
    서비스 발급자다 (P32 1절). jwks_uri 는 issuer 를 따른다. 내부 MCPServer 는 이 문서를 읽지 않고
    설정의 서비스 JWKS 주소로 검증한다.
    개인 토큰의 슬롯은 플랫폼이 사람 토큰을 내는 모드(local, oidc)에만 오르므로 header 모드의
    JWKS 는 Mongo 에 슬롯이 남아 있어도 플랫폼 키 하나이고, oidc 모드의 JWKS 는 슬롯을 싣는다 (P32
    9절, P53b 6.1절)."""
    for mode, issuer, jwks, slots, extra in (
        ("header", ISSUER, PLATFORM_JWKS, 0, {}),
        (
            "oidc",
            PUBLIC_URL,
            jwks_url(PUBLIC_URL),
            1,
            {"oidc_issuer": "https://idp.example", "oidc_audience": "daegam-api"},
        ),
    ):
        app = make_app(
            settings,
            fake_clusters,
            fake_secrets,
            auth_mode=mode,
            platform_issuer=ISSUER,
            public_url=PUBLIC_URL,
            **extra,
        )
        with TestClient(app) as root:
            doc = root.get("/.well-known/openid-configuration")
            assert doc.status_code == 200, mode
            assert doc.json()["issuer"] == issuer, mode
            assert doc.json()["jwks_uri"] == jwks, mode
            kid = app.state.internal_issuer.kid
            mongo["pat_keys"].insert_one(
                {
                    "_id": "pat-0123456789abcdef",
                    "master": kid,
                    "state": "pool",
                    "created_at": utcnow(),
                }
            )
            keys = root.get("/.well-known/jwks.json")
            assert keys.status_code == 200 and len(slot_keys(keys.json(), kid)) == slots, mode
            mongo["pat_keys"].delete_many({})
            # 그 키로 서비스 토큰이 검증된다 (ToolHive 가 하는 그대로)
            tok = app.state.internal_issuer.mint(sub="svc:vmcp:g", tenant="t", groups=[])
            key = jwt.PyJWK.from_dict(keys.json()["keys"][0]).key
            got = jwt.decode(tok, key, algorithms=["RS256"], audience=internal_audience("t"))
            assert got["tenant"] == "t"
            # 사람 인증 라우트는 여전히 local 만의 것이다
            assert root.get("/api/users").status_code in (401, 404)


def test_jwks_without_a_signing_key_is_503_in_the_platform_error_shape(
    settings, mongo, fake_clusters, fake_secrets
):
    """키를 못 읽은 배포본의 JWKS 503 도 다른 오류와 같은 {code, detail} 꼴이다."""
    app = make_app(settings, fake_clusters, fake_secrets, platform_issuer=ISSUER)
    with TestClient(app) as root:
        app.state.internal_issuer._signer = None
        r = root.get("/.well-known/jwks.json")
        assert r.status_code == 503
        assert r.json() == {
            "detail": {"code": "signer_unavailable", "detail": "signing key not loaded"}
        }
