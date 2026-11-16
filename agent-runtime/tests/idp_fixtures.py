"""외부 OIDC 인바운드(D27 F1)를 켠 앱의 테스트들이 함께 쓰는 도우미다.

테스트 안 RSA 키로 JWT 를 서명하는 Signer, discovery·JWKS 를 내는 MockTransport FakeIdp, 인스턴스
토큰과 OIDC 검증기를 둘 다 켠 앱 external_app 이다. 서명자마다 발급자가 있어 두 발급자(P53b 6.8절)의
시험은 발급자가 다른 서명자 둘을 쓴다. 인스턴스 토큰과 Bearer 헤더(INSTANCE_TOKEN·bearer)는 OIDC
밖에서도 쓰므로 app_fixtures.py 에 있다.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from runtime.auth import IssuerSettings
from runtime.state import Settings
from tests.app_fixtures import INSTANCE_TOKEN
from tests.conftest import RunningApp, run_app

ISSUER = "http://idp.test"
AUDIENCE = "daegam-mcp"


class Signer:
    """한 발급자의 RSA 키 하나와 그 JWKS. mock OIDC 와 같은 kid 규칙이다."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.kid = hashlib.sha256(der).hexdigest()[:16]

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def token(self, sub: str = "alice", **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": self.issuer, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 300}
        payload.update(claims)
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": self.kid})


class FakeIdp:
    """discovery·JWKS 를 내는 MockTransport. 경로별 호출 수를 센다."""

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            issuer = self.signer.issuer
            return httpx.Response(200, json={"issuer": issuer, "jwks_uri": issuer + "/jwks"})
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.signer.jwks())
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    @property
    def jwks_calls(self) -> int:
        return self.calls.count("/jwks")


@asynccontextmanager
async def external_app(
    doc: dict[str, Any],
    idp: FakeIdp,
    *,
    cors_origins: tuple[str, ...] = (),
    **fields: Any,
) -> AsyncIterator[RunningApp]:
    """인스턴스 토큰과 OIDC 검증기를 둘 다 켠 앱이다. fields 는 데이터 플레인이 믿는 발급자의
    `IssuerSettings` 칸(발급자 말고)이다."""
    oidc = IssuerSettings(
        issuer=ISSUER,
        insecure_http=True,  # 테스트의 mock 발급자는 평문 http 다
        **fields,
    )
    async with idp.client() as http:
        async with run_app(
            doc,
            settings=Settings(
                instance_token=INSTANCE_TOKEN,
                oidc_audience=AUDIENCE,
                oidc=oidc,
                cors_origins=cors_origins,
            ),
            oidc_http_client=http,
        ) as running:
            yield running
