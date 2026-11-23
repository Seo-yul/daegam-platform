"""P52 F2: Tool 규칙의 Cedar 를 cedarpy 로 ToolHive 의 principal 꼴에서 평가한다 (2절, F0 3번).

ToolHive 0.53 은 평가에 오류가 하나라도 있으면 거절하므로 모든 토큰에서 오류가 없어야 한다
(`tests/helpers/cedar_eval.py`). tools/list 는 Tool 마다 같은 `call_tool` 을 묻으므로 부를 수
있는지가 보이는지와 같다.
"""

from __future__ import annotations

import pytest

from daegam.auth.rbac import ROLES_CLAIM
from daegam.auth.rule_cedar import closing_forbid
from daegam.services.rule_compile import (
    RuleScope,
    item_policies,
    rules_digest,
    server_policies,
    source_forbids,
)
from daegam.store.mcp_servers import BackendSelection, MCPServerSpec, ToolOverride
from daegam.store.tool_rules import ArgCondition, ServerToolRule, ToolCallers
from tests.helpers.cedar_eval import allowed, evaluate

T = "acme"
BASE = ["permit(principal, action, resource);"]
AGENTS = {"a1": "agent-helper", "b1": "agent-other"}
LOCAL = RuleScope(tenant=T, mode="local", agents=AGENTS)
OIDC = RuleScope(
    tenant=T,
    mode="oidc",
    agents=AGENTS,
    role_groups={"developer": ("eng",), "tenant-admin": ("leads",)},
    agent_client_ids=("agent-client",),
    client_id_claim="azp",
)


def rule(**callers) -> ServerToolRule:
    return ServerToolRule(callers=ToolCallers(**callers))


def person(*roles: str, tenant: str = T, **extra) -> dict:
    return {
        "sub": "u1",
        "groups": [f"daegam-{tenant}"],
        ROLES_CLAIM: [f"daegam-role:{tenant}:{r}" for r in roles],
        **extra,
    }


def agent_own(k8s: str = "agent-helper", agent_id: str | None = "a1", tenant: str = T) -> dict:
    claims = {"sub": f"svc:agent:{k8s}", "tenant": tenant, "groups": [f"daegam-{tenant}"]}
    if agent_id is not None:
        claims["agent_id"] = agent_id
    return claims


def delegated(act, tenant: str = T, **extra) -> dict:
    return {"sub": "u1", "act": act, "bound_to": "gateway:g1", "tenant": tenant, **extra}


ACT_A = {"sub": "svc:agent:agent-helper", "agent_id": "a1"}
ACT_B = {"sub": "svc:agent:agent-other", "agent_id": "b1"}


def vmcp_policies(rules, scope=LOCAL):
    return BASE + source_forbids(rules, scope, "vmcp")


def ok(policies, claims, tool="a", **kw) -> bool:
    return allowed(policies, claims, tool, role_claim=ROLES_CLAIM, **kw)


# -- 사람과 역할 --------------------------------------------------------------------------


def test_a_role_is_a_minimum_and_other_tools_stay_open():
    policies = vmcp_policies({"a": rule(roles=["developer"])})
    assert ok(policies, person("developer"))
    assert ok(policies, person("tenant-admin"))
    assert not ok(policies, person("viewer"))
    assert not ok(policies, person())
    # 다른 테넌트의 역할은 받지 않는다
    assert not ok(policies, person("tenant-admin", tenant="other"))
    assert ok(policies, person(), tool="c")


def test_roles_pass_without_groups_and_a_rule_naming_only_people_hides_the_tool_from_agents():
    policies = vmcp_policies({"a": rule(roles=["viewer"])})
    claims = person("viewer")
    del claims["groups"]
    assert ok(policies, claims)
    assert not ok(policies, agent_own())
    assert not ok(policies, delegated(ACT_A))


def test_person_clauses_refuse_every_act_and_treat_null_as_absent():
    policies = vmcp_policies({"a": rule(roles=["viewer"])})
    for act in (ACT_A, "svc:agent:agent-helper", ["x"], 7, {"sub": "x"}):
        assert not ok(policies, person("developer", act=act)), act
    assert ok(policies, person("developer", act=None))


