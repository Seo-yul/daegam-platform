"""Mock OIDC provider 다. Okta 의 custom authorization server 를 흉내 낸다 (P6 F1·F4b).

환경변수
  MOCK_OIDC_ISSUER              issuer URL (기본 http://localhost:8080).
                                discovery 의 jwks_uri/token_endpoint 도 여기서 만든다
  MOCK_OIDC_AUDIENCE            access_token 의 aud (기본 daegam-mcp)
  MOCK_OIDC_CLIENTS             "id:secret:scope1 scope2,id2:secret2:scope"
                                (client_credentials 클라이언트 목록)
  MOCK_OIDC_KEY_PEM             RSA 개인키 PEM. 없으면 시작 시 생성
                                (레플리카를 늘리면 반드시 넣어라)
  MOCK_OIDC_ALLOW_TEST_TOKENS   true 면 POST /_test/token 으로 서명된 사용자 토큰을 발급
  MOCK_OIDC_TOKEN_TTL           초 (기본 3600)

Okta 와 같은 claim 을 낸다: client credentials 토큰은 sub=cid=client_id, scp=[...], groups 없음.
사용자 토큰(테스트용)에는 sub/email/groups 가 든다. 테스트용 토큰은 발급 시각(iat_offset)과
로그인 시각(auth_time_offset)을 지금에서 옮길 수 있고, id_token 이면 Keycloak 의 ID 토큰처럼 헤더에
typ=ID 를 싣는다. 플랫폼의 사람 기록(iat)과 최근 로그인(ID 토큰의 auth_time)을 e2e 가 이것으로
만든다 (P53b F8).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, Field

from mock_oidc import __version__

ALG = "RS256"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_clients(raw: str) -> dict[str, tuple[str, set[str]]]:
    """ "id:secret:scope1 scope2,id2:secret2:scope" -> {id: (secret, {scopes})}"""
    out: dict[str, tuple[str, set[str]]] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        parts = entry.split(":", 2)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(f"MOCK_OIDC_CLIENTS entry {entry!r} must be id:secret[:scopes]")
        scopes = set(parts[2].split()) if len(parts) == 3 else set()
        out[parts[0]] = (parts[1], scopes)
    return out


@dataclass
class Config:
    issuer: str = "http://localhost:8080"
    audience: str = "daegam-mcp"
    clients: dict[str, tuple[str, set[str]]] = field(default_factory=dict)
    key_pem: str | None = None
    allow_test_tokens: bool = False
    token_ttl: int = 3600

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            issuer=os.environ.get("MOCK_OIDC_ISSUER", "http://localhost:8080").rstrip("/"),
            audience=os.environ.get("MOCK_OIDC_AUDIENCE", "daegam-mcp"),
            clients=parse_clients(os.environ.get("MOCK_OIDC_CLIENTS", "")),
            key_pem=os.environ.get("MOCK_OIDC_KEY_PEM") or None,
            allow_test_tokens=_env_bool("MOCK_OIDC_ALLOW_TEST_TOKENS"),
            token_ttl=int(os.environ.get("MOCK_OIDC_TOKEN_TTL", "3600")),
        )


class Signer:
    def __init__(self, key_pem: str | None):
        if key_pem:
            self.private_key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        else:
            self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = hashlib.sha256(der).hexdigest()[:16]

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": ALG})
        return {"keys": [jwk]}

    def sign(self, claims: dict[str, Any], *, typ: str | None = None) -> str:
        headers = {"kid": self.kid, **({"typ": typ} if typ else {})}
        return jwt.encode(claims, self.private_key, algorithm=ALG, headers=headers)


class TestTokenRequest(BaseModel):
    sub: str = Field(min_length=1)
    email: str | None = None
    groups: list[str] = Field(default_factory=list)
    aud: str | None = None
    ttl: int | None = Field(default=None, ge=1, le=86400 * 30)
    scp: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    # 발급 시각을 지금에서 이만큼 옮긴다(초, 음수는 과거). iat·nbf·exp 가 함께 옮겨 간다
    iat_offset: int = Field(default=0, ge=-86400 * 30, le=3600)
    # auth_time 을 지금에서 이만큼 옮겨 싣는다(초, 음수는 과거). 없으면 싣지 않는다
    auth_time_offset: int | None = Field(default=None, ge=-86400 * 30, le=3600)
    # ID 토큰이다. 헤더에 typ=ID 를 싣는다(Keycloak 의 모양). aud 는 UI 의 client id 로 준다
    id_token: bool = False


def _oauth_error(status: int, error: str, description: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status, headers=headers
    )


def create_app(config: Config | None = None, signer: Signer | None = None) -> FastAPI:
    config = config or Config.from_env()
    signer = signer or Signer(config.key_pem)
    app = FastAPI(title="mock-oidc", version=__version__)
    app.state.config = config
    app.state.signer = signer

    def base_claims(ttl: int, shift: int = 0) -> dict[str, Any]:
        now = int(time.time()) + shift
        return {
            "iss": config.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
            "ver": 1,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "issuer": config.issuer, "kid": signer.kid}

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> dict[str, Any]:
        scopes = sorted({s for _, (_, sc) in config.clients.items() for s in sc})
        return {
            "issuer": config.issuer,
            "jwks_uri": f"{config.issuer}/jwks",
            "token_endpoint": f"{config.issuer}/token",
            # authorization_endpoint 는 미구현 (client_credentials 만)
            "authorization_endpoint": f"{config.issuer}/authorize",
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [ALG],
            "scopes_supported": scopes,
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "cid",
                "scp",
                "email",
                "groups",
            ],
        }

    @app.get("/jwks")
    async def jwks() -> dict[str, Any]:
        return signer.jwks()

    @app.post("/token")
    async def token(
        request: Request,
        grant_type: str | None = Form(default=None),
        client_id: str | None = Form(default=None),
        client_secret: str | None = Form(default=None),
        scope: str | None = Form(default=None),
        audience: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        # client_secret_basic 우선, 없으면 client_secret_post
        if authorization and authorization.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode()
                client_id, client_secret = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return _oauth_error(
                    401,
                    "invalid_client",
                    "malformed basic credentials",
                    **{"WWW-Authenticate": 'Basic realm="mock-oidc"'},
                )
        if grant_type != "client_credentials":
            return _oauth_error(
                400, "unsupported_grant_type", "only client_credentials is supported"
            )
        known = config.clients.get(client_id or "")
        if known is None or not secrets.compare_digest(known[0], client_secret or ""):
            return _oauth_error(
                401,
                "invalid_client",
                "unknown client or bad secret",
                **{"WWW-Authenticate": 'Basic realm="mock-oidc"'},
            )
        # Okta custom authorization server 는 scope 없는 client_credentials 를 거부한다
        requested = set((scope or "").split())
        if not requested:
            return _oauth_error(400, "invalid_scope", "scope parameter is required")
        if not requested <= known[1]:
            return _oauth_error(
                400, "invalid_scope", f"client is not allowed: {sorted(requested - known[1])}"
            )
        claims = {
            **base_claims(config.token_ttl),
            "aud": audience or config.audience,
            "sub": client_id,
            "cid": client_id,
            "scp": sorted(requested),
        }
        return JSONResponse(
            {
                "token_type": "Bearer",
                "expires_in": config.token_ttl,
                "access_token": signer.sign(claims),
                "scope": " ".join(sorted(requested)),
            }
        )

    @app.post("/_test/token")
    async def test_token(body: TestTokenRequest) -> dict[str, Any]:
        if not config.allow_test_tokens:
            raise HTTPException(
                403,
                {"code": "test_tokens_disabled", "hint": "set MOCK_OIDC_ALLOW_TEST_TOKENS=true"},
            )
        ttl = body.ttl or config.token_ttl
        claims: dict[str, Any] = {
            **base_claims(ttl, body.iat_offset),
            "aud": body.aud or config.audience,
            "sub": body.sub,
            "groups": body.groups,
            **body.extra,
        }
        if body.email:
            claims["email"] = body.email
        if body.scp:
            claims["scp"] = body.scp
        if body.auth_time_offset is not None:
            claims["auth_time"] = int(time.time()) + body.auth_time_offset
        token = signer.sign(claims, typ="ID" if body.id_token else None)
        out: dict[str, Any] = {"token_type": "Bearer", "expires_in": ttl, "claims": claims}
        out["id_token" if body.id_token else "access_token"] = token
        return out

    return app


app = create_app()
