"""P22 F1: Agent 그래프를 agent.json 으로 컴파일하기 (D26, D28, D33).

컴파일 결과가 agent-runtime/schema/fixtures 의 support-agent·minimal·redis-memory 표본과 같은
것(가변 값은 정규화), 서브에이전트·폴백·MCP Server 소스·header 모드의 툴 auth, memory 의 DSN env,
openrouter 를 openai SDK 로 싣는 것, 공유 모델 노드와 provider 차례를 든다. 규칙별 검증은
test_agent_graph_rules.py, knowledge 노드는 test_agent_graph_knowledge.py, 장기 기억은
test_agent_long_term.py 가 본다.
"""

from __future__ import annotations

import copy
import json

import pytest

from daegam.k8s.templates.agent import provider_key_env
from daegam.services import agent_graph
from daegam.services.agent_graph import GraphError, check_schema
from daegam.store.agents import AgentSpec
from daegam.store.settings import LLMProvider
from daegam.store.tenants import Binding
from tests.helpers.agents import (
    MINIMAL_SPEC,
    add_sub_agent,
    agent_fixture,
    compile_spec,
    issues_of,
    make_ctx,
    node_of,
    template_spec,
)


def test_openrouter_compiles_to_the_openai_sdk_and_its_address():
    """openrouter 는 agent.json 에 openai 와 OpenRouter 주소로 실려 런타임은 openrouter 를 몰라도
    된다. 모델을 비우면 kind 기본(Kimi K3)이다."""
    ctx = make_ctx(keyed=("router",))
    ctx.providers["router"] = LLMProvider(name="router", kind="openrouter", api_key_secret="k")
    doc = compile_spec(template_spec(provider="router"), ctx)
    assert check_schema(doc) == []
    assert doc["main"]["model"]["provider"] == {
        "name": "router",
        "kind": "openai",
        "env_key": "DAEGAM_LLM_ROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
    }
    assert doc["main"]["model"]["name"] == "moonshotai/kimi-k3"


# -- 컴파일 스냅샷 (fixture 공유) ------------------------------------------------------------


def test_compile_matches_support_agent_fixture():
    """템플릿을 채워 컴파일하면 support-agent.json 과 같다. 도구 URL 만 Gateway 의 클러스터 내부
    vMCP 주소로 다르고(fixture 는 자리표시 주소), profile.id 와 instance.id 는 이제 같은 문서
    id 다."""
    doc = compile_spec(template_spec())
    assert check_schema(doc) == []
    expected = agent_fixture("support-agent.json")
    assert doc["tools"][0]["url"] == "http://vmcp-gw-calc.tenant-a.svc.cluster.local:4483/mcp"
    doc["tools"][0]["url"] = expected["tools"][0]["url"]
    assert doc == expected
    # 키 값은 어디에도 없다
    assert "api_key" not in json.dumps(doc) and "client_secret" not in json.dumps(doc)


def test_compile_matches_minimal_fixture():
    """입구와 메인, 모델 노드만 있는 그래프는 minimal.json 과 같다 (anthropic, 메모리 없음). 키
    env 이름은 문서의 표가 provider 이름으로 정하므로(`provider_key_env`) 표본의 이름 대신 그것을
    본다."""
    spec = copy.deepcopy(MINIMAL_SPEC)
    doc = compile_spec(spec, agent_id="ai-minimal-0001", name="minimal", bindings=[])
    expected = agent_fixture("minimal.json")
    provider = expected["main"]["model"]["provider"]
    provider["env_key"] = provider_key_env(provider["name"])
    assert doc == expected