# -- Agent --------------------------------------------------------------------------------


def test_a_named_agent_passes_in_its_own_name_and_by_delegation_for_anyone():
    policies = vmcp_policies({"a": rule(agents=["a1"])})
    assert ok(policies, agent_own())
    assert ok(policies, delegated(ACT_A))
    # 위임한 사람이 누구든, 역할이 없어도 지난다
    assert ok(policies, delegated(ACT_A, **{ROLES_CLAIM: []}))


def test_an_unnamed_agent_is_refused_even_when_its_person_holds_the_role():
    policies = vmcp_policies({"a": rule(roles=["viewer"], agents=["a1"])})
    assert not ok(policies, agent_own("agent-other", "b1"))
    assert not ok(policies, delegated(ACT_B, **{ROLES_CLAIM: [f"daegam-role:{T}:tenant-admin"]}))


@pytest.mark.parametrize(
    "act",
    [
        "svc:agent:agent-helper",
        ["svc:agent:agent-helper", "a1"],
        3,
        {"sub": "svc:agent:agent-helper"},
        {**ACT_A, "extra": "x"},
        {"sub": "svc:agent:agent-helper", "agent_id": 1},
    ],
)
def test_the_delegated_clause_refuses_any_other_act_without_error(act):
    policies = vmcp_policies({"a": rule(agents=["a1"])})
    verdict = evaluate(policies, delegated(act), "a", role_claim=ROLES_CLAIM)
    assert verdict.errors == () and not verdict.allowed


def test_agent_clauses_check_the_agent_id_and_the_tenant():
    policies = vmcp_policies({"a": rule(agents=["a1"])})
    # 이 기능 전에 낸 토큰(agent_id 없음), 다른 테넌트의 같은 이름 Agent, 다시 만든 같은 이름의
    # Agent(새 id)는 지나지 못한다
    assert not ok(policies, agent_own(agent_id=None))
    assert not ok(policies, agent_own(tenant="other"))
    assert not ok(policies, agent_own(agent_id="new-id"))
    assert not ok(policies, delegated(ACT_A, tenant="other"))


def test_a_rule_naming_only_a_deleted_agent_stays_closed():
    scope = RuleScope(tenant=T, mode="local", agents={})
    policies = BASE + source_forbids({"a": rule(agents=["a1"])}, scope, "vmcp")
    assert not ok(policies, agent_own())
    assert not ok(policies, delegated(ACT_A))
    assert not ok(policies, person("tenant-admin"))


# -- 오류 없는 평가 ---------------------------------------------------------------------------


TOKENS = [
    {"sub": "x"},
    {"sub": "x", "tenant": 5},
    {"sub": "x", "tenant": ["acme"]},
    {"sub": "x", "act": None},
    {"sub": "x", "act": {"sub": 1}},
    {"sub": "x", "agent_id": 4, "tenant": T},
    {"sub": "svc:vmcp:gw", "tenant": T, "rules": 9},
    {"sub": "svc:vmcp:gw", "tenant": T, "rules": {"d": 1}},
    {"sub": "x", "groups": "daegam-acme", ROLES_CLAIM: "x"},
    {"sub": "x", "cid": 3, "azp": ["agent-client"]},
    {"sub": "x", "client": {"id": 3}},
]


@pytest.mark.parametrize("claims", TOKENS)
@pytest.mark.parametrize("scope", [LOCAL, OIDC, RuleScope(T, "oidc", client_id_claim="client.id")])
def test_every_rendered_forbid_evaluates_without_error(claims, scope):
    rules = {"a": rule(roles=["developer"], agents=["a1"], groups=["eng"])}
    rules_only = {"a": rule(roles=["viewer"])}
    sets = [
        source_forbids(rules, scope, "vmcp"),
        source_forbids(rules, scope, "front"),
        source_forbids(rules, scope, "internal", digest="d1", front="mcp-x"),
        source_forbids(rules_only, scope, "image", digest="d1"),
    ]
    for forbids in sets:
        verdict = evaluate(BASE + forbids, claims, "a", role_claim=ROLES_CLAIM)
        assert verdict.errors == (), (forbids, claims)


