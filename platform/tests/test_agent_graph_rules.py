"""P22 F1: Agent 그래프의 규칙별 검증 (D26, D28, D33, D42).

채운 템플릿은 통과하고 템플릿 자체는 빈자리를 표시하는 것, 입구·메인 개수, 닿지 않는 에이전트와
위임 순환, memory 개수와 memory 가 필요한 설정, redis memory 의 checkpointer 능력, 도구 소스·
provider·oidc 모드의 agent_oauth 규칙, 배포되지 않은 도구 소스, 간선·노드 data·경고, 에이전트마다
모델 간선이 하나인 것(D28)을 든다. 컴파일은 test_agent_compile.py, knowledge 노드의 규칙은
test_agent_graph_knowledge.py 가 본다.
"""

from __future__ import annotations

import copy
from typing import Any

from daegam.services.agent_templates import templates
from tests.helpers.agents import (
    add_sub_agent,
    codes,
    edge_of,
    issues_of,
    make_ctx,
    model_node,
    node_of,
    template_spec,
)


def drop_node(spec: dict[str, Any], node_id: str, *, edges: bool = True) -> None:
    spec["graph"]["nodes"] = [n for n in spec["graph"]["nodes"] if n["id"] != node_id]
    if edges:
        spec["graph"]["edges"] = [
            e for e in spec["graph"]["edges"] if node_id not in (e["source"], e["target"])
        ]


# -- 규칙별 검증 ---------------------------------------------------------------------------


def test_template_filled_is_valid_and_template_itself_marks_placeholders():
    assert issues_of(template_spec()) == []
    t = templates()[0]
    issues = issues_of({"graph": t["graph"], "service": t["service"]})
    assert codes(issues) == {"unknown_provider", "unknown_source", "unknown_data_service"}
    # provider 자리는 모델 노드에 표시된다 (D28)
    assert {i["node_id"] for i in issues} == {"model-main", "tools-calculator", "memory"}
    assert set(t["layout"]) == {n["id"] for n in t["graph"]["nodes"]}
    (model_edge,) = [e for e in t["graph"]["edges"] if e["kind"] == "model"]
    assert model_edge == {
        "id": "e-model",
        "source": "agent-main",
        "target": "model-main",
        "kind": "model",
    }


def test_entry_and_main_counts():
    spec = template_spec()
    drop_node(spec, "entry", edges=False)
    issues = issues_of(spec)
    # 입구가 없으면 entry_count 하나로 충분하다. 닿지 않는다는 이슈는 겹쳐 내지 않는다
    assert codes(issues) == {"entry_count", "dangling_edge"}

    spec = template_spec()
    spec["graph"]["nodes"].append({"id": "entry-2", "type": "entry", "data": {}})
    issues = issues_of(spec)
    assert [i["node_id"] for i in issues if i["code"] == "entry_count"] == ["entry-2"]

    spec = template_spec()
    node_of(spec, "agent-main")["data"]["role"] = "sub"
    issues = issues_of(spec)
    assert "main_agent_count" in codes(issues)
    assert any(i["code"] == "invalid_edge_kind" and i["edge_id"] == "e-entry" for i in issues), (
        issues
    )

    spec = template_spec()
    second = copy.deepcopy(node_of(spec, "agent-main"))
    second["id"], second["data"]["name"] = "agent-main-2", "support-2"
    spec["graph"]["nodes"].append(second)
    issues = issues_of(spec)
    assert [i["node_id"] for i in issues if i["code"] == "main_agent_count"] == ["agent-main-2"]
    assert any(i["code"] == "unreachable_agent" and i["node_id"] == "agent-main-2" for i in issues)


def test_unreachable_and_delegation_rules():
    spec = template_spec()
    add_sub_agent(spec, "agent-x", "x")
    issues = issues_of(spec)
    assert [i["node_id"] for i in issues if i["code"] == "unreachable_agent"] == ["agent-x"]
    assert codes(issues) == {"unreachable_agent"}

    spec["graph"]["edges"].append(
        {"id": "e-d", "source": "agent-main", "target": "agent-x", "kind": "delegate"}
    )
    assert issues_of(spec) == []

    # 자기 순환
    spec = template_spec()
    spec["graph"]["edges"].append(
        {"id": "e-self", "source": "agent-main", "target": "agent-main", "kind": "delegate"}
    )
    issues = issues_of(spec)
    assert [i["edge_id"] for i in issues if i["code"] == "delegation_cycle"] == ["e-self"]

    # main → x → y → x. 서브에서 나가는 위임은 invalid_edge_kind 이고 되돌아가는 간선은 순환이다
    spec = template_spec()
    add_sub_agent(spec, "agent-x", "x")
    add_sub_agent(spec, "agent-y", "y")
    spec["graph"]["edges"] += [
        {"id": "e-1", "source": "agent-main", "target": "agent-x", "kind": "delegate"},
        {"id": "e-2", "source": "agent-x", "target": "agent-y", "kind": "delegate"},
        {"id": "e-3", "source": "agent-y", "target": "agent-x", "kind": "delegate"},
    ]
    issues = issues_of(spec)
    assert [i["edge_id"] for i in issues if i["code"] == "delegation_cycle"] == ["e-3"]
    assert {i["edge_id"] for i in issues if i["code"] == "invalid_edge_kind"} == {"e-2", "e-3"}

    # 서브 이름 중복 (템플릿의 메인 이름과 같은 것을 준다)
    spec = template_spec()
    add_sub_agent(spec, "agent-x", "Agent")
    spec["graph"]["edges"].append(
        {"id": "e-1", "source": "agent-main", "target": "agent-x", "kind": "delegate"}
    )
    assert codes(issues_of(spec)) == {"duplicate_agent_name"}


