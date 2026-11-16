"""개인 토큰의 슬롯 키와 서명, JWKS 조립 (P32 4절·5절).

슬롯의 키는 저장하지 않고 `kid` 에서 다시 만든다. `d = int(HKDF-SHA256(ikm = 플랫폼 서명 키, salt =
"daegam-pat-v1", info = kid, 40바이트)) mod (n − 1) + 1` 인 P-256 키다. 40바이트는 n 보다 64비트
길어 나머지의 치우침이 무시할 만하다. 이 키를 만들 수 있는 것은 서명 키 Secret 을 가진 쪽뿐이고,
그쪽은 이미 모든 토큰을 서명할 수 있다.

JWKS 의 공개 좌표도 Mongo 에서 읽지 않는다. 조립할 때 `kid` 로 키를 다시 만들고 레플리카가 `kid`
별로 캐시한다. Mongo 에 쓸 수 있는 사람이 슬롯 문서를 심어도 JWKS 에 오르는 것은 플랫폼만 서명할 수
있는 키다. 슬롯은 플랫폼이 사람 토큰을 내는 모드(local, oidc,
`Settings.platform_mints_human_tokens`)에만 오른다. header 모드와 서비스 토큰 검증기가 받는
`?use=service` 는 플랫폼 RSA 키 하나이고 Mongo 를 읽지 않는다 (P53b 6.1절).

조립은 레플리카 안에서 한 번에 하나다. 캐시가 빈 순간 무인증 요청이 몰려도 슬롯 읽기와 키 유도는
한 번이고, 늦게 끝난 조립이 옛 kid 목록으로 만든 묶음을 새 묶음 위에 덮지 못한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import ACCESS_TOKEN_TYP, PlatformSigner, public_jwk
from daegam.errors import error
from daegam.store.pat_keys import MongoPatKeyStore
from daegam.ttl_cache import TTLCache

logger = logging.getLogger("daegam.auth.pat_keys")

SLOT_ALG = "ES256"
SLOT_SALT = b"daegam-pat-v1"
SLOT_SEED_BYTES = 40
# P-256 군의 위수 n. cryptography 가 곡선의 상수를 내지 않아 적어 둔다
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
# 레플리카의 JWKS 캐시. 회수 한도(16분)의 재료라 설정으로 흔들지 않는다 (P32 3절)
JWKS_CACHE_SECONDS = 30.0


def slot_private_key(signer: PlatformSigner, kid: str) -> ec.EllipticCurvePrivateKey:
    seed = signer.derive(SLOT_SALT, kid.encode(), SLOT_SEED_BYTES)
    d = int.from_bytes(seed, "big") % (P256_ORDER - 1) + 1
    return ec.derive_private_key(d, ec.SECP256R1())


def slot_jwk(signer: PlatformSigner, kid: str) -> dict[str, Any]:
    public_key = slot_private_key(signer, kid).public_key()
    return public_jwk(ECAlgorithm, public_key, kid=kid, alg=SLOT_ALG)


def sign_personal_token(signer: PlatformSigner, kid: str, claims: dict[str, Any]) -> str:
    """슬롯 kid 의 키로 서명한다. 토큰마다 키가 다르므로 회수는 그 키를 JWKS 에서 빼는 것이다."""
    return jwt.encode(
        claims,
        slot_private_key(signer, kid),
        algorithm=SLOT_ALG,
        headers={"kid": kid, "typ": ACCESS_TOKEN_TYP},
    )


class PatKeyRing:
    """플랫폼 JWKS 와 개인 토큰 슬롯의 pool. 라우트가 `jwks` 를, 리컨실이 `fill_pool` 을 부른다.
    mints 는 플랫폼이 사람 토큰을 내는가다."""

    def __init__(
        self,
        issuer: InternalIssuer,
        slots: MongoPatKeyStore,
        *,
        mints: bool,
        pool_size: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.issuer = issuer
        self.slots = slots
        self.mints = mints
        self.pool_size = pool_size
        self._bundles: TTLCache[str, dict[str, Any]] = TTLCache(JWKS_CACHE_SECONDS, clock=clock)
        # kid 별 공개키. 조립할 때마다 지금 JWKS 에 든 kid 만 남긴다
        self._slot_jwks: dict[str, dict[str, Any]] = {}
        # 조립 하나만 돌게 한다. 기다린 요청은 앞선 조립이 넣은 묶음을 받는다
        self._assembling = asyncio.Lock()

    async def jwks(self, *, service: bool = False) -> dict[str, Any]:
        """`/.well-known/jwks.json` 의 본문. 서명 키가 없으면 503 `signer_unavailable`, 슬롯을 읽다
        Mongo 오류가 나면 503 `jwks_unavailable` 이다. 그때 RSA 키만 든 200 을 내면 ToolHive 가 그
        묶음으로 바꿔 모든 개인 토큰을 15분 동안 거절한다. 503 이면 옛 묶음을 든다."""
        if not self.issuer.loaded:
            # home 클러스터가 없어 키를 못 읽은 배포본. 검증기는 503 을 일시 장애로 본다
            raise error(503, "signer_unavailable", "signing key not loaded")
        signer = self.issuer.signer
        if service or not self.mints:
            return signer.jwks()
        bundle = self._bundles.get(signer.kid)
        if bundle is not None:
            return bundle
        async with self._assembling:
            bundle = self._bundles.get(signer.kid)
            if bundle is None:
                bundle = {"keys": signer.jwks()["keys"] + await self._slot_keys(signer)}
                self._bundles.put(signer.kid, bundle)
        return bundle

    async def _slot_keys(self, signer: PlatformSigner) -> list[dict[str, Any]]:
        try:
            kids = await self.slots.kids(signer.kid)
        except PyMongoError as e:
            logger.warning("jwks: reading the personal token key slots failed: %s", e)
            raise error(503, "jwks_unavailable", "personal token key slots unavailable") from e
        known = self._slot_jwks
        missing = [kid for kid in kids if kid not in known]
        if missing:
            # 레플리카가 처음 조립할 때는 활성 토큰 수만큼 유도한다. 요청 루프를 막지 않는다
            known |= await run_in_threadpool(lambda: {k: slot_jwk(signer, k) for k in missing})
        self._slot_jwks = {kid: known[kid] for kid in kids}
        return list(self._slot_jwks.values())

    async def fill_pool(self, now: datetime) -> dict[str, list[str]]:
        """리컨실 단계. 플랫폼이 사람 토큰을 내면 지금 kid 의 pool 슬롯을 `pool_size` 개로 채운다.
        결과 키 `created` 는 새로 만든 kid 다."""
        if not (self.mints and self.issuer.loaded):
            return {}
        return {"created": await self.slots.fill_pool(self.issuer.kid, self.pool_size, now)}
