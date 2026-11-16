"""agent-runtime/schema/agent.schema.json 계약 고정 (P12 F1a).

fixture 다섯이 스키마를 통과하고 계약 위반 예시가 전부 거부되는 것을 든다. 플랫폼 compile
스냅샷(F1b)과 런타임 로더(F2a)가 같은 fixture 를 공유한다. schema_version "2" 의 knowledge
키 셋(P15 F3a, D32)은 knowledge-agent.json 이, memory.checkpointer.kind redis(P16 F1, D33)는
redis-memory.json 이, 기능 skills 의 키 셋(D64)은 skills-agent.json 이, 기능 web_guard 의 키
셋(D64)은 web-agent.json 이 든다. Mongo 는 쓰지 않는다.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.helpers.consts import REPO

SCHEMA_DIR = REPO / "agent-runtime" / "schema"
SCHEMA_PATH = SCHEMA_DIR / "agent.schema.json"
FIXTURES_DIR = SCHEMA_DIR / "fixtures"
FIXTURE_NAMES = [
    "support-agent.json",
    "minimal.json",
    "knowledge-agent.json",
    "redis-memory.json",
    "skills-agent.json",
    "web-agent.json",
]


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def fixtures() -> dict[str, Any]:
    return {name: _load(FIXTURES_DIR / name) for name in FIXTURE_NAMES}


def _errors(validator: Draft202012Validator, doc: Any) -> list[str]:
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def _set(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    cur: Any = out
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return out


def _del(doc: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    cur: Any = out
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    del cur[parts[-1]]
    return out


def test_fixture_dir_has_exactly_the_contract_fixtures() -> None:
    assert sorted(p.name for p in FIXTURES_DIR.glob("*.json")) == sorted(FIXTURE_NAMES)


def test_top_level_keys_are_fixed() -> None:
    # knowledge 만 schema_version 에 따라 갈리므로 required 에 없고 if/then/else 가 정한다.
    # features 는 판과 따로인 선택 키이고 skills·web_data_tools·code_execution 은 features 에 그
    # 이름이 있을 때만 받는다(D65·D64·D95)
    schema = _load(SCHEMA_PATH)
    expected = [
        "schema_version",
        "profile",
        "instance",
        "main",
        "subagents",
        "tools",
        "knowledge",
        "memory",
        "guardrails",
        "entry",
        "service",
        "stub",
        "features",
        "skills",
        "web_data_tools",
        "code_execution",
    ]
    optional = {"knowledge", "features", "skills", "web_data_tools", "code_execution"}
    assert list(schema["properties"]) == expected
    assert schema["required"] == [k for k in expected if k not in optional]
    assert schema["additionalProperties"] is False
    features = schema["properties"]["features"]
    assert features["uniqueItems"] is True
    assert features["items"]["pattern"] == "^[a-z][a-z0-9_]{0,31}$"
    # 버전은 사다리다. "1" 은 둘 다 없고, "2" 는 knowledge 키 셋, "3" 은 거기에 memory.long_term 이
    # 더해진다(D32·D34). 직교하는 두 기능에 선형 버전을 쓰는 값이라 allOf 두 조건으로 편다.
    assert schema["properties"]["schema_version"]["enum"] == ["1", "2", "3"]
    ladder = [c for c in schema["allOf"] if "schema_version" in c["if"].get("properties", {})]
    assert len(ladder) == 2, "사다리 조건이 둘이어야 한다"

    knowledge, long_term = ladder
    assert knowledge["if"]["properties"]["schema_version"]["enum"] == ["2", "3"]
    assert knowledge["then"]["required"] == ["knowledge"]
    assert knowledge["else"]["properties"]["knowledge"] is False

    assert long_term["if"]["properties"]["schema_version"]["const"] == "3"
    assert long_term["then"]["properties"]["memory"]["required"] == ["long_term"]
    assert long_term["else"]["properties"]["memory"]["properties"]["long_term"] is False


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_passes(
    validator: Draft202012Validator, fixtures: dict[str, Any], name: str
) -> None:
    assert _errors(validator, fixtures[name]) == []


def test_fixture_documents_do_not_carry_secret_values(fixtures: dict[str, Any]) -> None:
    # 키 값은 env 이름으로만 가리킨다. 문서 어디에도 secret·api_key 류 키가 없다.
    forbidden = {"api_key", "client_secret", "secret", "token", "password", "dsn"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert not forbidden & set(node), f"비밀 값이 들어갈 키가 있다: {forbidden & set(node)}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for doc in fixtures.values():
        walk(doc)


def test_support_agent_fixture_shape(fixtures: dict[str, Any]) -> None:
    """템플릿 "단일 상담 에이전트" 다.

    입구, 메인, Gateway 도구 1개(승인 툴 1개 포함), 메모리, 입력 가드레일 1개로 이뤄진다.
    """
    doc = fixtures["support-agent.json"]
    assert doc["stub"] is True
    assert doc["main"]["model"]["provider"]["kind"] == "stub"
    assert doc["main"]["model"]["provider"]["env_key"] is None
    assert len(doc["tools"]) == 1
    tool = doc["tools"][0]
    assert tool["source"]["kind"] == "gateway_instance"
    assert tool["auth"]["kind"] == "oauth_client_credentials"
    assert doc["main"]["tools"] == [tool["ref"]]
    assert len(tool["approval"]) == 1
    assert doc["memory"]["checkpointer"]["kind"] == "postgres"
    assert [g["position"] for g in doc["guardrails"]] == ["input"]
    assert doc["subagents"] == []


def test_redis_memory_fixture_shape(fixtures: dict[str, Any]) -> None:
    """support-agent.json 과 memory.checkpointer.kind 만 다른 "1" 문서다(D33). Redis 도 접속
    env 는 같은 바인딩 접두어의 _URL 이다."""
    doc = fixtures["redis-memory.json"]
    base = fixtures["support-agent.json"]
    assert doc["memory"]["checkpointer"] == {"kind": "redis", "dsn_env": "DAEGAM_DB_0_URL"}
    assert _set(doc, "memory.checkpointer.kind", "postgres") == base


def test_minimal_fixture_shape(fixtures: dict[str, Any]) -> None:
    doc = fixtures["minimal.json"]
    assert doc["stub"] is False
    assert doc["tools"] == [] and doc["main"]["tools"] == []
    assert doc["memory"] is None
    assert doc["guardrails"] == []
    assert doc["entry"]["rate_limit"] is None


def test_v1_fixtures_have_no_knowledge_keys(fixtures: dict[str, Any]) -> None:
    """schema_version "1" 문서는 P15 전과 글자 그대로 같다. knowledge 키 셋이 어디에도 없다."""
    for name in ("support-agent.json", "minimal.json", "redis-memory.json"):
        doc = fixtures[name]
        assert doc["schema_version"] == "1"
        assert "knowledge" not in doc and "knowledge" not in doc["main"]
        assert all("knowledge" not in s for s in doc["subagents"])


def test_knowledge_fixture_shape(fixtures: dict[str, Any]) -> None:
    """schema_version "2" 문서다. stub 임베딩 Knowledge Base 하나가 메인의 검색 도구로 붙고
    서브에이전트는 knowledge 가 비어 있다. store 는 memory 와 같은 Database 를 가리킨다."""
    doc = fixtures["knowledge-agent.json"]
    assert doc["schema_version"] == "2"
    assert doc["stub"] is True
    assert len(doc["knowledge"]) == 1
    item = doc["knowledge"][0]
    assert doc["main"]["knowledge"] == [item["ref"]]
    assert [s["knowledge"] for s in doc["subagents"]] == [[]]
    assert item["store"]["kind"] == "postgres"
    assert item["store"]["table"] == f"daegam_kb_{item['base']['id']}"
    assert item["store"]["dimensions"] == 64
    assert item["store"]["dsn_env"] == doc["memory"]["checkpointer"]["dsn_env"]
    assert item["embedding"]["provider"]["kind"] == "stub"
    assert item["embedding"]["provider"]["env_key"] is None
    assert item["tool"]["name"] == "search_faq"


# 삭제 표식. value 자리에 오면 그 경로를 지운다.
DELETE = object()

MIN = "minimal.json"
SUP = "support-agent.json"
KB = "knowledge-agent.json"
RM = "redis-memory.json"

# (이름, fixture, 점 경로, 값). 어느 것도 스키마를 통과하면 안 된다.
BAD_CASES: list[tuple[str, str, str, Any]] = [
    ("top_level_unknown_key", MIN, "extra", 1),
    ("api_key_in_provider", MIN, "main.model.provider.api_key", "sk"),
    ("client_secret_in_auth", SUP, "tools.0.auth.client_secret", "s"),
    ("unknown_param", MIN, "main.model.params.top_p", 0.9),
    ("unknown_entry_key", MIN, "entry.max_tokens", 1),
    ("stub_flag_mismatch", MIN, "stub", True),
    ("stub_kind_mismatch", SUP, "stub", False),
    ("stub_with_env_key", SUP, "main.model.provider.env_key", "K"),
    ("real_provider_without_env_key", MIN, "main.model.provider.env_key", None),
    ("missing_entry", MIN, "entry", DELETE),
    ("missing_memory_key", MIN, "memory", DELETE),
    ("compaction_fraction_over_one", MIN, "main.compaction.trigger.value", 1.5),
    (
        "compaction_tokens_not_integer",
        MIN,
        "main.compaction.trigger",
        {"kind": "tokens", "value": 0.5},
    ),
    ("keep_kind_tokens", MIN, "main.compaction.keep", {"kind": "tokens", "value": 10}),
    ("unknown_effort", MIN, "main.model.params.effort", "ultra"),
    ("unknown_decision", SUP, "tools.0.approval.Divide", ["delete"]),
    ("empty_decisions", SUP, "tools.0.approval.Divide", []),
    ("unknown_transport", SUP, "tools.0.transport", "sse"),
    ("bearer_without_secret_env", SUP, "tools.0.auth", {"kind": "bearer"}),
    ("unknown_auth_kind", SUP, "tools.0.auth", {"kind": "basic", "secret_env": "DAEGAM_X"}),
    ("pii_regex_without_pattern", SUP, "guardrails.0.rules.1.value.types", ["regex"]),
    ("unknown_guardrail_kind", SUP, "guardrails.0.rules.0", {"kind": "rewrite", "value": 1}),
    ("guardrail_rewrite", SUP, "guardrails.0.on_violation", "rewrite"),
    # memory.checkpointer.kind 는 postgres·redis 둘뿐이다(D33)
    ("memory_mysql", SUP, "memory.checkpointer.kind", "mysql"),
    ("memory_kind_upper", RM, "memory.checkpointer.kind", "Redis"),
    ("memory_kind_missing", RM, "memory.checkpointer.kind", DELETE),
    ("bad_env_name", SUP, "memory.checkpointer.dsn_env", "lower-case"),
    ("redis_memory_bad_env_name", RM, "memory.checkpointer.dsn_env", "lower-case"),
    ("json_schema_without_schema", MIN, "main.response_format", {"kind": "json_schema"}),
    ("schema_version_unknown", MIN, "schema_version", "3"),
    ("revision_zero", MIN, "profile.revision", 0),
    # schema_version "1" 은 knowledge 키가 오면 거부된다(옛 런타임보다 느슨해지지 않는다)
    ("v1_with_knowledge", MIN, "knowledge", []),
    ("v1_with_main_knowledge", MIN, "main.knowledge", []),
    ("v1_from_knowledge_doc", KB, "schema_version", "1"),
    # schema_version "2" 는 knowledge 키 셋이 전부 있어야 한다
    ("v2_without_knowledge", KB, "knowledge", DELETE),
    ("v2_without_main_knowledge", KB, "main.knowledge", DELETE),
    ("v2_without_subagent_knowledge", KB, "subagents.0.knowledge", DELETE),
    ("v2_from_minimal", MIN, "schema_version", "2"),
    # knowledge 항목의 모양
    ("knowledge_unknown_key", KB, "knowledge.0.extra", 1),
    ("knowledge_ref_empty", KB, "knowledge.0.ref", ""),
    ("knowledge_main_ref_duplicate", KB, "main.knowledge", ["kb-faq", "kb-faq"]),
    ("knowledge_store_redis", KB, "knowledge.0.store.kind", "redis"),
    ("knowledge_bad_dsn_env", KB, "knowledge.0.store.dsn_env", "lower-case"),
    ("knowledge_bad_table_prefix", KB, "knowledge.0.store.table", "kb_" + "a" * 32),
    ("knowledge_bad_table_short", KB, "knowledge.0.store.table", "daegam_kb_" + "a" * 31),
    ("knowledge_bad_table_upper", KB, "knowledge.0.store.table", "daegam_kb_" + "A" * 32),
    ("knowledge_dimensions_zero", KB, "knowledge.0.store.dimensions", 0),
    ("knowledge_dimensions_over", KB, "knowledge.0.store.dimensions", 8193),
    ("knowledge_dimensions_float", KB, "knowledge.0.store.dimensions", 64.5),
    ("knowledge_embedding_anthropic", KB, "knowledge.0.embedding.provider.kind", "anthropic"),
    ("knowledge_stub_with_env_key", KB, "knowledge.0.embedding.provider.env_key", "K"),
    ("knowledge_api_key_in_provider", KB, "knowledge.0.embedding.provider.api_key", "sk"),
    ("knowledge_model_empty", KB, "knowledge.0.embedding.model", ""),
    ("knowledge_tool_name_dash", KB, "knowledge.0.tool.name", "search-faq"),
    ("knowledge_tool_name_empty", KB, "knowledge.0.tool.name", ""),
    ("knowledge_tool_name_long", KB, "knowledge.0.tool.name", "a" * 65),
    ("knowledge_top_k_zero", KB, "knowledge.0.tool.top_k", 0),
    ("knowledge_top_k_over", KB, "knowledge.0.tool.top_k", 21),
    ("knowledge_min_score_over", KB, "knowledge.0.tool.min_score", 1.5),
    ("knowledge_min_score_negative", KB, "knowledge.0.tool.min_score", -0.1),
    ("knowledge_min_score_missing", KB, "knowledge.0.tool.min_score", DELETE),
    # features 는 소문자 스네이크 이름의 유일한 목록이다(D65)
    ("features_not_a_list", MIN, "features", "cancel"),
    ("features_bad_name", MIN, "features", ["Bad-Name"]),
    ("features_name_too_long", MIN, "features", ["a" * 33]),
    ("features_duplicate", MIN, "features", ["cancel", "cancel"]),
    # Skill 키 셋은 features 에 skills 가 있을 때만 받는다(D64)
    ("skills_without_feature", MIN, "skills", []),
    ("main_skills_without_feature", MIN, "main.skills", []),
    ("subagent_skills_without_feature", KB, "subagents.0.skills", []),
    # 웹 Tool 의 키 셋은 features 에 web_guard 가 있을 때만 받는다(D64)
    ("web_data_tools_without_feature", MIN, "web_data_tools", False),
    ("web_without_feature", "web-agent.json", "features", DELETE),
]


@pytest.mark.parametrize(
    ("case", "fixture", "path", "value"), BAD_CASES, ids=[c[0] for c in BAD_CASES]
)
def test_contract_violation_is_rejected(
    validator: Draft202012Validator,
    fixtures: dict[str, Any],
    case: str,
    fixture: str,
    path: str,
    value: Any,
) -> None:
    base = fixtures[fixture]
    doc = _del(base, path) if value is DELETE else _set(base, path, value)
    assert _errors(validator, doc), f"{case}: 계약 위반이 통과했다"


def test_v1_with_subagent_knowledge_key_is_rejected(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    # "1" 문서의 서브에이전트가 knowledge 키를 가지면 그 자리에서 거부된다
    doc = _set(fixtures["knowledge-agent.json"], "schema_version", "1")
    doc = _del(doc, "knowledge")
    doc = _del(doc, "main.knowledge")
    assert any(e.startswith("subagents/0:") for e in _errors(validator, doc))


def test_v2_with_empty_knowledge_is_accepted(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    # "2" 는 키 셋이 있으면 되고 비어 있어도 된다
    doc = _set(fixtures["minimal.json"], "schema_version", "2")
    doc = _set(doc, "knowledge", [])
    doc = _set(doc, "main.knowledge", [])
    assert _errors(validator, doc) == []


def test_features_ride_on_any_version(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    """features 는 판과 따로인 선택 키라 "1"·"2" 어느 문서에든 실리고 빈 목록도 된다(D65)."""
    assert _errors(validator, _set(fixtures["minimal.json"], "features", ["cancel"])) == []
    assert _errors(validator, _set(fixtures["minimal.json"], "features", [])) == []
    kb = _set(fixtures["knowledge-agent.json"], "features", ["cancel", "skills"])
    assert _errors(validator, kb) == []


def test_skills_ride_on_the_skills_feature(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    """기능 skills 가 있으면 최상위 핀과 에이전트별 이름 목록을 받는다(D64). 표본은
    skills-agent.json 이고 런타임의 init 테스트도 같은 문서를 읽는다."""
    doc = fixtures["skills-agent.json"]
    assert doc["features"] == ["skills"] and doc["skills"]
    assert doc["main"]["skills"] and any(s.get("skills") for s in doc["subagents"])
    assert _errors(validator, doc) == []
    # 핀에는 bytes 가 있어야 하고, 기능이 빠지면 Skill 키 셋을 받지 않는다
    pin = {k: v for k, v in doc["skills"][0].items() if k != "bytes"}
    assert _errors(validator, _set(doc, "skills", [pin, *doc["skills"][1:]]))
    assert _errors(validator, _del(doc, "features"))


def test_web_keys_ride_on_the_web_guard_feature(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    """기능 web_guard 가 있으면 최상위 web_data_tools 와 소스별 web 을 받고, 그때는
    web_data_tools 가 필수이며 어느 소스의 allow 도 비지 않는다(배포 때의 Tool 핀, D64). 표본은
    web-agent.json 이고 런타임의 웹 가드 테스트도 같은 문서를 읽는다."""
    doc = fixtures["web-agent.json"]
    assert doc["features"] == ["web_guard"] and doc["web_data_tools"] is True
    assert _errors(validator, doc) == []
    assert _errors(validator, _del(doc, "web_data_tools"))
    assert _errors(validator, _set(doc, "tools.0.allow", []))
    assert _errors(validator, _set(doc, "tools.1.web.allow_domains", ["Docs.Example.com"]))


def test_knowledge_openai_embedding_and_min_score_are_accepted(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    doc = _set(
        fixtures["knowledge-agent.json"],
        "knowledge.0.embedding",
        {
            "provider": {
                "name": "local-openai",
                "kind": "openai",
                "env_key": "DAEGAM_KNOWLEDGE_0_API_KEY",
                "base_url": "https://gateway.example.com/v1",
            },
            "model": "text-embedding-3-large",
        },
    )
    doc = _set(doc, "knowledge.0.store.dimensions", 3072)
    doc = _set(doc, "knowledge.0.tool.min_score", 0.35)
    doc = _set(doc, "subagents.0.knowledge", ["kb-faq"])
    assert _errors(validator, doc) == []


def test_pii_regex_with_pattern_is_accepted(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    doc = _set(
        fixtures["support-agent.json"],
        "guardrails.0.rules.1.value",
        {"types": ["email", "regex"], "regex": r"\bACME-\d{6}\b"},
    )
    assert _errors(validator, doc) == []


def test_json_schema_response_format_is_accepted(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    doc = _set(
        fixtures["minimal.json"],
        "main.response_format",
        {
            "kind": "json_schema",
            "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        },
    )
    assert _errors(validator, doc) == []


def test_subagent_and_fallback_use_the_same_model_object(
    validator: Draft202012Validator, fixtures: dict[str, Any]
) -> None:
    base = fixtures["minimal.json"]
    model = _set(
        base["main"]["model"],
        "provider",
        {
            "name": "local-openai",
            "kind": "openai",
            "env_key": "OPENAI_API_KEY",
            "base_url": "https://gateway.example.com/v1",
        },
    )
    model = _set(model, "params.temperature", 0.2)
    doc = _set(base, "main.fallback", model)
    doc = _set(doc, "main.subagents", ["researcher"])
    doc = _set(
        doc,
        "subagents",
        [
            {
                "name": "researcher",
                "description": "자료를 찾아 요약한다.",
                "system_prompt": "찾은 것만 말한다.",
                "model": model,
                "tools": [],
                "limits": {"max_tool_calls_per_turn": 4, "turn_timeout_seconds": 60, "retries": 1},
                "interrupt_on": {},
            }
        ],
    )
    assert _errors(validator, doc) == []


def test_check_script_runs_green() -> None:
    """agent-runtime/schema/check.py 는 같은 검사를 독립 실행으로 든다."""
    result = subprocess.run(
        [sys.executable, str(SCHEMA_DIR / "check.py")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "모두 기대대로다" in result.stdout
