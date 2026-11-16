"""oidc 모드의 개인 MCP 토큰 (P53b 6.1·6.4·6.5·6.6절).

플랫폼은 oidc 모드에서도 사람의 데이터 플레인 토큰을 낸다. 사람은 사용자 문서 대신 사람 기록이고,
비밀번호 대신 10분 안의 IdP 로그인(`X-Daegam-Id-Token` 의 `auth_time`)을 본다. 토큰은 테넌트의 고정
그룹과, 사람 기록의 IdP 그룹 가운데 그 테넌트의 Tool 규칙과 노출 Agent 가 이름 댄 것을
`daegam-group:<tenant>:<group>` 꼴로 싣는다. 저장된 `mcp_group` 은 토큰에 닿지 않는다.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from daegam.auth import rbac
from daegam.auth.pat_keys import slot_jwk
from daegam.services.named_groups import NamedGroups
from daegam.store.exposure import AgentExposure
from tests.helpers.access import sweep
from tests.helpers.auth import bearer, recent_id_token, slot_keys, with_ui_client
from tests.helpers.consts import PUBLIC_URL
from tests.helpers.docs import MCP_SPEC, agent_doc, gateway_doc, mcp_server_doc
from tests.helpers.http import detail_code
from tests.helpers.oauth import PAT_TENANT, Person, person

MCP_AUDIENCE = "daegam-mcp"
TENANT = "a"
# 테넌트 a 의 developer 로 매핑된 그룹(tests/helpers/consts.py)과, 규칙이 이름 댄 그룹, 아무도 이름
# 대지 않은 그룹
DEV = "daegam-a-dev"
OPS = "ops"
STRAY = "team-x"


@pytest.fixture
def oidc_settings(oidc_settings):
    """UI 의 client id 와 공개 주소가 있는 oidc 플랫폼. 사람 발급자가 공개 주소다."""
    return with_ui_client(oidc_settings)


@pytest.fixture
def ruled(oidc, mongo) -> TestClient:
    """테넌트 a 의 Gateway 규칙이 OPS 와 DEV 를, Agent 노출이 readers 를 이름 댄다."""
    rule = {"t1": {"callers": {"groups": [OPS, DEV]}}}
    spec = {"backends": [{"hub_item_id": "h1", "tool_rules": rule}]}
    mongo["gateways"].insert_one(gateway_doc("gw-r", tenant=TENANT, spec=spec).to_doc())
    exposure = AgentExposure(allowed_groups=["readers"])
    mongo["agents"].insert_one(agent_doc("ag-r", tenant=TENANT, exposure=exposure).to_doc())
    return oidc


def signed_in(token, sub: str, *groups: str, age: float | None = 30) -> dict[str, str]:
    """sub 의 IdP 액세스 토큰과, age 가 있으면 그만큼 전의 로그인을 실은 ID 토큰 헤더."""
    headers = bearer(token(sub, *groups, email=f"{sub}@example.com"), tenant=TENANT)
    return headers if age is None else {**headers, **recent_id_token(token, sub, age=age)}


def post(c: TestClient, headers: dict[str, str], *, tenant: str = TENANT, **body):
    expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    payload = {"tenant": tenant, "label": "ci", "expires_at": expires_at, **body}
    return c.post("/me/tokens", json=payload, headers=headers)


def test_a_member_gets_a_platform_token_after_a_recent_sign_in(ruled, token, mongo):
    """`sub` 은 IdP 의 것, `iss` 는 플랫폼의 사람 발급자(공개 주소)다. 그룹은 고정 그룹과 규칙이
    이름 댄 IdP 그룹이고, `daegam-` 로 시작하는 그룹과 이름 대지 않은 그룹은 싣지 않는다. `tv` 는
    없고 문서의 tv 는 0 이다. 서명 키는 JWKS 에 오른 슬롯이다."""
    c = ruled
    r = post(c, signed_in(token, "p1", DEV, OPS, STRAY))
    assert r.status_code == 201, r.text
    body = r.json()
    plain = body.pop("token")
    kid = jwt.get_unverified_header(plain)["kid"]
    issuer = c.app.state.internal_issuer
    claims = jwt.decode(
        plain,
        jwt.PyJWK.from_dict(slot_jwk(issuer.signer, kid)).key,
        algorithms=["ES256"],
        audience=MCP_AUDIENCE,
        issuer=PUBLIC_URL,
    )
    assert claims["sub"] == "p1" and claims["tenant"] == TENANT
    assert claims["groups"] == ["daegam-a", rbac.idp_group(TENANT, OPS)]
    assert claims["daegam_roles"] == [rbac.role_group(TENANT, "developer")]
    assert "tv" not in claims and claims["preferred_username"] == "p1@example.com"
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    assert (doc["tv"], doc["group"], doc["idp_groups"]) == (0, "daegam-a", [OPS])
    assert body["group"] == "daegam-a" and body["status"] in ("active", "pending")
    jwks = c.portal.call(c.app.state.pat_keys.jwks)
    assert kid in {k["kid"] for k in slot_keys(jwks, issuer.kid)}
    # 접근 정리는 방금 낸 토큰을 그대로 둔다(고정 그룹, 지금 발급자, 지금 키)
    assert sweep(c)["revoked"] == []


def test_issuing_needs_a_recent_sign_in_of_the_same_person(ruled, token, mongo):
    """ID 토큰이 없거나 로그인이 10분(시계 여유 30초)보다 오래됐거나 남의 것이면 403
    `reauth_required` 이고 아무것도 남기지 않는다. 비밀번호는 보지 않는다."""
    c = ruled
    assert detail_code(post(c, signed_in(token, "p1", DEV, age=None))) == (403, "reauth_required")
    stale = signed_in(token, "p1", DEV, age=11 * 60)
    assert detail_code(post(c, stale)) == (403, "reauth_required")
    someone_else = {**signed_in(token, "p1", DEV), **recent_id_token(token, "p2")}
    assert detail_code(post(c, someone_else)) == (403, "reauth_required")
    assert mongo["personal_tokens"].count_documents({}) == 0
    assert post(c, signed_in(token, "p1", DEV), password="ignored").status_code == 201


def test_a_stored_mcp_group_changes_nothing_a_token_carries(ruled, token, mongo):
    """테넌트 관리자가 mcp_group 에 다른 테넌트의 고정 그룹을 적어도 토큰은 제 테넌트의 고정 그룹을
    싣는다 (P53b 6.1절)."""
    c = ruled
    mongo["tenant_settings"].insert_one({"_id": TENANT, "tenant": TENANT, "mcp_group": "daegam-b"})
    r = post(c, signed_in(token, "p1", DEV))
    assert r.status_code == 201, r.text
    claims = jwt.decode(r.json()["token"], options={"verify_signature": False})
    assert claims["groups"] == ["daegam-a"] and r.json()["group"] == "daegam-a"


@pytest.mark.parametrize("sub", ["svc:vmcp:gw", "sa:acct-1"])
def test_service_and_account_subjects_get_no_person_token(ruled, token, sub):
    """`svc:` 나 `sa:` 로 시작하는 IdP sub 은 개인 토큰을 받지 못한다. 경계가 그 토큰을 서비스나
    서비스 어카운트로 읽기 때문이다. 같은 이름의 서비스 어카운트 토큰을 읽거나 회수하지도 못한다."""
    headers = signed_in(token, sub, DEV)
    assert detail_code(post(ruled, headers)) == (403, "forbidden")
    assert detail_code(ruled.get("/me/tokens", headers=headers)) == (403, "forbidden")
    assert detail_code(ruled.delete("/me/tokens/t1", headers=headers)) == (403, "forbidden")


def test_only_a_member_and_only_a_recently_confirmed_person(ruled, token, mongo):
    """소속 줄이 없는 테넌트는 403 `not_a_member` 다. 사람 기록의 마지막 IdP 확인이 긴 한도를
    넘었으면(그 요청의 토큰도 오래전에 났으면) 403 `reauth_required` 다."""
    c = ruled
    r = post(c, signed_in(token, "p1", DEV), tenant="b")
    assert detail_code(r) == (403, "not_a_member")
    old = int(time.time() - timedelta(days=8).total_seconds())
    headers = bearer(token("p3", DEV, extra={"iat": old}), tenant=TENANT)
    headers.update(recent_id_token(token, "p3"))
    assert detail_code(post(c, headers)) == (403, "reauth_required")


def test_a_group_the_record_loses_revokes_the_token(ruled, token, mongo):
    """토큰이 실은 IdP 그룹이 사람 기록에서 빠지면 접근 정리가 `groups_changed` 로 회수하고, 목록은
    그 까닭을 보인다. 문서는 rc1 이 아는 `membership` 도 함께 적는다."""
    c = ruled
    r = post(c, signed_in(token, "p1", DEV, OPS))
    token_id = r.json()["token_id"]
    assert c.get("/me", headers=bearer(token("p1", DEV), tenant=TENANT)).status_code == 200
    assert sweep(c)["revoked"] == [token_id]
    [listed] = c.get("/me/tokens", headers=bearer(token("p1", DEV), tenant=TENANT)).json()
    assert (listed["status"], listed["revoke_reason"]) == ("revoked", "groups_changed")
    doc = mongo["personal_tokens"].find_one({"_id": token_id})
    assert (doc["revoke_reason"], doc["revoke_cause"]) == ("membership", "groups_changed")


def test_admin_token_routes_stay_local_and_header_mode_has_none(oidc, client, oidc_headers):
    """다른 사람의 토큰을 보는 관리자 라우트는 local 모드에만 있다. header 모드는 플랫폼이 사람
    토큰을 내지 않으므로 개인 토큰 라우트가 모두 404 다."""
    assert oidc.get("/users/p1/tokens", headers=oidc_headers).status_code == 404
    assert oidc.delete("/users/p1/tokens/t1", headers=oidc_headers).status_code == 404
    assert client.get("/me/tokens").status_code == 404


def test_local_mode_still_asks_for_the_password(local_client, admin, mongo):
    """local 모드는 비밀번호를 다시 받는다. 없으면 422 `invalid_value`(field password)다."""
    alice: Person = person(local_client, admin, mongo, "alice", PAT_TENANT)
    r = post(local_client, alice.headers, tenant=PAT_TENANT)
    assert detail_code(r) == (422, "invalid_value") and r.json()["detail"]["field"] == "password"
    assert post(local_client, alice.headers, tenant=PAT_TENANT, password=alice.password).is_success


def test_named_groups_read_rules_of_every_revision_and_agent_groups(oidc, mongo):
    """Gateway 와 MCP Server 는 지금 정의와 모든 리비전의 규칙을, Agent 는 노출의 allowed_groups 를
    읽는다. 다른 테넌트의 것은 섞이지 않고, 30초 동안 기억한다."""
    old_rule = {"op1": {"callers": {"groups": ["auditors"]}}}
    pinned = {"backends": [{**MCP_SPEC["backends"][0], "tool_rules": old_rule}]}
    server = mcp_server_doc("ms-r", tenant=TENANT, spec=MCP_SPEC, pinned=pinned)
    mongo["mcp_servers"].insert_one(server.to_doc())
    elsewhere = {"t": {"callers": {"groups": ["g"]}}}
    spec = {"backends": [{"hub_item_id": "h", "tool_rules": elsewhere}]}
    mongo["gateways"].insert_one(gateway_doc("gw-b", tenant="b", spec=spec).to_doc())
    readers = AgentExposure(allowed_groups=["readers"])
    mongo["agents"].insert_one(agent_doc("ag-a", tenant=TENANT, exposure=readers).to_doc())
    now = [0.0]
    state = oidc.app.state
    named = NamedGroups(state.mcp_servers, state.gateways, state.agents, clock=lambda: now[0])
    assert oidc.portal.call(named.of, TENANT) == {"auditors", "readers"}
    assert oidc.portal.call(named.of, "b") == {"g"}
    mongo["agents"].delete_many({})
    assert oidc.portal.call(named.of, TENANT) == {"auditors", "readers"}
    now[0] = 31.0
    assert oidc.portal.call(named.of, TENANT) == {"auditors"}


def test_carried_groups_skip_platform_names():
    assert rbac.carried_idp_groups(["ops", "daegam-a", "x", "ops"], {"ops", "daegam-a"}) == ["ops"]
    assert rbac.idp_group("a", "ops") == "daegam-group:a:ops"
