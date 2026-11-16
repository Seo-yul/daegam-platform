"""P47 6.2절: Bot Connector 토큰의 검증 (`OIDCVerifier.verify_connector`).

공유 검증기의 키 받기를 쓰고 규칙은 따로다. RS256 만, 300초 여유, `iss`·`aud`·`exp`·`nbf`·
`serviceurl` 이 모두 있어야 한다. 서명한 키의 JWK 원문을 같은 스냅샷에서 돌려주고, 키 묶음을 받지
못하면 503 이다. F0.1 의 OpenID 문서와 키 묶음 고정물이 이 규칙의 근거다.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from daegam.auth.oidc import (
    CONNECTOR_ALGORITHMS,
    AuthError,
    OIDCVerifier,
)
from tests.helpers.oidc import Signer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "teams"
APP_ID = "c9e8c047-2a74-40a2-b28a-b162d5f5327c"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# 검증기의 발급자와 키 묶음 주소는 Microsoft 의 OpenID 문서(F0.1 고정물) 그대로다
OPENID = fixture("connector-openid.json")
ISSUER = OPENID["issuer"]
KEYS_URL = OPENID["jwks_uri"]


@dataclass
class Keys:
    """키 묶음 하나를 내는 Microsoft. Microsoft 의 꼴(`alg` 없음, `endorsements` 배열)이다."""

    signer: Signer = field(default_factory=lambda: Signer(None))
    down: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down or str(request.url) != KEYS_URL:
            return httpx.Response(502)
        jwk = dict(self.signer.jwks()["keys"][0])
        jwk.pop("alg", None)
        jwk["endorsements"] = ["msteams", "skype"]
        return httpx.Response(200, json={"keys": [jwk]})


def token(signer: Signer, **claims: Any) -> str:
    """Connector 가 낸 꼴의 토큰. 값이 None 인 클레임은 뺀다."""
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": ISSUER,
        "aud": APP_ID,
        "nbf": now - 10,
        "exp": now + 3600,
        "serviceurl": SERVICE_URL,
        **claims,
    }
    return signer.sign({k: v for k, v in body.items() if v is not None})


def _verifier(keys: Keys) -> OIDCVerifier:
    return OIDCVerifier(
        ISSUER,
        APP_ID,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(keys.handle)),
        jwks_url=KEYS_URL,
    )


def _check(keys: Keys, raw: str):
    return asyncio.run(_verifier(keys).verify_connector(raw))


@pytest.fixture
def fake() -> Keys:
    return Keys()


def test_the_rules_map_to_microsofts_published_documents():
    """F0.1: OpenID 문서의 알고리즘과 키 묶음의 `endorsements` 가 규칙과 맞는다."""
    assert OPENID["id_token_signing_alg_values_supported"] == CONNECTOR_ALGORITHMS
    keys = fixture("connector-keys-excerpt.json")["keys"]
    assert all(isinstance(k["endorsements"], list) and "alg" not in k for k in keys)
    assert ["msteams" in k["endorsements"] for k in keys] == [True, False]


def test_the_published_keys_parse_and_keep_their_document():
    """Microsoft 의 키 모양(`alg` 없는 RSA)이 파싱되고 원문이 키와 함께 남는다."""
    verifier = OIDCVerifier(ISSUER, APP_ID, jwks_url=KEYS_URL)
    keys, docs = verifier._parse_keys(fixture("connector-keys-excerpt.json"))
    assert set(keys) == set(docs) and len(keys) == 2
    assert all(docs[kid]["kid"] == kid for kid in docs)


def test_a_connector_token_verifies_with_its_key_document(fake):
    found = _check(fake, token(fake.signer))
    assert found.service_url == SERVICE_URL
    assert found.endorsements == ["msteams", "skype"]
    assert found.jwk["kid"] == fake.signer.kid


@pytest.mark.parametrize("missing", ["iss", "aud", "exp", "nbf", "serviceurl"])
def test_every_required_claim_must_be_there(fake, missing):
    with pytest.raises(AuthError) as e:
        _check(fake, token(fake.signer, **{missing: None}))
    assert e.value.status == 401


def test_the_service_url_claim_is_case_sensitive(fake):
    """문서가 쓰는 `serviceUrl` 은 SDK 가 읽는 `serviceurl` 이 아니다 (F0.1)."""
    raw = token(fake.signer, serviceurl=None, serviceUrl=SERVICE_URL)
    with pytest.raises(AuthError):
        _check(fake, raw)


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-app"},
        # Emulator 가 쓰는 Entra 토큰의 발급자
        {"iss": "https://sts.windows.net/72f988bf-86f1-41af-91ab-2d7cd011db47/"},
        {"iss": "https://login.microsoftonline.com/72f988bf/v2.0"},
    ],
)
def test_other_audiences_and_entra_issuers_are_refused(fake, claims):
    with pytest.raises(AuthError) as e:
        _check(fake, token(fake.signer, **claims))
    assert e.value.status == 401


def test_only_rs256_is_accepted(fake):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": APP_ID,
        "nbf": now,
        "exp": now + 60,
        "serviceurl": SERVICE_URL,
    }
    raw = pyjwt.encode(
        claims, fake.signer.private_key, algorithm="RS384", headers={"kid": fake.signer.kid}
    )
    with pytest.raises(AuthError, match="unsupported alg"):
        _check(fake, raw)


def test_the_leeway_is_five_minutes(fake):
    now = int(time.time())
    assert _check(fake, token(fake.signer, exp=now - 200))
    with pytest.raises(AuthError) as e:
        _check(fake, token(fake.signer, exp=now - 400))
    assert e.value.code == "token_expired"
    with pytest.raises(AuthError):
        _check(fake, token(fake.signer, nbf=now + 400))


def test_a_key_set_that_cannot_be_fetched_is_503(fake):
    fake.down = True
    with pytest.raises(AuthError) as e:
        _check(fake, token(fake.signer))
    assert e.value.status == 503


def test_a_token_of_another_key_is_401(fake):
    with pytest.raises(AuthError) as e:
        _check(fake, token(Signer(None)))
    assert e.value.status == 401
