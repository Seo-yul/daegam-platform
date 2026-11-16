from __future__ import annotations

import base64
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from mock_oidc.app import Config, Signer, create_app, parse_clients

ISSUER = "http://mock-oidc.test"


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer(None)


@pytest.fixture
def client(signer) -> TestClient:
    cfg = Config(
        issuer=ISSUER,
        audience="daegam-mcp",
        clients=parse_clients("agent:agent-secret:mcp.invoke mcp.read,api:api-secret:daegam.api"),
        allow_test_tokens=True,
        token_ttl=600,
    )
    return TestClient(create_app(cfg, signer))


def _verify(client: TestClient, token: str, audience: str = "daegam-mcp") -> dict:
    """discovery 로 jwks_uri 를 얻고 kid 로 키를 골라 PyJWT 로 검증한다
    (플랫폼 oidc.py 와 같은 경로)."""
    disc = client.get("/.well-known/openid-configuration").json()
    assert disc["issuer"] == ISSUER and disc["jwks_uri"] == f"{ISSUER}/jwks"
    jwks = client.get("/jwks").json()
    kid = jwt.get_unverified_header(token)["kid"]
    key = jwt.PyJWK.from_dict(next(k for k in jwks["keys"] if k["kid"] == kid)).key
    return jwt.decode(token, key, algorithms=["RS256"], audience=audience, issuer=ISSUER)


def test_discovery(client):
    d = client.get("/.well-known/openid-configuration").json()
    assert d["token_endpoint"] == f"{ISSUER}/token"
    assert d["grant_types_supported"] == ["client_credentials"]
    assert d["scopes_supported"] == ["daegam.api", "mcp.invoke", "mcp.read"]
    assert client.get("/healthz").json()["status"] == "ok"


def test_jwks_has_one_rsa_key(client, signer):
    keys = client.get("/jwks").json()["keys"]
    assert len(keys) == 1 and keys[0]["kty"] == "RSA" and keys[0]["kid"] == signer.kid
    assert keys[0]["use"] == "sig" and keys[0]["alg"] == "RS256"


def test_client_credentials_post(client):
    r = client.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "agent",
            "client_secret": "agent-secret",
            "scope": "mcp.invoke",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 600
    claims = _verify(client, body["access_token"])
    assert claims["sub"] == "agent" and claims["cid"] == "agent" and claims["scp"] == ["mcp.invoke"]
    assert claims["aud"] == "daegam-mcp" and "groups" not in claims
    assert claims["exp"] - claims["iat"] == 600


def test_client_credentials_basic_and_audience_override(client):
    basic = base64.b64encode(b"api:api-secret").decode()
    r = client.post(
        "/token",
        data={"grant_type": "client_credentials", "scope": "daegam.api", "audience": "daegam-api"},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert r.status_code == 200, r.text
    claims = _verify(client, r.json()["access_token"], audience="daegam-api")
    assert claims["cid"] == "api" and claims["aud"] == "daegam-api"


def test_bad_secret_is_401(client):
    r = client.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "agent",
            "client_secret": "nope",
            "scope": "mcp.invoke",
        },
    )
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert r.headers["WWW-Authenticate"].startswith("Basic")
    r = client.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "ghost",
            "client_secret": "x",
            "scope": "mcp.invoke",
        },
    )
    assert r.status_code == 401


def test_scope_required_and_checked(client):
    base = {
        "grant_type": "client_credentials",
        "client_id": "agent",
        "client_secret": "agent-secret",
    }
    r = client.post("/token", data=base)
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    r = client.post("/token", data={**base, "scope": "mcp.invoke daegam.api"})
    assert r.status_code == 400 and "daegam.api" in r.json()["error_description"]
    r = client.post("/token", data={**base, "grant_type": "password", "scope": "mcp.invoke"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


def test_test_token(client):
    r = client.post(
        "/_test/token",
        json={
            "sub": "u1",
            "email": "u1@example.com",
            "groups": ["daegam-tenant-a-admin"],
            "ttl": 60,
        },
    )
    assert r.status_code == 200, r.text
    claims = _verify(client, r.json()["access_token"])
    assert claims["sub"] == "u1" and claims["email"] == "u1@example.com"
    assert claims["groups"] == ["daegam-tenant-a-admin"] and "cid" not in claims
    assert 0 < claims["exp"] - time.time() <= 60


def test_test_token_disabled(signer):
    cfg = Config(issuer=ISSUER, clients={}, allow_test_tokens=False)
    c = TestClient(create_app(cfg, signer))
    assert c.post("/_test/token", json={"sub": "u1"}).status_code == 403


def test_wrong_signer_rejected(client):
    other = Signer(None)
    token = other.sign(
        {"iss": ISSUER, "aud": "daegam-mcp", "sub": "x", "exp": int(time.time()) + 60}
    )
    jwks = client.get("/jwks").json()
    key = jwt.PyJWK.from_dict(jwks["keys"][0]).key
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, key, algorithms=["RS256"], audience="daegam-mcp", issuer=ISSUER)


def test_key_pem_env_roundtrip(signer):
    from cryptography.hazmat.primitives import serialization

    pem = signer.private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert Signer(pem).kid == signer.kid  # 같은 키면 같은 kid 다 (레플리카 간 공유용)


def test_test_token_moves_iat_and_auth_time(client):
    """사람 기록의 확인 시각(iat)과 최근 로그인(auth_time)을 e2e 가 만든다 (P53b F8). iat 를 옮기면
    exp 도 함께 옮겨 간다."""
    r = client.post(
        "/_test/token",
        json={"sub": "u1", "ttl": 7200, "iat_offset": -3600, "auth_time_offset": -900},
    )
    assert r.status_code == 200, r.text
    claims = _verify(client, r.json()["access_token"])
    now = time.time()
    assert abs(claims["iat"] - (now - 3600)) < 5 and claims["exp"] - claims["iat"] == 7200
    assert abs(claims["auth_time"] - (now - 900)) < 5
    plain = _verify(client, client.post("/_test/token", json={"sub": "u1"}).json()["access_token"])
    assert "auth_time" not in plain
    too_far = client.post("/_test/token", json={"sub": "u1", "iat_offset": 7200})
    assert too_far.status_code == 422


def test_test_id_token_carries_typ_id_and_the_client_audience(client):
    r = client.post(
        "/_test/token",
        json={"sub": "u1", "aud": "daegam-ui", "id_token": True, "auth_time_offset": 0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" not in body
    assert jwt.get_unverified_header(body["id_token"])["typ"] == "ID"
    claims = _verify(client, body["id_token"], audience="daegam-ui")
    assert claims["aud"] == "daegam-ui" and abs(claims["auth_time"] - time.time()) < 5
