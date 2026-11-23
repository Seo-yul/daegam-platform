"""P52 F2: Tool 규칙이 내부 MCPServer·허브 image 항목 서버의 울타리에 실리는가.

규칙이 없으면 울타리는 오늘과 같다. 규칙이 있으면 울타리 뒤에 forbid 가 붙고, 이름을 바꾼
operationId 는 돌고 있는 백엔드 파드의 리비전이 쓰던 이름도 닫는다. 조건이 있는 Tool 은 조건을
지키는지 알 때까지 닫힌다. 0.53 아래 클러스터의 다시 렌더는 규칙을 든 서버를 그대로 둔다.
"""

from __future__ import annotations

from daegam.auth.cedar import tenant_fence_policies
from daegam.auth.rbac import ROLES_CLAIM
from daegam.k8s.labels import LABEL_ID, LABEL_PROFILE_REVISION
from daegam.k8s.templates.authz import AUTHZ_CONFIG_KIND
from daegam.services.advertised_names import gateway_exposed_name
from tests.helpers.cedar_eval import allowed
from tests.helpers.clusters import record_toolhive
from tests.helpers.consts import DEFAULT_NS, GV, GW, HUB, MS, OTHER, OTHER_NS
from tests.helpers.exposure import cluster_with_ingress, prep_ingress
from tests.helpers.gateways import cedar_policies, gateway_up, vmcp_of
from tests.helpers.http import detail_code
from tests.helpers.hub import clone_name_of, hub_env, other_tenant_gateway, shared_image_env
from tests.helpers.mcp_servers import clone_and_deploy, mcp_server_env
from tests.helpers.svc_tokens import claims_of, entry_token_secret
from tests.helpers.workers import run_data_plane_auth
from tests.test_tool_rules_api import stored_agent

DEV = {"callers": {"roles": ["developer"]}}


def forget_rule_caches(client, tenant: str = "default") -> None:
    """재적용 틱의 판 계산이 잠깐 기억한 테넌트의 사실과 규칙 표시를 버린다. 시험은 한 틱 안에서
    입력을 바꾸므로 다음 틱처럼 새로 읽게 한다."""
    state = client.app.state
    state.mcp_server_service.rule_scopes.forget(tenant)
    state.mcp_server_service._marks = None


def cedar_policies_of(fake, name: str, namespace: str) -> list[str]:
    return fake.get(GV, AUTHZ_CONFIG_KIND, f"{name}-authz", namespace)["spec"]["config"]["policies"]


def _policies(fake, name: str) -> list[str]:
    return cedar_policies_of(fake, name, DEFAULT_NS)


