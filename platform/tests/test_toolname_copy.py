"""플랫폼과 mcp-runtime 의 FastMCP 툴 이름 규칙이 어긋나지 않게 막는다.

두 레포가 lock 과 이미지 컨텍스트를 따로 가져 공용 패키지 대신 같은 규칙을 두 벌 둔다. 플랫폼은
MCP Profile 의 툴 이름 오버라이드를 이 규칙으로 받고, mcp-runtime 은 같은 규칙으로 profile.json 을
읽어 FastMCP 에 이름을 넘긴다. 한쪽만 바뀌면 플랫폼이 받아 준 이름을 런타임이 조용히 다른 이름으로
내보내고, 서빙되는 툴 이름이 고정한 프로필과 달라진다. test_oidc_copy.py 와 같은 이유의 드리프트
테스트다. 여기서는 파일 전체가 아니라 공유해야 하는 상수와 함수 본문만 비교한다. 두
`validate_tool_name` 은 올리는 예외가 서로 달라 일부러 다르다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import daegam.services.toolnames as platform_toolnames
from tests.helpers.source import MCP_RUNTIME, find_assignment, find_function, parse_python

PLATFORM_COPY = Path(platform_toolnames.__file__)
RUNTIME_COPY = MCP_RUNTIME / "profile.py"
SHARED = ("TOOL_NAME_RE", "FASTMCP_NAME_MAX")


def _assignment(mod: ast.Module, name: str) -> str:
    return ast.dump(find_assignment(mod, name).value)


def _body_without_docstring(mod: ast.Module, name: str) -> str:
    function = find_function(mod, name)
    body = function.body[1:] if ast.get_docstring(function) is not None else function.body
    return "\n".join(ast.dump(stmt) for stmt in body)


def test_shared_toolname_constants_match() -> None:
    platform_mod, runtime_mod = parse_python(PLATFORM_COPY), parse_python(RUNTIME_COPY)
    for name in SHARED:
        assert _assignment(platform_mod, name) == _assignment(runtime_mod, name), (
            f"{name} 이 {PLATFORM_COPY} 와 {RUNTIME_COPY} 에서 다르다. 두 곳을 같이 고친다"
        )


def test_fastmcp_normalize_body_matches() -> None:
    platform_mod, runtime_mod = parse_python(PLATFORM_COPY), parse_python(RUNTIME_COPY)
    assert _body_without_docstring(platform_mod, "fastmcp_normalize") == _body_without_docstring(
        runtime_mod, "fastmcp_normalize"
    ), (
        f"fastmcp_normalize 가 {PLATFORM_COPY} 와 {RUNTIME_COPY} 에서 다르다. "
        "FastMCP 의 slugify 를 흉내 내는 규칙이라 한쪽만 고치면 안 된다"
    )
