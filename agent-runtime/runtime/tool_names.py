"""deepagents·langchain 이 스스로 붙이는 도구의 이름이다.

컴파일(파일 도구 숨기기, 구조화 출력 도구의 이름, 서브에이전트가 있을 때의 task),
번역(서브에이전트의 결과, 구조화 출력 배관), 서브에이전트 턴 제한, 스텁(내장 도구를 스스로 고르지
않는다)이 같은 이름을 읽는다. 에이전트마다 실제로 붙는 내장 이름은 컴파일이 deepagents 스택에서
읽는다(`compile.builtin_names`). 플랫폼의 `RESERVED_TOOL_NAMES`(`services/toolnames.py`)가
`BUILTIN_TOOLS` 와 같은 집합이고 플랫폼 테스트가 이 파일을 실어 맞댄다. 그래서 표준 라이브러리만
쓴다.
"""

from __future__ import annotations

FILESYSTEM_TOOLS = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")
"""deepagents FilesystemMiddleware 가 주입하는 도구 이름이다."""

READ_FILE_TOOL = "read_file"
FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete"})
"""파일을 쓰거나 고치거나 지우는 파일 도구다. 셋 다 경로를 FILE_PATH_ARG 로 받는다."""
FILE_PATH_ARG = "file_path"

SUBAGENT_TOOL = "task"
"""deepagents 가 서브에이전트를 부르는 도구다. 결과는 서브에이전트의 마지막 글이다."""
SUBAGENT_TYPE_ARG = "subagent_type"
"""task 호출에서 부를 서브에이전트의 이름을 담는 인자다."""

EXECUTE_TOOL = "execute"
"""파일시스템 미들웨어가 실행 백엔드가 있을 때만 보이는 실행 도구다(P40, 코드 실행)."""

STRUCTURED_RESPONSE_TOOL = "structured_response"
"""구조화 출력 도구의 이름이다. 컴파일이 응답 형식 스키마의 title 을 늘 이 이름으로 덮어쓰므로
도구 전략에서 langchain 이 만드는 도구가 이 이름이고, 번역은 이 이름의 이벤트를 버린다(D65)."""

TODOS_TOOL = "write_todos"
"""할 일 목록 미들웨어의 도구다. deepagents 0.7.20 은 Codex 모델의 harness profile 에만 붙인다."""

CODEX_MODELS = frozenset({"gpt-5.1-codex", "gpt-5.2-codex", "gpt-5.3-codex"})
"""OpenAI SDK 로 부르는 모델 가운데 deepagents 가 Codex harness profile(write_todos)을 거는
이름이다. deepagents 의 `_CODEX_MODEL_SPECS` 와 같아야 하고 런타임 테스트가 맞댄다."""

ALWAYS_BUILTIN_TOOLS = frozenset({*FILESYSTEM_TOOLS, EXECUTE_TOOL})
"""모델과 그래프에 상관없이 에이전트마다 붙는 내장 도구다."""

BUILTIN_TOOLS = frozenset(
    {*ALWAYS_BUILTIN_TOOLS, SUBAGENT_TOOL, TODOS_TOOL, STRUCTURED_RESPONSE_TOOL}
)
"""deepagents·langchain 이 자동으로 붙이는 도구 전부다. 스텁은 이것을 스스로 고르지 않고, 플랫폼은
이 이름의 검색 도구를 reserved_tool_name 으로 막는다."""


def builtin_tools_for(*, subagents: bool, structured: bool, todos: bool) -> frozenset[str]:
    """한 에이전트에 붙는 내장 도구다. subagents 는 서브에이전트를 부를 수 있는 메인, structured 는
    응답 형식이 있는 에이전트, todos 는 Codex 모델이다. 플랫폼의 `builtin_tool_names`
    (`services/toolnames.py`)와 `compile.builtin_names` 가 이 결과와 같아야 하고 두 쪽의 테스트가
    맞댄다."""
    names = set(ALWAYS_BUILTIN_TOOLS)
    if subagents:
        names.add(SUBAGENT_TOOL)
    if structured:
        names.add(STRUCTURED_RESPONSE_TOOL)
    if todos:
        names.add(TODOS_TOOL)
    return frozenset(names)
