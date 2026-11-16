"""P37: 런타임 이벤트 보내기와 플랫폼 수집의 사본 파리티 (D62).

런타임 `shipper.py`·`events.py`·`stream.py`·`budget.py`·`guards.py`·`trace.py` 가 원본이고 플랫폼
수집(`services/telemetry/ingest.py`), 이벤트 규칙(`normalize.py`), 지표(`agent_metrics.py`), 원장
(`store/usage.py`), Agent 렌더(`k8s/templates/agent.py`)가 같은 값을 둔다. 어긋나면 런타임이 보낸
묶음이나 이벤트를 플랫폼이 거절하거나 다르게 세는데 아무도 모른다. 런타임 파일은 import 하지 않고
글자와 구문 트리로 읽는다.
"""

from __future__ import annotations

import ast
from typing import get_args

from daegam.k8s.templates import agent as agent_template
from daegam.services.telemetry import agent_metrics, ingest, normalize
from daegam.store.usage import Operation, Route
from tests.helpers.source import (
    AGENT_RUNTIME,
    find_assignment,
    parse_python,
    py_constant,
    read_source,
)

SHIPPER = AGENT_RUNTIME / "shipper.py"
EVENTS = AGENT_RUNTIME / "events.py"
STREAM = AGENT_RUNTIME / "stream.py"
TURN = AGENT_RUNTIME / "turn.py"
BUDGET = AGENT_RUNTIME / "budget.py"
GUARDS = AGENT_RUNTIME / "guards.py"
TRACE = AGENT_RUNTIME / "trace.py"


def _strings(path, name: str) -> set[str]:
    """`NAME = frozenset({...})` 처럼 식 안의 글자 상수들이다."""
    node = find_assignment(parse_python(path), name)
    return {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant)}


def _envelope_keys() -> tuple[str, ...]:
    """런타임 `EventShipper._envelope` 가 돌려주는 사전의 칸이다."""
    for node in ast.walk(parse_python(SHIPPER)):
        if isinstance(node, ast.FunctionDef) and node.name == "_envelope":
            (ret,) = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            assert isinstance(ret.value, ast.Dict)
            return tuple(k.value for k in ret.value.keys if isinstance(k, ast.Constant))
    raise AssertionError("_envelope 를 찾지 못했다")


def test_the_envelope_and_its_limits_match_the_runtime() -> None:
    assert py_constant(SHIPPER, "SCHEMA_VERSION") == ingest.SCHEMA_VERSION
    assert py_constant(SHIPPER, "BATCH_EVENTS") <= ingest.MAX_EVENTS
    assert py_constant(EVENTS, "MAX_EVENT_BYTES") == ingest.MAX_EVENT_BYTES
    assert py_constant(EVENTS, "MAX_BODY_CHARS") == ingest.MAX_BODY_CHARS
    assert _strings(SHIPPER, "COST_EVENTS") == normalize.COST_EVENTS
    assert _envelope_keys() == ingest.ENVELOPE_KEYS


def test_the_render_sets_the_env_the_shipper_reads() -> None:
    source = read_source(SHIPPER)
    for name in (
        agent_template.EVENTS_URL_ENV,
        agent_template.POD_NAME_ENV,
        agent_template.POD_UID_ENV,
    ):
        assert f'env.get("{name}")' in source, name


def test_rejections_read_the_runtime_vocabulary() -> None:
    assert py_constant(BUDGET, "ACTION_REJECTED") == normalize.BUDGET_REJECTED
    assert py_constant(BUDGET, "SCOPE_DAILY") == normalize.BUDGET_DAILY
    assert py_constant(GUARDS, "POSITION_INPUT") == normalize.GUARDRAIL_INPUT
    assert py_constant(GUARDS, "ACTION_BLOCK") == normalize.GUARDRAIL_BLOCK


def test_turn_results_are_the_runtime_turn_statuses() -> None:
    """turn_end 의 status 는 끝난 결과(done, pending), 멈춤(cancelled), 턴 오류 코드다."""
    runtime = {
        py_constant(STREAM, name)
        for name in ("DONE", "BUDGET_EXCEEDED", "TURN_TIMEOUT", "TURN_FAILED", "QUOTA_EXHAUSTED")
    }
    runtime |= {"pending", py_constant(TURN, "TURN_END_CANCELLED")}
    assert runtime == set(agent_metrics.TURN_RESULTS)
    assert normalize.RUN_OK_STATUSES == {"done", "pending", py_constant(TURN, "TURN_END_CANCELLED")}


def test_ledger_values_match_what_the_runtime_writes() -> None:
    routes = {py_constant(TRACE, "ROUTE_DIRECT"), py_constant(TRACE, "ROUTE_GATEWAY")}
    assert routes == set(get_args(Route))
    operations = {py_constant(TRACE, "OPERATION_CHAT"), py_constant(TRACE, "OPERATION_EMBEDDINGS")}
    assert operations <= set(get_args(Operation))
    calls = {py_constant(TRACE, "STATUS_OK"), py_constant(TRACE, "STATUS_ERROR")}
    assert py_constant(TRACE, "STATUS_OK") in normalize.CALL_OK_STATUSES
    assert not normalize.call_failed(py_constant(TRACE, "STATUS_OK"))
    assert all(normalize.call_failed(s) for s in calls - normalize.CALL_OK_STATUSES)
