"""P52 F3: 사람의 토큰이 싣는 역할(`daegam_roles`), 역할이 낮아진 개인 토큰의 회수, 역할 그룹
접두사의 예약.

개인 토큰과 데이터 플레인 토큰은 발급 때 소속의 역할을 싣고, 개인 토큰은 그 역할을 문서에도
남긴다. 역할이 낮아지면 그 테넌트의 개인 토큰을 훅과 접근 정리(`role_changed`)가 회수한다. 역할을
남기지 않은 옛 토큰은 접근 정리가 건너뛴다.
"""

from __future__ import annotations

import jwt
import pytest

from daegam.auth.rbac import ROLES_CLAIM
from daegam.auth.service_token import turn_token_claims
from daegam.services.rule_compile import RuleScope, source_forbids
from daegam.store.tool_rules import ServerToolRule, ToolCallers
from tests.helpers.access import sweep as run_access_sweep
from tests.helpers.agent_runtime import deployed_agent
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.cedar_eval import allowed
from tests.helpers.consts import DEFAULT_NS, MS
from tests.helpers.docs import TENANT
from tests.helpers.http import detail_code
from tests.helpers.oauth import PAT_TENANT, Person, issued, join, person, refresh
from tests.helpers.svc_tokens import plant_key
from tests.helpers.workers import run_data_plane_auth


@pytest.fixture
def alice(local_client, admin, mongo) -> Person:
    return person(local_client, admin, mongo, "alice", PAT_TENANT)


def set_role(c, admin, tenant: str, sub: str, role: str) -> None:
    """tenant 의 manual 소속에서 sub 의 역할을 바꾼다."""
    rows = c.get(f"/tenants/{tenant}/members", headers=admin).json()
    body = [{"sub": m["sub"], "role": role if m["sub"] == sub else m["role"]} for m in rows]
    r = c.put(f"/tenants/{tenant}/members", json=body, headers=admin)
    assert r.status_code == 200, r.text


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def _rule(role: str) -> list[str]:
    scope = RuleScope(tenant=PAT_TENANT, mode="local")
    rule = ServerToolRule(callers=ToolCallers(roles=[role]))
    return ["permit(principal, action, resource);", *source_forbids({"a": rule}, scope, "vmcp")]


def test_a_personal_token_carries_its_role_and_passes_a_role_rule(
    local_client, admin, alice, mongo
):
    set_role(local_client, admin, PAT_TENANT, alice.id, "developer")
    body = issued(local_client, alice)
    claims = _claims(body["token"])
    assert claims[ROLES_CLAIM] == [f"daegam-role:{PAT_TENANT}:developer"]
    assert claims["groups"] == [f"daegam-{PAT_TENANT}"]
    assert mongo["personal_tokens"].find_one({"_id": body["token_id"]})["role"] == "developer"
    assert allowed(_rule("viewer"), claims, "a", role_claim=ROLES_CLAIM)
    assert allowed(_rule("developer"), claims, "a", role_claim=ROLES_CLAIM)
    assert not allowed(_rule("tenant-admin"), claims, "a", role_claim=ROLES_CLAIM)


def test_a_data_plane_token_carries_one_role_per_tenant(local_client, admin, alice, mongo):
    join(local_client, admin, "other-team", (alice.id, "tenant-admin"))
    r = local_client.post("/auth/token", headers=alice.headers)
    assert r.status_code == 200, r.text
    claims = _claims(r.json()["access_token"])
    assert sorted(claims[ROLES_CLAIM]) == sorted(
        [f"daegam-role:{PAT_TENANT}:viewer", "daegam-role:other-team:tenant-admin"]
    )


def test_lowering_a_role_revokes_that_tenants_personal_tokens(local_client, admin, alice, mongo):
    set_role(local_client, admin, PAT_TENANT, alice.id, "developer")
    body = issued(local_client, alice)
    # 올리는 것은 건드리지 않는다
    set_role(local_client, admin, PAT_TENANT, alice.id, "tenant-admin")
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    assert doc.get("revoke_reason") is None
    set_role(local_client, admin, PAT_TENANT, alice.id, "viewer")
    doc = mongo["personal_tokens"].find_one({"_id": body["token_id"]})
    # 2.0.0-rc1 이 모르는 사유는 새 필드에 두고 rc1 이 아는 사유를 함께 적는다 (P53b 6.5절)
    assert (doc["revoke_reason"], doc["revoke_cause"]) == ("membership", "role_changed")
    assert doc["revoked_at"] is not None
    [listed] = [
        t
        for t in local_client.get("/me/tokens", headers=alice.headers).json()
        if t["token_id"] == body["token_id"]
    ]
    assert listed["revoke_reason"] == "role_changed"


def test_the_sweep_revokes_a_lowered_role_and_skips_tokens_without_one(
    local_client, admin, alice, mongo
):
    set_role(local_client, admin, PAT_TENANT, alice.id, "developer")
    lowered = issued(local_client, alice, label="lowered")
    old = issued(local_client, alice, label="old")
    mongo["personal_tokens"].update_one({"_id": old["token_id"]}, {"$unset": {"role": ""}})
    # 훅을 거치지 않고 소속만 바꾼 것처럼 둔다
    mongo["memberships"].update_one(
        {"sub": alice.id, "tenant": PAT_TENANT}, {"$set": {"role": "viewer"}}
    )
    out = run_access_sweep(local_client)
    assert lowered["token_id"] in out["revoked"] and old["token_id"] not in out["revoked"]
    doc = mongo["personal_tokens"].find_one({"_id": lowered["token_id"]})
    assert (doc["revoke_reason"], doc["revoke_cause"]) == ("membership", "role_changed")


