"""개인 MCP 토큰 API (P32 5절·6절). 발급·목록·회수, 비밀번호 재확인과 잠금, 소속, 상한, ready_at.

계정 사건의 훅과 접근 정리는 test_access_revocation.py 가 본다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from daegam.auth.pat_keys import slot_jwk
from daegam.config import LIFETIME_DAYS_LIMIT
from daegam.services import personal_tokens as pat
from daegam.store.base import utcnow
from daegam.store.login_throttle import MAX_FAILURES
from daegam.store.pat_keys import BOUND, WARMUP
from tests.helpers.auth import LOCAL_BOOT_PASSWORD, bearer, login
from tests.helpers.consts import ISSUER
from tests.helpers.http import detail_code
from tests.helpers.oauth import (
    OTHER_TEAM,
    PAT_TENANT,
    Person,
    fresh_slot_kids,
    issue,
    issued,
    person,
)


def warm_pool(c: TestClient, mongo) -> None:
    """리컨실이 pool 을 채우고 20분이 지난 것처럼 둔다."""
    c.portal.call(c.app.state.pat_keys.fill_pool, utcnow())
    mongo["pat_keys"].update_many({}, {"$set": {"created_at": utcnow() - WARMUP * 2}})


@pytest.fixture
def alice(local_client, admin, mongo) -> Person:
    return person(local_client, admin, mongo, "alice", PAT_TENANT)


# -- 발급 ----------------------------------------------------------------------------------


def test_token_shape_and_one_time_value(local_client, alice, mongo):
    """ES256 이고 토큰마다 다른 슬롯 kid 다. groups 는 테넌트 그룹 하나이고 tv 가 없다. 평문은 발급
    응답에만 있고 Mongo 에는 토큰도 해시도 없다 (P32 5절)."""
    warm_pool(local_client, mongo)
    body = issued(local_client, alice)
    token = body.pop("token")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256" and header["typ"] == "at+jwt"
    assert header["kid"].startswith("pat-")
    # ToolHive 가 하는 그대로 JWKS 의 키로 검증한다
    key = slot_jwk(local_client.app.state.internal_issuer.signer, header["kid"])
    claims = jwt.decode(
        token,
        jwt.PyJWK.from_dict(key).key,
        algorithms=["ES256"],
        audience="daegam-mcp",
        issuer=ISSUER,
    )
    assert claims["groups"] == [f"daegam-{PAT_TENANT}"] and claims["tenant"] == PAT_TENANT
    assert claims["sub"] == alice.id and claims["preferred_username"] == "alice"
    assert claims["jti"] == body["token_id"] and claims["scope"] == "mcp"
    assert "tv" not in claims and "cid" not in claims
    assert body["status"] == "active" and body["ready_at"] is None
    assert body["group"] == f"daegam-{PAT_TENANT}" and body["label"] == "laptop"
    exp = datetime.fromtimestamp(claims["exp"], UTC)
    assert datetime.fromisoformat(body["expires_at"]) == exp
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    assert doc["kid"] == header["kid"] and token not in str(doc)
    # 발급 응답에만 평문이 있다. 목록에는 없다
    [listed] = local_client.get("/me/tokens", headers=alice.headers).json()
    assert "token" not in listed and listed["token_id"] == body["token_id"]
    # 만료는 1일부터 DAEGAM_PAT_MAX_DAYS 까지이고 화면은 그 상한을 /auth/config 로 읽는다
    max_days = local_client.get("/auth/config").json()["personal_tokens"]["max_days"]
    assert max_days == LIFETIME_DAYS_LIMIT
    for days in (1, max_days):
        assert issue(local_client, alice, days=days).status_code == 201, days


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"days": 366}, "expires_at"),
        ({"label": "x" * 65}, "label"),
    ],
)
def test_label_and_expiry_are_checked(local_client, alice, kw, field):
    r = issue(local_client, alice, **kw)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_value" and r.json()["detail"]["field"] == field


def test_wrong_password_is_403_and_counts_toward_the_login_lock(local_client, alice, mongo):
    """401 이면 화면의 401 훅이 세션을 지우므로 403 이다. 틀린 비밀번호는 로그인 잠금에 세고, 잠기면
    맞는 비밀번호도 429 login_locked 다 (P32 6절)."""
    for _ in range(MAX_FAILURES - 1):
        r = issue(local_client, alice, password="not my password")
        assert detail_code(r) == (403, "invalid_credentials")
    r = issue(local_client, alice, password="not my password")
    assert detail_code(r) == (429, "login_locked")
    r = issue(local_client, alice)
    assert r.status_code == 429 and r.headers["Retry-After"]
    assert login(local_client, "alice", alice.password).status_code == 429
    assert mongo["personal_tokens"].count_documents({}) == 0


def test_only_a_member_of_the_tenant_gets_its_token(local_client, admin, alice, mongo):
    """소속 줄이 있어야 한다. 역할은 묻지 않는다. platform-admin 의 `*` 는 소속이 아니다."""
    r = issue(local_client, alice, tenant=OTHER_TEAM)
    assert detail_code(r) == (403, "not_a_member")
    root = Person("admin", "admin", LOCAL_BOOT_PASSWORD, admin)
    for tenant in (PAT_TENANT, "*"):
        r = issue(local_client, root, tenant=tenant)
        assert detail_code(r) == (403, "not_a_member"), r.text


def test_three_limits(local_client, alice, mongo, monkeypatch):
    """한 사람의 활성 토큰, 한 사람의 한 시간 발급, 플랫폼 전체 활성 토큰. 넘으면 429
    token_limit 이고 시간이 지나면 풀리는 상한만 Retry-After(초)를 싣는다 (P32 4절)."""
    now = utcnow()

    def doc(i: int, **kw) -> dict:
        base = {
            "_id": f"t{i}",
            "sub": alice.id,
            "username": "alice",
            "tenant": PAT_TENANT,
            "group": f"daegam-{PAT_TENANT}",
            "label": "x",
            "kid": f"pat-{i:016x}",
            "iss": ISSUER,
            "tv": 0,
            "created_at": now - timedelta(days=1),
            "expires_at": now + timedelta(days=1),
            "revoked_at": None,
            "revoke_requested_at": None,
            "purge_at": now + timedelta(days=31),
        }
        return {**base, **kw}

    mongo["personal_tokens"].insert_many([doc(i) for i in range(pat.MAX_LIVE_PER_PERSON)])
    r = issue(local_client, alice)
    assert r.status_code == 429 and r.json()["detail"]["limit"] == "person"
    assert "Retry-After" not in r.headers
    # 회수한 토큰은 활성이 아니지만 지난 한 시간의 발급으로는 센다
    mongo["personal_tokens"].delete_many({})
    oldest = now - timedelta(minutes=40)
    recent = [
        doc(i, created_at=oldest + timedelta(minutes=i), revoked_at=now, revoke_requested_at=now)
        for i in range(pat.MAX_ISSUED_PER_WINDOW)
    ]
    mongo["personal_tokens"].insert_many(recent)
    r = issue(local_client, alice)
    assert r.status_code == 429 and r.json()["detail"]["limit"] == "hourly"
    assert 19 * 60 <= int(r.headers["Retry-After"]) <= 20 * 60 + 1
    mongo["personal_tokens"].delete_many({})
    monkeypatch.setattr(pat, "MAX_LIVE_TOTAL", 2)
    mongo["personal_tokens"].insert_many([doc(i, sub=f"other-{i}") for i in range(2)])
    r = issue(local_client, alice)
    assert r.status_code == 429 and r.json()["detail"]["limit"] == "platform"


def test_concurrent_issues_do_not_overshoot_the_limit(local_client, alice, mongo, monkeypatch):
    """동시에 온 발급이 모두 앞의 검사를 지나도 문서를 넣은 뒤 다시 세어 넘친 것을 되돌린다. 되돌린
    발급은 문서도 슬롯도 남기지 않는다 (P32 4절)."""
    monkeypatch.setattr(pat, "MAX_LIVE_PER_PERSON", 2)
    svc = local_client.app.state.personal_tokens
    racers = 4
    # 모두 앞의 검사를 지난 뒤에야 넣는다. 앞의 검사만 있으면 넷이 다 발급된다
    barrier = asyncio.Barrier(racers)
    insert = svc.tokens.insert

    async def gated(token):
        await barrier.wait()
        await insert(token)

    monkeypatch.setattr(svc.tokens, "insert", gated)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    async def one() -> str:
        try:
            await svc.issue(
                alice.id,
                tenant=PAT_TENANT,
                label="race",
                expires_at=expires_at,
                password=alice.password,
            )
        except pat.PersonalTokenError as e:
            return e.code
        return "issued"

    async def race() -> list[str]:
        return await asyncio.gather(*(one() for _ in range(racers)))

    results = local_client.portal.call(race)
    issued_n = results.count("issued")
    assert issued_n <= pat.MAX_LIVE_PER_PERSON
    assert results.count("token_limit") == racers - issued_n
    assert mongo["personal_tokens"].count_documents({"sub": alice.id}) == issued_n
    assert mongo["pat_keys"].count_documents({"state": BOUND}) == issued_n


# -- 목록과 회수 ---------------------------------------------------------------------------


def test_revoke_removes_the_key_from_jwks(local_client, alice, mongo):
    before = utcnow()
    body = issued(local_client, alice)
    # 따뜻한 슬롯이 없어도 발급은 거절하지 않는다. pool 이 비어 새로 만든 슬롯은 20분 뒤부터 쓸 수
    # 있고 그동안 목록의 상태는 pending 이다 (P32 4절)
    ready = datetime.fromisoformat(body["ready_at"]).replace(tzinfo=None)
    assert before + WARMUP <= ready <= utcnow() + WARMUP
    assert body["status"] == "pending"
    kid = mongo["personal_tokens"].find_one({"_id": body["token_id"]})["kid"]
    assert kid in fresh_slot_kids(local_client)
    r = local_client.delete(f"/me/tokens/{body['token_id']}", headers=alice.headers)
    assert r.status_code == 200 and r.json() == {"token_id": body["token_id"], "revoked": True}
    assert kid not in fresh_slot_kids(local_client)
    [listed] = local_client.get("/me/tokens", headers=alice.headers).json()
    assert listed["status"] == "revoked" and listed["revoke_reason"] == "user"
    assert listed["revoked_at"]
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    assert doc["revoked_by"] == alice.id
    # 다시 불러도 같다. 처음 찍은 시각을 덮지 않는다
    again = local_client.delete(f"/me/tokens/{body['token_id']}", headers=alice.headers)
    assert again.status_code == 200
    assert (
        mongo["personal_tokens"].find_one({"_id": body["token_id"]})["revoked_at"]
        == (doc["revoked_at"])
    )
    # 발급에는 경로 인자가 없다. 핸들러가 둔 토큰 id 와 테넌트가 감사 행에 들어 테넌트 감사
    # 화면에 잡힌다 (P32 12절). 평문과 비밀번호는 남지 않는다
    rows = list(mongo["audit_events"].find({"resource": "me"}).sort("ts", 1))
    assert [(e["action"], e["resource_id"], e["tenant"]) for e in rows] == [
        ("issue_my_token", body["token_id"], PAT_TENANT),
        ("revoke_my_token", body["token_id"], PAT_TENANT),
        ("revoke_my_token", body["token_id"], PAT_TENANT),
    ]
    assert alice.password not in str(rows) and body["token"] not in str(rows)


def test_someone_elses_token_is_404(local_client, admin, alice, mongo):
    bob = person(local_client, admin, mongo, "bob", PAT_TENANT)
    body = issued(local_client, alice)
    for path in (f"/me/tokens/{body['token_id']}", "/me/tokens/nope"):
        r = local_client.delete(path, headers=bob.headers)
        assert detail_code(r) == (404, "not_found")
    assert local_client.get("/me/tokens", headers=bob.headers).json() == []
    assert mongo["personal_tokens"].find_one({"_id": body["token_id"]})["revoked_at"] is None


def test_admin_lists_and_revokes_a_persons_tokens(local_client, admin, alice, mongo):
    body = issued(local_client, alice)
    r = local_client.get(f"/users/{alice.id}/tokens", headers=admin)
    assert r.status_code == 200 and [t["token_id"] for t in r.json()] == [body["token_id"]]
    assert local_client.get("/users/nope/tokens", headers=admin).status_code == 404
    # 다른 사람의 경로로는 회수하지 못한다
    r = local_client.delete(f"/users/nope/tokens/{body['token_id']}", headers=admin)
    assert r.status_code == 404
    r = local_client.delete(f"/users/{alice.id}/tokens/{body['token_id']}", headers=admin)
    assert r.status_code == 200 and r.json()["revoked"] is True
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    assert doc["revoke_reason"] == "admin" and doc["revoked_by"] != alice.id
    # 사용자 관리는 users:write 다
    assert local_client.get(f"/users/{alice.id}/tokens", headers=alice.headers).status_code == 403


# -- 경계 ----------------------------------------------------------------------------------


def test_control_plane_rejects_a_personal_token(local_client, alice):
    """aud·kid·tv 가 모두 제어면 토큰과 다르다 (P32 5절)."""
    token = issued(local_client, alice)["token"]
    r = local_client.get("/me", headers=bearer(token))
    assert r.status_code == 401


def test_personal_tokens_exist_only_in_local_mode(client):
    assert client.get("/me/tokens").status_code == 404
    assert client.get("/users/u/tokens").status_code == 404
    assert "personal_tokens" not in client.get("/auth/config").json()
