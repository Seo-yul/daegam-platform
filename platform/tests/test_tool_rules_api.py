"""P52 F1: Tool 규칙의 API. MCP Server·Gateway 정의와 허브 image 항목의 저장, 0.53 거절."""

from __future__ import annotations

from daegam.store.agents import Agent, AgentSpec
from tests.helpers.backend_apis import uploaded_backend
from tests.helpers.clusters import default_cluster, record_toolhive
from tests.helpers.consts import GW, HUB, MS, OTHER
from tests.helpers.gateways import create_gateway
from tests.helpers.http import detail_code
from tests.helpers.hub import hub_env, shared_image_env
from tests.helpers.mcp_servers import create_server

DEV = {"callers": {"roles": ["developer"]}}


def _server(client, bp=None, name="calc-mcp", **rules):
    bp = bp or uploaded_backend(client)
    body = {
        "name": name,
        "backends": [
            {"backend_profile_id": bp["id"], "spec_version": 1, "tools": [], "tool_rules": rules}
        ],
    }
    return bp, client.post(MS, json=body)


def test_rules_are_stored_with_the_revision_and_answered(client, mongo):
    cond = {"path": "a", "op": "max", "value": 10}
    _bp, r = _server(client, Add={"callers": {"roles": ["viewer"]}, "conditions": [cond]})
    assert r.status_code == 201, r.text
    rule = r.json()["backends"][0]["tool_rules"]["Add"]
    assert rule["callers"]["roles"] == ["viewer"] and rule["conditions"][0]["path"] == "a"
    doc = mongo["mcp_servers"].find_one({"_id": r.json()["id"]})
    assert doc["revisions"][0]["backends"][0]["tool_rules"]["Add"]["callers"]["roles"] == ["viewer"]


def test_a_server_without_rules_stores_no_rule_field(client, mongo):
    bp = uploaded_backend(client)
    r = create_server(client, bp, tools=["Add"])
    assert r.json()["backends"][0]["tool_rules"] == {}
    doc = mongo["mcp_servers"].find_one({"_id": r.json()["id"]})
    assert "tool_rules" not in doc["spec"]["backends"][0]


def test_unknown_keys_conditions_and_agents_are_refused(client):
    bp = uploaded_backend(client)
    base = {"backend_profile_id": bp["id"], "spec_version": 1}

    def post(rules, tools=()):
        return client.post(
            MS,
            json={"name": "x", "backends": [{**base, "tools": list(tools), "tool_rules": rules}]},
        )

    assert detail_code(post({"Divide": DEV}, tools=["Add"])) == (422, "rule_names_unknown_tool")
    bad_path = {"conditions": [{"path": "c", "op": "equals", "value": 1}]}
    assert detail_code(post({"Add": bad_path})) == (422, "rule_unknown_argument")
    bad_type = {"conditions": [{"path": "a", "op": "prefix", "value": "x"}]}
    assert detail_code(post({"Add": bad_type})) == (422, "rule_argument_type")
    ghost = {"callers": {"agents": ["no-such-agent"]}}
    assert detail_code(post({"Add": ghost})) == (422, "rule_unknown_agent")
    # 그룹은 oidc 모드에서만 받는다 (테스트 앱은 header 모드다)
    assert detail_code(post({"Add": {"callers": {"groups": ["eng"]}}})) == (422, "invalid_value")


def test_conditions_on_a_custom_image_are_refused(client):
    bp = uploaded_backend(client)
    sel = {
        "backend_profile_id": bp["id"],
        "spec_version": 1,
        "tool_rules": {"Add": {"conditions": [{"path": "a", "op": "min", "value": 0}]}},
    }
    r = client.post(MS, json={"name": "x", "backends": [sel], "runtime": {"image": "me/mcp:1"}})
    assert detail_code(r) == (422, "rule_custom_image")


def stored_agent(mongo, name: str, tenant: str = "default") -> str:
    """정의만 있는 Agent 문서를 넣고 그 id 를 돌려준다."""
    doc = Agent(tenant=tenant, name=name, display_name=name, spec=AgentSpec())
    mongo["agents"].insert_one(doc.to_doc())
    return doc.id


def test_agent_callers_must_be_agents_of_the_tenant(client, mongo):
    other = stored_agent(mongo, "far", tenant=OTHER)
    bp, r = _server(client, Add={"callers": {"agents": [other]}})
    assert detail_code(r) == (422, "rule_unknown_agent")
    mine = stored_agent(mongo, "helper")
    _bp, r = _server(client, bp, Add={"callers": {"agents": [mine]}})
    assert r.status_code == 201, r.text