# -- oidc 모드 ------------------------------------------------------------------------------


def test_oidc_roles_compile_to_mapped_groups_and_groups_admit_directly():
    policies = BASE + source_forbids({"a": rule(roles=["developer"])}, OIDC, "vmcp")
    assert ok(policies, {"sub": "u2", "groups": ["eng"]})
    assert ok(policies, {"sub": "u2", "groups": ["leads"]})
    assert not ok(policies, {"sub": "u2", "groups": ["sales"]})
    policies = BASE + source_forbids({"a": rule(groups=["sales"])}, OIDC, "vmcp")
    assert ok(policies, {"sub": "u2", "groups": ["sales"]})


@pytest.mark.parametrize("claim", ["cid", "azp"])
def test_oidc_person_clauses_refuse_the_tenants_agent_clients(claim):
    policies = BASE + source_forbids(
        {"a": rule(roles=["developer"], groups=["sales"])}, OIDC, "vmcp"
    )
    client = {"sub": "client-sub", claim: "agent-client", "groups": ["eng", "sales"]}
    assert not ok(policies, client)
    assert ok(policies, {**client, claim: "someone-else"})


def test_oidc_idp_tokens_with_act_are_refused_and_agents_have_no_clause():
    policies = BASE + source_forbids({"a": rule(roles=["developer"], agents=["a1"])}, OIDC, "vmcp")
    assert not ok(policies, {"sub": "u2", "groups": ["eng"], "act": {"sub": "x"}})
    assert not ok(policies, delegated(ACT_A))
    assert not ok(policies, agent_own())
    closed = BASE + source_forbids({"a": rule(agents=["a1"])}, OIDC, "vmcp")
    assert not ok(closed, {"sub": "u2", "groups": ["eng"]})


# -- 서버 ----------------------------------------------------------------------------------


def _spec(rules, overrides=None) -> MCPServerSpec:
    return MCPServerSpec(
        backends=[
            BackendSelection(
                backend_profile_id="b1",
                spec_version=1,
                tool_rules=rules,
                tool_overrides=overrides or {},
            )
        ]
    )


def test_internal_servers_take_named_agents_the_digest_vmcp_the_front_and_the_platform():
    spec = _spec({"get_a": rule(roles=["developer"], agents=["a1"], groups=["eng"])})
    digest = rules_digest("s1", {"get_a": spec.backends[0].tool_rules["get_a"]}, LOCAL)
    policies = BASE + server_policies(spec, LOCAL, server_id="s1", front="mcp-x")
    tool = "get_a"
    assert ok(policies, agent_own(), tool)
    assert ok(policies, delegated(ACT_A), tool)
    assert not ok(policies, agent_own("agent-other", "b1"), tool)
    assert not ok(policies, delegated(ACT_B), tool)
    vmcp = {"sub": "svc:vmcp:gw-a", "tenant": T, "groups": ["eng"]}
    assert ok(policies, {**vmcp, "rules": digest}, tool)
    assert not ok(policies, {**vmcp, "rules": "stale"}, tool)
    assert not ok(policies, vmcp, tool)
    assert not ok(policies, {**vmcp, "rules": digest, "tenant": "other"}, tool)
    assert ok(policies, {"sub": "svc:expose:mcp-x", "tenant": T}, tool)
    assert not ok(policies, {"sub": "svc:expose:mcp-y", "tenant": T}, tool)
    assert ok(policies, {"sub": "svc:platform", "tenant": T}, tool)
    # 서버에는 사람 절도 그룹 절도 없다. 서비스 토큰이 모두 테넌트 그룹을 싣기 때문이다
    assert not ok(policies, person("tenant-admin", groups=["eng"]), tool)
    assert not ok(policies, {"sub": "svc:expose:other", "tenant": T, "groups": ["eng"]}, tool)


