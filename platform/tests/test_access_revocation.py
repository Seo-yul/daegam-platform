"""회수 훅과 접근 정리 (P32 7절, P33 12절). 계정 사건과 멤버 교체가 해당 개인 토큰과 OAuth 연결만
끊고, 훅이 놓친 것은 접근 정리가 다음 틱에, 연결이면 다음 갱신도 잡는다. 슬롯 정리 가운데 옛 서명
키와 pool 은 test_pat_keys.py 가 본다."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from daegam.services.access_revocation import ORPHAN_GRACE, SWEEP_SUB
from daegam.store.base import utcnow
from daegam.store.pat_keys import BOUND
from tests.helpers.access import sweep
from tests.helpers.docs import TENANT
from tests.helpers.oauth import (
    CLAUDE,
    CLAUDE_DOC,
    NEW_PASSWORD,
    OTHER_TEAM,
    PAT_TENANT,
    FakeWeb,
    Person,
    connect,
    fresh_slot_kids,
    grant_of,
    issued,
    person,
    refresh,
    rotate_signing_key,
)


def token_doc(mongo, token: dict) -> dict:
    return mongo["personal_tokens"].find_one({"_id": token["token_id"]})


def reasons(mongo, *tokens: dict) -> list[str | None]:
    """토큰마다 회수가 끝났으면 사유, 아니면 None."""
    docs = [token_doc(mongo, t) for t in tokens]
    return [d["revoke_reason"] if d["revoked_at"] else None for d in docs]


def admin_sub(mongo) -> str:
    return mongo["users"].find_one({"username": "admin"})["_id"]


@pytest.fixture
def alice(local_client, admin, mongo) -> Person:
    return person(local_client, admin, mongo, "alice", PAT_TENANT, OTHER_TEAM)


@pytest.fixture
def bob(local_client, admin, mongo) -> Person:
    return person(local_client, admin, mongo, "bob", PAT_TENANT)


# -- 훅 ------------------------------------------------------------------------------------


def _disable(c: TestClient, admin: dict, who: Person) -> None:
    assert c.patch(f"/users/{who.id}", json={"disabled": True}, headers=admin).status_code == 200


def _change(c: TestClient, admin: dict, who: Person) -> None:
    body = {"current_password": who.password, "new_password": NEW_PASSWORD}
    assert c.post("/auth/password", json=body, headers=who.headers).status_code == 200


def test_account_events_revoke_every_token_of_that_person_only(
    local_client, admin, alice, bob, mongo
):
    """계정을 지우면 그 사람의 모든 테넌트 토큰을 회수하고 다른 사람의 토큰은 그대로다. 비밀번호
    바꾸기의 훅이 실패한 뒤는 아래 감사 행 시험과 OAuth 연결의 훅 실패 시험이, 막기의 훅은 슬롯
    삭제 실패 시험이 본다."""
    mine = [issued(local_client, alice), issued(local_client, alice, tenant=OTHER_TEAM)]
    theirs = issued(local_client, bob)
    kids = {token_doc(mongo, t)["kid"] for t in mine}
    assert local_client.delete(f"/users/{alice.id}", headers=admin).status_code == 200
    assert reasons(mongo, *mine, theirs) == ["deleted", "deleted", None]
    assert {token_doc(mongo, t)["revoked_by"] for t in mine} == {admin_sub(mongo)}
    assert not kids & fresh_slot_kids(local_client)
    # 훅이 끝냈으므로 접근 정리가 더 할 일이 없다
    assert sweep(local_client)["revoked"] == []


def test_leaving_a_tenant_revokes_only_that_tenants_tokens(local_client, admin, alice, bob, mongo):
    """멤버 교체에서 빠진 사람의 그 테넌트 토큰만 회수한다. 남은 사람과 다른 테넌트는 그대로다."""
    in_t, in_x, bobs = (
        issued(local_client, alice),
        issued(local_client, alice, tenant=OTHER_TEAM),
        issued(local_client, bob),
    )
    r = local_client.put(
        f"/tenants/{PAT_TENANT}/members", json=[{"sub": bob.id, "role": "viewer"}], headers=admin
    )
    assert r.status_code == 200, r.text
    assert reasons(mongo, in_t, in_x, bobs) == ["membership", None, None]
    assert token_doc(mongo, in_t)["revoked_by"] == admin_sub(mongo)


# -- 훅이 놓친 것 ---------------------------------------------------------------------------


def test_a_failed_slot_delete_leaves_the_token_revoking(
    local_client, admin, alice, mongo, monkeypatch
):
    """슬롯을 지우지 못하면 `revoked_at` 을 찍지 않는다. 목록은 회수 중이고 키는 JWKS 에 남는다.
    다음 접근 정리가 슬롯을 지우고 나서 회수됨을 찍는다 (P32 7절)."""
    token = issued(local_client, alice)
    kid = token_doc(mongo, token)["kid"]
    slots = local_client.app.state.pat_keys.slots

    async def broken(*args, **kw):
        raise PyMongoError("mongo is away")

    monkeypatch.setattr(slots, "remove_kids", broken)
    _disable(local_client, admin, alice)
    doc = token_doc(mongo, token)
    assert doc["revoke_requested_at"] and doc["revoked_at"] is None
    [listed] = local_client.get(f"/users/{alice.id}/tokens", headers=admin).json()
    assert listed["status"] == "revoking" and listed["revoke_reason"] == "disabled"
    assert kid in fresh_slot_kids(local_client)
    monkeypatch.undo()
    result = sweep(local_client)
    assert result["finished"] == [token["token_id"]] and result["revoked"] == []
    assert kid not in fresh_slot_kids(local_client)
    assert reasons(mongo, token) == ["disabled"]
    # 다시 열어도 회수한 토큰은 살아나지 않는다
    r = local_client.patch(f"/users/{alice.id}", json={"disabled": False}, headers=admin)
    assert r.status_code == 200
    assert reasons(mongo, token) == ["disabled"]


# -- 발급자와 서명 키 ------------------------------------------------------------------------


def test_a_platform_that_stops_minting_revokes_every_token(local_client, alice, mongo):
    """플랫폼이 사람 토큰을 내지 않게 되면(header 모드) 사람 발급자가 없어진 것이다. 남은 개인
    토큰은 `issuer_changed` 다."""
    token = issued(local_client, alice)
    local_client.app.state.local_auth.revoker.mints = False
    sweep(local_client)
    assert reasons(mongo, token) == ["issuer_changed"]


def test_a_new_signing_key_revokes_every_token(local_client, alice, mongo, caplog):
    """서명 키를 바꾸면(D40) 옛 master 의 슬롯이 JWKS 에서 빠지고 그 토큰은 `key_rotated` 다."""
    token = issued(local_client, alice)
    rotate_signing_key(local_client)
    with caplog.at_level(logging.INFO, logger="daegam.access_revocation"):
        sweep(local_client)
    assert reasons(mongo, token) == ["key_rotated"]
    assert "access_revoked reason=key_rotated tokens=1 grants=0" in caplog.messages
    assert mongo["pat_keys"].count_documents({}) == 0


# -- 슬롯 정리 -----------------------------------------------------------------------------


def test_orphan_slots_wait_out_the_issue_gap(local_client, alice, mongo):
    """문서가 없는 bound 슬롯은 발급이 문서를 넣기 전일 수 있어 유예 뒤에만 지운다. 회수한 토큰의
    슬롯을 다시 넣으면 바로 지운다. 문서가 있는데 슬롯이 없으면 `key_missing` 이다."""
    master = local_client.app.state.internal_issuer.kid
    now = utcnow()

    def bound(kid: str, token_id: str, age: timedelta) -> dict:
        return {
            "_id": kid,
            "master": master,
            "state": BOUND,
            "created_at": now - age,
            "bound_at": now - age,
            "token_id": token_id,
        }

    young = bound("pat-00000000000000a1", "gone-1", ORPHAN_GRACE - timedelta(minutes=1))
    old = bound("pat-00000000000000a2", "gone-2", ORPHAN_GRACE + timedelta(minutes=1))
    mongo["pat_keys"].insert_many([young, old])
    revoked = issued(local_client, alice)
    local_client.delete(f"/me/tokens/{revoked['token_id']}", headers=alice.headers)
    replanted = bound(token_doc(mongo, revoked)["kid"], revoked["token_id"], timedelta(0))
    mongo["pat_keys"].insert_one(replanted)
    missing = issued(local_client, alice)
    mongo["pat_keys"].delete_one({"_id": token_doc(mongo, missing)["kid"]})
    result = sweep(local_client)
    assert sorted(result["removed"]) == sorted([old["_id"], replanted["_id"]])
    assert mongo["pat_keys"].find_one({"_id": young["_id"]}) is not None
    assert reasons(mongo, missing) == ["key_missing"]


def test_the_sweep_leaves_one_system_audit_row_per_reason_and_tenant(
    local_client, alice, mongo, monkeypatch, caplog
):
    """비밀번호 바꾸기의 훅이 실패해도 요청은 성공하고 경고만 남는다. 접근 정리가 다음 틱에 토큰을
    회수하고 사유와 테넌트마다 시스템 감사 행을 하나씩 남긴다."""
    tokens = [
        issued(local_client, alice),
        issued(local_client, alice),
        issued(local_client, alice, tenant=OTHER_TEAM),
    ]

    async def broken(*args, **kw):
        raise PyMongoError("mongo is away")

    monkeypatch.setattr(local_client.app.state.local_auth.revoker.tokens, "live_ids", broken)
    with caplog.at_level(logging.WARNING, logger="daegam.access_revocation"):
        _change(local_client, {}, alice)
    assert any("access revocation hook failed" in m for m in caplog.messages)
    assert reasons(mongo, *tokens) == [None] * 3
    monkeypatch.undo()
    with caplog.at_level(logging.INFO, logger="daegam.access_revocation"):
        sweep(local_client)
    assert reasons(mongo, *tokens) == ["password"] * 3
    assert "access_revoked reason=password tokens=3 grants=0" in caplog.messages
    rows = list(mongo["audit_events"].find({"sub": SWEEP_SUB}).sort("tenant", 1))
    assert [(e["tenant"], e["error_code"]) for e in rows] == [
        (PAT_TENANT, "password"),
        (OTHER_TEAM, "password"),
    ]
    assert {
        (e["subject_type"], e["action"], e["method"], e["path"], e["resource"], e["status"])
        for e in rows
    } == {("system", "access_revoked", "SYSTEM", "reconcile", "personal-tokens", 200)}


# -- OAuth 연결 (P33 12절) -------------------------------------------------------------------


@pytest.fixture
def carol(oauth_client, oauth_admin, mongo) -> Person:
    return person(oauth_client, oauth_admin, mongo, "carol", TENANT)


def _refused(c: TestClient, client_id: str, body: dict) -> bool:
    r = refresh(c, client_id, body["refresh_token"])
    return r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_account_events_revoke_oauth_connections(
    oauth_client, oauth_admin, carol, client_id, mongo
):
    """계정 사건의 훅은 개인 토큰과 같은 호출로 연결도 끊는다. 사유는 개인 토큰과 같은 판정이다."""
    body = connect(oauth_client, client_id, carol)
    _disable(oauth_client, oauth_admin, carol)
    assert grant_of(mongo, body)["revoked_reason"] == "disabled"
    assert _refused(oauth_client, client_id, body)


def test_a_failing_hook_leaves_the_next_refresh_to_end_the_connection(
    oauth_client, carol, client_id, mongo, monkeypatch, caplog
):
    """갱신은 사람과 비밀번호 판을 늘 다시 본다. 훅이 놓쳐도 다음 갱신에서 끊긴다. 훅의 실패는
    요청을 실패시키지 않고 경고로 남는다."""
    body = connect(oauth_client, client_id, carol)

    async def broken(*args, **kw):
        raise PyMongoError("mongo is away")

    monkeypatch.setattr(oauth_client.app.state.local_auth.revoker.grants, "live_ids", broken)
    with caplog.at_level(logging.WARNING, logger="daegam.access_revocation"):
        _change(oauth_client, {}, carol)
    assert any("access revocation hook failed" in m for m in caplog.messages)
    assert grant_of(mongo, body)["revoked_at"] is None
    monkeypatch.undo()
    assert _refused(oauth_client, client_id, body)
    assert grant_of(mongo, body)["revoked_reason"] == "password"


def test_the_sweep_revokes_connections_that_lost_access(
    oauth_client, carol, client_id, mongo, caplog
):
    """OAuth 를 끄면 남은 연결도 끊긴다. 잠든 서버는 끊지 않는다(test_access_oidc.py). 요청 없는
    회수는 로그 한 줄과 시스템 주체의 감사 행(resource=oauth-grants)으로 남는다. 사람이 접근을 잃은
    사유는 토큰과 같은 판정이다."""
    reason = "oauth_disabled"
    body = connect(oauth_client, client_id, carol)
    oauth_client.app.state.local_auth.revoker.oauth_off = True
    with caplog.at_level(logging.INFO, logger="daegam.access_revocation"):
        result = sweep(oauth_client)
    grant = grant_of(mongo, body)
    assert result["grants"] == [grant["_id"]] and grant["revoked_reason"] == reason
    assert f"access_revoked reason={reason} tokens=0 grants=1" in caplog.messages
    [row] = mongo["audit_events"].find({"sub": SWEEP_SUB})
    assert (row["resource"], row["tenant"], row["error_code"]) == ("oauth-grants", TENANT, reason)
    assert sweep(oauth_client)["grants"] == []


def test_a_cimd_client_off_the_allowlist_loses_its_connections(
    oauth_client, carol, client_id, mongo, monkeypatch
):
    """DAEGAM_OAUTH_CIMD_CLIENTS 는 새 인가만이 아니라 남은 연결도 묶는다. 목록에서 빠진 CIMD
    클라이언트의 연결은 다음 갱신이, 갱신이 오지 않으면 접근 정리가 client_disallowed 로 끊는다.
    DCR 연결은 그대로다."""
    FakeWeb(monkeypatch).serve(CLAUDE, CLAUDE_DOC)
    refreshing, idle = connect(oauth_client, CLAUDE, carol), connect(oauth_client, CLAUDE, carol)
    dcr = connect(oauth_client, client_id, carol)
    oauth_client.app.state.local_auth.revoker.cimd_clients = frozenset()
    assert _refused(oauth_client, CLAUDE, refreshing)
    assert grant_of(mongo, refreshing)["revoked_reason"] == "client_disallowed"
    assert sweep(oauth_client)["grants"] == [grant_of(mongo, idle)["_id"]]
    assert grant_of(mongo, idle)["revoked_reason"] == "client_disallowed"
    assert grant_of(mongo, dcr)["revoked_at"] is None
