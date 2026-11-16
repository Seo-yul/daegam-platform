"""P6 F5(a): 플랫폼 API 감사 (audit_events), updated_by, GET /audit."""

from __future__ import annotations

from datetime import timedelta

from daegam.audit.middleware import resource_id_of, resource_of
from daegam.auth import rbac
from daegam.store.base import utcnow
from daegam.urls import API_PREFIX
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.auth import admin_headers, as_principal, bearer
from tests.helpers.backend_apis import upload_backend
from tests.helpers.consts import TENANT_HEADER
from tests.helpers.docs import TENANT, seed_gateway
from tests.helpers.http import detail_code
from tests.helpers.oauth import (
    GW_HOST,
    PAT_TENANT,
    REGISTER,
    REVOKE,
    connect,
    consent,
    handle_for,
    issued,
    person,
    refresh,
    register,
    stale_headers,
)
from tests.helpers.runtime import FILES_FEATURES, add_session_files


def events(mongo, **q):
    return list(mongo["audit_events"].find(q).sort("ts", 1))


# -- 미들웨어 (header 모드) ------------------------------------------------------------


def test_admin_token_revocation_lands_in_the_tokens_tenant(local_client, mongo):
    """관리자 회수의 경로에는 테넌트가 없다. 핸들러가 토큰의 테넌트와 id 를 두므로 그 테넌트의 감사
    화면에 관리자의 회수가 보인다 (P32 12절)."""
    admin = admin_headers(local_client, mongo)
    alice = person(local_client, admin, mongo, "alice", PAT_TENANT)
    token = issued(local_client, alice)["token_id"]
    r = local_client.delete(f"/users/{alice.id}/tokens/{token}", headers=admin)
    assert r.status_code == 200, r.text
    r = local_client.get(
        "/audit", params={"tenant": PAT_TENANT, "action": "revoke_user_token"}, headers=admin
    )
    assert r.status_code == 200, r.text
    [row] = r.json()
    assert (row["tenant"], row["resource_id"], row["status"]) == (PAT_TENANT, token, 200)


def test_audit_failure_does_not_break_request(client, mongo, monkeypatch):
    async def boom(event):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(client.app.state.audit, "record", boom)
    assert upload_backend(client).status_code == 201
    assert events(mongo) == []


def test_helpers():
    assert resource_of("/api/backend-apis/x/tokens") == "backend-apis"
    assert resource_of("/backend-apis/x/tokens") == "backend-apis"
    assert resource_of("/api/") is None and resource_of("/") is None
    assert resource_id_of({"profile_id": "p1", "token_id": "t1"}) == "p1"
    assert resource_id_of({"tenant": "a", "n": 2}) is None
    assert resource_id_of({"tenant": "a", "name": "x"}) == "x"


# -- updated_by ---------------------------------------------------------------------


# -- GET /audit ---------------------------------------------------------------------


def test_query_filters(client, mongo):
    upload_backend(client, headers={TENANT_HEADER: "a"})
    upload_backend(client, name="other", fixture="orders.wsdl", headers={TENANT_HEADER: "b"})
    client.patch("/settings", json={"default_model": "m"})

    r = client.get("/audit")  # header 모드 = platform-admin, 테넌트 무관
    assert r.status_code == 200
    assert [e["action"] for e in r.json()] == [
        "patch_platform_settings",
        "upload_spec",
        "upload_spec",
    ]
    assert all(isinstance(e["ts"], str) and "id" in e for e in r.json())
    # 나가는 시각에는 UTC 표시가 붙는다. 없으면 브라우저가 로컬 시각으로 읽는다.
    # 그렇게 나간 값을 since·until 로 그대로 되돌려 쓸 수 있어야 화면의 필터가 맞는다
    newest = r.json()[0]["ts"]
    assert newest.endswith("+00:00")
    assert [e["ts"] for e in client.get("/audit", params={"since": newest}).json()] == [newest]
    r = client.get("/audit", params={"tenant": "a"})
    assert [e["tenant"] for e in r.json()] == ["a"]
    r = client.get("/audit", params={"action": "upload_spec", "limit": 1})
    assert len(r.json()) == 1 and r.json()[0]["tenant"] == "b"
    r = client.get("/audit", params={"resource": "settings"})
    assert [e["action"] for e in r.json()] == ["patch_platform_settings"]
    r = client.get("/audit", params={"sub": "nobody"})
    assert r.json() == []
    later = (utcnow() + timedelta(minutes=1)).isoformat() + "Z"
    assert client.get("/audit", params={"since": later}).json() == []
    assert client.get("/audit", params={"until": later}).status_code == 200
    assert len(client.get("/audit", params={"until": later}).json()) == 3
    r = client.get("/audit", params={"since": "yesterday"})
    assert detail_code(r) == (422, "invalid_value")
    assert client.get("/audit", params={"limit": 0}).status_code == 422
    # 감사 조회 자체는 GET 이라 기록되지 않는다
    assert len(events(mongo)) == 3
    # 경로의 테넌트가 헤더보다 앞서고, 그 행의 리소스는 tenants 다
    r = client.patch("/tenants/acme/settings", json={"quota": {"pods": "10"}})
    assert r.status_code == 200, r.text
    row = events(mongo)[-1]
    assert (row["action"], row["tenant"], row["resource"]) == (
        "patch_tenant_settings",
        "acme",
        "tenants",
    )