def test_a_rename_names_the_new_name_and_the_earlier_names_of_running_pods():
    spec = _spec({"get_a": rule(roles=["viewer"])}, {"get_a": ToolOverride(name="read_a")})
    policies = BASE + server_policies(
        spec, LOCAL, server_id="s1", front=None, earlier={"read_a": ["get_a", "fetch_a"]}
    )
    for tool in ("read_a", "get_a", "fetch_a"):
        assert not ok(policies, agent_own(), tool)
    assert ok(policies, agent_own(), "other")


def test_conditions_close_their_tools_for_everyone_until_they_are_known():
    cond = ServerToolRule(conditions=[ArgCondition(path="x", op="equals", value=1)])
    spec = _spec({"get_a": cond})
    policies = BASE + server_policies(spec, LOCAL, server_id="s1", front=None)
    assert policies[1:] == closing_forbid(["get_a"])
    assert not ok(policies, {"sub": "svc:platform", "tenant": T}, "get_a")
    assert server_policies(spec, LOCAL, server_id="s1", front=None, conditions_known=True) == []


def test_image_item_servers_take_only_the_vmcp_and_the_platform():
    rules = {"echo": rule(roles=["viewer"])}
    digest = rules_digest("i1", rules, LOCAL)
    policies = BASE + item_policies(rules, LOCAL, item_id="i1")
    assert ok(policies, {"sub": "svc:vmcp:gw", "tenant": T, "rules": digest}, "echo")
    assert ok(policies, {"sub": "svc:platform", "tenant": T}, "echo")
    assert not ok(policies, agent_own(), "echo")
    assert not ok(policies, person("tenant-admin"), "echo")


def test_header_mode_renders_no_caller_rule():
    header = RuleScope(tenant=T, mode="header")
    assert source_forbids({"a": rule(roles=["viewer"])}, header, "vmcp") == []
    cond = ServerToolRule(conditions=[ArgCondition(path="x", op="equals", value=1)])
    assert server_policies(_spec({"get_a": cond}), header, server_id="s1", front=None) == (
        closing_forbid(["get_a"])
    )


def test_an_owner_permit_all_and_another_source_do_not_widen_a_rule():
    """forbid 는 소유자의 permit 곁에 서고, 출처마다 따로 서므로 한 출처가 다른 출처를 넓히지
    못한다."""
    gateway = source_forbids({"a": rule(roles=["viewer"])}, LOCAL, "vmcp")
    server = source_forbids({"a": rule(roles=["tenant-admin"])}, LOCAL, "vmcp")
    policies = [*BASE, "permit(principal, action, resource);", *gateway, *server]
    assert not ok(policies, person("developer"))
    assert ok(policies, person("tenant-admin"))


def test_the_digest_follows_the_compiled_rules():
    rules = {"a": rule(roles=["developer"]), "b": rule(agents=["a1"])}
    d = rules_digest("s1", rules, LOCAL)
    assert len(d) == 16
    assert rules_digest("s1", {"b": rules["a"], "a": rules["b"]}, LOCAL) != d
    assert rules_digest("s2", rules, LOCAL) != d
    # 최소 역할을 같은 받는 역할로 적어도 같다
    assert rules_digest("s1", {**rules, "a": rule(roles=["tenant-admin", "developer"])}, LOCAL) == d
    renamed = RuleScope(tenant=T, mode="local", agents={"a1": "agent-renamed"})
    assert rules_digest("s1", rules, renamed) != d
    # 지운 Agent 만 남은 이름도 지문에 든다
    gone = RuleScope(tenant=T, mode="local", agents={})
    assert rules_digest("s1", rules, gone) != rules_digest("s1", {"a": rules["a"]}, gone)
    assert rules_digest("s1", {"a": ServerToolRule(read_only=True)}, LOCAL) is None


