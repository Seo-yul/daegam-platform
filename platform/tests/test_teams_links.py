"""P47 6.3절: 연결 코드의 확인, 연결의 끝과 끊기, 턴마다의 사람 확인.

코드는 개인 토큰과 같은 확인으로 만든다(local 은 지금 비밀번호, oidc 는 최근 로그인). 연결은 계정
훅(막기, 삭제, 비밀번호 바꾸기)과 리컨실의 정리(끝, 90일 동안 쓰지 않음, 계정을 잃음, 다른 봇)가
끊는다. 턴마다의 확인(`BoundPersonCheck`)은 요구할 scope 를 받고, Teams 의 엄한 확인은 oidc 의
기록이 없거나 local 의 마지막 로그인이 긴 한도를 넘었으면 `unconfirmed` 다.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from daegam.services.channels.links import CODE_ALPHABET, CODE_LENGTH
from daegam.services.channels.texts import KO
from daegam.services.runs.person import CAUSE_ROLE, CAUSE_UNCONFIRMED, RUN_SCOPES
from daegam.store.base import utcnow
from tests.helpers.access import run_step
from tests.helpers.auth import LOCAL_BOOT_PASSWORD, recent_id_token, with_ui_client
from tests.helpers.http import detail_code
from tests.helpers.oidc import Signer
from tests.helpers.runs import (
    BOUND,
    LOCAL,
    OIDC,
    FakePeople,
    FakeUsers,
    alice,
    idp_person,
    person_check,
)
from tests.helpers.teams import FakeMicrosoft, attach, configure_teams, plant_link

CODE = "/me/channels/teams/link-code"


def test_a_local_code_needs_the_current_password(local_admin):
    assert detail_code(local_admin.post(CODE, json={})) == (422, "invalid_value")
    wrong = local_admin.post(CODE, json={"password": "wrong-password"})
    assert detail_code(wrong) == (403, "invalid_credentials")
    r = local_admin.post(CODE, json={"password": LOCAL_BOOT_PASSWORD})
    assert r.status_code == 201
    code = r.json()["code"]
    assert len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET)
    assert local_admin.get("/me/channels").json()["teams"]["code_expires_at"] is not None


def test_the_code_is_stored_as_a_keyed_hmac(local_admin, mongo):
    """코드는 플랫폼 서명 키에서 만든 키의 HMAC 이다. 키 없이 sha256 으로 거꾸로 찾을 수 없다."""
    code = local_admin.post(CODE, json={"password": LOCAL_BOOT_PASSWORD}).json()["code"]
    stored = mongo["channel_link_codes"].find_one({})
    assert code not in repr(stored) and stored["tv"] == 0
    assert stored["code_hmac"] != hashlib.sha256(code.encode()).hexdigest()
    assert len(stored["code_hmac"]) == 64


@pytest.fixture
def oidc_settings(oidc_settings):
    return with_ui_client(oidc_settings)


def test_an_oidc_code_needs_a_recent_sign_in(oidc, token):
    headers = {"Authorization": f"Bearer {token('p1', 'daegam-admins')}"}
    assert detail_code(oidc.post(CODE, json={}, headers=headers)) == (403, "reauth_required")
    recent = {**headers, **recent_id_token(token, "p1")}
    r = oidc.post(CODE, json={}, headers=recent)
    assert r.status_code == 201, r.text


# -- 끊기 ------------------------------------------------------------------------------------


def _admin_id(mongo) -> str:
    return mongo["users"].find_one({"username": "admin"})["_id"]


@pytest.fixture
def linked(local_admin, mongo, fake_secrets) -> tuple[TestClient, FakeMicrosoft]:
    configure_teams(mongo, fake_secrets)
    fake = FakeMicrosoft(Signer(None))
    attach(local_admin.app, fake)
    return local_admin, fake


def test_disabling_the_account_cuts_its_link(linked, mongo):
    c, _ = linked
    r = c.post("/users", json={"username": "kim", "password": "kim-password-1234"})
    assert r.status_code == 201, r.text
    kim = r.json()["id"]
    plant_link(mongo, kim)
    plant_link(mongo, _admin_id(mongo))
    assert c.patch(f"/users/{kim}", json={"disabled": True}).status_code == 200
    assert mongo["channel_links"].find_one({"sub": kim}) is None
    assert mongo["channel_links"].count_documents({}) == 1
    row = mongo["audit_events"].find_one({"action": "teams_links_cut", "error_code": "account"})
    assert row is not None


def test_a_person_unlinks_on_the_account_menu(linked, mongo):
    c, _ = linked
    plant_link(mongo, _admin_id(mongo))
    assert c.delete("/me/channels/teams").json() == {"unlinked": True}
    assert c.delete("/me/channels/teams").json() == {"unlinked": False}
    assert detail_code(c.patch("/me/channels/teams", json={"language": "en"})) == (
        404,
        "not_found",
    )


def test_the_sweep_cuts_ended_idle_and_lost_links(linked, mongo):
    c, fake = linked
    now = utcnow()
    admin = _admin_id(mongo)
    ended = plant_link(mongo, admin, ends_at=now - timedelta(minutes=1))
    idle = plant_link(mongo, "ghost-1", last_turn_at=now - timedelta(days=91))
    gone = plant_link(mongo, "ghost-2")
    other_bot = plant_link(mongo, "ghost-3", app_id="11111111-2222-3333-4444-555555555555")
    other_tenant = plant_link(mongo, "ghost-4", tenant="00000000-0000-0000-0000-000000000000")
    result = run_step(c, "channel_links")
    assert set(result["cut"]) == {ended, idle, gone, other_bot, other_tenant}
    assert mongo["channel_links"].count_documents({}) == 0
    # 끝이나 쓰지 않아 끊긴 두 연결의 대화에 한 줄씩이다
    assert sorted(fake.texts()) == [KO["bot.linkEnded"]] * 2
    reasons = {r["error_code"] for r in mongo["audit_events"].find({"action": "teams_links_cut"})}
    assert reasons == {"ended", "idle", "account", "bot_changed"}


def test_the_sweep_keeps_a_valid_link(linked, mongo):
    c, fake = linked
    plant_link(mongo, _admin_id(mongo))
    assert run_step(c, "channel_links") == {}
    assert mongo["channel_links"].count_documents({}) == 1 and fake.posted == []


# -- 턴마다의 사람 확인 -------------------------------------------------------------------------


def _lost(check, **kw) -> str | None:
    return asyncio.run(check.lost(BOUND, "default", **kw))


def test_the_scope_to_require_is_a_parameter():
    """viewer 는 `agent:chat` 이 있고 `agent:write` 가 없다. Teams 턴은 되고 실행은 안 된다."""
    check = person_check(
        LOCAL, FakeUsers(alice(last_login_at=utcnow())), ("alice", "default", "viewer")
    )
    assert _lost(check) == CAUSE_ROLE
    assert _lost(check, scopes=RUN_SCOPES) == CAUSE_ROLE
    assert _lost(check, scopes={"agent:chat"}, strict=True) is None


def test_strict_needs_a_recent_local_sign_in():
    rows = ("alice", "default", "developer")
    old = utcnow() - LOCAL.idp_confirm_bound - timedelta(hours=1)
    fresh = person_check(LOCAL, FakeUsers(alice(last_login_at=utcnow())), rows)
    stale = person_check(LOCAL, FakeUsers(alice(last_login_at=old)), rows)
    never = person_check(LOCAL, FakeUsers(alice()), rows)
    assert _lost(stale) is None  # 실행은 마지막 로그인을 보지 않는다
    assert _lost(fresh, strict=True) is None
    assert _lost(stale, strict=True) == CAUSE_UNCONFIRMED
    assert _lost(never, strict=True) == CAUSE_UNCONFIRMED


def test_strict_refuses_an_oidc_person_without_a_record():
    rows = ("alice", "default", "developer")
    nobody = person_check(OIDC, None, rows, people=FakePeople())
    assert _lost(nobody) is None  # 실행은 Agent 의 이름으로 돈다 (P53b 6.3절)
    assert _lost(nobody, strict=True) == CAUSE_UNCONFIRMED
    known = person_check(OIDC, None, rows, people=FakePeople(idp_person("alice")))
    assert _lost(known, strict=True) is None