def test_compile_subagent_fallback_mcp_source_and_header_mode():
    spec = template_spec(provider="local-openai")
    main = node_of(spec, "agent-main")["data"]
    model = node_of(spec, "model-main")["data"]
    model["temperature"] = 0.2
    model["fallback"] = {"provider": "local-anthropic", "model": "claude-opus-5"}
    main["response_format"] = {"json_schema": {"type": "object"}}
    main["language"] = ""
    main["context"] = {"compaction": {"trigger": {"tokens": 170000}, "keep": {"messages": 20}}}
    add_sub_agent(spec, "agent-research", "researcher", "local-anthropic")
    spec["graph"]["nodes"].append(
        {
            "id": "tools-orders",
            "type": "tools",
            "data": {
                "source": {"kind": "mcp_instance", "id": "mi-orders-0001"},
                "approval": {"CreateOrder": ["approve", "edit", "reject"]},
            },
        }
    )
    spec["graph"]["edges"] += [
        {
            "id": "e-delegate",
            "source": "agent-main",
            "target": "agent-research",
            "kind": "delegate",
        },
        {
            "id": "e-sub-tools",
            "source": "agent-research",
            "target": "tools-orders",
            "kind": "tools",
        },
        {
            "id": "e-sub-calc",
            "source": "agent-research",
            "target": "tools-calculator",
            "kind": "tools",
        },
    ]
    ctx = make_ctx(oidc=False)
    doc = compile_spec(spec, ctx)
    assert check_schema(doc) == []
    assert doc["stub"] is False
    m = doc["main"]
    assert m["model"]["provider"] == {
        "name": "local-openai",
        "kind": "openai",
        "env_key": "DAEGAM_LLM_LOCAL_OPENAI_API_KEY",
        "base_url": "https://gateway.example.com/v1",
    }
    assert m["model"]["name"] == "gpt-4o" and m["model"]["params"]["temperature"] == 0.2
    assert m["fallback"]["provider"]["env_key"] == "DAEGAM_LLM_LOCAL_ANTHROPIC_API_KEY"
    assert m["fallback"]["name"] == "claude-opus-5"
    assert m["fallback"]["params"] == m["model"]["params"]
    assert m["response_format"] == {"kind": "json_schema", "schema": {"type": "object"}}
    assert m["language"] is None
    assert m["compaction"] == {
        "trigger": {"kind": "tokens", "value": 170000},
        "keep": {"kind": "messages", "value": 20},
    }
    assert m["tools"] == ["tools-calculator"] and m["subagents"] == ["researcher"]
    # 서브에이전트: graph.nodes 순서의 tools 참조, 자기 툴의 승인만 interrupt_on 에
    (sub,) = doc["subagents"]
    assert sub["name"] == "researcher" and sub["description"] == "researcher 을 맡는다."
    assert sub["tools"] == ["tools-calculator", "tools-orders"]
    assert sub["interrupt_on"] == {
        "Divide": ["approve", "reject"],
        "CreateOrder": ["approve", "edit", "reject"],
    }
    assert sub["model"]["provider"]["kind"] == "anthropic"
    # 툴 소스: header 모드의 Gateway 는 auth none, MCP Server 는 프록시 Service 주소
    by_ref = {t["ref"]: t for t in doc["tools"]}
    assert set(by_ref) == {"tools-calculator", "tools-orders"}
    assert by_ref["tools-calculator"]["auth"] == {"kind": "none"}
    # MCP Server 는 모든 모드에서 플랫폼이 낸 서비스 토큰을 env 로 받는다 (D41)
    assert by_ref["tools-orders"]["auth"] == {"kind": "bearer", "secret_env": "DAEGAM_MCP_TOKEN"}
    assert by_ref["tools-orders"]["source"] == {
        "kind": "mcp_instance",
        "id": "mi-orders-0001",
        "name": "orders",
    }
    assert by_ref["tools-orders"]["url"] == (
        "http://mcp-mcp-orders-proxy.tenant-a.svc.cluster.local:8080/mcp"
    )
    assert by_ref["tools-orders"]["allow"] == [] and by_ref["tools-orders"]["timeouts"] == {}