def test_memory_count_and_memory_required():
    spec = template_spec()
    spec["graph"]["nodes"].append(
        {"id": "memory-2", "type": "memory", "data": {"checkpointer": {"data_service_id": "ds-pg"}}}
    )
    issues = issues_of(spec)
    assert [i["node_id"] for i in issues if i["code"] == "memory_count"] == ["memory-2"]
    assert any(i["code"] == "unattached_node" and i["level"] == "warning" for i in issues)
    spec["graph"]["edges"].append(
        {"id": "e-m2", "source": "agent-main", "target": "memory-2", "kind": "memory"}
    )
    issues = issues_of(spec)
    assert {i.get("edge_id") for i in issues if i["code"] == "memory_count"} >= {"e-m2"}

    # 메모리 없이 승인·분당 한도·일당 예산이 켜지면 memory_required
    spec = template_spec()
    drop_node(spec, "memory")
    issues = issues_of(spec)
    assert codes(issues) == {"memory_required"}
    (mr,) = [i for i in issues if i["code"] == "memory_required"]
    assert "entry.rate_limit" in mr["message"] and "approval" in mr["message"]
    node_of(spec, "entry")["data"].pop("rate_limit")
    node_of(spec, "tools-calculator")["data"]["approval"] = {}
    assert issues_of(spec) == []
    spec["service"]["budget"]["daily_tokens"] = 1_000_000
    issues = issues_of(spec)
    assert codes(issues) == {"memory_required"}
    assert "service.budget.daily_tokens" in issues[0]["message"]

    # memory_required 문구는 종류를 말하지 않는다(postgres·redis 둘 다 된다)
    assert issues[0]["message"].startswith("a memory node (checkpointer and shared counters)")


def test_memory_redis_needs_ready_checkpointer_capability():
    """redis Database 는 verify 가 남긴 capabilities.checkpointer 가 ready 일 때만 memory 가 된다
    (D33). 아니면 error redis_modules_missing 이고 detail 은 값에 따라 셋이다."""
    spec = template_spec(ds_id="ds-redis")
    details = {
        None: "run verify first; the checkpointer needs Redis 8 with RediSearch and RedisJSON",
        "modules_missing": "Redis has no RediSearch/RedisJSON (Redis 8 bundles them)",
        "unknown": (
            "the verify account cannot run FT._LIST; grant it or verify with another account"
        ),
    }
    for capability, detail in details.items():
        issues = issues_of(spec, make_ctx(redis_checkpointer=capability))
        assert codes(issues) == {"redis_modules_missing"}, capability
        (issue,) = [i for i in issues if i["code"] == "redis_modules_missing"]
        assert issue["node_id"] == "memory" and issue["level"] == "error"
        assert issue["message"] == f"memory node 'memory': data service 'cache': {detail}"
    assert issues_of(spec, make_ctx(redis_checkpointer="ready")) == []
    # 빈 자리의 안내는 두 종류를 말한다
    node_of(spec, "memory")["data"]["checkpointer"]["data_service_id"] = ""
    (issue,) = issues_of(spec)
    assert issue["code"] == "unknown_data_service"
    assert issue["message"] == "memory node 'memory': choose a postgres or redis data service"


