"""P46: Agent 호출의 약속을 런타임과 플랫폼이 따로 적은 사본의 파리티 (6.3절).

부르는 파드의 `agent-runtime/runtime/agent_calls.py` 가 호출 경로에 싣는 머리 넷, 취소의 까닭,
묻기 하나가 기다려 달라고 하는 시간, 끝난 상태를 플랫폼 `api/agent_calls.py` 와
`services/agent_call_runs.py`, `store/agent_runs.py` 가 따로 적는다. 어긋나면 플랫폼이 세션과 턴을
읽지 못해 header 모드의 호출을 401 로 거절하거나, 취소를 422 로 거절하거나, 런타임이 끝난 실행을
끝없이 다시 묻는다. 런타임 쪽이 원본이므로 플랫폼의 사본을 그 값에 맞춘다.
"""

from __future__ import annotations

from typing import get_args

from daegam.api import agent_calls as routes
from daegam.services.agent_call_runs import REQUEST_WAIT_SECONDS, CancelReason
from daegam.store.agent_runs import MOVING_STATUSES, RunStatus
from tests.helpers.source import AGENT_RUNTIME, exec_excerpt

RUNTIME_COPY = AGENT_RUNTIME / "agent_calls.py"
HEADERS = ("OUTSIDE_HEADER", "SESSION_HEADER", "TURN_HEADER", "OWNER_HEADER")
STATUSES = ("SUCCEEDED", "WAITING_APPROVAL", "SETTLED")
CANCELS = ("CANCEL_TIMEOUT", "CANCEL_ENDED")
WAITS = ("POLL_WAIT_SECONDS", "REQUEST_TIMEOUT_SECONDS")
RUNTIME = exec_excerpt(RUNTIME_COPY, {}, constants=(*HEADERS, *STATUSES, *CANCELS, *WAITS))


def test_the_caller_headers_match_the_runtime() -> None:
    for name in HEADERS:
        assert getattr(routes, name) == RUNTIME[name], name


def test_the_cancel_reasons_match_the_runtime() -> None:
    assert set(get_args(CancelReason)) == {RUNTIME[name] for name in CANCELS}


def test_a_poll_asks_for_no_longer_than_a_request_waits() -> None:
    """런타임이 더 길게 청하면 경로가 422 로 거절하고, 플랫폼의 답은 런타임의 요청 상한 안이다."""
    assert RUNTIME["POLL_WAIT_SECONDS"] <= REQUEST_WAIT_SECONDS < RUNTIME["REQUEST_TIMEOUT_SECONDS"]


def test_the_runtime_stops_asking_only_at_statuses_that_do_not_move() -> None:
    """런타임이 끝난 것으로 읽는 상태는 모두 실행의 상태이고, 움직이는 상태(`MOVING_STATUSES`)는
    그 안에 없어 다시 묻는다."""
    assert RUNTIME["SETTLED"] <= set(get_args(RunStatus))
    assert not RUNTIME["SETTLED"] & set(MOVING_STATUSES)