def test_compile_redis_memory_matches_fixture():
    """memory 의 Database 가 redis 면 컴파일이 `kind: redis` 를 내고 redis-memory.json 과 같다(D33).
    dsn_env 는 postgres 와 같이 바인딩 접두어의 _URL 이다."""
    spec = template_spec(ds_id="ds-redis")
    ctx = make_ctx(redis_checkpointer="ready")
    doc = compile_spec(
        spec, ctx, bindings=[Binding(data_service_id="ds-redis", env_prefix="DAEGAM_DB_0")]
    )
    assert check_schema(doc) == []
    assert doc["memory"] == {"checkpointer": {"kind": "redis", "dsn_env": "DAEGAM_DB_0_URL"}}
    assert doc["schema_version"] == "1"
    expected = agent_fixture("redis-memory.json")
    doc["tools"][0]["url"] = expected["tools"][0]["url"]
    assert doc == expected
    doc = compile_spec(
        spec, ctx, bindings=[Binding(data_service_id="ds-redis", env_prefix="DAEGAM_DB_2")]
    )
    assert doc["memory"]["checkpointer"] == {"kind": "redis", "dsn_env": "DAEGAM_DB_2_URL"}
    # 스키마는 postgres·redis 밖의 kind 를 거부한다
    bad = copy.deepcopy(doc)
    bad["memory"]["checkpointer"]["kind"] = "mysql"
    assert any("memory" in e for e in check_schema(bad))
    # ready 가 아니면 컴파일도 검증에서 막힌다
    with pytest.raises(GraphError) as e:
        compile_spec(spec, make_ctx(redis_checkpointer="modules_missing"))
    assert {i["code"] for i in e.value.to_dict()["issues"]} == {"redis_modules_missing"}


def test_compile_refuses_invalid_graph():
    spec = template_spec(provider="nokey")
    with pytest.raises(GraphError) as e:
        compile_spec(spec)
    d = e.value.to_dict()
    assert d["code"] == "invalid_graph" and "provider_no_key" in d["detail"]
    assert {i["code"] for i in d["issues"]} == {"provider_no_key"}


# -- 모델 노드 (D28) ------------------------------------------------------------------------


def test_shared_model_node_compiles_into_each_agent():
    """메인과 서브가 한 모델 노드를 공유한다. 컴파일 결과의 model 객체는 둘 다 같고 provider 키
    복사 목록도 하나다."""
    spec = template_spec(provider="local-openai")
    node_of(spec, "model-main")["data"]["model"] = "gpt-4o-mini"
    add_sub_agent(spec, "agent-x", "x", model="model-main")
    spec["graph"]["edges"].append(
        {"id": "e-d", "source": "agent-main", "target": "agent-x", "kind": "delegate"}
    )
    assert issues_of(spec) == []
    doc = compile_spec(spec)
    (sub,) = doc["subagents"]
    assert sub["model"] == doc["main"]["model"]
    assert doc["main"]["model"]["name"] == "gpt-4o-mini"
    assert agent_graph.used_providers(AgentSpec.model_validate(spec), {}) == ["local-openai"]
    # 이슈는 공유 모델 노드 하나에만 붙는다
    node_of(spec, "model-main")["data"]["provider"] = "nokey"
    issues = issues_of(spec)
    assert [(i["code"], i["node_id"]) for i in issues] == [("provider_no_key", "model-main")]


def test_used_providers_puts_the_main_model_first():
    spec = template_spec(provider="local-openai")
    node_of(spec, "model-main")["data"]["fallback"] = {"provider": "local-anthropic"}
    add_sub_agent(spec, "agent-x", "x", "local-stub")
    # 서브의 모델 노드가 graph.nodes 에서 메인의 모델보다 앞에 와도 메인이 먼저다
    nodes = spec["graph"]["nodes"]
    nodes.insert(0, nodes.pop(next(i for i, n in enumerate(nodes) if n["id"] == "model-agent-x")))
    assert agent_graph.used_providers(AgentSpec.model_validate(spec), {}) == [
        "local-openai",
        "local-anthropic",
        "local-stub",
    ]
