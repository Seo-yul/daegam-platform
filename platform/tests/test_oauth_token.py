"""토큰 엔드포인트 (P33 8절~11절). 코드 교환, 갱신 토큰의 회전과 재사용, 갱신 때의 재확인, 접근
토큰의 클레임.

인가와 동의는 tests/helpers/oauth.py 의 도우미로 지난다. 테넌트는 노출 빌더의 것(acme)이다. 시간이
흐른 것은 Mongo 의 연결 문서를 고쳐 흉내 낸다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from daegam.services.oauth.audit import OAuthAudit
from daegam.services.oauth.errors import OAuthError
from daegam.services.oauth.tokens import MAX_LIVE_GRANTS, REUSE_GRACE
from daegam.store.base import hash_token, utcnow
from tests.helpers.consts import PUBLIC_URL
from tests.helpers.docs import TENANT, seed_gateway
from tests.helpers.oauth import (
    GW_HOST,
    GW_URL,
    REDIRECT,
    REGISTER,
    REGISTERED,
    TOKEN,
    VERIFIER,
    Person,
    approve,
    connect,
    exchange,
    grant_of,
    move_issuer,
    refresh,
    register,
    rotate_signing_key,
    token,
)


def error_of(r: httpx.Response, status: int = 400) -> str:
    assert r.status_code == status, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()["error"]


# -- 폼 (9절) ------------------------------------------------------------------------------------


def test_the_form_is_parsed_by_hand_and_never_echoed(oauth_client, client_id, member):
    """JSON 본문과 같은 인자 둘은 invalid_request 다. 오류가 받은 코드를 되풀이하지 않는다."""
    code = approve(oauth_client, client_id, member)
    r = oauth_client.post(TOKEN, json={"grant_type": "authorization_code", "code": code})
    assert error_of(r) == "invalid_request" and code not in r.text
    twice = f"grant_type=authorization_code&code={code}&code={code}"
    r = oauth_client.post(
        TOKEN, content=twice, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert error_of(r) == "invalid_request" and code not in r.text
    assert error_of(token(oauth_client)) == "invalid_request"
    assert error_of(token(oauth_client, grant_type="password")) == "unsupported_grant_type"
    r = exchange(oauth_client, client_id, code, code_verifier="w" * 64)
    assert error_of(r) == "invalid_grant" and code not in r.text


# -- 코드 교환 (8절) -------------------------------------------------------------------------------


def test_the_access_token_claims(oauth_client, client_id, connected, member, mongo):
    """aud 는 공유 audience 와 리소스, groups 는 테넌트 그룹 하나, sid 는 연결 id 다 (10절). 토큰
    요청의 resource 가 다르거나 없어도 aud 는 동의한 리소스다."""
    signer = oauth_client.app.state.internal_issuer.signer
    header = jwt.get_unverified_header(connected["access_token"])
    assert header == {"alg": "RS256", "typ": "at+jwt", "kid": signer.kid}
    claims = jwt.decode(
        connected["access_token"],
        signer.public_key(),
        algorithms=["RS256"],
        audience="daegam-mcp",
        issuer=PUBLIC_URL,
    )
    grant = grant_of(mongo, connected)
    assert claims["aud"] == ["daegam-mcp", GW_URL]
    assert claims["exp"] - claims["iat"] == 900
    assert (claims["sub"], claims["sid"], claims["tenant"], claims["groups"]) == (
        member.id,
        grant["_id"],
        TENANT,
        [f"daegam-{TENANT}"],
    )
    assert claims["client_id"] == grant["client_ref"] and claims["scope"] == "mcp"
    assert claims["preferred_username"] == "alice" and "tv" not in claims
    # local 의 그룹은 daegam-<tenant> 로 고정이다. 테넌트 설정에 다른 테넌트의 그룹이 저장돼 있어도
    # 갱신과 코드 교환이 낸 토큰은 연결의 테넌트 그룹이다
    mongo["tenant_settings"].insert_one(
        {"_id": TENANT, "tenant": TENANT, "mcp_group": "daegam-team-x"}
    )
    # 갱신은 요청의 resource 를 보지 않고 동의한 리소스로 낸다
    renewed = refresh(oauth_client, client_id, connected["refresh_token"], resource=PUBLIC_URL)
    claims = jwt.decode(renewed.json()["access_token"], options={"verify_signature": False})
    assert claims["groups"] == [f"daegam-{TENANT}"]
    assert claims["aud"] == ["daegam-mcp", GW_URL]
    # 코드 교환도 같다. 첫 로그인에 인가 서버 주소를 보내는 클라이언트를 넘긴다 (9절)
    for resource in (PUBLIC_URL, None):
        r = exchange(
            oauth_client, client_id, approve(oauth_client, client_id, member), resource=resource
        )
        assert r.status_code == 200, r.text
        claims = jwt.decode(r.json()["access_token"], options={"verify_signature": False})
        assert claims["aud"] == ["daegam-mcp", GW_URL]
        assert claims["groups"] == [f"daegam-{TENANT}"]


def test_a_client_without_refresh_gets_no_refresh_token(oauth_client, mongo, member):
    seed_gateway(mongo, "gw-1", GW_HOST)
    body = {"redirect_uris": [REGISTERED], "grant_types": ["authorization_code"]}
    cid = oauth_client.post(REGISTER, json=body).json()["client_id"]
    issued = connect(oauth_client, cid, member)
    assert "refresh_token" not in issued and issued["scope"] == "mcp"
    grant = grant_of(mongo, issued)
    assert grant["refresh_sha256"] is None
    assert grant["refresh_expires_at"] - grant["created_at"] == timedelta(seconds=900)


def test_pkce_and_the_binding_to_client_and_redirect(oauth_client, client_id, member, mongo):
    """verifier 의 꼴이 틀리면 코드를 쓰기 전에 invalid_request 다. 맞지 않는 verifier, 다른
    redirect_uri 와 client_id 는 invalid_grant 이고 그 코드는 다시 쓸 수 없다."""
    other = register(oauth_client, "Other")
    code = approve(oauth_client, client_id, member)
    for verifier in ("v" * 42, "v" * 129, "v" * 42 + "!"):
        r = exchange(oauth_client, client_id, code, code_verifier=verifier)
        assert error_of(r) == "invalid_request", verifier
    for bad in (
        {"code_verifier": "w" * 64},
        {"redirect_uri": "http://localhost:53112/other"},
        {"client_id": other},
    ):
        code = approve(oauth_client, client_id, member)
        assert error_of(exchange(oauth_client, client_id, code, **bad)) == "invalid_grant", bad
        assert error_of(exchange(oauth_client, client_id, code)) == "invalid_grant", bad
    assert mongo["oauth_grants"].count_documents({}) == 0
    tampered = client_id[:-2] + ("AA" if not client_id.endswith("AA") else "BB")
    r = exchange(oauth_client, tampered, approve(oauth_client, client_id, member))
    assert error_of(r, 401) == "invalid_client"


def test_two_exchanges_of_one_code_leave_no_live_connection(oauth_client, client_id, member, mongo):
    """재사용이 첫 교환이 연결을 넣기 전에 와도 그 연결은 끊긴다. 둘 가운데 하나는 코드 재사용으로
    거절되고, 토큰을 받은 쪽이 있어도 그 연결은 code_reuse 로 끊겨 있다."""
    tokens = oauth_client.app.state.oauth_tokens
    code = approve(oauth_client, client_id, member)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": VERIFIER,
    }

    async def both():
        calls = (tokens.token(form, OAuthAudit()), tokens.token(form, OAuthAudit()))
        return await asyncio.gather(*calls, return_exceptions=True)

    results = oauth_client.portal.call(both)
    failed = [r for r in results if isinstance(r, OAuthError)]
    assert failed and all(e.audit_code == "code_reuse" for e in failed), results
    (grant,) = mongo["oauth_grants"].find()
    assert grant["revoked_reason"] == "code_reuse" and grant["refresh_sha256"] is None


def test_an_expired_code_is_rejected(oauth_client, client_id, member, mongo):
    code = approve(oauth_client, client_id, member)
    mongo["oauth_codes"].update_many({}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    assert error_of(exchange(oauth_client, client_id, code)) == "invalid_grant"


def test_the_exchange_rechecks_the_person(oauth_client, client_id, member, mongo):
    """허용 뒤 비밀번호 판이 바뀌었으면 코드로 연결을 만들지 않는다."""
    code = approve(oauth_client, client_id, member)
    mongo["users"].update_one({"_id": member.id}, {"$inc": {"token_version": 1}})
    assert error_of(exchange(oauth_client, client_id, code)) == "invalid_grant"
    assert mongo["oauth_grants"].count_documents({}) == 0


def test_a_person_keeps_at_most_fifty_live_connections(
    oauth_client, connected, client_id, member, mongo
):
    """51번째 연결을 만들면 마지막 갱신이 가장 오래된 연결이 퇴출된다 (11절)."""
    base = grant_of(mongo, connected)
    now = utcnow()
    mongo["oauth_grants"].insert_many(
        [
            {
                **base,
                "_id": f"old-{i}",
                "family_id": f"{i:032x}",
                "last_refresh_at": now - timedelta(hours=i + 1),
            }
            for i in range(MAX_LIVE_GRANTS - 1)
        ]
    )
    connect(oauth_client, client_id, member)
    live = {"sub": member.id, "revoked_at": None}
    assert mongo["oauth_grants"].count_documents(live) == MAX_LIVE_GRANTS
    oldest = mongo["oauth_grants"].find_one({"_id": f"old-{MAX_LIVE_GRANTS - 2}"})
    assert oldest["revoked_reason"] == "evicted"


# -- 갱신 (11절) ----------------------------------------------------------------------------------


def test_refresh_rotates_the_family(oauth_client, client_id, connected, member, mongo):
    """값은 <family_id>.<secret> 이고 세대가 하나 오른다. 가족 문서에는 해시 둘만 있다."""
    # 코드 교환이 낸 접근 토큰과 갱신 토큰, 그리고 연결 문서
    assert connected["token_type"] == "Bearer" and connected["expires_in"] == 900
    assert connected["scope"] == "mcp offline_access"
    grant = grant_of(mongo, connected)
    family, secret = connected["refresh_token"].split(".")
    assert grant["family_id"] == family and grant["_id"] != family
    assert grant["refresh_sha256"] == hash_token(secret) and grant["refresh_generation"] == 0
    assert (grant["sub"], grant["tenant"], grant["resource"], grant["resource_kind"]) == (
        member.id,
        TENANT,
        GW_URL,
        "gateway",
    )
    assert grant["client_id"] == client_id and grant["client_ref"].startswith("dcr:")
    assert grant["expires_at"] - grant["created_at"] == timedelta(days=30)
    assert grant["refresh_expires_at"] - grant["created_at"] == timedelta(days=14)
    # 코드에는 교환 순간의 연결 id 가 찍혀 있다
    code = mongo["oauth_codes"].find_one()
    assert code["used_at"] is not None and code["grant_id"] == grant["_id"]
    r = refresh(oauth_client, client_id, connected["refresh_token"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    renewed = r.json()
    family, secret = renewed["refresh_token"].split(".")
    grant = grant_of(mongo, renewed)
    assert family == grant["family_id"] == connected["refresh_token"].split(".")[0]
    assert grant["refresh_generation"] == 1
    assert grant["refresh_sha256"] == hash_token(secret)
    assert grant["prev_refresh_sha256"] == hash_token(connected["refresh_token"].split(".")[1])
    assert grant["last_refresh_at"] == grant["prev_rotated_at"]
    assert secret not in str(grant) and renewed["scope"] == "mcp offline_access"


def test_refresh_needs_the_same_client(oauth_client, client_id, connected, mongo):
    other = register(oauth_client, "Other")
    assert error_of(refresh(oauth_client, other, connected["refresh_token"])) == "invalid_grant"
    assert grant_of(mongo, connected)["revoked_at"] is None
    assert refresh(oauth_client, client_id, connected["refresh_token"]).status_code == 200


def test_any_other_secret_is_reuse_and_revokes_the_family(
    oauth_client, client_id, connected, mongo
):
    """유예가 지난 앞 세대는 지금 세대도 유예 안의 앞 세대도 아니므로 재사용이다. 더 오래된 세대와
    모르는 비밀도 같은 갈래다."""
    first = connected["refresh_token"]
    refresh(oauth_client, client_id, first)
    mongo["oauth_grants"].update_one(
        {"family_id": first.split(".")[0]},
        {"$set": {"prev_rotated_at": utcnow() - REUSE_GRACE - timedelta(seconds=1)}},
    )
    assert error_of(refresh(oauth_client, client_id, first)) == "invalid_grant"
    grant = grant_of(mongo, connected)
    assert grant["revoked_reason"] == "refresh_reuse"
    assert grant["refresh_sha256"] is None and grant["prev_refresh_sha256"] is None


def test_an_unknown_family_or_a_grant_id_revokes_nothing(oauth_client, client_id, connected, mongo):
    """가족은 갱신 토큰 안에만 있다. 화면과 감사에 보이는 연결 id 로는 가족을 찾지 못한다."""
    grant = grant_of(mongo, connected)
    secret = connected["refresh_token"].split(".")[1]
    for value in (
        f"{'0' * 32}.{secret}",
        f"{grant['_id']}.{secret}",
        f"{grant['_id'].replace('-', '')}.{secret}",
        "garbage",
    ):
        assert error_of(refresh(oauth_client, client_id, value)) == "invalid_grant", value
    assert grant_of(mongo, connected)["revoked_at"] is None


def test_two_refreshes_with_one_token_yield_one_new_token(
    oauth_client, client_id, connected, mongo
):
    tokens = oauth_client.app.state.oauth_tokens
    form = {
        "grant_type": "refresh_token",
        "refresh_token": connected["refresh_token"],
        "client_id": client_id,
    }

    async def both():
        calls = (tokens.token(form, OAuthAudit()), tokens.token(form, OAuthAudit()))
        return await asyncio.gather(*calls, return_exceptions=True)

    results = oauth_client.portal.call(both)
    issued = [r for r in results if isinstance(r, dict)]
    failed = [r for r in results if isinstance(r, OAuthError)]
    assert len(issued) == 1 and len(failed) == 1, results
    assert failed[0].code == "invalid_grant" and failed[0].audit_code == "refresh_reuse_grace"
    assert grant_of(mongo, connected)["refresh_generation"] == 1
    # 유예 안의 앞 세대는 그 요청만 거절하고 가족은 산다
    assert grant_of(mongo, connected)["revoked_at"] is None
    assert refresh(oauth_client, client_id, issued[0]["refresh_token"]).status_code == 200


def test_scope_can_only_narrow(oauth_client, client_id, connected):
    r = refresh(oauth_client, client_id, connected["refresh_token"], scope="mcp")
    assert r.status_code == 200 and r.json()["scope"] == "mcp", r.text
    r = refresh(oauth_client, client_id, r.json()["refresh_token"], scope="mcp admin")
    assert error_of(r) == "invalid_scope"


def _leave(c: TestClient, mongo, who: Person) -> None:
    mongo["memberships"].delete_many({"sub": who.id, "tenant": TENANT})


def _move_issuer(c: TestClient, mongo, who: Person) -> None:
    move_issuer(c)


def _rotate_key(c: TestClient, mongo, who: Person) -> None:
    rotate_signing_key(c)


def _unexpose(c: TestClient, mongo, who: Person) -> None:
    mongo["gateways"].update_one({"_id": "gw-1"}, {"$set": {"exposure.enabled": False}})


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_leave, "membership"),
        (_move_issuer, "issuer_changed"),
        (_rotate_key, "key_rotated"),
        (_unexpose, "resource_gone"),
    ],
)
def test_refresh_rechecks_the_connection(
    oauth_client, client_id, connected, member, mongo, change, reason
):
    """훅을 거치지 않고 바뀐 것도 갱신이 잡아 연결을 끊는다. 막기와 비밀번호 바꾸기의 사유는
    test_access_revocation.py 의 OAuth 연결 시험이 본다."""
    change(oauth_client, mongo, member)
    assert error_of(refresh(oauth_client, client_id, connected["refresh_token"])) == (
        "invalid_grant"
    )
    assert grant_of(mongo, connected)["revoked_reason"] == reason


def test_an_idle_connection_ends(oauth_client, client_id, connected, mongo):
    """갱신 토큰 하나는 14일이다. 쓰지 않으면 연결이 끝난다."""
    mongo["oauth_grants"].update_many(
        {}, {"$set": {"refresh_expires_at": utcnow() - timedelta(seconds=1)}}
    )
    assert error_of(refresh(oauth_client, client_id, connected["refresh_token"])) == (
        "invalid_grant"
    )
    assert grant_of(mongo, connected)["revoked_at"] is None