def test_a_put_without_rules_keeps_them_and_emptying_callers_answers_the_opening(client):
    bp, r = _server(client, Add=DEV, Divide=DEV)
    sid = r.json()["id"]
    keep = client.put(
        f"{MS}/{sid}",
        json={
            "name": "calc-mcp",
            "backends": [{"backend_profile_id": bp["id"], "spec_version": 1}],
        },
    )
    assert keep.status_code == 200, keep.text
    assert set(keep.json()["backends"][0]["tool_rules"]) == {"Add", "Divide"}
    assert keep.json()["rules_notice"] == {"opened": [], "rolling_agents": []}
    opened = client.put(
        f"{MS}/{sid}",
        json={
            "name": "calc-mcp",
            "backends": [
                {"backend_profile_id": bp["id"], "spec_version": 1, "tool_rules": {"Divide": DEV}}
            ],
        },
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["rules_notice"]["opened"] == [f"{bp['id']}/Add"]


def test_deploying_rules_below_toolhive_053_is_refused(client, mongo):
    cid = default_cluster(client)
    _bp, r = _server(client, Add=DEV)
    sid = r.json()["id"]
    for version in ("0.52.3", None, "latest"):
        record_toolhive(mongo, cid, version)
        assert detail_code(client.post(f"{MS}/{sid}/deploy", json={})) == (
            422,
            "rules_toolhive_old",
        )
    record_toolhive(mongo, cid, "0.53.0")
    assert client.post(f"{MS}/{sid}/deploy", json={}).status_code == 201


def test_read_only_marks_alone_deploy_on_older_toolhive(client, mongo):
    cid = default_cluster(client)
    _bp, r = _server(client, Add={"read_only": True})
    record_toolhive(mongo, cid, "0.52.3")
    assert client.post(f"{MS}/{r.json()['id']}/deploy", json={}).status_code == 201


# -- Gateway 와 허브 image 항목 ----------------------------------------------------------


def test_gateway_rules_name_offered_tools_and_survive_a_put_without_them(client, fake_clusters):
    _cid, items, *_ = hub_env(client, fake_clusters)
    calc = items["calculator"]["id"]
    backends = [{"hub_item_id": calc, "tools": ["Add"], "tool_rules": {"Divide": DEV}}]
    r = create_gateway(client, items, backends=backends)
    assert detail_code(r) == (422, "rule_names_unknown_gateway_tool")
    backends = [{"hub_item_id": calc, "tool_rules": {"Divide": DEV}}]
    r = create_gateway(client, items, backends=backends)
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json()["backends"][0]["tool_rules"]["Divide"]["callers"]["roles"] == ["developer"]
    put = client.put(f"{GW}/{gid}", json={"name": "a", "backends": [{"hub_item_id": calc}]})
    assert put.status_code == 200, put.text
    assert "Divide" in put.json()["backends"][0]["tool_rules"]
    assert put.json()["rules_notice"]["opened"] == []
    opened = client.put(
        f"{GW}/{gid}", json={"name": "a", "backends": [{"hub_item_id": calc, "tool_rules": {}}]}
    )
    assert opened.json()["rules_notice"]["opened"] == [f"{calc}/Divide"]


def test_a_gateway_with_owner_cedar_refuses_rules(client, fake_clusters, mongo):
    _cid, items, *_ = hub_env(client, fake_clusters)
    calc = items["calculator"]["id"]
    gid = create_gateway(client, items, backends=[{"hub_item_id": calc}]).json()["id"]
    owner = {"spec": {"incomingAuth": {"authzConfig": {"inline": {"policies": []}}}}}
    mongo["gateways"].update_one({"_id": gid}, {"$set": {"overrides": owner}})
    backends = [{"hub_item_id": calc, "tool_rules": {"Add": DEV}}]
    r = client.put(f"{GW}/{gid}", json={"name": "a", "backends": backends})
    assert detail_code(r) == (422, "rules_owner_cedar")


def test_image_item_rules_take_roles_pin_the_tool_list_and_answer_the_opening(
    client, fake_clusters
):
    item, _fake = shared_image_env(client, fake_clusters)
    url = f"{HUB}/{item['id']}/tool-rules"
    assert detail_code(client.put(url, json={"rules": {"Nope": DEV}})) == (
        422,
        "rule_names_unknown_tool",
    )
    agents = {"callers": {"agents": ["a1"]}}
    assert detail_code(client.put(url, json={"rules": {"Echo": agents}})) == (422, "invalid_value")
    r = client.put(url, json={"rules": {"Echo": {**DEV, "read_only": True}}})
    assert r.status_code == 200, r.text
    assert r.json()["tool_rules"]["Echo"]["read_only"] is True
    assert r.json()["rules_tool_names"] == ["Echo"]
    r = client.put(url, json={"rules": {"Echo": {"read_only": True}}})
    assert r.json()["rules_notice"]["opened"] == ["Echo"]
    assert r.json().get("rules_tool_names", []) == []


def test_only_image_items_hold_rules(client, fake_clusters):
    _cid, items, *_ = hub_env(client, fake_clusters)
    r = client.put(f"{HUB}/{items['spike']['id']}/tool-rules", json={"rules": {"x": DEV}})
    assert detail_code(r) == (422, "invalid_kind")
