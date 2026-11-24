#!/usr/bin/env python3
"""agent.schema.json 계약 검사다.

fixtures/*.json 이 스키마를 통과하고, 계약 위반 예시(모르는 키, 비밀 값, stub 불일치 등)가
전부 실패하는지 확인한다. schema_version 사다리(knowledge 는 "2" 부터, 장기 기억은 "3" 에서만)는
fixture 를 손봐 만든 LADDER_CASES 가 양쪽 방향으로 든다. 표준 라이브러리와 jsonschema 만 쓴다.
jsonschema 가 없으면 `uv run --with jsonschema` 로 자기 자신을 다시 실행한다.

    python3 agent-runtime/schema/check.py

같은 검사를 platform/tests/test_agent_schema.py 가 pytest 로 든다.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "agent.schema.json"
FIXTURES_DIR = HERE / "fixtures"
REEXEC_MARK = "DAEGAM_SCHEMA_CHECK_REEXEC"


def _ensure_jsonschema() -> None:
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        if os.environ.get(REEXEC_MARK):
            sys.exit("jsonschema 를 가져올 수 없다. `uv run --with jsonschema` 재실행도 실패했다.")
        os.environ[REEXEC_MARK] = "1"
        argv = ["uv", "run", "--no-project", "--with", "jsonschema", str(Path(__file__).resolve())]
        os.execvp("uv", argv + sys.argv[1:])


def set_path(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """deep copy 위에 점 경로(`a.b.0.c`)의 값을 심는다. 마지막 조각이 없으면 새로 만든다."""
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


def del_path(doc: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    cur: Any = out
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    del cur[parts[-1]]
    return out


# 삭제 표식. value 자리에 오면 그 경로를 지운다.
DELETE = object()

MIN = "minimal.json"
SUP = "support-agent.json"
KB = "knowledge-agent.json"
RM = "redis-memory.json"
SK = "skills-agent.json"
WEB = "web-agent.json"

# 기능 skills 의 핀 하나다(D64). 표본 문서의 것을 그대로 쓴다
SKILL_PIN = json.loads((FIXTURES_DIR / SK).read_text(encoding="utf-8"))["skills"][0]

# 기능 agent_tools 의 Agent 소스 하나다. 표본 문서가 없어 최소 문서에 심는다
AGENT_SOURCE = {
    "ref": "tools-research",
    "source": {"kind": "agent", "id": "ag-research-0001", "name": "research"},
    "url": "http://platform.daegam-system.svc:8000/api/internal/agent-calls/ag-research-0001",
    "allow": ["ask_research"],
    "approval": {},
    "timeouts": {},
    "description": "Answers research questions.",
    "timeout_seconds": 630,
}
# 작업을 맡기는 Agent 소스 하나다. Tool 은 다섯이고 기다리는 시간이 없다
TASK_TOOLS = [
    "start_async_task",
    "check_async_task",
    "update_async_task",
    "cancel_async_task",
    "list_async_tasks",
]
TASK_DOC = [
    ("features", ["agent_tools"]),
    (
        "tools",
        [
            {
                **{k: v for k, v in AGENT_SOURCE.items() if k != "timeout_seconds"},
                "url": "http://platform.daegam-system.svc:8000/api/internal/agent-protocol/ag-research-0001",
                "allow": TASK_TOOLS,
                "mode": "task",
            }
        ],
    ),
    ("main.tools", ["tools-research"]),
]
AGENT_DOC = [
    ("features", ["agent_tools"]),
    ("tools", [AGENT_SOURCE]),
    ("main.tools", ["tools-research"]),
]

# (이름, fixture 파일, 점 경로, 값) 계약 위반 예시. 하나라도 통과하면 계약이 느슨한 것이다.
BAD_CASES: list[tuple[str, str, str, Any]] = [
    ("top_level_unknown_key", MIN, "extra", 1),
    ("api_key_in_provider", MIN, "main.model.provider.api_key", "sk-x"),
    ("client_secret_in_auth", SUP, "tools.0.auth.client_secret", "s"),
    ("unknown_param", MIN, "main.model.params.top_p", 0.9),
    ("stub_flag_mismatch", MIN, "stub", True),
    ("stub_kind_mismatch", SUP, "stub", False),
    ("stub_with_env_key", SUP, "main.model.provider.env_key", "K"),
    ("missing_entry", MIN, "entry", DELETE),
    ("compaction_fraction_over_one", MIN, "main.compaction.trigger.value", 1.5),
    (
        "compaction_tokens_not_integer",
        MIN,
        "main.compaction.trigger",
        {"kind": "tokens", "value": 0.5},
    ),
    ("unknown_decision", SUP, "tools.0.approval.Divide", ["delete"]),
    ("unknown_transport", SUP, "tools.0.transport", "sse"),
    ("pii_regex_without_pattern", SUP, "guardrails.0.rules.1.value.types", ["regex"]),
    ("unknown_guardrail_kind", SUP, "guardrails.0.rules.0", {"kind": "rewrite", "value": 1}),
    ("memory_mysql", SUP, "memory.checkpointer.kind", "mysql"),
    ("bad_env_name", SUP, "memory.checkpointer.dsn_env", "lower-case"),
    ("redis_memory_bad_env_name", RM, "memory.checkpointer.dsn_env", "lower-case"),
    ("json_schema_without_schema", MIN, "main.response_format", {"kind": "json_schema"}),
    ("schema_version_unknown", MIN, "schema_version", "4"),
    ("v1_with_knowledge", MIN, "knowledge", []),
    ("v1_with_main_knowledge", MIN, "main.knowledge", []),
    ("v2_without_knowledge", KB, "knowledge", DELETE),
    ("v2_without_main_knowledge", KB, "main.knowledge", DELETE),
    ("v2_without_subagent_knowledge", KB, "subagents.0.knowledge", DELETE),
    ("knowledge_bad_tool_name", KB, "knowledge.0.tool.name", "search-faq"),
    ("knowledge_dimensions_over", KB, "knowledge.0.store.dimensions", 8193),
    ("knowledge_bad_table", KB, "knowledge.0.store.table", "daegam_kb_faq"),
    # 장기 기억은 "3" 에서만이고 그때는 필수다(D34 의 사다리)
    ("v1_with_long_term", SUP, "memory.long_term", {"retention_days": 30}),
    ("v2_with_long_term", KB, "memory.long_term", {"retention_days": 30}),
    ("v3_without_long_term", KB, "schema_version", "3"),
    # features 는 소문자 스네이크 이름의 유일한 목록이다(D65)
    ("features_not_a_list", MIN, "features", "cancel"),
    ("features_bad_name", MIN, "features", ["Bad-Name"]),
    ("features_duplicate", MIN, "features", ["cancel", "cancel"]),
    # Skill 키 셋은 features 에 skills 가 있을 때만 받고 에이전트별 목록은 열 개까지다(D64)
    ("skills_without_feature", SK, "features", DELETE),
    ("main_skills_without_feature", MIN, "main.skills", ["report-style"]),
    ("subagent_skills_without_feature", KB, "subagents.0.skills", ["report-style"]),
    ("skills_bad_digest", SK, "skills.0.digest", "sha256:x"),
    ("skills_bad_name", SK, "skills.0.name", "Report_Style"),
    ("skills_pin_unknown_key", SK, "skills.0.content", "x"),
    ("skills_eleven_on_main", SK, "main.skills", [f"s{i}" for i in range(11)]),
    # 웹 Tool 의 키 셋은 features 에 web_guard 가 있을 때만 받고, 그때는 web_data_tools 가 필수이며
    # 소스마다 Tool 이름이 박혀 있다(D64)
    ("web_without_feature", WEB, "features", DELETE),
    ("web_data_tools_without_feature", MIN, "web_data_tools", False),
    ("web_without_data_tools", WEB, "web_data_tools", DELETE),
    ("web_source_unpinned", WEB, "tools.1.allow", []),
    ("web_other_source_unpinned", WEB, "tools.0.allow", []),
    ("web_domain_upper_case", WEB, "tools.1.web.allow_domains", ["Docs.Example.com"]),
    ("web_domain_with_scheme", WEB, "tools.1.web.block_domains", ["https://a.com"]),
    ("web_domain_unicode", WEB, "tools.1.web.allow_domains", ["한국.kr"]),
    ("web_per_user_zero", WEB, "tools.1.web.per_user.fetch_per_day", 0),
    ("web_unknown_key", WEB, "tools.1.web.proxy", "http://p"),
    # Agent 소스의 칸은 MCP 소스에 실리지 않는다
    ("mcp_source_with_description", SUP, "tools.0.description", "x"),
    ("mcp_source_with_timeout_seconds", SUP, "tools.0.timeout_seconds", 30),
    ("mcp_source_without_transport", SUP, "tools.0.transport", DELETE),
    ("mcp_source_without_auth", SUP, "tools.0.auth", DELETE),
]

# 여러 곳을 함께 바꿔야 하는 경우다. (이름, fixture, 변경 목록) 이고 pass 가 통과 기대다.
# schema_version "3" 은 fixture 가 없어 knowledge 문서를 손봐 만든다. 계약 fixture 는 플랫폼
# 테스트가 이름까지 고정한다.
LADDER_CASES: list[tuple[str, str, list[tuple[str, Any]], bool]] = [
    (
        "v3_with_knowledge_and_long_term",
        KB,
        [("schema_version", "3"), ("memory.long_term", {"retention_days": 30})],
        True,
    ),
    (
        "v3_retention_null",
        KB,
        [("schema_version", "3"), ("memory.long_term", {"retention_days": None})],
        True,
    ),
    (
        "v3_without_knowledge_items",
        KB,
        [
            ("schema_version", "3"),
            ("memory.long_term", {"retention_days": 1}),
            ("knowledge", []),
            ("main.knowledge", []),
            ("subagents.0.knowledge", []),
        ],
        True,
    ),
    (
        "v3_long_term_unknown_key",
        KB,
        [("schema_version", "3"), ("memory.long_term", {"retention_days": 30, "extra": 1})],
        False,
    ),
    (
        "v3_long_term_retention_zero",
        KB,
        [("schema_version", "3"), ("memory.long_term", {"retention_days": 0})],
        False,
    ),
    (
        "v3_long_term_without_retention_key",
        KB,
        [("schema_version", "3"), ("memory.long_term", {})],
        False,
    ),
    # 장기 기억은 저장소가 필요하다. memory 가 null 이면 그 키를 둘 자리가 없다
    (
        "v3_without_memory_node",
        MIN,
        [("schema_version", "3"), ("knowledge", []), ("main.knowledge", [])],
        False,
    ),
    # features 는 판과 따로인 선택 키라 어느 판에든 실린다(D65)
    ("v1_with_features", MIN, [("features", ["cancel"])], True),
    ("v1_with_empty_features", MIN, [("features", [])], True),
    ("v2_with_features", KB, [("features", ["cancel", "skills"])], True),
    # 기능 skills 의 키 셋은 판과 따로라 knowledge 문서("2")에도 실린다(D64)
    (
        "skills_with_feature",
        KB,
        [
            ("features", ["skills"]),
            ("skills", [SKILL_PIN]),
            ("main.skills", ["report-style"]),
            ("subagents.0.skills", ["report-style"]),
        ],
        True,
    ),
    # web 블록은 fetch·search 가운데 하나만 주는 소스에도 실린다
    (
        "web_search_only",
        WEB,
        [("tools.1.web.fetch", None), ("tools.1.allow", ["web_search"])],
        True,
    ),
    # Agent 소스는 features 에 agent_tools 가 있을 때만 받는다. Tool 은 이름 하나이고 설명과 제한
    # 시간이 박혀 있으며 MCP 의 칸과 표시 칸은 받지 않는다
    ("agent_source", MIN, AGENT_DOC, True),
    (
        "agent_source_with_approval",
        MIN,
        [*AGENT_DOC, ("tools.0.approval", {"ask_research": ["approve", "reject"]})],
        True,
    ),
    ("agent_source_without_feature", MIN, AGENT_DOC[1:], False),
    ("agent_source_without_description", MIN, [*AGENT_DOC, ("tools.0.description", DELETE)], False),
    (
        "agent_source_without_timeout",
        MIN,
        [*AGENT_DOC, ("tools.0.timeout_seconds", DELETE)],
        False,
    ),
    ("agent_source_two_tools", MIN, [*AGENT_DOC, ("tools.0.allow", ["ask_a", "ask_b"])], False),
    ("agent_source_other_tool_name", MIN, [*AGENT_DOC, ("tools.0.allow", ["research"])], False),
    (
        "agent_source_with_timeouts",
        MIN,
        [*AGENT_DOC, ("tools.0.timeouts", {"ask_research": 9})],
        False,
    ),
    (
        "agent_source_with_transport",
        MIN,
        [*AGENT_DOC, ("tools.0.transport", "streamable_http")],
        False,
    ),
    ("agent_source_with_auth", MIN, [*AGENT_DOC, ("tools.0.auth", {"kind": "none"})], False),
    (
        "agent_source_with_read",
        MIN,
        [
            *AGENT_DOC,
            ("features", ["agent_tools", "tool_safety"]),
            ("tools.0.read", ["ask_research"]),
        ],
        False,
    ),
    (
        "agent_source_with_inside",
        MIN,
        [
            *AGENT_DOC,
            ("features", ["agent_tools", "tool_safety"]),
            ("tools.0.inside", ["ask_research"]),
        ],
        False,
    ),
    # 작업을 맡기는 소스는 Tool 다섯을 함께 싣고 기다리는 시간이 없다. 읽는 두 Tool 만 read 에,
    # 답을 싣지 않는 네 Tool 만 inside 에 적는다
    ("task_source", MIN, TASK_DOC, True),
    (
        "task_source_with_read",
        MIN,
        [
            *TASK_DOC,
            ("features", ["agent_tools", "tool_safety"]),
            ("tools.0.inside", [t for t in TASK_TOOLS if t != "check_async_task"]),
            ("tools.0.read", ["check_async_task", "list_async_tasks"]),
        ],
        True,
    ),
    (
        "task_source_answer_marked_inside",
        MIN,
        [
            *TASK_DOC,
            ("features", ["agent_tools", "tool_safety"]),
            ("tools.0.inside", ["check_async_task"]),
        ],
        False,
    ),
    ("task_source_four_tools", MIN, [*TASK_DOC, ("tools.0.allow", TASK_TOOLS[:4])], False),
    ("task_source_with_timeout", MIN, [*TASK_DOC, ("tools.0.timeout_seconds", 30)], False),
    ("task_source_unknown_mode", MIN, [*TASK_DOC, ("tools.0.mode", "stream")], False),
    (
        "task_source_write_marked_read",
        MIN,
        [
            *TASK_DOC,
            ("features", ["agent_tools", "tool_safety"]),
            ("tools.0.read", ["start_async_task"]),
        ],
        False,
    ),
    ("mcp_source_with_mode", SUP, [("tools.0.mode", "task")], False),
]


def mutate(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    return del_path(doc, path) if value is DELETE else set_path(doc, path, value)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    _ensure_jsonschema()
    from jsonschema import Draft202012Validator

    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    fixtures = {p.name: load_json(p) for p in sorted(FIXTURES_DIR.glob("*.json"))}
    failed = 0

    for name, doc in fixtures.items():
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        if errors:
            failed += 1
            print(f"FAIL fixture {name}")
            for err in errors:
                print(f"  {'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}")
        else:
            print(f"ok   fixture {name}")

    for case, fixture, path, value in BAD_CASES:
        doc = mutate(fixtures[fixture], path, value)
        if validator.is_valid(doc):
            failed += 1
            print(f"FAIL bad case {case} ({fixture}) 가 통과했다")
        else:
            print(f"ok   bad case {case}")

    for case, fixture, changes, expected in LADDER_CASES:
        doc = fixtures[fixture]
        for path, value in changes:
            doc = mutate(doc, path, value)
        if validator.is_valid(doc) != expected:
            failed += 1
            want = "통과" if expected else "거부"
            print(f"FAIL ladder case {case} ({fixture}) 가 {want} 하지 않았다")
            for err in validator.iter_errors(doc):
                print(f"  {'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}")
        else:
            print(f"ok   ladder case {case}")

    if failed:
        print(f"{failed} 건 실패")
        return 1
    print(
        f"{len(fixtures)} fixture, {len(BAD_CASES)} bad case, "
        f"{len(LADDER_CASES)} ladder case 모두 기대대로다"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
