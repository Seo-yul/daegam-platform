"""P15 F3b: knowledge 노드의 검증·컴파일 (D32).

검증 코드 표(unknown_knowledge_base·provider_no_key·unknown_provider·embedding_not_supported·
duplicate_tool_name·reserved_tool_name·knowledge_base_deleting·knowledge_base_empty·
knowledge_base_failed·unattached_node), 간선 규칙(메인·서브 모두), 컴파일(knowledge 없는 템플릿은
"1" 문서 그대로, knowledge 항목·env_key·DSN env·서브에이전트 참조, knowledge-agent.json 과 같은
문서), used_providers 의 중복 없음, knowledge_providers, 런타임 내장 도구 이름과의 대조를 든다.
Deployment env·Secret 복사·바인딩 순서는 test_agent_runtime_knowledge.py 가 본다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from daegam.k8s.templates.agent import DEFAULT_DB_PREFIX, db_env_prefix
from daegam.services import agent_graph
from daegam.services.agent_graph import (
    RESERVED_TOOL_NAMES,
    KnowledgeData,
    check_schema,
)
from daegam.store.agents import AgentSpec
from daegam.store.settings import LLMProvider
from daegam.store.tenants import Binding
from tests.helpers.agents import (
    FAQ_ID,
    add_sub_agent,
    agent_fixture,
    attach,
    codes,
    compile_spec,
    ctx_with,
    issues_of,
    kb,
    knowledge_node,
    node_of,
    template_spec,
    with_knowledge,
)
from tests.helpers.source import AGENT_RUNTIME, runtime_module

DOCS_ID = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
DOCS_HEX = DOCS_ID.replace("-", "")


# -- 도우미 ----------------------------------------------------------------------------------


def errors_with_nodes(issues: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(i["code"], i["node_id"]) for i in issues if i["level"] == "error"]


def warnings_with_nodes(issues: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(i["code"], i["node_id"]) for i in issues if i["level"] == "warning"]


# -- 노드 data 와 예약 이름 ------------------------------------------------------------------


def test_reserved_tool_names_match_the_runtime_builtin_tools():
    """deepagents 내장 도구 이름은 런타임 tool_names.py 의 BUILTIN_TOOLS 가 정본이다. 두 집합이
    어긋나면 validate 가 놓친 이름을 ToolNode 가 조용히 덮는다. 그 모듈은 표준 라이브러리만 써서
    런타임 꾸러미 없이 실린다."""
    module = runtime_module(AGENT_RUNTIME / "tool_names.py")
    assert module.BUILTIN_TOOLS == RESERVED_TOOL_NAMES
    assert len(RESERVED_TOOL_NAMES) == 11


def test_knowledge_data_defaults_and_limits():
    d = KnowledgeData.model_validate({"knowledge_base_id": "x"})
    assert (d.tool_name, d.description, d.top_k, d.min_score) == ("search_knowledge", "", 5, None)
    assert KnowledgeData.model_config.get("extra") == "forbid"
    for bad in (
        {"anything": 1},
        {"tool_name": "bad name"},
        {"tool_name": ""},
        {"tool_name": "x" * 65},
        {"top_k": 0},
        {"top_k": 21},
        {"min_score": 1.5},
        {"min_score": -0.1},
    ):
        with pytest.raises(ValidationError):
            KnowledgeData.model_validate({"knowledge_base_id": "x", **bad})
    assert KnowledgeData.model_validate({"top_k": 20, "min_score": 1}).min_score == 1
    assert KnowledgeData.model_validate({"min_score": 0}).min_score == 0


def test_stored_reserved_nodes_are_readable_and_flagged():
    """옛 예약 노드(`{}` 나 임의 data)는 읽을 때 깨지지 않고 validate 가 표식을 단다. 마이그레이션은
    없다."""
    spec = template_spec()
    spec["graph"]["nodes"].append({"id": "rag", "type": "knowledge", "data": {"anything": 1}})
    AgentSpec.model_validate(spec)
    assert errors_with_nodes(issues_of(spec, ctx_with())) == [("invalid_node_data", "rag")]
    spec = template_spec()
    spec["graph"]["nodes"].append({"id": "rag", "type": "knowledge", "data": {}})
    issues = issues_of(spec, ctx_with())
    assert errors_with_nodes(issues) == [("unknown_knowledge_base", "rag")]
    assert "choose a knowledge base" in issues[-1]["message"]
    assert warnings_with_nodes(issues) == [("unattached_node", "rag")]
    assert "reserved_node" not in {i["code"] for i in issues}


# -- 검증 코드 표 ----------------------------------------------------------------------------


def test_knowledge_base_existence_provider_and_state():
    faq = kb(FAQ_ID)
    assert issues_of(with_knowledge(), ctx_with(faq)) == []

    issues = issues_of(with_knowledge(kb_id="nope"), ctx_with(faq))
    assert errors_with_nodes(issues) == [("unknown_knowledge_base", "kb-faq")]
    assert "does not exist in this tenant" in issues[0]["message"]
    # 다른 테넌트의 Knowledge Base 는 문맥에 없으므로 같은 오류다. 빈 id 는 "고르라" 는 문구다
    issues = issues_of(with_knowledge(kb_id=""), ctx_with(faq))
    assert errors_with_nodes(issues) == [("unknown_knowledge_base", "kb-faq")]
    assert "choose a knowledge base" in issues[0]["message"]

    # 임베딩 provider: 키 없음(표식은 knowledge 노드), 미등록, stub 면제, 키 있는 openai
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, provider="nokey")))
    assert errors_with_nodes(issues) == [("provider_no_key", "kb-faq")]
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, provider="gone")))
    assert errors_with_nodes(issues) == [("unknown_provider", "kb-faq")]
    assert issues_of(with_knowledge(), ctx_with(faq, keyed=())) == []
    assert issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, provider="local-openai"))) == []
    issues = issues_of(
        with_knowledge(), ctx_with(kb(FAQ_ID, provider="local-openai"), keyed=("local-anthropic",))
    )
    assert errors_with_nodes(issues) == [("provider_no_key", "kb-faq")]
    # 만든 뒤 임베딩 없는 kind 로 바뀐 provider 는 키가 있어도 오류다
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, provider="local-anthropic")))
    assert errors_with_nodes(issues) == [("embedding_not_supported", "kb-faq")]

    # 삭제 중 (orphaned_table 도 deleting_at 이 있다)
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, deleting=True)))
    assert errors_with_nodes(issues) == [("knowledge_base_deleting", "kb-faq")]

    # 인덱싱 상태는 경고다. status 가 없으면 비어 있는 것으로 본다
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, indexed=0)))
    assert errors_with_nodes(issues) == []
    assert warnings_with_nodes(issues) == [("knowledge_base_empty", "kb-faq")]
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, indexed=1, failed=2)))
    assert warnings_with_nodes(issues) == [("knowledge_base_failed", "kb-faq")]
    assert "2 failed" in issues[0]["message"]
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, indexed=0, failed=1)))
    assert codes(issues, "warning") == {"knowledge_base_empty", "knowledge_base_failed"}
    issues = issues_of(with_knowledge(), ctx_with(kb(FAQ_ID, status=False)))
    assert warnings_with_nodes(issues) == [("knowledge_base_empty", "kb-faq")]


def test_knowledge_tool_name_rules():
    faq = kb(FAQ_ID)
    for name in sorted(RESERVED_TOOL_NAMES):
        issues = issues_of(with_knowledge(tool_name=name), ctx_with(faq))
        assert errors_with_nodes(issues) == [("reserved_tool_name", "kb-faq")], name
    # 한 에이전트에 같은 이름 둘: 표식은 뒤의 노드. 다른 에이전트끼리는 같아도 된다
    spec = with_knowledge(tool_name="search_faq")
    spec["graph"]["nodes"].append(knowledge_node("kb-2", tool_name="search_faq"))
    attach(spec, "agent-main", "kb-2")
    issues = issues_of(spec, ctx_with(faq))
    assert errors_with_nodes(issues) == [("duplicate_tool_name", "kb-2")]
    assert "'kb-faq' and 'kb-2'" in issues[0]["message"]
    spec = with_knowledge(tool_name="search_faq")
    add_sub_agent(spec, "agent-sum", "summarizer")
    attach(spec, "agent-main", "agent-sum", "delegate")
    spec["graph"]["nodes"].append(knowledge_node("kb-2", tool_name="search_faq"))
    attach(spec, "agent-sum", "kb-2")
    assert issues_of(spec, ctx_with(faq)) == []
    # 기본 이름도 둘이면 중복이다
    spec = with_knowledge()
    spec["graph"]["nodes"].append(knowledge_node("kb-2"))
    attach(spec, "agent-main", "kb-2")
    assert errors_with_nodes(issues_of(spec, ctx_with(faq))) == [("duplicate_tool_name", "kb-2")]


def test_knowledge_edges_and_attachment():
    faq = kb(FAQ_ID)
    # 서브에이전트도 붙일 수 있고 한 노드를 메인·서브가 공유할 수 있다
    spec = with_knowledge()
    add_sub_agent(spec, "agent-sum", "summarizer")
    attach(spec, "agent-main", "agent-sum", "delegate")
    attach(spec, "agent-sum", "kb-faq")
    assert issues_of(spec, ctx_with(faq)) == []
    # entry 에서는 못 붙이고 간선 종류는 knowledge 여야 한다
    spec = template_spec()
    spec["graph"]["nodes"].append(knowledge_node())
    attach(spec, "entry", "kb-faq")
    issues = issues_of(spec, ctx_with(faq))
    assert [(i["code"], i["edge_id"]) for i in issues if i["level"] == "error"] == [
        ("invalid_edge_kind", "e-entry-kb-faq")
    ]
    spec = template_spec()
    spec["graph"]["nodes"].append(knowledge_node())
    attach(spec, "agent-main", "kb-faq", "tools")
    issues = issues_of(spec, ctx_with(faq))
    assert [(i["code"], i["edge_id"]) for i in issues if i["level"] == "error"] == [
        ("invalid_edge_kind", "e-agent-main-kb-faq")
    ]
    # 붙지 않은 노드는 경고이고 나머지 규칙은 그대로 본다 (모델 노드와 같다)
    spec = template_spec()
    spec["graph"]["nodes"].append(knowledge_node())
    issues = issues_of(spec, ctx_with(kb(FAQ_ID, provider="nokey")))
    assert errors_with_nodes(issues) == [("provider_no_key", "kb-faq")]
    assert warnings_with_nodes(issues) == [("unattached_node", "kb-faq")]
    # 닿지 않는 서브에이전트에만 붙은 노드도 붙지 않은 것이다
    spec = with_knowledge()
    add_sub_agent(spec, "agent-lost", "lost")
    spec["graph"]["nodes"].append(knowledge_node("kb-2", tool_name="other"))
    attach(spec, "agent-lost", "kb-2")
    issues = issues_of(spec, ctx_with(faq))
    assert codes(issues) == {"unreachable_agent"}
    assert warnings_with_nodes(issues) == [
        ("unattached_node", "model-agent-lost"),
        ("unattached_node", "kb-2"),
    ]


# -- 컴파일 ----------------------------------------------------------------------------------


def test_compile_without_knowledge_is_the_version_1_document():
    """닿는 에이전트에 knowledge 노드가 없으면 Knowledge Base 가 있어도, 붙지 않은 knowledge 노드가
    있어도 support-agent.json 과 바이트 동일한 "1" 문서다."""
    expected = agent_fixture("support-agent.json")
    plain = template_spec()
    loose = template_spec()
    loose["graph"]["nodes"].append(knowledge_node())
    for spec in (plain, loose):
        doc = compile_spec(spec, ctx_with(kb(FAQ_ID)))
        assert doc["schema_version"] == "1"
        assert "knowledge" not in doc and "knowledge" not in doc["main"]
        assert all("knowledge" not in s for s in doc["subagents"])
        doc["tools"][0]["url"] = expected["tools"][0]["url"]
        assert json.dumps(doc, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert agent_graph.schema_version(False, False) == "1"
    assert agent_graph.schema_version(True, False) == "2"
    # 사다리의 맨 위. 장기 기억이 켜지면 knowledge 항목이 있든 없든 "3" 이다 (D34)
    assert agent_graph.schema_version(False, True) == "3"
    assert agent_graph.schema_version(True, True) == "3"


def test_compile_knowledge_items_refs_env_key_and_dsn():
    spec = with_knowledge(tool_name="search_faq")
    add_sub_agent(spec, "agent-sum", "summarizer")
    attach(spec, "agent-main", "agent-sum", "delegate")
    spec["graph"]["nodes"].append(
        knowledge_node(
            "kb-docs", DOCS_ID, description="Search product docs.", top_k=3, min_score=0.5
        )
    )
    attach(spec, "agent-sum", "kb-faq")
    attach(spec, "agent-sum", "kb-docs")
    docs = kb(DOCS_ID, "docs", provider="local-openai", ds_id="ds-pg2", dimensions=1536)
    ctx = ctx_with(kb(FAQ_ID), docs)
    bindings = [
        Binding(data_service_id="ds-pg", env_prefix="DAEGAM_DB_0"),
        Binding(data_service_id="ds-pg2", env_prefix="DAEGAM_DB_1"),
    ]
    doc = compile_spec(spec, ctx, bindings=bindings)
    assert check_schema(doc) == []
    assert doc["schema_version"] == "2"
    assert [k["ref"] for k in doc["knowledge"]] == ["kb-faq", "kb-docs"]
    faq_item, docs_item = doc["knowledge"]
    assert faq_item == {
        "ref": "kb-faq",
        "base": {"id": FAQ_ID, "name": "faq"},
        "store": {
            "kind": "postgres",
            "dsn_env": "DAEGAM_DB_0_URL",
            "table": f"daegam_kb_{FAQ_ID}",
            "dimensions": 64,
        },
        "embedding": {
            "provider": {"name": "local-stub", "kind": "stub", "env_key": None, "base_url": None},
            "model": "stub",
        },
        "tool": {
            "name": "search_faq",
            "description": "Search the knowledge base 'faq' for passages relevant to a query.",
            "top_k": 5,
            "min_score": None,
        },
    }
    assert docs_item["base"] == {"id": DOCS_ID, "name": "docs"}
    assert docs_item["store"] == {
        "kind": "postgres",
        "dsn_env": "DAEGAM_DB_1_URL",
        "table": f"daegam_kb_{DOCS_HEX}",
        "dimensions": 1536,
    }
    # env_key 는 knowledge[] 순서 n 이다 (두 번째 항목이라 1)
    assert docs_item["embedding"] == {
        "provider": {
            "name": "local-openai",
            "kind": "openai",
            "env_key": "DAEGAM_KNOWLEDGE_1_API_KEY",
            "base_url": "https://gateway.example.com/v1",
        },
        "model": "text-embedding-3-small",
    }
    assert docs_item["tool"] == {
        "name": "search_knowledge",
        "description": "Search product docs.",
        "top_k": 3,
        "min_score": 0.5,
    }
    assert doc["main"]["knowledge"] == ["kb-faq"]
    (sub,) = doc["subagents"]
    assert sub["knowledge"] == ["kb-faq", "kb-docs"]
    assert doc["memory"]["checkpointer"]["dsn_env"] == "DAEGAM_DB_0_URL"
    assert "api_key" not in json.dumps(doc) and "sk-" not in json.dumps(doc)

    # 순서를 바꾸면 n 도 바뀐다: openai 항목이 먼저 오면 DAEGAM_KNOWLEDGE_0_API_KEY
    nodes = spec["graph"]["nodes"]
    nodes.insert(
        nodes.index(node_of(spec, "kb-faq")), nodes.pop(nodes.index(node_of(spec, "kb-docs")))
    )
    doc = compile_spec(spec, ctx, bindings=bindings)
    assert [(k["ref"], k["embedding"]["provider"]["env_key"]) for k in doc["knowledge"]] == [
        ("kb-docs", "DAEGAM_KNOWLEDGE_0_API_KEY"),
        ("kb-faq", None),
    ]
    assert sub["knowledge"] == ["kb-faq", "kb-docs"]  # 참조는 노드 순서를 따른다
    assert doc["subagents"][0]["knowledge"] == ["kb-docs", "kb-faq"]

    # 같은 Database 면 같은 env 를 가리키고, 바인딩이 없으면 기본 접두어다
    same = ctx_with(kb(FAQ_ID), kb(DOCS_ID, "docs", provider="local-openai", ds_id="ds-pg"))
    doc = compile_spec(
        spec, same, bindings=[Binding(data_service_id="ds-pg", env_prefix="DAEGAM_DB_3")]
    )
    assert {k["store"]["dsn_env"] for k in doc["knowledge"]} == {"DAEGAM_DB_3_URL"}
    assert doc["memory"]["checkpointer"]["dsn_env"] == "DAEGAM_DB_3_URL"
    doc = compile_spec(spec, ctx, bindings=[])
    assert {k["store"]["dsn_env"] for k in doc["knowledge"]} == {"DAEGAM_DB_0_URL"}
    assert doc["memory"]["checkpointer"]["dsn_env"] == "DAEGAM_DB_0_URL"

    # 기본 설명은 표시 이름을 쓴다
    doc = compile_spec(with_knowledge(), ctx_with(kb(FAQ_ID, display_name="제품 FAQ")))
    assert doc["knowledge"][0]["tool"]["description"] == (
        "Search the knowledge base '제품 FAQ' for passages relevant to a query."
    )
    assert doc["main"]["knowledge"] == ["kb-faq"] and doc["subagents"] == []


def test_compile_matches_knowledge_agent_fixture():
    """stub 임베딩 Knowledge Base 하나를 메인이 붙이고 서브에이전트는 붙이지 않은 그래프는
    knowledge-agent.json 과 정확히 같다 (memory 와 같은 Database, 설명은 노드 값)."""
    fixture = agent_fixture("knowledge-agent.json")
    spec: dict[str, Any] = {
        "graph": {
            "nodes": [
                {
                    "id": "entry",
                    "type": "entry",
                    "data": {"greeting": "안녕하세요. 제품에 관해 무엇이든 물어보세요."},
                },
                {
                    "id": "agent-main",
                    "type": "agent",
                    "data": {
                        "role": "main",
                        "name": "knowledge",
                        "system_prompt": fixture["main"]["system_prompt"],
                        "language": "ko",
                    },
                },
                {
                    "id": "model-main",
                    "type": "model",
                    "data": {"name": "stub", "provider": "local-stub"},
                },
                {
                    "id": "agent-sum",
                    "type": "agent",
                    "data": {
                        "role": "sub",
                        "name": "summarizer",
                        "description": "긴 답을 세 문장으로 줄일 때 맡긴다.",
                        "system_prompt": fixture["subagents"][0]["system_prompt"],
                        "max_tool_calls_per_turn": 2,
                        "turn_timeout_seconds": 60,
                        "retries": 0,
                    },
                },
                {
                    "id": "model-sum",
                    "type": "model",
                    "data": {
                        "name": "stub",
                        "provider": "local-stub",
                        "max_output_tokens": 512,
                        "reasoning": {"effort": "low"},
                    },
                },
                knowledge_node(
                    "kb-faq",
                    FAQ_ID,
                    tool_name="search_faq",
                    description=fixture["knowledge"][0]["tool"]["description"],
                ),
                {
                    "id": "memory",
                    "type": "memory",
                    "data": {"checkpointer": {"data_service_id": "ds-pg"}},
                },
            ],
            "edges": [
                {"id": "e-entry", "source": "entry", "target": "agent-main", "kind": "entry"},
                {"id": "e-model", "source": "agent-main", "target": "model-main", "kind": "model"},
                {
                    "id": "e-delegate",
                    "source": "agent-main",
                    "target": "agent-sum",
                    "kind": "delegate",
                },
                {
                    "id": "e-model-sum",
                    "source": "agent-sum",
                    "target": "model-sum",
                    "kind": "model",
                },
                {"id": "e-kb", "source": "agent-main", "target": "kb-faq", "kind": "knowledge"},
                {"id": "e-memory", "source": "agent-main", "target": "memory", "kind": "memory"},
            ],
        },
        "service": {"budget": {"session_tokens": 200000}},
    }
    doc = compile_spec(spec, ctx_with(kb(FAQ_ID)), agent_id="ai-knowledge-0001", name="knowledge")
    assert check_schema(doc) == []
    assert doc == fixture


def test_compile_refuses_knowledge_errors():
    with pytest.raises(agent_graph.GraphError) as e:
        compile_spec(with_knowledge(), ctx_with(kb(FAQ_ID, provider="nokey")))
    assert {i["code"] for i in e.value.to_dict()["issues"]} == {"provider_no_key"}
    with pytest.raises(agent_graph.GraphError) as e:
        compile_spec(with_knowledge(), ctx_with())
    assert {i["code"] for i in e.value.to_dict()["issues"]} == {"unknown_knowledge_base"}


# -- provision 접근자 --------------------------------------------------------------------------


def test_used_providers_and_knowledge_providers():
    spec = template_spec(provider="local-openai")
    # id 는 테이블 이름이 되므로 32 hex 여야 한다 (스키마가 잡는다)
    a, b, c, d = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
    for node_id, kb_id, name in (("kb-a", a, "a"), ("kb-b", b, "b"), ("kb-c", c, "c")):
        spec["graph"]["nodes"].append(knowledge_node(node_id, kb_id, tool_name=f"search_{name}"))
        attach(spec, "agent-main", node_id)
    spec["graph"]["nodes"].append(knowledge_node("kb-loose", d, tool_name="search_d"))
    kbs = {
        a: kb(a, "a", provider="local-openai"),
        b: kb(b, "b", provider="local-stub"),
        c: kb(c, "c", provider="local-openai"),
        d: kb(d, "d", provider="loose-openai"),
    }
    rev = AgentSpec.model_validate(spec)
    # 모델 provider 뒤에 knowledge 의 임베딩 provider 가 중복 없이 붙는다. 붙지 않은 노드는 뺀다
    assert agent_graph.used_providers(rev, kbs) == ["local-openai", "local-stub"]
    assert agent_graph.used_providers(rev, {}) == ["local-openai"]
    # knowledge[] 순서 n 과 provider. 같은 provider 도 항목마다 있다
    assert agent_graph.knowledge_providers(rev, kbs) == [
        (0, "local-openai"),
        (1, "local-stub"),
        (2, "local-openai"),
    ]
    # 테넌트에 없는 Knowledge Base 는 건너뛴다 (validate 가 먼저 막는다)
    assert agent_graph.knowledge_providers(rev, {b: kbs[b]}) == [(1, "local-stub")]
    assert agent_graph.used_providers(rev, {b: kbs[b]}) == ["local-openai", "local-stub"]
    # compile 의 knowledge[] 와 같은 순서·같은 env 번호다
    ctx = ctx_with(*kbs.values(), oidc=False, keyed=("local-openai", "loose-openai"))
    ctx.providers["loose-openai"] = LLMProvider(name="loose-openai", kind="openai")
    doc = compile_spec(spec, ctx)
    assert [(n, k["embedding"]["provider"]["name"]) for n, k in enumerate(doc["knowledge"])] == (
        agent_graph.knowledge_providers(rev, kbs)
    )
    assert [k["embedding"]["provider"]["env_key"] for k in doc["knowledge"]] == [
        "DAEGAM_KNOWLEDGE_0_API_KEY",
        None,
        "DAEGAM_KNOWLEDGE_2_API_KEY",
    ]


def test_references_knowledge_base():
    rev = AgentSpec.model_validate(with_knowledge())
    assert agent_graph.references_knowledge_base(rev, FAQ_ID)
    assert not agent_graph.references_knowledge_base(rev, "other")
    assert not agent_graph.references_knowledge_base(
        AgentSpec.model_validate(template_spec()), FAQ_ID
    )
    # 붙지 않은 노드도 참조다 (삭제 가드)
    spec = template_spec()
    spec["graph"]["nodes"].append(knowledge_node())
    assert agent_graph.references_knowledge_base(AgentSpec.model_validate(spec), FAQ_ID)


def test_db_env_prefix_is_the_only_spelling_of_the_rule():
    """Database 바인딩 env 접두어는 `db_env_prefix` 하나가 짓는다.

    이름을 짓는 곳(`agents.bind`)과 기본값(`DEFAULT_DB_PREFIX`), 그리고 그것을 읽는
    `dsn_env` 가 문자열을 따로 쓰면 한쪽만 고쳐도 아무도 못 잡는다. 규칙이 파드 env 이름 규칙의
    자리(`k8s/templates/agent.py`) 하나에 있고 0번이 기본값과 같다는 것만 본다
    (docs/guide/agents.md 5절).
    """
    assert db_env_prefix(0) == "DAEGAM_DB_0"
    assert db_env_prefix(3) == "DAEGAM_DB_3"
    assert DEFAULT_DB_PREFIX == db_env_prefix(0)
    # 접두어를 짓는 자리는 DB_PREFIX_FORMAT 하나뿐이다. 설명 글의 `DAEGAM_DB_<i>` 는 따옴표가
    # 앞에 없어 걸리지 않고, 이름을 만드는 리터럴("DAEGAM_DB_… 나 f"DAEGAM_DB_…)만 걸린다
    daegam = Path(__file__).resolve().parents[1] / "daegam"
    for name in ("services/agent_graph.py", "services/agents.py", "k8s/templates/agent.py"):
        lines = (daegam / name).read_text().splitlines()
        spellings = [line.strip() for line in lines if '"DAEGAM_DB_' in line]
        assert spellings in ([], ['DB_PREFIX_FORMAT = "DAEGAM_DB_{index}"']), f"{name}: {spellings}"


def test_provider_shared_by_model_and_embedding_reads_its_key_once():
    """모델 노드와 Knowledge Base 임베딩이 같은 provider 를 쓰면 검증 한 번에 키를 한 번만 본다.

    키 확인은 동기 K8s Secret 조회다. provider 검사와 knowledge 검사가 key_checker 를 따로 만들면
    같은 Secret 을 두 번 읽는다."""
    ctx = ctx_with(kb(FAQ_ID, provider="local-openai"))
    calls: list[str] = []
    keyed = ctx.has_key
    ctx.has_key = lambda p: calls.append(p.name) or keyed(p)
    assert issues_of(with_knowledge(template_spec(provider="local-openai")), ctx) == []
    assert calls == ["local-openai"]