# -- P9 F3: 허브 게시·해제·정책 변경 ---------------------------------------------------------


def test_an_approver_downloading_session_files_is_audited(client, fake_clusters, mongo):
    """P36 10절: 승인자로 부른 세션 파일 다운로드는 남이 업로드한 파일을 밖으로 낼 수 있어 GET
    인데도 남는다. 같은 화면의 목록과, 승인자가 아닌 사람의 자기 파일 다운로드는 남지 않는다."""
    agent = deployed_agent(client, fake_clusters, mongo, features=FILES_FEATURES)
    add_session_files(agent.runtime)
    mongo["audit_events"].delete_many({})
    files = f"/agents/{agent.id}/sessions/other/files"
    assert client.get(f"{files}/reports/a.md").status_code == 200
    assert client.get(files).status_code == 200
    with as_principal(client, "viewer-1", rbac.VIEWER):
        assert client.get(f"/agents/{agent.id}/sessions/s1/files/a.md").status_code == 200

    rows = events(mongo)
    assert [(r["action"], r["method"], r["status"]) for r in rows] == [
        ("download_session_file", "GET", 200)
    ]
    assert rows[0]["resource_id"] == agent.id and rows[0]["path_params"]["session_id"] == "other"
    assert rows[0]["path_params"]["path"] == "reports/a.md"


def test_rbac_and_subject_type(oidc, token, mongo):
    dev_a = token("dev-a", "daegam-a-dev")
    dev_b = token("dev-b", "daegam-b-dev")
    viewer = token("view-a", "daegam-a-view")
    admin = token("root", "daegam-admins")

    assert upload_backend(oidc, headers=bearer(dev_a)).status_code == 201
    assert upload_backend(oidc, "bp", "orders.wsdl", headers=bearer(dev_b)).status_code == 201
    # 서비스 앱(client credentials) 호출: sub 는 cid, subject_type 은 client
    r = oidc.patch("/settings", json={"default_model": "m"}, headers=bearer(admin))
    assert r.status_code == 200
    svc = token("svc-app", "daegam-a-dev", extra={"cid": "svc-app"})
    assert (
        upload_backend(oidc, "svc", "orders.openapi.yaml", headers=bearer(svc)).status_code == 201
    )
    rows = {(e["sub"], e["action"]): e for e in events(mongo)}
    assert rows[("dev-a", "upload_spec")]["tenant"] == "a"
    assert rows[("dev-a", "upload_spec")]["subject_type"] == "user"
    assert rows[("svc-app", "upload_spec")]["subject_type"] == "client"
    assert rows[("root", "patch_platform_settings")]["tenant"] is None

    # developer 는 자기 테넌트만
    r = oidc.get("/audit", headers=bearer(dev_a))
    assert r.status_code == 200, r.text
    assert {e["tenant"] for e in r.json()} == {"a"}
    assert {e["sub"] for e in r.json()} == {"dev-a", "svc-app"}
    assert oidc.get("/audit", params={"tenant": "a"}, headers=bearer(dev_a)).status_code == 200
    r = oidc.get("/audit", params={"tenant": "b"}, headers=bearer(dev_a))
    assert r.status_code == 403
    # viewer 는 audit:read 가 없다
    r = oidc.get("/audit", headers=bearer(viewer))
    assert r.status_code == 403 and r.json()["detail"]["scope"] == "audit:read"
    # platform-admin 은 전체, 또는 테넌트 지정
    r = oidc.get("/audit", headers=bearer(admin))
    assert {e["tenant"] for e in r.json()} == {"a", "b", None}
    r = oidc.get("/audit", params={"tenant": "b"}, headers=bearer(admin))
    assert [e["sub"] for e in r.json()] == ["dev-b"]
    # 토큰 없음
    assert oidc.get("/audit").status_code == 401
    # 인증되지 않은 쓰기는 sub 없이 남는다. 라우트 매칭 전의 405 는 action 이 "METHOD path" 다
    assert oidc.post("/settings", json={}).status_code == 405
    assert oidc.patch("/settings", json={"default_model": "m"}).status_code == 401
    anonymous = events(mongo)[-2:]
    assert [(e["action"], e["status"], e["sub"]) for e in anonymous] == [
        ("POST /api/settings", 405, None),
        ("patch_platform_settings", 401, None),
    ]
    assert anonymous[1]["error_code"] == "unauthorized" and anonymous[1]["tenant"] is None