def _put_rules(client, bp, ms, rules, *, overrides=None):
    body = {
        "name": ms["name"],
        "backends": [
            {
                "backend_profile_id": bp["id"],
                "spec_version": 1,
                "tools": ["Add", "Divide"],
                "tool_rules": rules,
                "tool_overrides": overrides or {},
            }
        ],
        "bindings": [],
    }
    r = client.put(f"{MS}/{ms['id']}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _deployed(client, fake_clusters, rules, **kw):
    cid, bp, _ds, ms = mcp_server_env(client, ds=False)
    ms = _put_rules(client, bp, ms, rules, **kw)
    r = clone_and_deploy(client, ms, name="calc-mcp")
    assert r.status_code == 201, r.text
    return cid, bp, r.json(), fake_clusters.by_name["edge"]


PLATFORM = {"sub": "svc:platform", "tenant": "default"}
KNOWN_BOTH = (
    'forbid(principal, action == Action::"call_tool", resource) '
    'unless { resource in [Tool::"Add", Tool::"Divide"] };'
)


def test_a_server_without_rules_closes_only_the_names_it_does_not_know(client, fake_clusters):
    """규칙이 없는 서버도 울타리 뒤에 아는 이름 밖을 닫는 forbid 하나를 싣는다. 아는 Tool 과 Tool
    호출이 아닌 액션은 오늘과 같다."""
    _cid, _bp, _inst, fake = _deployed(client, fake_clusters, {})
    policies = _policies(fake, "mcp-calc-mcp")
    assert policies == [*tenant_fence_policies("default"), KNOWN_BOTH]
    assert allowed(policies, PLATFORM, "Add") and allowed(policies, PLATFORM, "Divide")
    assert not allowed(policies, PLATFORM, "Multiply")
    assert allowed(policies, PLATFORM, "anything", action="read_resource")


def test_header_mode_closes_conditioned_tools_but_renders_no_caller_rule(client, fake_clusters):
    cond = {"conditions": [{"path": "a", "op": "max", "value": 10}]}
    _cid, _bp, _inst, fake = _deployed(client, fake_clusters, {"Add": cond, "Divide": DEV})
    policies = _policies(fake, "mcp-calc-mcp")
    assert policies[:2] == tenant_fence_policies("default")
    assert policies[2:] == [
        KNOWN_BOTH,
        'forbid(principal, action == Action::"call_tool", resource) '
        'when { resource in [Tool::"Add"] };',
    ]
    vmcp = {"sub": "svc:vmcp:gw-a", "tenant": "default", "rules": "x"}
    assert not allowed(policies, vmcp, "Add") and allowed(policies, vmcp, "Divide")


def test_local_mode_renders_named_agents_the_vmcp_digest_and_the_platform(
    local_admin, fake_clusters, mongo
):
    agent = stored_agent(mongo, "helper")
    rules = {"Add": {"callers": {"roles": ["developer"], "agents": [agent]}}}
    _cid, _bp, _inst, fake = _deployed(local_admin, fake_clusters, rules)
    policies = _policies(fake, "mcp-calc-mcp")
    own = {"sub": "svc:agent:agent-helper", "agent_id": agent, "tenant": "default"}
    assert allowed(policies, own, "Add")
    assert not allowed(policies, {**own, "agent_id": "other"}, "Add")
    assert allowed(policies, {"sub": "svc:platform", "tenant": "default"}, "Add")
    assert not allowed(policies, {"sub": "svc:vmcp:gw-a", "tenant": "default"}, "Add")
    assert allowed(policies, {"sub": "svc:vmcp:gw-a", "tenant": "default"}, "Divide")


def test_a_rename_keeps_the_earlier_name_closed_while_old_pods_run(client, fake_clusters):
    """ToolHive 는 프록시를 백엔드보다 먼저 굴리므로 새 Cedar 가 옛 컨테이너를 만난다(F0). 옛
    리비전의 파드가 남아 있는 동안 옛 이름도 닫고, 파드가 없어지면 새 이름만 닫는다."""
    cond = {"conditions": [{"path": "a", "op": "min", "value": 0}]}
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": cond})
    labels = {LABEL_ID: inst["id"], LABEL_PROFILE_REVISION: str(inst["deployed_revision"])}
    fake.add_pod(DEFAULT_NS, "mcp-calc-mcp-0", labels)
    renamed = _put_rules(client, bp, inst, {"Add": cond}, overrides={"Add": {"name": "plus"}})
    r = client.post(f"{MS}/{renamed['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert 'Tool::"Add", Tool::"plus"' in _policies(fake, "mcp-calc-mcp")[-1]
    fake.delete("v1", "Pod", "mcp-calc-mcp-0", DEFAULT_NS)
    r = client.post(f"{MS}/{renamed['id']}/redeploy")
    assert r.status_code == 200, r.text
    assert _policies(fake, "mcp-calc-mcp")[-1].endswith('[Tool::"plus"] };')


def test_a_reapply_below_053_leaves_a_ruled_server_untouched(client, fake_clusters, mongo):
    """판이 0.53 아래로 알려지거나 읽지 못하면 다시 렌더가 규칙을 든 서버를 그대로 둔다. 판정이
    판의 입력이라 클러스터가 0.53 에 이르면 다시 렌더한다."""
    cond = {"conditions": [{"path": "a", "op": "min", "value": 0}]}
    cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": cond})
    assert inst["id"] not in run_data_plane_auth(client)["reapplied"]
    for version in ("0.52.3", "not-a-version"):
        record_toolhive(mongo, cid, version)
        start = len(fake.applied)
        run_data_plane_auth(client)
        assert [o for o in fake.applied[start:] if o["kind"] == "MCPServer"] == []
    record_toolhive(mongo, cid, "0.53.0")
    start = len(fake.applied)
    assert inst["id"] in run_data_plane_auth(client)["reapplied"]
    assert [o for o in fake.applied[start:] if o["kind"] == "MCPServer"] != []


def test_deleting_a_named_agent_rerenders_the_server_without_it(local_admin, fake_clusters, mongo):
    agent = stored_agent(mongo, "helper")
    rules = {"Add": {"callers": {"agents": [agent]}}}
    _cid, _bp, inst, fake = _deployed(local_admin, fake_clusters, rules)
    own = {"sub": "svc:agent:agent-helper", "agent_id": agent, "tenant": "default"}
    assert allowed(_policies(fake, "mcp-calc-mcp"), own, "Add")
    assert inst["id"] not in run_data_plane_auth(local_admin)["reapplied"]
    mongo["agents"].delete_one({"_id": agent})
    forget_rule_caches(local_admin)
    assert inst["id"] in run_data_plane_auth(local_admin)["reapplied"]
    assert not allowed(_policies(fake, "mcp-calc-mcp"), own, "Add")


def test_image_item_rules_pin_the_filter_and_admit_only_the_vmcp_and_platform(
    client, fake_clusters
):
    item, fake = shared_image_env(client, fake_clusters)
    before = _policies(fake, "hub-org-img")
    assert before == tenant_fence_policies("default")
    r = client.put(f"{HUB}/{item['id']}/tool-rules", json={"rules": {"Echo": DEV}})
    assert r.status_code == 200, r.text
    # header 모드라 누가 부를지의 규칙은 렌더하지 않는다. 고정한 Tool 목록은 그대로 렌더한다
    assert _policies(fake, "hub-org-img") == before
    tools = fake.get(GV, "MCPToolConfig", "hub-org-img-tools", DEFAULT_NS)
    assert tools["spec"]["toolsFilter"] == ["Echo"]


# -- Gateway 의 vMCP ------------------------------------------------------------------------


def _vmcp_policies(fake, gateway) -> list[str]:
    return cedar_policies(vmcp_of(fake, gateway["k8s"]["name"]), fake=fake)


def _local_gateway(local_admin, fake_clusters, **rules):
    _cid, items, calc, _orders, _remote, fake = hub_env(local_admin, fake_clusters)
    backends = [
        {"hub_item_id": items["calculator"]["id"], "tool_rules": rules},
        {"hub_item_id": items["orders"]["id"]},
    ]
    return items, calc, gateway_up(local_admin, items, backends=backends), fake


def test_gateway_rules_compile_by_advertised_name_and_pin_the_backend(local_admin, fake_clusters):
    items, _calc, gateway, fake = _local_gateway(local_admin, fake_clusters, Divide=DEV)
    policies = _vmcp_policies(fake, gateway)
    name = gateway_exposed_name(
        next(
            b["workload"]
            for b in gateway["deployed_backends"]
            if b["hub_item_name"] == "calculator"
        ),
        "Divide",
        gateway["deployed_conflict_resolution"],
        gateway["tool_overrides"],
    )
    person = {"sub": "u1", "groups": ["daegam-default"]}
    dev = {**person, ROLES_CLAIM: ["daegam-role:default:developer"]}
    assert allowed(policies, dev, name, role_claim=ROLES_CLAIM)
    assert not allowed(policies, {**person, ROLES_CLAIM: ["daegam-role:default:viewer"]}, name)
    assert allowed(policies, person, "Add", role_claim=ROLES_CLAIM)
    config = fake.get(GV, AUTHZ_CONFIG_KIND, f"{gateway['k8s']['name']}-authz", DEFAULT_NS)
    assert config["spec"]["config"]["role_claim_name"] == ROLES_CLAIM
    # 모두 고른 백엔드도 배포 스냅샷의 Tool 로 고정한다. 규칙이 없는 백엔드는 오늘처럼 비운다
    tools = vmcp_of(fake, gateway["k8s"]["name"])["spec"]["config"]["aggregation"]["tools"]
    calc = next(t for t in tools if t["workload"].endswith("calculator"))
    orders = next(t for t in tools if t["workload"].endswith("orders"))
    assert set(calc["filter"]) >= {"Add", "Divide"} and "filter" not in orders


def test_a_gateway_without_rules_renders_as_today(local_admin, fake_clusters):
    _items, _calc, gateway, fake = _local_gateway(local_admin, fake_clusters)
    config = fake.get(GV, AUTHZ_CONFIG_KIND, f"{gateway['k8s']['name']}-authz", DEFAULT_NS)
    assert "role_claim_name" not in config["spec"]["config"]
    assert not any("call_tool" in p for p in _vmcp_policies(fake, gateway))
    assert all(b.get("rules_digest") is None for b in gateway["deployed_backends"])


def test_a_server_rule_reaches_the_vmcp_with_a_digest_its_server_admits(
    local_admin, fake_clusters, mongo
):
    agent = stored_agent(mongo, "helper")
    items, calc, gateway, fake = _local_gateway(local_admin, fake_clusters)
    server = local_admin.get(f"{MS}/{calc['id']}").json()
    backends = server["backends"]
    backends[0]["tool_rules"] = {"Add": {"callers": {"agents": [agent]}}}
    r = local_admin.put(
        f"{MS}/{calc['id']}",
        json={"name": server["name"], "backends": backends, "bindings": server["bindings"]},
    )
    assert r.status_code == 200, r.text
    assert local_admin.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    r = local_admin.post(f"{GW}/{gateway['id']}/redeploy")
    assert r.status_code == 200, r.text
    gateway = r.json()
    calc_backend = next(
        b for b in gateway["deployed_backends"] if b["hub_item_name"] == "calculator"
    )
    digest = calc_backend["rules_digest"]
    assert digest and len(digest) == 16
    secret = entry_token_secret(gateway)
    assert secret.endswith(digest[:8])
    token = fake.get("v1", "Secret", secret, DEFAULT_NS)["stringData"]["token"]
    claims = claims_of(token)
    assert claims["rules"] == digest
    server_policies = _policies(fake, calc["k8s"]["name"])
    assert allowed(server_policies, claims, "Add")
    assert not allowed(server_policies, {**claims, "rules": "0" * 16}, "Add")
    # vMCP 에서는 그 Agent 만 부른다
    vmcp = _vmcp_policies(fake, gateway)
    own = {"sub": "svc:agent:agent-helper", "agent_id": agent, "tenant": "default"}
    assert allowed(vmcp, {**own, "groups": ["daegam-default"]}, "Add")
    assert not allowed(vmcp, {"sub": "u1", "groups": ["daegam-default"]}, "Add")


# -- 노출 앞단 ------------------------------------------------------------------------------


def _front(local_admin, fake_clusters, rules):
    _cid, bp, _ds, mp = cluster_with_ingress(local_admin)
    fake = fake_clusters.by_name["edge"]
    prep_ingress(fake)
    mp = _put_rules(local_admin, bp, mp, rules)
    r = clone_and_deploy(
        local_admin, mp, name="calc-mcp", exposure={"enabled": True, "host": "calc.e2e.local"}
    )
    assert r.status_code == 201, r.text
    return bp, r.json(), fake


def test_the_front_holds_role_rules_and_a_rule_naming_only_an_agent_is_closed_there(
    local_admin, fake_clusters, mongo
):
    agent = stored_agent(mongo, "helper")
    rules = {"Add": DEV, "Divide": {"callers": {"agents": [agent]}}}
    _bp, inst, fake = _front(local_admin, fake_clusters, rules)
    front = f"front-{inst['k8s']['name']}"
    config = fake.get(GV, AUTHZ_CONFIG_KIND, f"{front}-authz", DEFAULT_NS)["spec"]["config"]
    assert config["role_claim_name"] == ROLES_CLAIM
    policies = config["policies"]
    person = {"sub": "u1", "groups": ["daegam-default"]}
    dev = {**person, ROLES_CLAIM: ["daegam-role:default:developer"]}
    assert allowed(policies, dev, "Add", role_claim=ROLES_CLAIM)
    assert not allowed(policies, person, "Add", role_claim=ROLES_CLAIM)
    assert not allowed(policies, dev, "Divide", role_claim=ROLES_CLAIM)
    # 서버는 앞단을 이름으로 받는다
    server = _policies(fake, inst["k8s"]["name"])
    claims = {"sub": f"svc:expose:{inst['k8s']['name']}", "tenant": "default"}
    assert allowed(server, claims, "Add") and allowed(server, claims, "Divide")
    assert not allowed(server, {**claims, "sub": "svc:expose:other"}, "Add")


def test_a_redeploy_applies_the_front_before_an_existing_server(local_admin, fake_clusters):
    bp, inst, fake = _front(local_admin, fake_clusters, {"Add": DEV})
    _put_rules(local_admin, bp, inst, {"Add": {"callers": {"roles": ["viewer"]}}})
    start = len(fake.applied)
    r = local_admin.post(f"{MS}/{inst['id']}/redeploy")
    assert r.status_code == 200, r.text
    kinds = [o["kind"] for o in fake.applied[start:]]
    assert kinds.index("VirtualMCPServer") < kinds.index("MCPServer")


def test_a_gateway_rerenders_when_its_backend_servers_rules_change(
    local_admin, fake_clusters, mongo
):
    """서버 규칙의 지문이 Gateway 판의 입력이라, 서버를 다시 배포하면 다음 재적용이 Gateway 의
    Entry 토큰을 새 지문으로 낸다. 그 전까지 vMCP 는 서버가 받지 않는 옛 지문을 싣는다."""
    _items, calc, gateway, fake = _local_gateway(local_admin, fake_clusters)
    assert gateway["id"] not in run_data_plane_auth(local_admin)["reapplied"]
    server = local_admin.get(f"{MS}/{calc['id']}").json()
    server["backends"][0]["tool_rules"] = {"Add": DEV}
    body = {"name": server["name"], "backends": server["backends"], "bindings": server["bindings"]}
    assert local_admin.put(f"{MS}/{calc['id']}", json=body).status_code == 200
    assert local_admin.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    assert gateway["id"] in run_data_plane_auth(local_admin)["reapplied"]
    doc = local_admin.get(f"{GW}/{gateway['id']}").json()
    calc_backend = next(b for b in doc["deployed_backends"] if b["hub_item_name"] == "calculator")
    token = fake.get("v1", "Secret", entry_token_secret(doc), DEFAULT_NS)["stringData"]["token"]
    assert claims_of(token)["rules"] == calc_backend["rules_digest"]
    assert allowed(_policies(fake, calc["k8s"]["name"]), claims_of(token), "Add")


# -- 상태 경고 ------------------------------------------------------------------------------


def _codes(status: dict) -> set[str]:
    return {w.split(":", 1)[0] for w in status["warnings"]}


def test_server_status_warns_of_header_mode_closed_conditions_and_old_toolhive(
    client, fake_clusters, mongo
):
    cond = {
        "callers": {"roles": ["viewer"]},
        "conditions": [{"path": "a", "op": "min", "value": 0}],
    }
    cid, _bp, inst, _fake = _deployed(client, fake_clusters, {"Add": cond})
    status = client.get(f"{MS}/{inst['id']}").json()["status"]
    assert {"rules_not_enforced", "conditions_closed"} <= _codes(status)
    assert "rules_toolhive_old" not in _codes(status)
    record_toolhive(mongo, cid, "0.52.0")
    assert "rules_toolhive_old" in _codes(client.get(f"{MS}/{inst['id']}").json()["status"])


def test_gateway_status_names_a_backend_behind_its_servers_digest(local_admin, fake_clusters):
    _items, calc, gateway, _fake = _local_gateway(local_admin, fake_clusters)
    assert not _codes(local_admin.get(f"{GW}/{gateway['id']}").json()["status"]) & {
        "rules_digest_behind"
    }
    server = local_admin.get(f"{MS}/{calc['id']}").json()
    server["backends"][0]["tool_rules"] = {"Add": DEV}
    body = {"name": server["name"], "backends": server["backends"], "bindings": server["bindings"]}
    assert local_admin.put(f"{MS}/{calc['id']}", json=body).status_code == 200
    assert local_admin.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    status = local_admin.get(f"{GW}/{gateway['id']}").json()["status"]
    assert "rules_digest_behind" in _codes(status)
    run_data_plane_auth(local_admin)
    status = local_admin.get(f"{GW}/{gateway['id']}").json()["status"]
    assert "rules_digest_behind" not in _codes(status)


# -- 이름 바꾸기, 0.53 아래, 항목과 복제본 -----------------------------------------------------


def test_a_rename_keeps_the_earlier_name_closed_through_a_gateway(local_admin, fake_clusters):
    """vMCP 는 지금 이름만 컴파일하므로 서버는 앞 이름에 vMCP 절을 싣지 않는다. 이름 바꾸기가
    굴러가는 동안 Gateway 를 거친 옛 이름의 호출은 서버에서 닫히고, 새 이름은 지문으로 열린다."""
    _items, calc, gateway, fake = _local_gateway(local_admin, fake_clusters)
    server = local_admin.get(f"{MS}/{calc['id']}").json()
    admin_rule = {"callers": {"roles": ["tenant-admin"]}}
    server["backends"][0]["tool_rules"] = {"Add": admin_rule}
    body = {"name": server["name"], "backends": server["backends"], "bindings": server["bindings"]}
    assert local_admin.put(f"{MS}/{calc['id']}", json=body).status_code == 200
    assert local_admin.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    deployed = local_admin.get(f"{MS}/{calc['id']}").json()
    labels = {LABEL_ID: calc["id"], LABEL_PROFILE_REVISION: str(deployed["deployed_revision"])}
    fake.add_pod(DEFAULT_NS, f"{calc['k8s']['name']}-0", labels)
    server["backends"][0]["tool_overrides"] = {"Add": {"name": "plus"}}
    body = {"name": server["name"], "backends": server["backends"], "bindings": server["bindings"]}
    assert local_admin.put(f"{MS}/{calc['id']}", json=body).status_code == 200
    assert local_admin.post(f"{MS}/{calc['id']}/redeploy").status_code == 200
    r = local_admin.post(f"{GW}/{gateway['id']}/redeploy")
    assert r.status_code == 200, r.text
    token = fake.get("v1", "Secret", entry_token_secret(r.json()), DEFAULT_NS)["stringData"]
    entry = claims_of(token["token"])
    policies = _policies(fake, calc["k8s"]["name"])
    assert allowed(policies, entry, "plus")
    assert not allowed(policies, entry, "Add")
    # 그 앞 이름을 직접 부르는 플랫폼은 그대로 받는다
    assert allowed(policies, {"sub": "svc:platform", "tenant": "default"}, "Add")


def test_advanced_paths_below_053_close_a_ruled_servers_tools(client, fake_clusters, mongo):
    """다시 렌더가 건너뛰는 판에서도 Advanced 저장·드리프트 되돌리기의 렌더는 규칙과 조건이 있는
    Tool 을 누구에게나 닫는다."""
    cond = {"conditions": [{"path": "a", "op": "min", "value": 0}]}
    cid, _bp, inst, fake = _deployed(client, fake_clusters, {"Add": cond, "Divide": DEV})
    record_toolhive(mongo, cid, "0.52.3")
    r = client.post(f"{MS}/{inst['id']}/drift/reset")
    assert r.status_code == 200, r.text
    policies = _policies(fake, "mcp-calc-mcp")
    assert policies[:2] == tenant_fence_policies("default")
    vmcp = {"sub": "svc:vmcp:gw-a", "tenant": "default", "rules": "x"}
    assert not allowed(policies, vmcp, "Add")
    # header 모드는 누가 부를지의 규칙을 지키지 않으므로 Divide 는 닫지 않는다
    assert allowed(policies, vmcp, "Divide")


def test_image_items_below_053_keep_their_spec_and_refuse_new_rules(client, fake_clusters, mongo):
    item, fake = shared_image_env(client, fake_clusters)
    url = f"{HUB}/{item['id']}/tool-rules"
    assert client.put(url, json={"rules": {"Echo": DEV}}).status_code == 200
    cid = mongo["hub_items"].find_one({"_id": item["id"]})["cluster_id"]
    record_toolhive(mongo, cid, "0.52.3")
    assert detail_code(client.put(url, json={"rules": {"Echo": DEV}})) == (
        422,
        "rules_toolhive_old",
    )
    assert client.put(url, json={"rules": {}}).status_code == 200
    mongo["hub_items"].update_one(
        {"_id": item["id"]},
        {"$set": {"tool_rules": {"Echo": DEV}, "rules_tool_names": ["Echo"], "render_epoch": 0}},
    )
    start = len(fake.applied)
    run_data_plane_auth(client)
    assert [o for o in fake.applied[start:] if o["metadata"]["name"] == "hub-org-img"] == []
    status = client.get(f"{HUB}/{item['id']}").json()["status"]
    assert "rules_toolhive_old" in _codes(status)


def test_item_and_clone_rules_admit_only_the_vmcp_with_the_items_digest(local_admin, fake_clusters):
    """local 모드에서 image 항목의 규칙은 그 항목 서버와 다른 테넌트의 복제본 서버에 vMCP 와
    플랫폼만 받는 forbid 로 실린다. 복제본을 쓰는 Gateway 의 Entry 토큰은 항목의 지문을 싣고 그
    복제본이 그것을 받는다."""
    item, fake = shared_image_env(local_admin, fake_clusters)
    viewer = {"callers": {"roles": ["viewer"]}}
    r = local_admin.put(f"{HUB}/{item['id']}/tool-rules", json={"rules": {"Echo": viewer}})
    assert r.status_code == 200, r.text
    own = _policies(fake, "hub-org-img")
    assert not allowed(own, {"sub": "svc:vmcp:gw-a", "tenant": "default", "rules": "x"}, "Echo")
    assert allowed(own, {"sub": "svc:platform", "tenant": "default"}, "Echo")
    _profile, deployed = other_tenant_gateway(local_admin, item)
    assert deployed.status_code == 201, deployed.text
    gw = deployed.json()
    backend = gw["deployed_backends"][0]
    assert backend["rules_digest"]
    secret = fake.get("v1", "Secret", entry_token_secret(gw), OTHER_NS)["stringData"]["token"]
    entry = claims_of(secret)
    assert entry["rules"] == backend["rules_digest"] and entry["tenant"] == OTHER
    clone = cedar_policies_of(fake, clone_name_of(item), OTHER_NS)
    assert allowed(clone, entry, "Echo")
    assert not allowed(clone, {**entry, "rules": "0" * 16}, "Echo")
    assert not allowed(clone, {"sub": "svc:agent:agent-x", "tenant": OTHER}, "Echo")


def test_pods_without_a_revision_label_close_every_earlier_name(client, fake_clusters):
    """파드가 있는데 리비전 라벨을 싣지 않으면 무엇이 도는지 모르므로 모든 리비전의 이름을 닫는다.
    ToolHive 가 podTemplateSpec 의 라벨을 백엔드 파드에 옮기는지는 아직 재지 않았다."""
    cond = {"conditions": [{"path": "a", "op": "min", "value": 0}]}
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {"Add": cond})
    fake.add_pod(DEFAULT_NS, "mcp-calc-mcp-0", {LABEL_ID: inst["id"]})
    renamed = _put_rules(client, bp, inst, {"Add": cond}, overrides={"Add": {"name": "plus"}})
    assert client.post(f"{MS}/{renamed['id']}/redeploy").status_code == 200
    assert 'Tool::"Add", Tool::"plus"' in _policies(fake, "mcp-calc-mcp")[-1]


# -- 낡은 정책이 새 백엔드를 만날 때 (2절) -----------------------------------------------------


def _selection(client, bp, ms, tools, *, rules=None, overrides=None):
    body = {
        "name": ms["name"],
        "backends": [
            {
                "backend_profile_id": bp["id"],
                "spec_version": 1,
                "tools": tools,
                "tool_rules": rules or {},
                "tool_overrides": overrides or {},
            }
        ],
        "bindings": [],
    }
    r = client.put(f"{MS}/{ms['id']}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_old_policy_refuses_every_caller_a_name_it_does_not_know(
    local_admin, fake_clusters, mongo
):
    """프록시가 옛 정책으로 도는 동안 백엔드가 먼저 바뀌어도, 옛 정책이 모르는 이름은 이름을 바꾼
    것이든 규칙과 함께 더한 것이든 규칙 없이 더한 것이든 누구에게도 열리지 않는다. 옛 정책이 아는
    이름은 규칙대로다."""
    agent = stored_agent(mongo, "helper")
    rules = {"Add": {"callers": {"roles": ["developer"], "agents": [agent]}}}
    _cid, _bp, inst, fake = _deployed(local_admin, fake_clusters, rules)
    old = _policies(fake, "mcp-calc-mcp")
    own = {"sub": "svc:agent:agent-helper", "agent_id": agent, "tenant": "default"}
    stranger = {"sub": "svc:agent:agent-other", "agent_id": "other", "tenant": "default"}
    front = {"sub": f"svc:expose:{inst['k8s']['name']}", "tenant": "default"}
    vmcp = {"sub": "svc:vmcp:gw-a", "tenant": "default", "rules": "x"}
    for caller in (own, stranger, front, vmcp, PLATFORM):
        for unknown in ("plus", "Multiply", "Extra"):
            assert not allowed(old, caller, unknown), (caller["sub"], unknown)
    assert allowed(old, own, "Add") and not allowed(old, stranger, "Add")
    assert allowed(old, PLATFORM, "Add") and allowed(old, stranger, "Divide")


def test_an_old_policy_with_conditions_refuses_an_unknown_name(client, fake_clusters):
    cond = {"conditions": [{"path": "a", "op": "max", "value": 10}]}
    _cid, _bp, _inst, fake = _deployed(client, fake_clusters, {"Add": cond})
    old = _policies(fake, "mcp-calc-mcp")
    assert not allowed(old, PLATFORM, "plus") and not allowed(old, PLATFORM, "Multiply")
    # 조건을 지키는지 모르는 동안 Add 는 지금처럼 닫혀 있고, 조건 없는 Divide 는 열려 있다
    assert not allowed(old, PLATFORM, "Add") and allowed(old, PLATFORM, "Divide")


def test_a_rename_deploy_applies_once_and_keeps_the_old_backends_names_callable(
    client, fake_clusters
):
    """새 정책이 옛 컨테이너를 만나는 동안 옛 이름도 불린다. 클러스터의 MCPServer 가 실은 리비전과
    돌고 있는 파드의 리비전이 내놓는 이름을 함께 안다. 그 리비전이 사라지면 이름도 빠진다."""
    _cid, bp, inst, fake = _deployed(client, fake_clusters, {})
    labels = {LABEL_ID: inst["id"], LABEL_PROFILE_REVISION: str(inst["deployed_revision"])}
    renamed = _selection(
        client, bp, inst, ["Add", "Divide"], overrides={"Divide": {"name": "over"}}
    )
    start = len(fake.applied)
    assert client.post(f"{MS}/{renamed['id']}/redeploy").status_code == 200
    assert [o["kind"] for o in fake.applied[start:]].count("MCPServer") == 1
    rolling = _policies(fake, "mcp-calc-mcp")
    for name in ("Add", "Divide", "over"):
        assert allowed(rolling, PLATFORM, name), name
    # 옛 리비전의 파드가 남아 있으면 다음 렌더도 그 이름을 안다
    fake.add_pod(DEFAULT_NS, "mcp-calc-mcp-0", labels)
    assert client.post(f"{MS}/{renamed['id']}/redeploy").status_code == 200
    assert allowed(_policies(fake, "mcp-calc-mcp"), PLATFORM, "Divide")
    fake.delete("v1", "Pod", "mcp-calc-mcp-0", DEFAULT_NS)
    assert client.post(f"{MS}/{renamed['id']}/redeploy").status_code == 200
    settled = _policies(fake, "mcp-calc-mcp")
    assert allowed(settled, PLATFORM, "over") and not allowed(settled, PLATFORM, "Divide")


def test_the_known_names_stay_out_of_the_rules_digest(local_admin, fake_clusters):
    """아는 이름은 지문에 들지 않는다. Tool 을 더해도 규칙의 forbid 는 글자까지 같아 Entry 토큰을
    다시 내지 않는다."""
    _cid, bp, _ds, ms = mcp_server_env(local_admin, ds=False)
    ms = _selection(local_admin, bp, ms, ["Add"], rules={"Add": DEV})
    r = clone_and_deploy(local_admin, ms, name="calc-mcp")
    assert r.status_code == 201, r.text
    fake = fake_clusters.by_name["edge"]
    before = _policies(fake, "mcp-calc-mcp")
    _selection(local_admin, bp, r.json(), ["Add", "Divide"], rules={"Add": DEV})
    assert local_admin.post(f"{MS}/{r.json()['id']}/redeploy").status_code == 200
    after = _policies(fake, "mcp-calc-mcp")
    assert before[2] != after[2] and "unless { resource in" in after[2]
    assert before[3:] == after[3:] and 'principal.claim_rules == "' in after[3]
