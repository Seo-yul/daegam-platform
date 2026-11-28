"""P46: 맡긴 작업의 약속을 런타임과 플랫폼이 따로 적은 사본의 파리티 (6.7절).

시작 워커는 작업의 실행을 런타임의 `/chat` 에 `run_id` 와 `task` 로, 다음 실행은 그 세션의
`session_id` 와 함께 보낸다(`services/agent_runtime.py` 의 `AgentRuntimeProxy.chat`). 런타임이 그
칸을 모르면 작업 세션이 첫 턴 뒤에 닫히거나 다음 실행이 422 로 거절된다. 작업 세션이 앞 턴을 아직
돌릴 때 런타임이 답하는 코드도 시작 워커가 이름으로 읽어 다시 한다. 부르는 런타임의 작업 Tool
다섯(`agent_task_tools.py`)의 이름과 그 Tool 이 읽는 상태도 플랫폼이 따로 적는다. 런타임 쪽이
원본이므로 플랫폼의 사본을 그 값에 맞춘다.
"""

from __future__ import annotations

import ast
from typing import Any

from daegam.services import agent_task_runs as tasks
from daegam.services.agent_call_runs import CALLER_ENDED, CALLER_TIMEOUT
from daegam.services.agent_calls import TASK_MODE
from daegam.services.runs.approvals import SESSION_CLOSED
from daegam.services.runs.start import TURN_IN_PROGRESS
from daegam.services.tool_effects import TASK_ANSWER_TOOL, TASK_READ_TOOLS, TASK_TOOLS
from tests.helpers.consts import REPO
from tests.helpers.source import (
    AGENT_RUNTIME,
    UI_LIB,
    exec_excerpt,
    parse_python,
    read_source,
    runtime_module,
)
from tests.test_agent_protocol_api import RUN_BODY, UPDATE_BODY

# 부르는 런타임의 작업 Tool 이 적은 이름과 상태다
TOOL_NAMES = ("START", "CHECK", "UPDATE", "CANCEL", "LIST", "TASK_TOOLS")
STATUSES = ("PENDING", "RUNNING", "SUCCESS", "ERROR", "CANCELLED", "FINAL_STATUSES", "WITH_ERROR")
REQUESTS = ("RUN_OPTIONS", "INTERRUPT", "CANCEL_QUERY")
CALLING = exec_excerpt(
    AGENT_RUNTIME / "agent_task_tools.py",
    {"Any": Any},
    constants=(*TOOL_NAMES, *STATUSES, *REQUESTS),
)

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


def test_the_runtime_refuses_a_dead_task_session_with_the_codes_the_routes_explain() -> None:
    """승인 대기로 남았거나 닫힌 작업 세션에 보낸 실행은 런타임이 댄 코드로 끝난다. 경로는 그
    코드에 새 작업을 시작하라는 문장을 붙인다."""
    codes = runtime_module(AGENT_RUNTIME / "error_codes.py")
    for code in (tasks.SESSION_PENDING, SESSION_CLOSED):
        assert code in codes.STATUS and code in tasks.ERROR_SENTENCES


def test_the_task_tools_the_platform_marks_are_the_ones_the_runtime_builds() -> None:
    """플랫폼은 이 이름으로 노드의 방식을 가르고 효과와 표시를 정한다. 런타임이 이름을 바꾸면
    맡기는 노드가 답을 기다리는 노드로 읽힌다."""
    assert CALLING["TASK_TOOLS"] == TASK_TOOLS
    assert {CALLING["CHECK"], CALLING["LIST"]} == TASK_READ_TOOLS
    assert CALLING["CHECK"] == TASK_ANSWER_TOOL
    config = exec_excerpt(AGENT_RUNTIME / "config.py", {}, constants=("AGENT_TASK_MODE",))
    assert config["AGENT_TASK_MODE"] == TASK_MODE
    # 클러스터 게이트가 맡기는 Agent 의 노드에 적는 이름도 같다
    body = exec_excerpt(REPO / "scripts" / "lib" / "agent_body.py", {}, constants=("TASK_TOOLS",))
    assert tuple(body["TASK_TOOLS"]) == TASK_TOOLS


def test_the_runs_tab_has_a_sentence_for_each_reason_a_calling_agent_ends_a_run_with() -> None:
    """화면의 까닭 표(`ui/src/lib/runs.ts` 의 `reasonTable`)가 부른 Agent 가 실행을 끝낸 까닭마다
    문장을 가진다. 이름이 어긋나면 실행 탭이 코드를 그대로 보인다."""
    text = read_source(UI_LIB / "runs.ts")
    reasons = (tasks.CALLER_CANCELLED, tasks.CALLER_UPDATED, CALLER_ENDED, CALLER_TIMEOUT)
    missing = [reason for reason in reasons if f"    {reason}: " not in text]
    assert not missing, f"reasonTable 에 없는 까닭: {missing}"


def test_the_routes_answer_only_statuses_the_task_tools_read() -> None:
    """경로가 답하는 상태는 런타임이 아는 다섯이고, 런타임이 끝난 것으로 읽는 상태에 움직이는
    `pending`·`running` 은 없다. `error` 칸을 싣는 상태도 런타임이 그 칸을 읽는 상태와 같다."""
    answered = {*tasks.PROTOCOL_STATUS.values(), tasks.ERROR}
    names = ("PENDING", "RUNNING", "SUCCESS", "ERROR", "CANCELLED")
    assert answered == {CALLING[name] for name in names}
    assert {tasks.PENDING, tasks.RUNNING}.isdisjoint(CALLING["FINAL_STATUSES"])
    assert tasks.WITH_ERROR == CALLING["WITH_ERROR"]


def test_the_route_tests_pin_the_bodies_the_task_tools_send() -> None:
    """경로의 시험(`tests/test_agent_protocol_api.py`)이 받는 실행의 본문이 런타임이 싣는 칸과
    같다. 메시지와 `assistant_id` 는 호출마다 다르다."""
    fixed = {k: v for k, v in RUN_BODY.items() if k not in ("input", "assistant_id")}
    assert fixed == CALLING["RUN_OPTIONS"]
    assert {k: v for k, v in UPDATE_BODY.items() if k not in RUN_BODY} == CALLING["INTERRUPT"]
    assert CALLING["CANCEL_QUERY"] == {"wait": 0, "action": "interrupt"}