def test_sources_providers_and_oauth_rules():
    spec = template_spec(source_id="nope")
    issues = issues_of(spec)
    assert codes(issues) == {"unknown_source"} and issues[0]["node_id"] == "tools-calculator"
    # kind 가 다르면 같은 id 라도 모른다
    spec = template_spec()
    node_of(spec, "tools-calculator")["data"]["source"]["kind"] = "mcp_instance"
    assert codes(issues_of(spec)) == {"unknown_source"}

    # provider: 없음, 키 없음, stub 면제, fallback 도 같은 규칙. 표식은 모델 노드다 (D28)
    issues = issues_of(template_spec(provider="ghost"))
    assert codes(issues) == {"unknown_provider"} and issues[0]["node_id"] == "model-main"
    issues = issues_of(template_spec(provider="nokey"))
    assert codes(issues) == {"provider_no_key"} and issues[0]["node_id"] == "model-main"
    assert codes(issues_of(template_spec(provider="local-stub"))) == set()
    assert codes(issues_of(template_spec(), make_ctx(keyed=()))) == set()
    spec = template_spec()
    node_of(spec, "model-main")["data"]["fallback"] = {"provider": "nokey"}
    issues = issues_of(spec)
    assert codes(issues) == {"provider_no_key"} and issues[0]["node_id"] == "model-main"
    assert "fallback" in issues[0]["message"]

    # oidc 모드의 Gateway 소스: agent_oauth 필수, client_id 는 허용 목록 안
    spec = template_spec()
    issues = issues_of(spec, make_ctx(oauth=False))
    assert codes(issues) == {"agent_oauth_missing"} and issues[0]["node_id"] == "tools-calculator"
    assert codes(issues_of(spec, make_ctx(client_ids=("other",)))) == {
        "agent_client_id_not_allowed"
    }
    # header 모드나 MCP Server 소스에는 해당 없다
    assert issues_of(spec, make_ctx(oidc=False, oauth=False)) == []
    node_of(spec, "tools-calculator")["data"]["source"] = {
        "kind": "mcp_instance",
        "id": "mi-orders-0001",
    }
    assert issues_of(spec, make_ctx(oauth=False)) == []


def test_tool_source_must_be_a_deployed_mcp_server():
    """도구 소스는 `deployed` 인 MCP Server 만 받는다 (D42). 정의만 있거나(draft) 실패했거나
    배포 중인 것은 URL 이 없다. 사전에 없는 것과 구분해 source_not_deployed 로 말한다."""
    spec = template_spec()
    node_of(spec, "tools-calculator")["data"]["source"] = {
        "kind": "mcp_instance",
        "id": "mi-orders-0001",
    }
    for state in ("draft", "failed", "deploying", "undeploying"):
        issues = issues_of(spec, make_ctx(mi_state=state))
        assert codes(issues) == {"source_not_deployed"}, state
        assert issues[0]["node_id"] == "tools-calculator" and state in issues[0]["message"]
    assert issues_of(spec, make_ctx(mi_state="deployed")) == []


def test_tool_source_must_be_a_deployed_gateway():
    """Gateway 소스도 같다 (D42). draft 는 사전에 들되 URL 이 없어 source_not_deployed 다."""
    spec = template_spec()
    node_of(spec, "tools-calculator")["data"]["source"] = {
        "kind": "gateway_instance",
        "id": "gi-calc-0001",
    }
    for state in ("draft", "failed", "deploying", "undeploying"):
        issues = issues_of(spec, make_ctx(gw_state=state))
        assert codes(issues) == {"source_not_deployed"}, state
        assert issues[0]["node_id"] == "tools-calculator" and state in issues[0]["message"]
    assert issues_of(spec, make_ctx(gw_state="deployed")) == []


