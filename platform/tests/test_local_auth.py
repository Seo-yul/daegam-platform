"""auth_mode=local (D40): 로그인·토큰·비밀번호·사용자 관리·디스커버리."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import jwt
from fastapi.testclient import TestClient

from daegam.auth.oidc import OIDCVerifier
from daegam.config import Settings
from daegam.store.base import utcnow
from daegam.store.login_throttle import LOCK, MAX_FAILURES
from tests.helpers.auth import LOCAL_BOOT_PASSWORD, admin_headers, bearer, login, slot_keys
from tests.helpers.consts import ISSUER, PLATFORM_JWKS
from tests.helpers.http import detail_code
from tests.helpers.oauth import NEW_PASSWORD, make_user, password_of

# -- 로그인 ------------------------------------------------------------------------


def test_wrong_password_and_unknown_user_look_the_same(local_client, mongo):
    """틀린 비밀번호와 없는 사용자는 같은 401 이다. 실패한 로그인은 주체가 없어 sub 가 비고, 시도한
    이름은 앞뒤 공백을 지우고 소문자로 바꿔 감사 API 의 login_username 에 남는다."""
    wrong = login(local_client, "admin", "definitely not it")
    unknown = login(local_client, " Mallory ", "definitely not it")
    for r in (wrong, unknown):
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["code"] == "invalid_credentials"
        assert 'error="invalid_credentials"' in r.headers["WWW-Authenticate"]
    assert wrong.json() == unknown.json()
    r = local_client.get(
        "/audit", params={"action": "login"}, headers=admin_headers(local_client, mongo)
    )
    assert r.status_code == 200, r.text
    failed = [e for e in r.json() if e["result"] == "error"]
    assert sorted((e["login_username"], e["sub"]) for e in failed) == [
        ("admin", None),
        ("mallory", None),
    ]


def test_a_lock_or_window_that_ended_before_the_ttl_sweep_starts_over(local_client, mongo):
    """다섯 번째 틀린 시도가 그 사용자 이름만 잠근다. Retry-After 는 개인 토큰 발급 상한과 같은
    꼴(남은 초)이다. TTL 인덱스는 끝난 문서를 1분쯤 늦게 지운다. 그 사이에 틀리면 지난 잠금
    때문에 429 가 되거나 끝난 창의 실패 수를 이어받지 않고, 새 창의 첫 실패로 센다."""
    for _ in range(MAX_FAILURES - 1):
        assert login(local_client, "admin", "wrong wrong wrong").status_code == 401
    fifth = login(local_client, "admin", "wrong wrong wrong")
    assert detail_code(fifth) == (429, "login_locked")
    assert LOCK.total_seconds() - 60 <= int(fifth.headers["Retry-After"]) <= LOCK.total_seconds()
    assert login(local_client, "someone-else", "x" * 12).status_code == 401
    past = utcnow() - timedelta(seconds=1)
    ended = {"$set": {"locked_until": past, "expires_at": past}}
    mongo["login_failures"].update_one({"_id": "admin"}, ended)
    r = login(local_client, "admin", "wrong wrong wrong")
    assert detail_code(r) == (401, "invalid_credentials")
    doc = mongo["login_failures"].find_one({"_id": "admin"})
    assert doc["count"] == 1 and "locked_until" not in doc
    # 잠금 없이 창만 끝난 문서도 같다. 끝난 창의 넷에 하나를 더해 잠그지 않는다
    for _ in range(MAX_FAILURES - 2):
        login(local_client, "admin", "wrong wrong wrong")
    mongo["login_failures"].update_one({"_id": "admin"}, {"$set": {"expires_at": past}})
    assert login(local_client, "admin", "wrong wrong wrong").status_code == 401
    assert mongo["login_failures"].find_one({"_id": "admin"})["count"] == 1
    assert login(local_client, "admin", LOCAL_BOOT_PASSWORD).status_code == 200


def test_change_password_kills_old_tokens(local_client, mongo):
    old = admin_headers(local_client, mongo)
    r = local_client.post(
        "/auth/password",
        json={"current_password": "not the current one", "new_password": NEW_PASSWORD},
        headers=old,
    )
    assert detail_code(r) == (401, "invalid_credentials")
    r = local_client.post(
        "/auth/password",
        json={"current_password": LOCAL_BOOT_PASSWORD, "new_password": "short"},
        headers=old,
    )
    assert detail_code(r) == (422, "weak_password")
    r = local_client.post(
        "/auth/password",
        json={"current_password": LOCAL_BOOT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=old,
    )
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is False
    fresh = bearer(r.json()["access_token"])
    # 옛 토큰은 tv 가 달라 죽고 새 토큰은 산다
    assert local_client.get("/me", headers=old).status_code == 401
    assert local_client.get("/me", headers=fresh).status_code == 200
    assert login(local_client, "admin", LOCAL_BOOT_PASSWORD).status_code == 401
    assert login(local_client, "admin", NEW_PASSWORD).status_code == 200


# -- 사용자 관리 -------------------------------------------------------------------------


def test_user_crud_and_roles(local_client, mongo):
    h = admin_headers(local_client, mongo)
    alice = make_user(
        local_client, h, "alice", mongo, email="alice@example.com", display_name="Alice"
    )
    assert alice["platform_admin"] is False and alice["must_change_password"] is True
    assert "password" not in alice and "password_hash" not in alice
    # 같은 이름은 409, 모양이 틀리면 422, 짧은 비밀번호는 422
    r = local_client.post(
        "/users", json={"username": "alice", "password": "another password 1"}, headers=h
    )
    assert detail_code(r) == (409, "name_conflict")
    r = local_client.post("/users", json={"username": "Bad Name", "password": "x" * 12}, headers=h)
    assert detail_code(r) == (422, "invalid_value")
    r = local_client.post("/users", json={"username": "bob", "password": "short"}, headers=h)
    assert detail_code(r) == (422, "weak_password")
    # alice 는 소속이 없어 로그인은 되지만 테넌트가 없고, 사용자 관리는 403
    r = login(local_client, "alice", password_of("alice"))
    assert r.status_code == 200, r.text
    ah = bearer(r.json()["access_token"])
    assert local_client.get("/me", headers=ah).json()["memberships"] == []
    assert local_client.get("/users", headers=ah).status_code == 403
    assert (
        local_client.post(
            "/users", json={"username": "x", "password": "y" * 12}, headers=ah
        ).status_code
        == 403
    )
    # 관리자가 alice 를 테넌트 a 의 tenant-admin 으로 넣으면 사람 고르기 목록(directory)을 본다.
    # 전체 목록은 여전히 users:write 다
    assert local_client.get("/users/directory", headers=ah).status_code == 403
    r = local_client.put(
        "/tenants/a/members", json=[{"sub": alice["id"], "role": "tenant-admin"}], headers=h
    )
    assert r.status_code == 200, r.text
    assert local_client.get("/users", headers=ah).status_code == 403
    r = local_client.get("/users/directory", headers=ah)
    assert r.status_code == 200, r.text
    # 계정 상태(disabled)는 다른 테넌트 관리자가 알 일이 아니라 빠진다
    assert set(r.json()[0]) == {"id", "username", "display_name"}
    # 관리자는 전체 모양을 본다
    full = local_client.get("/users", headers=h).json()
    assert {u["username"] for u in full} == {"admin", "alice"}
    assert "must_change_password" in full[0]
    # PATCH: 프로필·platform_admin
    r = local_client.patch(
        f"/users/{alice['id']}",
        json={"display_name": "Alice L.", "platform_admin": True},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["platform_admin"] is True
    assert r.json()["display_name"] == "Alice L."
    assert local_client.get("/me", headers=ah).json()["is_platform_admin"] is True
    # 관리자 비밀번호 재설정: 다음 로그인에 바꾸라고 하고 옛 토큰은 죽는다
    r = local_client.post(
        f"/users/{alice['id']}/password", json={"password": NEW_PASSWORD}, headers=h
    )
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert local_client.get("/me", headers=ah).status_code == 401
    assert login(local_client, "alice", NEW_PASSWORD).json()["must_change_password"] is True
    # 삭제하면 소속도 사라진다
    r = local_client.delete(f"/users/{alice['id']}", headers=h)
    assert r.status_code == 200 and r.json() == {"id": alice["id"], "deleted": True}
    assert local_client.get(f"/users/{alice['id']}", headers=h).status_code == 404
    assert local_client.get("/tenants/a/members", headers=h).json() == []


def test_last_active_platform_admin_is_protected(local_client, mongo):
    h = admin_headers(local_client, mongo)
    admin_id = mongo["users"].find_one({"username": "admin"})["_id"]
    for body in ({"platform_admin": False}, {"disabled": True}):
        r = local_client.patch(f"/users/{admin_id}", json=body, headers=h)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "last_admin"
    assert local_client.delete(f"/users/{admin_id}", headers=h).status_code == 422
    # 두 번째 관리자가 생기면 첫 관리자를 내릴 수 있다
    make_user(local_client, h, "root2", mongo, platform_admin=True)
    r = local_client.patch(f"/users/{admin_id}", json={"platform_admin": False}, headers=h)
    assert r.status_code == 200 and r.json()["platform_admin"] is False
    # 방금 내려간 관리자의 토큰은 아직 살아 있지만 이제 권한이 없다
    assert local_client.get("/users", headers=h).status_code == 403


def test_disabled_account(local_client, mongo):
    h = admin_headers(local_client, mongo)
    bob = make_user(local_client, h, "bob", mongo)
    bh = bearer(login(local_client, "bob", password_of("bob")).json()["access_token"])
    assert local_client.get("/me", headers=bh).status_code == 200
    r = local_client.patch(f"/users/{bob['id']}", json={"disabled": True}, headers=h)
    assert r.status_code == 200 and r.json()["disabled"] is True
    # 살아 있던 토큰도 즉시 끊기고 로그인은 account_disabled 다
    assert local_client.get("/me", headers=bh).status_code == 401
    r = login(local_client, "bob", password_of("bob"))
    assert detail_code(r) == (401, "account_disabled")
    r = local_client.patch(f"/users/{bob['id']}", json={"disabled": False}, headers=h)
    assert (
        r.status_code == 200 and login(local_client, "bob", password_of("bob")).status_code == 200
    )


# -- 데이터 플레인 --------------------------------------------------------------------------


def test_data_plane_token_and_discovery(local_app, local_client, mongo):
    h = admin_headers(local_client, mongo)
    r = local_client.post("/auth/token", headers=h)
    assert r.status_code == 200, r.text
    dp = r.json()
    # 회수가 없는 토큰이라 수명이 끊기는 한도다. 모든 사람 토큰의 한도를 16분 안에 둔다 (P32 6절)
    assert dp["audience"] == "daegam-mcp" and dp["expires_in"] == 900
    # groups 는 테넌트 소속의 MCP 그룹이다. platform-admin 의 * 는 펼치지 않는다
    assert jwt.decode(dp["access_token"], options={"verify_signature": False})["groups"] == []
    alice = make_user(local_client, h, "alice", mongo)
    local_client.put(
        "/tenants/a/members", json=[{"sub": alice["id"], "role": "developer"}], headers=h
    )
    # local 모드의 그룹은 daegam-<tenant> 고정이다. 다른 모드에서 저장된 mcp_group 은 쓰이지 않는다
    mongo["tenant_settings"].insert_one({"_id": "b", "tenant": "b", "mcp_group": "team-b"})
    local_client.put("/tenants/b/members", json=[{"sub": alice["id"], "role": "viewer"}], headers=h)
    ah = bearer(login(local_client, "alice", password_of("alice")).json()["access_token"])
    alice_dp = local_client.post("/auth/token", headers=ah).json()["access_token"]
    assert jwt.decode(alice_dp, options={"verify_signature": False})["groups"] == [
        "daegam-a",
        "daegam-b",
    ]
    # 데이터 플레인 토큰으로 제어면에는 못 들어온다 (D27)
    assert local_client.get("/me", headers=bearer(dp["access_token"])).status_code == 401
    # 디스커버리와 JWKS 는 발급자 루트에 있고 인증이 없다. JWKS 에는 리컨실이 채운 개인 토큰의 슬롯
    # 키가 플랫폼 키와 함께 오른다 (P32 4절)
    with TestClient(local_app) as root:
        doc = root.get("/.well-known/openid-configuration")
        assert doc.status_code == 200 and doc.json()["issuer"] == ISSUER
        assert doc.json()["jwks_uri"] == PLATFORM_JWKS
        created = root.portal.call(local_app.state.pat_keys.fill_pool, utcnow())["created"]
        jwks = root.get("/.well-known/jwks.json").json()
        slots = slot_keys(jwks, local_app.state.internal_issuer.kid)
        assert sorted(k["kid"] for k in slots) == sorted(created)

    # 런타임(D27)이 하는 그대로 디스커버리로 JWKS 를 받아 검증한다. 플랫폼 앱을 발급자로 붙인다
    async def run():
        v = OIDCVerifier(
            ISSUER,
            "daegam-mcp",
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=local_app)),
            insecure_http=True,
        )
        claims = await v.verify(alice_dp)
        assert claims.sub == alice["id"] and claims.groups == ["daegam-a", "daegam-b"]
        assert claims.raw["preferred_username"] == "alice"
        claims = await v.verify(dp["access_token"])
        assert claims.groups == [] and claims.groups_present is True
        await v.aclose()

    asyncio.run(run())


def test_local_mode_ignores_idp_settings():
    """같은 ConfigMap 에 DAEGAM_OIDC_ISSUER 가 남아 있어도 local 은 뜬다. 모드 한 줄만 바꿔 local 로
    갈 수 있어야 한다(e2e 가 그렇게 한다). 발급자는 플랫폼 자신이다."""
    s = Settings(auth_mode="local", platform_issuer=ISSUER, oidc_issuer="https://idp.example")
    assert s.issuer == ISSUER and s.ui_client_id is None
    s = Settings(auth_mode="local", platform_issuer=ISSUER + "/")
    assert s.token_auth and s.issuer == ISSUER and s.insecure_issuer and s.ui_client_id is None
    s = Settings(auth_mode="local", platform_issuer="https://u.example")
    assert s.insecure_issuer is False


def test_password_change_is_enforced_by_the_server(local_client, mongo):
    """must_change_password 인 사람은 비밀번호 바꾸기·로그아웃·/me 만 지난다. UI 만 유도하면 배포에
    박힌 부트스트랩 비밀번호가 curl 한 번으로 살아 있는 platform-admin 자격이 된다."""
    # users 가 비어 있으면 설정의 첫 관리자가 생긴다. 해시가 저장되고 평문은 어디에도 없다
    doc = mongo["users"].find_one({"username": "admin"})
    assert doc["password_hash"].startswith("scrypt$") and LOCAL_BOOT_PASSWORD not in str(doc)
    r = login(local_client, "admin", LOCAL_BOOT_PASSWORD)
    assert r.json()["must_change_password"] is True
    h = bearer(r.json()["access_token"])
    me = local_client.get("/me", headers=h)
    assert me.status_code == 200 and me.json()["is_platform_admin"] is True
    for path in ("/clusters", "/users", "/settings"):
        r = local_client.get(path, headers=h)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["detail"]["code"] == "password_change_required"
    assert local_client.post("/auth/token", headers=h).status_code == 403
    r = local_client.post(
        "/auth/password",
        json={"current_password": LOCAL_BOOT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=h,
    )
    assert r.status_code == 200, r.text
    fresh = bearer(r.json()["access_token"])
    assert local_client.get("/clusters", headers=fresh).status_code == 200
    assert local_client.post("/auth/logout", headers=fresh).status_code == 204