# -- 테넌트의 사실 ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_oidc_scopes_compile_roles_through_the_tenants_group_mappings():
    """oidc 모드의 역할은 그 테넌트의 Group Mapping 이 그 역할로 잇는 IdP 그룹으로 컴파일된다.
    Agent 는 그 테넌트의 것만, Agent 클라이언트와 플랫폼이 검증하는 클레임은 Tenant Settings 와
    설정에서 온다."""
    import asyncio

    from daegam.config import Settings
    from daegam.services.rule_compile import RuleScopes
    from daegam.store.memberships import GroupMapping
    from daegam.store.tenants import TenantSettings

    settings = Settings(
        mongo_url="mongodb://unused",
        mongo_db="unused",
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_client_id_claim="azp",
        data_plane_issuer="idp",
    )

    async def ts_get(tenant):
        return TenantSettings(tenant=tenant, agent_client_ids=["agent-client"])

    async def mappings():
        return [
            GroupMapping(group="eng", tenant=T, role="developer"),
            GroupMapping(group="ops", tenant="other", role="developer"),
        ]

    async def agents_list(tenant, include_draft=False):
        return [_Stub(id="a1", name="helper")]

    scopes = RuleScopes(
        settings,
        _Stub(get=ts_get),
        _Stub(list_mappings=mappings),
        _Stub(list=agents_list),
    )
    scope = asyncio.run(scopes.scope(T))
    assert scope.mode == "oidc" and scope.role_groups == {"developer": ("eng",)}
    assert scope.agents == {"a1": "agent-helper"}
    assert scope.agent_client_ids == ("agent-client",) and scope.client_id_claim == "azp"
    policies = BASE + source_forbids({"a": rule(roles=["viewer"])}, scope, "vmcp")
    assert ok(policies, {"sub": "u2", "groups": ["eng"]})
    assert not ok(policies, {"sub": "u2", "groups": ["ops"]})
    assert not ok(policies, {"sub": "c", "azp": "agent-client", "groups": ["eng"]})


# -- 아는 이름 밖을 닫는 forbid (2절) ---------------------------------------------------------


def test_the_known_forbid_closes_tool_calls_outside_its_names_only():
    from daegam.auth.rule_cedar import known_forbid

    base = ["permit(principal, action, resource);"]
    caller = {"sub": "svc:platform", "tenant": "t1"}
    policy = known_forbid(["b", "a", "b"])
    assert policy == [
        'forbid(principal, action == Action::"call_tool", resource) '
        'unless { resource in [Tool::"a", Tool::"b"] };'
    ]
    assert allowed(base + policy, caller, "a") and not allowed(base + policy, caller, "c")
    assert allowed(base + policy, caller, "c", action="get_prompt")
    assert not allowed(base + known_forbid([]), caller, "a")


def test_known_names_follow_the_revision_and_the_running_backends():
    from daegam.services.rule_compile import known_names
    from daegam.store.backend_apis import BackendAPIVersion
    from daegam.store.mcp_servers import MCPServerRevision

    def rev(n: int, **sel) -> MCPServerRevision:
        backend = {"backend_profile_id": "bp1", "spec_version": 1, **sel}
        return MCPServerRevision.model_validate({"revision": n, "backends": [backend]})

    one = rev(1, tools=["getA"])
    two = rev(2, tools=["getA", "getB"], tool_overrides={"getA": {"name": "alpha"}})
    three = rev(3)
    revisions = [one, two, three]
    assert known_names(revisions, two, (), {}) == ["alpha", "getB"]
    assert known_names(revisions, two, {1}, {}) == ["alpha", "getA", "getB"]
    # 무엇이 도는지 모르면 모든 리비전의 이름이다. 버전 문서를 못 읽은 다른 리비전의 이름은 뺀다
    assert known_names(revisions, two, None, {}) == ["alpha", "getA", "getB"]
    # 필터 없는 리비전은 버전 문서가 있어야 이름을 안다
    assert known_names(revisions, three, (), {}) is None
    version = BackendAPIVersion.model_construct(tool_names=["getA", "getC"])
    assert known_names(revisions, three, (), {("bp1", 1): version}) == ["getA", "getC"]
