"""연결 끊기 (P33 12절). 클라이언트의 RFC 7009 회수와, 화면의 연결 목록과 끊기(본인·관리자).

연결은 tests/helpers/oauth.py 의 도우미로 만든다. 끊긴 연결은 갱신이 그 자리에서 invalid_grant 다.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from tests.helpers.docs import TENANT
from tests.helpers.oauth import (
    GW_URL,
    REVOKE,
    fresh_slot_kids,
    grant_of,
    issued,
    person,
    refresh,
    register,
)


def revoke(c: TestClient, cid: str | None, value: str, **extra: str) -> httpx.Response:
    form = {"token": value, **({"client_id": cid} if cid is not None else {}), **extra}
    return c.post(REVOKE, data=form)


def reason_of(mongo, body: dict) -> str | None:
    return grant_of(mongo, body)["revoked_reason"]


# -- RFC 7009 ------------------------------------------------------------------------------------


def test_revoking_the_refresh_token_ends_the_family(oauth_client, client_id, connected, mongo):
    r = revoke(oauth_client, client_id, connected["refresh_token"], token_type_hint="refresh_token")
    assert r.status_code == 200 and r.content == b"", r.text
    assert r.headers["cache-control"] == "no-store"
    assert reason_of(mongo, connected) == "client"
    r = refresh(oauth_client, client_id, connected["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_ending_a_family_leaves_the_persons_personal_token_live(
    oauth_client, client_id, connected, member, mongo
):
    """연결을 끊는 일은 그 연결의 가족만 끝낸다. 같은 사람의 개인 토큰은 회수되지 않고 그 키가
    JWKS 에 남아 Gateway 가 계속 받는다 (e2e-oauth STEP 16 이 설치본에서 보는 것)."""
    pat = issued(oauth_client, member, tenant=TENANT)
    kid = mongo["personal_tokens"].find_one({"_id": pat["token_id"]})["kid"]
    r = revoke(oauth_client, client_id, connected["refresh_token"], token_type_hint="refresh_token")
    assert r.status_code == 200 and reason_of(mongo, connected) == "client"
    assert refresh(oauth_client, client_id, connected["refresh_token"]).status_code == 400
    [listed] = oauth_client.get("/me/tokens", headers=member.headers).json()
    assert listed["token_id"] == pat["token_id"] and listed["status"] == pat["status"]
    assert listed["revoked_at"] is None and listed["revoke_reason"] is None
    assert kid in fresh_slot_kids(oauth_client)


def test_unknown_tokens_are_fine_and_other_clients_are_not(
    oauth_client, client_id, connected, mongo
):
    """모르는 토큰은 명세대로 200 이다. 다른 클라이언트의 토큰은 401 이고 연결은 산다."""
    for value in ("nope", f"{'0' * 32}.secret", "a.b.c"):
        assert revoke(oauth_client, client_id, value).status_code == 200, value
    other = register(oauth_client, "Other")
    for value in (connected["refresh_token"], connected["access_token"]):
        r = revoke(oauth_client, other, value)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client", r.text
    r = revoke(oauth_client, None, connected["refresh_token"])
    assert r.status_code == 401 and r.json()["error"] == "invalid_client", r.text
    assert grant_of(mongo, connected)["revoked_at"] is None
    # 제 클라이언트의 접근 토큰은 서명을 확인하고 sid 의 연결을 끊는다. 그 토큰 자체는 만료까지 산다
    r = revoke(oauth_client, client_id, connected["access_token"], token_type_hint="access_token")
    assert r.status_code == 200, r.text
    assert reason_of(mongo, connected) == "client"


# -- 화면 ----------------------------------------------------------------------------------------


def test_a_person_lists_and_revokes_only_their_own_connections(
    oauth_client, client_id, connected, member, oauth_admin, mongo
):
    """다른 사람의 연결은 목록에 없고 끊으려 하면 404 다. 본인은 화면이 읽는 모양으로 보고 끊으며,
    끊은 연결은 사유 user 로 남고 그 갱신 토큰은 invalid_grant 다."""
    bob = person(oauth_client, oauth_admin, mongo, "bob", TENANT)
    grant_id = grant_of(mongo, connected)["_id"]
    assert oauth_client.get("/me/oauth-grants", headers=bob.headers).json() == []
    r = oauth_client.delete(f"/me/oauth-grants/{grant_id}", headers=bob.headers)
    assert r.status_code == 404, r.text
    assert grant_of(mongo, connected)["revoked_at"] is None
    # 본인은 화면이 읽는 모양으로 보고 끊는다. 가족과 갱신 비밀의 해시는 내지 않는다
    [listed] = oauth_client.get("/me/oauth-grants", headers=member.headers).json()
    assert listed["grant_id"] == grant_id and listed["status"] == "active"
    assert (listed["client_name"], listed["client_kind"], listed["client_host"]) == (
        "Script Client",
        "dcr",
        None,
    )
    assert (listed["tenant"], listed["resource"], listed["resource_kind"]) == (
        TENANT,
        GW_URL,
        "gateway",
    )
    assert listed["scope"] == ["mcp", "offline_access"] and listed["revoked_reason"] is None
    assert "family_id" not in listed and "refresh_sha256" not in listed
    r = oauth_client.delete(f"/me/oauth-grants/{grant_id}", headers=member.headers)
    assert r.status_code == 200 and r.json() == {"grant_id": grant_id, "revoked": True}
    [listed] = oauth_client.get("/me/oauth-grants", headers=member.headers).json()
    assert listed["status"] == "revoked" and listed["revoked_reason"] == "user"
    r = refresh(oauth_client, client_id, connected["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_an_admin_disconnects_and_the_audit_row_carries_the_tenant(
    oauth_client, connected, member, oauth_admin, mongo
):
    """관리자 라우트의 경로에는 테넌트가 없다. 핸들러가 연결의 테넌트를 감사 행에 둔다."""
    grant_id = grant_of(mongo, connected)["_id"]
    r = oauth_client.get(f"/users/{member.id}/oauth-grants", headers=oauth_admin)
    assert [g["grant_id"] for g in r.json()] == [grant_id]
    assert oauth_client.get("/users/nobody/oauth-grants", headers=oauth_admin).status_code == 404
    r = oauth_client.get(f"/users/{member.id}/oauth-grants", headers=member.headers)
    assert r.status_code == 403
    r = oauth_client.delete(f"/users/{member.id}/oauth-grants/{grant_id}", headers=oauth_admin)
    assert r.status_code == 200, r.text
    assert reason_of(mongo, connected) == "admin"
    row = mongo["audit_events"].find_one({"action": "revoke_user_oauth_grant"})
    assert (row["tenant"], row["resource_id"], row["status"]) == (TENANT, grant_id, 200)