def test_edges_node_data_and_warnings():
    spec = template_spec()
    spec["graph"]["edges"].append(
        {"id": "e-x", "source": "agent-main", "target": "nope", "kind": "tools"}
    )
    issues = issues_of(spec)
    assert [i["edge_id"] for i in issues if i["code"] == "dangling_edge"] == ["e-x"]

    spec = template_spec()
    edge_of(spec, "e-memory")["kind"] = "tools"
    spec["graph"]["edges"].append(
        {"id": "e-bad", "source": "entry", "target": "tools-calculator", "kind": "tools"}
    )
    issues = issues_of(spec)
    assert {i["edge_id"] for i in issues if i["code"] == "invalid_edge_kind"} == {
        "e-memory",
        "e-bad",
    }

    spec = template_spec()
    spec["graph"]["edges"].append(edge_of(spec, "e-tools"))
    assert codes(issues_of(spec)) == {"duplicate_edge_id"}
    spec = template_spec()
    spec["graph"]["nodes"].append(copy.deepcopy(node_of(spec, "memory")))
    assert codes(issues_of(spec)) == {"duplicate_node_id"}

    # 노드 data 형식은 그 노드에 표식이 붙는다
    spec = template_spec()
    node_of(spec, "entry")["data"]["max_turns"] = 0
    node_of(spec, "guardrail-input")["data"]["rules"] = [{"kind": "deny_pattern", "value": "("}]
    node_of(spec, "tools-calculator")["data"]["approval"] = {"Divide": []}
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"]) for i in issues] == [
        ("invalid_node_data", "entry"),
        ("invalid_node_data", "tools-calculator"),
        ("invalid_node_data", "guardrail-input"),
    ]
    spec = template_spec()
    add_sub_agent(spec, "agent-x", "x")
    node_of(spec, "agent-x")["data"]["description"] = " "
    spec["graph"]["edges"].append(
        {"id": "e-d", "source": "agent-main", "target": "agent-x", "kind": "delegate"}
    )
    assert codes(issues_of(spec)) == {"invalid_node_data"}
    # 옛 형식(에이전트 data 의 모델 필드)은 API 입력에서는 받지 않는다. 승격은 저장 문서를 읽을
    # 때뿐이다
    spec = template_spec()
    node_of(spec, "agent-main")["data"]["provider"] = "local-stub"
    issues = [i for i in issues_of(spec) if i["code"] == "invalid_node_data"]
    assert [i["node_id"] for i in issues] == ["agent-main"]
    assert "provider" in issues[0]["message"]

    # 경고: anthropic 의 temperature (모델 노드 표식), 붙지 않은 노드. 옛 예약 knowledge 노드
    # (임의 data)는 읽을 때 깨지지 않고 invalid_node_data 오류로 표식이 붙는다 (D32, 마이그레이션
    # 없음)
    spec = template_spec(provider="local-anthropic")
    node_of(spec, "model-main")["data"]["temperature"] = 0.5
    spec["graph"]["nodes"].append({"id": "rag", "type": "knowledge", "data": {"anything": 1}})
    spec["graph"]["nodes"].append(
        {
            "id": "tools-loose",
            "type": "tools",
            "data": {"source": {"kind": "gateway_instance", "id": "gi-calc-0001"}},
        }
    )
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"]) for i in issues if i["level"] == "error"] == [
        ("invalid_node_data", "rag")
    ]
    assert codes(issues, "warning") == {"unsupported_param", "unattached_node"}
    assert [i["node_id"] for i in issues if i["code"] == "unattached_node"] == [
        "tools-loose",
        "rag",
    ]
    assert [i["node_id"] for i in issues if i["code"] == "unsupported_param"] == ["model-main"]
    # openai 는 temperature 를 쓴다
    spec = template_spec(provider="local-openai")
    node_of(spec, "model-main")["data"]["temperature"] = 0.5
    assert codes(issues_of(spec), "warning") == set()


# -- 모델 노드 (D28) ------------------------------------------------------------------------


def test_model_count_requires_exactly_one_model_edge_per_agent():
    # 0개: 표식은 에이전트. 붙지 않은 모델 노드는 경고다
    spec = template_spec()
    spec["graph"]["edges"] = [e for e in spec["graph"]["edges"] if e["id"] != "e-model"]
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"], i["edge_id"]) for i in issues if i["level"] == "error"] == [
        ("model_count", "agent-main", None)
    ]
    assert [(i["code"], i["node_id"]) for i in issues if i["level"] == "warning"] == [
        ("unattached_node", "model-main")
    ]
    # 모델 노드 자체가 없어도 같은 오류다
    drop_node(spec, "model-main")
    assert [(i["code"], i["node_id"]) for i in issues_of(spec)] == [("model_count", "agent-main")]

    # 2개: 표식은 에이전트이고 넘치는 간선을 함께 가리킨다
    spec = template_spec()
    spec["graph"]["nodes"].append(model_node("model-2"))
    spec["graph"]["edges"].append(
        {"id": "e-model-2", "source": "agent-main", "target": "model-2", "kind": "model"}
    )
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"], i["edge_id"]) for i in issues] == [
        ("model_count", "agent-main", "e-model-2")
    ]

    # 서브에이전트도 자기 모델 간선이 있어야 한다
    spec = template_spec()
    add_sub_agent(spec, "agent-x", "x")
    spec["graph"]["edges"].append(
        {"id": "e-d", "source": "agent-main", "target": "agent-x", "kind": "delegate"}
    )
    assert issues_of(spec) == []
    spec["graph"]["edges"] = [e for e in spec["graph"]["edges"] if e["id"] != "e-model-agent-x"]
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"]) for i in issues if i["level"] == "error"] == [
        ("model_count", "agent-x")
    ]

    # 모델 간선은 에이전트에서만 나가고 모델 노드로만 간다
    spec = template_spec()
    spec["graph"]["edges"].append(
        {"id": "e-bad", "source": "entry", "target": "model-main", "kind": "model"}
    )
    assert [i["edge_id"] for i in issues_of(spec) if i["code"] == "invalid_edge_kind"] == ["e-bad"]
    spec = template_spec()
    edge_of(spec, "e-tools")["kind"] = "model"
    issues = issues_of(spec)
    assert [i["edge_id"] for i in issues if i["code"] == "invalid_edge_kind"] == ["e-tools"]
    assert "model_count" not in codes(issues)
