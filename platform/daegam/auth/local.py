"""local 모드의 사람 토큰 클레임과 제어면 검증기 (D40).

서명기·JWKS·디스커버리는 모든 모드가 쓰는 것이라 `auth/signer.py` 에 있다(D41). 여기는 사람 토큰의
클레임 꼴과, 플랫폼 자신이 HTTP 없이 자기 공개키로 검증하는 `LocalVerifier` 다. 데이터 플레인
(런타임 검증기·vMCP·노출 앞단)은 발급자 주소만 다른 채 기존 OIDC 경로로 같은 토큰을 검증한다.
"""

from __future__ import annotations

from typing import Any

import jwt

from daegam.auth.oidc import LEEWAY_SECONDS, AuthError
from daegam.auth.signer import ALG, PlatformSigner, registered_claims

# `POST /auth/token` 의 데이터 플레인 토큰 수명. 회수가 없는 토큰이라 이 수명이 끊기는 한도이고,
# 모든 사람 토큰의 한도를 16분 안에 두려고 15분이다 (P32 6절)
DATA_PLANE_TOKEN_TTL = 900


def access_claims(
    *,
    issuer: str,
    audience: str,
    sub: str,
    ttl: int,
    token_version: int,
    username: str,
    email: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    """제어면·데이터 플레인 토큰이 같은 꼴이다. `aud` 만 다르다."""
    claims: dict[str, Any] = {
        **registered_claims(issuer=issuer, audience=audience, sub=sub, ttl=ttl),
        "tv": token_version,
        "preferred_username": username,
    }
    if email:
        claims["email"] = email
    if display_name:
        claims["name"] = display_name
    return claims


# 사람 토큰이 반드시 싣는 클레임. `tv` 가 없는 토큰은 비밀번호 판을 볼 수 없어 받지 않는다
HUMAN_TOKEN_CLAIMS = ("exp", "sub", "tv")


class LocalVerifier:
    """플랫폼 자신이 낸 토큰을 자기 공개키로 검증한다.

    사용자 문서 검사(`tv`·disabled)는 호출자 몫이다. required 는 반드시 있어야 하는 클레임이다.
    OAuth 접근 토큰은 `tv` 대신 연결 id(`sid`)를 싣는다 (P33 10절·12절)."""

    def __init__(
        self,
        signer: PlatformSigner,
        *,
        issuer: str,
        audience: str,
        required: tuple[str, ...] = HUMAN_TOKEN_CLAIMS,
    ):
        self.signer = signer
        self.issuer = issuer
        self.audience = audience
        self.required = required

    def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") != ALG:
            raise AuthError(f"unsupported alg {header.get('alg')!r}")
        if header.get("kid") != self.signer.kid:
            raise AuthError("unknown signing key (kid)")
        try:
            return jwt.decode(
                token,
                self.signer.public_key(),
                algorithms=[ALG],
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": list(self.required)},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired", code="token_expired") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
