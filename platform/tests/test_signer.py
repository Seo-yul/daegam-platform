"""플랫폼 서명 키와 JWKS(auth/signer.py), local 모드 사람 토큰·검증기(auth/local.py).
디스커버리 문서는 test_service_token.py 와 test_oauth_metadata.py 가 본다."""

from __future__ import annotations

import re
import time

import jwt
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from daegam.auth.local import LocalVerifier, access_claims
from daegam.auth.oidc import AuthError
from daegam.auth.signer import (
    ALG,
    JWKS_PATH,
    SERVICE_JWKS_QUERY,
    SIGNING_SECRET,
    PlatformSigner,
)
from tests.helpers.consts import ISSUER
from tests.helpers.fake_secrets import FakeSecretStore
from tests.helpers.http import detail_code


def _claims(signer_ttl: int = 60, **over) -> dict:
    base = access_claims(
        issuer=ISSUER,
        audience="daegam-api",
        sub="u-1",
        ttl=signer_ttl,
        token_version=3,
        username="alice",
        email="alice@example.com",
        display_name="Alice",
    )
    return {**base, **over}


def test_key_is_created_once_and_reused():
    secrets = FakeSecretStore()
    first = PlatformSigner.load_or_create(secrets, namespace="daegam-system")
    assert secrets.get(SIGNING_SECRET, namespace="daegam-system") is not None
    second = PlatformSigner.load_or_create(secrets, namespace="daegam-system")
    assert first.kid == second.kid, "저장된 키를 다시 써야 한다"
    assert second.jwks()["keys"][0]["kid"] == first.kid
    assert PlatformSigner.generate().kid != first.kid


def test_first_writer_wins_when_two_replicas_race():
    """레플리카 둘이 동시에 처음 뜬다. 둘 다 키를 만들지만 Secret 은 create-if-absent 라 먼저 쓴
    쪽이 남고, 진 쪽은 저장된 키를 읽어 쓴다. SSA 로 쓰면 마지막 쓰기가 남아 둘이 파드 수명 내내
    갈린다."""

    class Racing(FakeSecretStore):
        def get(self, name, *, namespace):
            # A 가 "없다" 를 본 직후 B 가 먼저 써 버린 상황을 흉내낸다
            if not getattr(self, "_first_get_done", False):
                self._first_get_done = True
                pem = PlatformSigner.generate().private_pem()
                self.put(name, {"private.pem": pem}, namespace=namespace)
                return None
            return super().get(name, namespace=namespace)

    secrets = Racing()
    loser = PlatformSigner.load_or_create(secrets, namespace="daegam-system")
    stored = PlatformSigner(secrets.get(SIGNING_SECRET, namespace="daegam-system")["private.pem"])
    assert loser.kid == stored.kid, "진 쪽은 저장된 키를 써야 한다"


def test_sign_and_verify_roundtrip():
    signer = PlatformSigner.generate()
    tok = signer.sign(_claims())
    assert jwt.get_unverified_header(tok) == {"alg": ALG, "kid": signer.kid, "typ": "at+jwt"}
    got = LocalVerifier(signer, issuer=ISSUER, audience="daegam-api").verify(tok)
    assert (got["sub"], got["tv"], got["preferred_username"], got["name"]) == (
        "u-1",
        3,
        "alice",
        "Alice",
    )


def test_verifier_rejects_the_usual_suspects():
    signer = PlatformSigner.generate()
    v = LocalVerifier(signer, issuer=ISSUER, audience="daegam-api")
    with pytest.raises(AuthError, match="audience|Audience"):
        v.verify(signer.sign(_claims(aud="daegam-mcp")))
    with pytest.raises(AuthError, match="issuer|Issuer"):
        v.verify(signer.sign(_claims(iss="http://evil")))
    now = int(time.time())
    with pytest.raises(AuthError) as ei:
        v.verify(signer.sign(_claims(iat=now - 600, exp=now - 120)))
    assert ei.value.code == "token_expired"
    with pytest.raises(AuthError, match="kid"):
        v.verify(PlatformSigner.generate().sign(_claims()))
    # tv 가 없는 토큰은 우리가 낸 것이 아니다
    claims = _claims()
    del claims["tv"]
    with pytest.raises(AuthError, match="tv"):
        v.verify(signer.sign(claims))
    with pytest.raises(AuthError, match="malformed"):
        v.verify("not.a.jwt")


def test_jwks_verifies_the_token_like_a_remote_party_would():
    """런타임과 ToolHive 는 JWKS 만 보고 검증한다. 그 경로가 성립하는지 PyJWT 로 흉내낸다."""
    signer = PlatformSigner.generate()
    tok = signer.sign(_claims())
    key = jwt.PyJWK.from_dict(signer.jwks()["keys"][0])
    got = jwt.decode(tok, key.key, algorithms=[ALG], audience="daegam-api", issuer=ISSUER)
    assert got["sub"] == "u-1"


SERVICE_JWKS = f"{JWKS_PATH}?{SERVICE_JWKS_QUERY}"


def test_jwks_cache_headers_are_the_revocation_contract(local_root):
    """ToolHive 는 JWKS 를 max-age 뒤, 없으면 Expires 뒤, 둘 다 없으면 15분 뒤 다시 받고 헤더 값을
    15분 아래로 내리지 않는다. 회수 한도(16분)를 늘리는 것은 긴 max-age 나 먼 Expires 뿐이라
    max-age 는 900 이하이고 Expires 는 없다 (P32 3절). 서비스 토큰 검증기가 받는 `?use=service` 도
    같은 헤더다."""
    for r in (local_root.get(JWKS_PATH), local_root.get(SERVICE_JWKS)):
        assert r.status_code == 200
        cache = r.headers["cache-control"]
        max_age = re.search(r"max-age=(\d+)", cache)
        assert "public" in cache and max_age and int(max_age.group(1)) <= 900
        assert "expires" not in r.headers


def _break_slot_reads(app, monkeypatch) -> list[str]:
    """JWKS 가 슬롯을 읽을 때마다 Mongo 오류를 내게 한다. 읽으려 한 master 를 모은다."""
    calls: list[str] = []

    async def down(master: str) -> list[str]:
        calls.append(master)
        raise ServerSelectionTimeoutError("mongo is down")

    monkeypatch.setattr(app.state.pat_keys.slots, "kids", down)
    return calls


def test_jwks_is_503_without_cache_headers_when_the_slots_cannot_be_read(local_root, monkeypatch):
    """캐시가 비었는데 슬롯을 읽지 못하면 RSA 키만 든 200 이 아니라 503 `jwks_unavailable` 이다.
    그 200 을 ToolHive 가 받으면 모든 개인 토큰을 15분 동안 거절한다. 503 이면 옛 묶음을 든다
    (P32 4절). 캐시 헤더가 없어 중간 캐시도 이 답을 들지 않는다."""
    calls = _break_slot_reads(local_root.app, monkeypatch)
    r = local_root.get(JWKS_PATH)
    assert detail_code(r) == (503, "jwks_unavailable")
    assert "cache-control" not in r.headers and len(calls) == 1


def test_service_jwks_never_reads_mongo(local_root, monkeypatch):
    calls = _break_slot_reads(local_root.app, monkeypatch)
    r = local_root.get(SERVICE_JWKS)
    assert r.status_code == 200 and len(r.json()["keys"]) == 1 and calls == []
