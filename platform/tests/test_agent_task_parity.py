"""P46: 맡긴 작업의 약속을 런타임과 플랫폼이 따로 적은 사본의 파리티 (6.7절).

시작 워커는 작업의 실행을 런타임의 `/chat` 에 `run_id` 와 `task` 로, 다음 실행은 그 세션의
`session_id` 와 함께 보낸다(`services/agent_runtime.py` 의 `AgentRuntimeProxy.chat`). 런타임이 그
칸을 모르면 작업 세션이 첫 턴 뒤에 닫히거나 다음 실행이 422 로 거절된다. 작업 세션이 앞 턴을 아직
돌릴 때 런타임이 답하는 코드도 시작 워커가 이름으로 읽어 다시 한다. 런타임 쪽이 원본이므로 플랫폼의
사본을 그 값에 맞춘다.
"""

from __future__ import annotations

import ast

from daegam.services.runs.start import TURN_IN_PROGRESS
from tests.helpers.source import AGENT_RUNTIME, parse_python, runtime_module

# 플랫폼이 작업의 실행에 싣는 `/chat` 의 칸
TASK_FIELDS = {"session_id", "run_id", "task"}


def _annotated(node: ast.ClassDef) -> set[str]:
    return {
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


def test_the_runtimes_chat_body_takes_the_task_fields() -> None:
    classes = [
        n
        for n in ast.walk(parse_python(AGENT_RUNTIME / "routes.py"))
        if isinstance(n, ast.ClassDef)
    ]
    bodies = [c.name for c in classes if TASK_FIELDS <= _annotated(c)]
    assert bodies, f"런타임의 /chat 본문에 {sorted(TASK_FIELDS)} 칸이 함께 없다"


def test_the_runtime_refuses_a_busy_task_session_with_the_code_the_start_retries() -> None:
    codes = runtime_module(AGENT_RUNTIME / "error_codes.py")
    assert codes.STATUS[TURN_IN_PROGRESS] == 409