@pytest.mark.parametrize("prefix", ["daegam-role:", "daegam-group:"])
def test_group_mappings_and_mcp_group_cannot_use_the_platforms_prefixes(client, prefix):
    """플랫폼 토큰이 역할(P52 3절)과 IdP 그룹(P53b 6.6절)을 싣는 꼴은 예약돼 있다."""
    body = [{"group": f"{prefix}x:developer", "tenant": "default", "role": "developer"}]
    assert detail_code(client.put("/group-mappings", json=body)) == (422, "invalid_value")
    url = "/tenants/default/settings"
    r = client.patch(url, json={"mcp_group": f"{prefix}default:developer"})
    assert detail_code(r) == (422, "invalid_value")
    assert client.patch(url, json={"mcp_group": "team-default"}).status_code == 200


def test_an_oauth_refresh_carries_the_role_held_at_refresh(
    oauth_client, oauth_admin, client_id, connected, member, mongo
):
    claims = _claims(connected["access_token"])
    assert claims[ROLES_CLAIM] == [f"daegam-role:{TENANT}:viewer"]
    set_role(oauth_client, oauth_admin, TENANT, member.id, "developer")
    renewed = refresh(oauth_client, client_id, connected["refresh_token"])
    assert renewed.status_code == 200, renewed.text
    assert _claims(renewed.json()["access_token"])[ROLES_CLAIM] == [
        f"daegam-role:{TENANT}:developer"
    ]


# -- Agent 의 id (3절) -----------------------------------------------------------------------


def test_agent_service_tokens_carry_the_agent_id(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    secret = agent.fake.get("v1", "Secret", agent.doc["k8s"]["token_secret"], DEFAULT_NS)
    token = secret["stringData"]["mcp_token"]
    assert _claims(token)["agent_id"] == agent.id


def test_a_rule_naming_an_agent_reissues_its_tokens_once(client, fake_clusters, mongo):
    """이 기능 전에 낸 토큰(agent_id 없음)을 든 Agent 는 규칙이 그 Agent 를 이름 대면 다음 재적용이
    자기 id 를 실은 토큰으로 바꾼다. 이름 대지 않은 Agent 는 그대로다."""
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    name = agent.doc["k8s"]["token_secret"]
    issuer = client.app.state.internal_issuer
    old = issuer.mint(sub="svc:agent:agent-support", tenant="default", groups=["daegam-default"])
    plant_key(agent.fake, name, "mcp_token", old)
    assert agent.id not in run_data_plane_auth(client)["reapplied"]
    assert agent.fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["mcp_token"] == old
    bp = uploaded_backend(client, "named")
    body = {
        "name": "named-mcp",
        "backends": [
            {
                "backend_profile_id": bp["id"],
                "spec_version": 1,
                "tool_rules": {"Add": {"callers": {"agents": [agent.id]}}},
            }
        ],
    }
    server = client.post(MS, json=body)
    assert server.status_code == 201, server.text
    assert client.post(f"{MS}/{server.json()['id']}/deploy", json={}).status_code == 201
    client.app.state.agent_service.rule_scopes.forget("default")
    assert agent.id in run_data_plane_auth(client)["reapplied"]
    token = agent.fake.get("v1", "Secret", name, DEFAULT_NS)["stringData"]["mcp_token"]
    assert token != old and _claims(token)["agent_id"] == agent.id
    assert agent.id not in run_data_plane_auth(client)["reapplied"]


def test_turn_tokens_name_the_agent_in_act():
    claims = turn_token_claims(
        issuer="https://i",
        audience="a",
        sub="u1",
        ttl=60,
        actor="svc:agent:agent-helper",
        bound_to="gateway:g1",
        tenant="acme",
        username=None,
        agent_id="a1",
    )
    assert claims["act"] == {"sub": "svc:agent:agent-helper", "agent_id": "a1"}


def test_a_save_that_newly_names_a_deployed_agent_lists_it_as_rolling(client, fake_clusters, mongo):
    agent = deployed_agent(client, fake_clusters, mongo, runtime=False)
    bp = uploaded_backend(client, "named")
    sel = {"backend_profile_id": bp["id"], "spec_version": 1}
    sid = client.post(MS, json={"name": "named-mcp", "backends": [sel]}).json()["id"]
    named = {**sel, "tool_rules": {"Add": {"callers": {"agents": [agent.id]}}}}
    r = client.put(f"{MS}/{sid}", json={"name": "named-mcp", "backends": [named]})
    assert r.status_code == 200, r.text
    assert r.json()["rules_notice"]["rolling_agents"] == [{"id": agent.id, "name": "support"}]
    again = client.put(f"{MS}/{sid}", json={"name": "named-mcp", "backends": [named]})
    assert again.json()["rules_notice"]["rolling_agents"] == []
