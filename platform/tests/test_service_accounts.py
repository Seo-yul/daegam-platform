"""서비스 어카운트와 그 토큰 (P53b 6.7절).

테넌트 관리자가 이름과 역할로 계정을 만들고, 개인 토큰과 같은 슬롯 키로 토큰을 낸다. 토큰의 `sub`
는 `sa:<계정 id>` 이고 테넌트의 고정 그룹과 계정의 역할만 싣는다. 접근 정리는 그 토큰을 계정에
맞댄다. `DAEGAM_SERVICE_ACCOUNTS` 가 누가 넓히는지를 정하고, off 면 있는 토큰을 모두 회수한다.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from daegam.api.schemas.service_accounts import ServiceAccountsMode
from daegam.auth import rbac
from daegam.config import SERVICE_ACCOUNTS_MODES
from daegam.services import personal_tokens as pat
from tests.helpers.access import revoked, sweep
from tests.helpers.auth import (
    admin_headers,
    bearer,
    login,
    map_groups,
    recent_id_token,
    with_ui_client,
)
from tests.helpers.http import api_client, detail_code, make_app
from tests.helpers.oauth import OTHER_TEAM, PAT_TENANT, Person, issued, join, person
from tests.helpers.svc_tokens import claims_of

TENANT = PAT_TENANT
BASE = f"/tenants/{TENANT}/service-accounts"


def base(tenant: str = TENANT) -> str:
    return f"/tenants/{tenant}/service-accounts"


def create(
    c: TestClient, who: dict[str, str], name: str = "ci", role: str = "developer", **kw: str
):
    return c.post(base(**kw), json={"name": name, "role": role}, headers=who)


def issue_for(c: TestClient, who: Person, account_id: str, **body):
    payload = {"label": "pipeline", "password": who.password, **body}
    return c.post(f"{BASE}/{account_id}/tokens", json=payload, headers=who.headers)


def claims(body: dict) -> dict:
    return claims_of(body["token"])


@pytest.fixture
def ta(local_client, admin, mongo) -> Person:
    """team-a 의 tenant-admin."""
    return person(local_client, admin, mongo, "ta", TENANT, role="tenant-admin")


def test_the_mode_literal_matches_the_setting():
    assert get_args(ServiceAccountsMode) == SERVICE_ACCOUNTS_MODES


def test_a_tenant_admin_makes_an_account_and_its_token(local_client, ta, mongo):
    """토큰의 sub 는 `sa:<계정 id>` 이고 고정 그룹과 계정의 역할만 싣는다. 이름 클레임, act, IdP
    그룹은 없다. 만료를 고르지 않으면 90일이다. 목록은 만든 사람과 토큰을 보인다. 접근 정리는 계정이
    있는 토큰을 두고, 같은 이름의 계정은 409 다."""
    c = local_client
    r = create(c, ta.headers)
    assert r.status_code == 201, r.text
    account = r.json()
    assert (account["name"], account["role"], account["created_by"]) == ("ci", "developer", ta.id)
    assert detail_code(create(c, ta.headers)) == (409, "name_conflict")
    r = issue_for(c, ta, account["account_id"])
    assert r.status_code == 201, r.text
    got = claims(r.json())
    assert got["sub"] == f"sa:{account['account_id']}" and got["tenant"] == TENANT
    assert got["groups"] == [f"daegam-{TENANT}"]
    assert got["daegam_roles"] == [rbac.role_group(TENANT, "developer")]
    assert not {"preferred_username", "act", "tv"} & set(got)
    lifetime = got["exp"] - got["iat"]
    assert abs(lifetime - timedelta(days=90).total_seconds()) < 5
    doc = mongo["personal_tokens"].find_one({"_id": r.json()["token_id"]})
    assert (doc["username"], doc["tv"]) == ("ci", 0)
    listed = c.get(BASE, headers=ta.headers).json()
    assert (listed["mode"], listed["can_manage"]) == ("tenant-admin", True)
    [row] = listed["accounts"]
    assert row["created_by_name"] == "ta" and [t["token_id"] for t in row["tokens"]] == [
        r.json()["token_id"]
    ]
    assert sweep(c)["revoked"] == []


def test_issuing_asks_the_issuers_password_and_shares_the_limits(
    local_client, admin, ta, mongo, monkeypatch
):
    """local 은 내는 사람의 비밀번호를 다시 받는다. 만료는 DAEGAM_PAT_MAX_DAYS 까지이고 상한은 개인
    토큰과 함께 센다."""
    c = local_client
    account_id = create(c, ta.headers).json()["account_id"]
    r = issue_for(c, ta, account_id, password="wrong password")
    assert detail_code(r) == (403, "invalid_credentials")
    r = issue_for(c, ta, account_id, password=None)
    assert detail_code(r) == (422, "invalid_value")
    days = (datetime.now(UTC) + timedelta(days=400)).isoformat()
    assert detail_code(issue_for(c, ta, account_id, expires_at=days)) == (422, "invalid_value")
    issued(c, person(c, admin, mongo, "pat", TENANT))
    monkeypatch.setattr(pat, "MAX_LIVE_TOTAL", 1)
    assert detail_code(issue_for(c, ta, account_id)) == (429, "token_limit")


def test_lowering_the_role_or_deleting_the_account_revokes_its_tokens(local_client, ta, mongo):
    """역할을 낮추면 계정의 토큰이 `role_changed` 로, 계정을 지우면 남은 토큰이 `deleted` 로
    회수된다. 올린 역할은 토큰을 회수하지 않는다. 관리자의 회수는 `admin` 이다."""
    c = local_client
    account_id = create(c, ta.headers).json()["account_id"]
    first = issue_for(c, ta, account_id).json()["token_id"]
    raised = c.patch(f"{BASE}/{account_id}", json={"role": "tenant-admin"}, headers=ta.headers)
    assert raised.status_code == 200 and revoked(mongo, first) is None
    lowered = c.patch(f"{BASE}/{account_id}", json={"role": "developer"}, headers=ta.headers)
    assert lowered.status_code == 200 and lowered.json()["role"] == "developer"
    assert revoked(mongo, first) == ("membership", "role_changed")
    second = issue_for(c, ta, account_id).json()["token_id"]
    third = issue_for(c, ta, account_id).json()["token_id"]
    r = c.delete(f"{BASE}/{account_id}/tokens/{third}", headers=ta.headers)
    assert r.status_code == 200 and revoked(mongo, third) == ("admin", None)
    assert c.delete(f"{BASE}/{account_id}", headers=ta.headers).status_code == 200
    assert revoked(mongo, second) == ("deleted", None)
    assert c.get(BASE, headers=ta.headers).json()["accounts"] == []
    assert c.delete(f"{BASE}/{account_id}", headers=ta.headers).status_code == 404


def test_accounts_stay_in_their_tenant(local_client, ta, admin, mongo):
    """다른 테넌트의 계정은 제 테넌트 경로로 부르면 없는 것(404)이고, 그 테넌트의 경로로 부르면
    `members:write` 가 없어 403 이다. 설정 문서도 소속 줄도 없는 테넌트에는 계정을 만들지 못한다."""
    c = local_client
    join(c, admin, OTHER_TEAM, (ta.id, "viewer"))
    theirs = create(c, admin, name="theirs", tenant=OTHER_TEAM).json()["account_id"]
    assert c.delete(f"{BASE}/{theirs}", headers=ta.headers).status_code == 404
    assert issue_for(c, ta, theirs).status_code == 404
    other = base(OTHER_TEAM)
    assert c.delete(f"{other}/{theirs}", headers=ta.headers).status_code == 403
    assert c.get(other, headers=ta.headers).status_code == 403
    assert detail_code(create(c, admin, tenant="nowhere")) == (404, "not_found")


def test_only_tenant_admins_and_never_in_header_mode(local_client, ta, admin, mongo, client):
    """`members:write` 가 없는 멤버는 403 이고, header 모드에는 서비스 어카운트가 없다."""
    viewer = person(local_client, admin, mongo, "viewer", TENANT)
    assert create(local_client, viewer.headers).status_code == 403
    assert local_client.get(BASE, headers=viewer.headers).status_code == 403
    assert client.get(BASE).status_code == 404


@pytest.fixture
def mode_client(local_settings, mongo, fake_clusters, fake_secrets):
    """DAEGAM_SERVICE_ACCOUNTS 를 바꾼 local 플랫폼을 차례로 띄운다."""
    opened: list = []

    def start(mode: str) -> TestClient:
        settings = dataclasses.replace(local_settings, service_accounts=mode)
        c = api_client(make_app(settings, fake_clusters, fake_secrets))
        opened.append(c)
        return c.__enter__()

    yield start
    for c in opened:
        c.__exit__(None, None, None)


def test_platform_admin_mode_keeps_tokens_and_off_revokes_them(mode_client, mongo):
    """platform-admin 이면 테넌트 관리자는 만들지도 토큰을 내지도 못하고 회수와 지우기만 한다. 이미
    낸 토큰은 남는다. off 면 아무도 넓히지 못하고(409) 접근 정리가 남은 토큰을 `admin` 과
    `accounts_off` 로 회수한다. 목록은 그대로 보인다."""
    c = mode_client("tenant-admin")
    root = admin_headers(c, mongo)
    ta = person(c, root, mongo, "ta", TENANT, role="tenant-admin")
    account_id = create(c, ta.headers).json()["account_id"]
    kept = issue_for(c, ta, account_id).json()["token_id"]

    c = mode_client("platform-admin")
    ta = dataclasses.replace(ta, headers=bearer(login(c, "ta", ta.password).json()["access_token"]))
    assert detail_code(create(c, ta.headers, name="other")) == (403, "forbidden")
    assert detail_code(issue_for(c, ta, account_id)) == (403, "forbidden")
    listed = c.get(BASE, headers=ta.headers).json()
    assert (listed["mode"], listed["can_manage"]) == ("platform-admin", False)
    assert sweep(c)["revoked"] == [] and revoked(mongo, kept) is None
    root = admin_headers(c, mongo)
    by_root = create(c, root, name="by-root").json()["account_id"]
    # 낮추는 것은 좁히는 일이라 테넌트 관리자도 한다. 올리는 것은 넓히는 일이다
    up = c.patch(f"{BASE}/{by_root}", json={"role": "tenant-admin"}, headers=ta.headers)
    assert detail_code(up) == (403, "forbidden")
    down = c.patch(f"{BASE}/{by_root}", json={"role": "viewer"}, headers=ta.headers)
    assert down.status_code == 200 and down.json()["role"] == "viewer"

    c = mode_client("off")
    root = admin_headers(c, mongo)
    assert detail_code(create(c, root, name="third")) == (409, "service_accounts_off")
    assert c.get(BASE, headers=root).json()["mode"] == "off"
    assert kept in sweep(c)["revoked"]
    assert revoked(mongo, kept) == ("admin", "accounts_off")


@pytest.fixture
def oidc_settings(oidc_settings):
    return with_ui_client(oidc_settings)


def test_oidc_mode_issues_after_a_recent_sign_in(oidc_client, token, mongo):
    """oidc 모드의 테넌트 관리자는 비밀번호 대신 10분 안의 IdP 로그인으로 토큰을 낸다."""
    c = oidc_client
    map_groups(mongo, [{"group": "a-admins", "tenant": TENANT, "role": "tenant-admin"}])
    headers = bearer(token("boss", "a-admins", email="boss@example.com"))
    account = create(c, headers).json()
    assert account["created_by_name"] == "boss@example.com"
    url = f"{BASE}/{account['account_id']}/tokens"
    r = c.post(url, json={"label": "ci"}, headers=headers)
    assert detail_code(r) == (403, "reauth_required")
    r = c.post(url, json={"label": "ci"}, headers={**headers, **recent_id_token(token, "boss")})
    assert r.status_code == 201, r.text
    assert claims(r.json())["sub"] == f"sa:{account['account_id']}"
