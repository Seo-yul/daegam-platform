"""툴 이름 규칙. mcp-runtime/runtime/profile.py 의 validate_tool_name 과 같아야 한다.

FastMCP 4.0.x OpenAPIProvider 는 mcp_names 로 받은 이름에도 _slugify(구분자 -> '_',
[^A-Za-z0-9_] 제거, '_' 연속 축약, 양끝 '_' 제거)를 적용하고 56자에서 자른다.
정규화를 거쳐도 변하지 않는 이름만 받는다.
"""

from __future__ import annotations

import re

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
FASTMCP_NAME_MAX = 56

# deepagents·langchain 이 자동으로 붙이는 도구 이름(구조화 출력 도구 포함). 검색 도구가 이 이름이면
# ToolNode 가 조용히 덮으므로 agent 그래프 검증이 reserved_tool_name 으로 막는다. 런타임
# tool_names.py 의 BUILTIN_TOOLS 와 같은 집합이다 (tests/test_agent_graph_knowledge.py 가 대조한다)
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "task",
        "write_todos",
        "execute",
        "structured_response",
    }
)


# 모델과 그래프에 상관없이 모든 Agent 에 내장된 이름이다. 나머지 예약 이름은 하위 Agent, 응답
# 형식, 모델에 따라 내장된다
_CONDITIONAL = {"write_todos", "task", "structured_response"}
ALWAYS_BUILT_IN: frozenset[str] = RESERVED_TOOL_NAMES - _CONDITIONAL

# OpenAI SDK 로 부르는 모델 가운데 deepagents 가 Codex harness profile(write_todos)을 거는
# 이름이다. 런타임 tool_names.py 의 CODEX_MODELS 와 같은 집합이다
# (tests/test_builtin_tool_names.py 가 대조한다)
CODEX_MODELS: frozenset[str] = frozenset({"gpt-5.1-codex", "gpt-5.2-codex", "gpt-5.3-codex"})


def builtin_tool_names(*, subagents: bool, structured: bool, todos: bool) -> frozenset[str]:
    """한 Agent 에 내장되는 Tool 이름이다. subagents 는 하위 Agent 를 부르는 메인, structured 는
    응답 형식이 있는 Agent, todos 는 Codex 모델이다. 런타임 `compile.builtin_names` 와 같다."""
    names = set(ALWAYS_BUILT_IN)
    if subagents:
        names.add("task")
    if structured:
        names.add("structured_response")
    if todos:
        names.add("write_todos")
    return frozenset(names)


class InvalidToolName(ValueError):
    pass


def fastmcp_normalize(name: str) -> str:
    slug = re.sub(r"[\s\-\.]+", "_", name)
    slug = re.sub(r"[^a-zA-Z0-9_]", "", slug)
    slug = re.sub(r"_+", "_", slug)
    slug = slug.strip("_")
    return slug[:FASTMCP_NAME_MAX]


def fastmcp_default_name(operation_id: str) -> str:
    """이름 오버라이드가 없는 operationId 에 FastMCP 4.0.3 이 주는 이름이다. `__` 앞을 정규화한다
    (`OpenAPIProvider._generate_default_name`)."""
    return fastmcp_normalize(operation_id.split("__")[0])


def validate_tool_name(name: str) -> str:
    if not TOOL_NAME_RE.fullmatch(name):
        raise InvalidToolName(f"tool name {name!r} must match [A-Za-z0-9_] and be 1..64 chars")
    normalized = fastmcp_normalize(name)
    if normalized != name:
        raise InvalidToolName(
            f"tool name {name!r} would be exposed by FastMCP as {normalized!r} "
            f"(no leading/trailing/double underscores, max {FASTMCP_NAME_MAX} chars)"
        )
    return name
