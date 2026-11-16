"""플랫폼과 mcp-runtime 이 인자 조건을 같은 뜻으로 읽게 막는다 (P52 4절).

플랫폼은 저장 때 조건의 경로를 FastMCP 입력 스키마에서 풀고(`pipeline/tool_arguments.py`) 연산자마다
받는 형식을 본다(`services/tool_rules.py` `_OP_TYPES`). mcp-runtime 은 같은 규칙으로 서버를 지을 때
조건을 풀고 호출마다 지킨다(`mcp-runtime/runtime/conditions.py`). 두 디렉터리가 lock 과 이미지
컨텍스트를 따로 가져 같은 규칙을 두 벌 둔다. 한쪽만 바뀌면 플랫폼이 받아 준 조건이 런타임에서
닫히거나, 런타임이 거절할 값을 플랫폼이 받아 준다. 여기서는 공유해야 하는 상수와 함수 본문, 연산자
목록만 비교한다(`test_toolname_copy.py` 와 같은 방식). 값의 형식(`_fits` 와 `_value_fits`)은
본문을, 조건이 풀리는지(`_condition_fits` 와 `check_condition`)와 경로의 이름들(`argument_segments`
와 `arguments_of`)은 같은 입력에 대한 판정을 맞댄다. 두 쪽이 같은 FastMCP 판으로 스키마를 짓는지도
본다.
"""

from __future__ import annotations

import ast
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import daegam.pipeline.tool_arguments as platform_arguments
import daegam.services.tool_rules as platform_rules
import daegam.store.tool_rules as platform_model
from daegam.services.tool_rules import ToolRuleError, check_condition
from daegam.store.tool_rules import ArgCondition
from tests.helpers.consts import REPO
from tests.helpers.source import (
    MCP_RUNTIME,
    exec_excerpt,
    find_assignment,
    find_function,
    parse_python,
    read_source,
)

PLATFORM_ARGUMENTS = Path(platform_arguments.__file__)
PLATFORM_RULES = Path(platform_rules.__file__)
PLATFORM_MODEL = Path(platform_model.__file__)
RUNTIME_CONDITIONS = MCP_RUNTIME / "conditions.py"
RUNTIME_PROFILE = MCP_RUNTIME / "profile.py"
SHARED_CONSTANTS = ("MAX_DEPTH", "MAX_PATHS", "SCALARS", "UNKNOWN")
SHARED_FUNCTIONS = ("_deref", "_branches", "type_of", "arguments_of")
FIX = "두 곳을 같이 고친다. 플랫폼이 저장 때 받은 조건을 런타임이 같은 뜻으로 풀어야 한다"


def _value(mod: ast.Module, name: str) -> str:
    return ast.dump(find_assignment(mod, name).value)


def _body(mod: ast.Module, name: str) -> str:
    function = find_function(mod, name)
    body = function.body[1:] if ast.get_docstring(function) is not None else function.body
    return "\n".join(ast.dump(stmt) for stmt in body)


def test_schema_reading_constants_match() -> None:
    platform, runtime = parse_python(PLATFORM_ARGUMENTS), parse_python(RUNTIME_CONDITIONS)
    for name in SHARED_CONSTANTS:
        assert _value(platform, name) == _value(runtime, name), f"{name}: {FIX}"


def test_schema_reading_functions_match() -> None:
    platform, runtime = parse_python(PLATFORM_ARGUMENTS), parse_python(RUNTIME_CONDITIONS)
    for name in SHARED_FUNCTIONS:
        assert _body(platform, name) == _body(runtime, name), f"{name}: {FIX}"


def test_operator_types_match() -> None:
    platform, runtime = parse_python(PLATFORM_RULES), parse_python(RUNTIME_CONDITIONS)
    assert _value(platform, "_OP_TYPES") == _value(runtime, "OP_TYPES"), f"_OP_TYPES: {FIX}"


def test_operator_names_match() -> None:
    platform, runtime = parse_python(PLATFORM_MODEL), parse_python(RUNTIME_PROFILE)
    assert _value(platform, "ConditionOp") == _value(runtime, "ConditionOp"), f"ConditionOp: {FIX}"


def test_value_types_match() -> None:
    platform, runtime = parse_python(PLATFORM_RULES), parse_python(RUNTIME_CONDITIONS)
    assert _body(platform, "_value_fits") == _body(runtime, "_fits"), f"_fits: {FIX}"


def _runtime() -> dict[str, Any]:
    return exec_excerpt(
        RUNTIME_CONDITIONS,
        {"math": math, "Any": Any, "ArgCondition": object},
        functions=(*SHARED_FUNCTIONS, "argument_segments", "_fits", "_condition_fits"),
        constants=(*SHARED_CONSTANTS, "OP_TYPES"),
    )


KINDS = ("string", "integer", "number", "boolean", "object", "array", "unknown")
VALUES: tuple[Any, ...] = (
    "a",
    "",
    0,
    3,
    -2,
    2.5,
    True,
    None,
    math.nan,
    math.inf,
    [],
    ["a", "b"],
    [1, 2],
    [1, "a"],
    [math.nan],
    {"a": 1},
)


def _platform_accepts(op: str, kind: str, value: Any) -> bool:
    cond = ArgCondition(path="x", op=op, value=value)
    try:
        check_condition(cond, {"x": kind})
    except ToolRuleError:
        return False
    return True


@pytest.mark.parametrize("op", sorted(platform_rules._OP_TYPES))
def test_conditions_resolve_alike(op: str) -> None:
    """저장 때 플랫폼이 받는 조건과 서버를 지을 때 런타임이 푸는 조건이 같다."""
    runtime = _runtime()
    for kind in KINDS:
        for value in VALUES:
            cond = SimpleNamespace(path="x", op=op, value=value, optional=False)
            resolved = kind in runtime["OP_TYPES"][op] and runtime["_condition_fits"](cond, kind)
            assert resolved == _platform_accepts(op, kind, value), (op, kind, value)


SCHEMAS: tuple[dict[str, Any], ...] = (
    {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}},
    {
        "type": "object",
        "properties": {
            "customer": {
                "type": "object",
                "properties": {"tier": {"type": "string"}, "addr": {"$ref": "#/$defs/Addr"}},
            },
            "items": {"type": "array", "items": {"type": "object", "properties": {"x": {}}}},
            "maybe": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        },
        "$defs": {"Addr": {"type": "object", "properties": {"zip": {"type": "string"}}}},
    },
    {"type": "object", "properties": {"a.b": {"type": "string"}, "a": {"properties": {"b": {}}}}},
    {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(320)}},
)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_argument_segments_name_the_paths_arguments_of_reads(schema: dict[str, Any]) -> None:
    segments = _runtime()["argument_segments"](schema)
    assert set(segments) == set(platform_arguments.arguments_of(schema))
    for path, found in segments.items():
        assert all(".".join(names) == path for names in found), path


def test_both_sides_pin_the_same_fastmcp() -> None:
    """플랫폼은 저장 때 FastMCP 의 파서로 인자를 읽고 런타임은 같은 FastMCP 로 Tool 을 짓는다.
    런타임은 그 판의 OpenAPI Tool 이 백엔드 요청을 짓는 길(`_client`, 요청 훅)에도 기댄다."""
    pins = {
        name: re.findall(r'"fastmcp==([^"]+)"', read_source(REPO / name / "pyproject.toml"))
        for name in ("platform", "mcp-runtime")
    }
    assert pins == {"platform": ["4.1.2"], "mcp-runtime": ["4.1.2"]}, pins