# -- 저장소 스키마 ------------------------------------------------------------------------


# -- P14: via (비서가 대신 한 호출) -------------------------------------------------------------


# -- P33: OAuth 토큰·회수와 동의 (18절) ---------------------------------------------------------


def test_oauth_rows_name_the_client_and_split_the_token_action(oauth_client, oauth_admin, mongo):
    """토큰 요청은 인증 전 경로다. 핸들러가 확인한 sub·테넌트·client_ref 와 grant 별 행동 이름을
    두고, 갱신 토큰의 재사용은 응답이 invalid_grant 여도 감사가 유예와 가족 회수를 가른다.
    확인하지 못한 요청은 sub 없이 남고, 아무것도 쓰지 않는 인가 요청은 남지 않는다."""
    seed_gateway(mongo, "gw-1", GW_HOST)
    alice = person(oauth_client, oauth_admin, mongo, "alice", TENANT)
    cid = register(oauth_client)
    first = connect(oauth_client, cid, alice)["refresh_token"]
    second = refresh(oauth_client, cid, first).json()["refresh_token"]
    refresh(oauth_client, cid, first)  # 10초 안의 바로 앞 세대
    refresh(oauth_client, cid, "garbage")
    refresh(oauth_client, cid, f"{second.split('.')[0]}.{'x' * 43}")  # 가족 회수
    rows = events(mongo, path=f"{API_PREFIX}/oauth/token")
    ref = rows[0]["client_id"]
    assert ref.startswith("dcr:")
    assert [
        (e["action"], e["sub"], e["client_id"], e["tenant"], e["error_code"]) for e in rows
    ] == [
        ("oauth_token_code", alice.id, ref, TENANT, None),
        ("oauth_token_refresh", alice.id, ref, TENANT, None),
        ("oauth_token_refresh", alice.id, ref, TENANT, "refresh_reuse_grace"),
        ("oauth_token_refresh", None, None, None, "invalid_grant"),
        ("oauth_token_refresh", alice.id, ref, TENANT, "refresh_reuse"),
    ]
    assert all(e["subject_type"] == "user" and e["resource"] == "oauth" for e in rows)
    assert events(mongo, path=f"{API_PREFIX}/oauth/authorize") == []
    r = oauth_client.get("/audit", params={"client_id": ref}, headers=oauth_admin)
    # 등록과 허용의 행도 같은 client_id 다
    assert sorted(e["action"] for e in r.json()) == sorted(
        ["register", "approve_consent", "oauth_token_code"] + ["oauth_token_refresh"] * 3
    )

    # OAuth 모양의 오류 본문은 `error` 가 오류 코드다. 오래된 로그인의 허용은 reauth_required 다
    r = consent(
        oauth_client, "approve", handle_for(oauth_client, cid), stale_headers(oauth_client, alice)
    )
    assert r.status_code == 403
    oauth_client.post(REVOKE, data={"token": "x"})
    oauth_client.post(REGISTER, json={"redirect_uris": ["https://evil.example/cb"]})
    codes = {e["action"]: e["error_code"] for e in events(mongo) if e["status"] >= 400}
    assert codes["approve_consent"] == "reauth_required"
    assert codes["oauth_revoke"] == "invalid_client"
    assert codes["register"] == "invalid_redirect_uri"

    # 갱신이 연결을 끊으면 그 사유가 오류 코드다. 리소스의 노출을 끈 것(resource_gone)은 갱신만
    # 알아보므로 이 행이 그 회수의 유일한 감사다
    body = connect(oauth_client, cid, alice)
    mongo["gateways"].update_one({"_id": "gw-1"}, {"$set": {"exposure.enabled": False}})
    assert refresh(oauth_client, cid, body["refresh_token"]).status_code == 400
    row = events(mongo, path=f"{API_PREFIX}/oauth/token")[-1]
    assert (row["action"], row["sub"], row["tenant"], row["error_code"]) == (
        "oauth_token_refresh",
        alice.id,
        TENANT,
        "resource_gone",
    )
