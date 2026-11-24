"""다른 Agent 를 Tool 로 부르는 기계 경로 (P46 6.3절).

- `POST /internal/agent-calls/{agent_id}`: 본문 `{message}`. 호출받는 Agent 의 실행을 시작한다
- `GET /internal/agent-calls/{agent_id}/runs/{run_id}?wait=<초>`: 실행이 끝나거나 wait 초(25초까지)
  가 지나면 답한다
- `POST /internal/agent-calls/{agent_id}/runs/{run_id}/cancel`: 본문 `{reason}`. 실행을 취소한다

답은 `{run_id, status, answer?, code?}` 이고 취소는 `cancelled` 를 더한다. 부르는 Agent 의 런타임이
턴 위임의 토큰을 Bearer 로 붙여 부른다. header 모드에서만 인스턴스 토큰과 세션, 턴, 소유자의 머리를
받는다. 판단은 `services/agent_call_runs.py` 에 있다. 사람도 비서도 부르지 않으므로 OpenAPI 에 싣지
않는다. 경로가 스스로 감사 행을 쓰므로 감사 미들웨어에서 뺀다(`audit/middleware.py` 의
`UNAUDITED_PATHS`). Bearer 가 없는 요청(401)도 서비스가 행을 남기고 거절한다. 본문의 모양(422)과
크기(413)로 서비스에 닿기 전에 거절된 요청은 행이 없다. 본문 상한은 실행의 입력 한도
(`RUNTIME_INPUT_CAP` 글자)를 JSON 으로 실을 크기다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from daegam.api.deps import bearer_value
from daegam.services.agent_call_runs import (
    REQUEST_WAIT_SECONDS,
    AgentCallRuns,
    CallCredentials,
    CancelReason,
)
from daegam.services.runs.service import RUNTIME_INPUT_CAP
from daegam.urls import AGENT_CALL_PATHS, AGENT_CALL_ROUTES

router = APIRouter(include_in_schema=False)

# 한도의 글자가 모두 JSON 의 `\\uXXXX` 여섯 바이트로 와도 받고 그 밖의 칸에 여유를 둔다
AGENT_CALL_BODY_LIMIT = (AGENT_CALL_PATHS, 6 * RUNTIME_INPUT_CAP + (4 << 10))

# 런타임이 싣는 머리다(agent-runtime 의 `runtime/agent_calls.py`). 세션, 턴, 소유자는 위임 없는
# 턴만 싣고 소유자는 UTF-8 의 퍼센트 인코딩이다
OUTSIDE_HEADER = "X-Daegam-Caller-Outside"
SESSION_HEADER = "X-Daegam-Caller-Session"
TURN_HEADER = "X-Daegam-Caller-Turn"
OWNER_HEADER = "X-Daegam-Caller-Owner"
# 바깥 내용이 없다고 알리는 값이다. 그 밖의 값과 없는 머리는 가진 것으로 읽는다
NOT_OUTSIDE = "0"


class CallStartBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1)


class CallCancelBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: CancelReason


def call_credentials(request: Request) -> CallCredentials:
    """요청이 실어 온 Bearer 와 부르는 쪽의 머리다. 작업의 경로(`api/agent_protocol.py`)도 쓴다."""
    headers = request.headers
    return CallCredentials(
        bearer=bearer_value(request),
        session_id=headers.get(SESSION_HEADER),
        turn_id=headers.get(TURN_HEADER),
        owner=headers.get(OWNER_HEADER),
        outside=headers.get(OUTSIDE_HEADER) != NOT_OUTSIDE,
    )


def _calls(request: Request) -> AgentCallRuns:
    return request.app.state.agent_calls


@router.post(AGENT_CALL_ROUTES["start"])
async def start_agent_call(request: Request, agent_id: str, body: CallStartBody) -> dict[str, Any]:
    """실행을 시작하고 그 상태를 곧바로 답한다(끝은 묻기가 기다린다). 거절은 401, 403
    acts_as_unavailable, 409 agent_calls_disabled·agent_source_unpublished·agent_call_cycle·
    agent_call_too_deep·not_deployed, 410 turn_grant_ended, 413 max_input_chars, 429
    agent_call_busy·agent_call_fanout·agent_call_rate_limited 다."""
    return await _calls(request).start(agent_id, call_credentials(request), body.message)


@router.get(AGENT_CALL_ROUTES["poll"])
async def poll_agent_call(
    request: Request,
    agent_id: str,
    run_id: str,
    wait: Annotated[float, Query(ge=0, le=REQUEST_WAIT_SECONDS)] = 0,
) -> dict[str, Any]:
    """실행의 지금 상태다. 이 턴이 시작한 호출이 아니면 404 다."""
    return await _calls(request).poll(agent_id, run_id, call_credentials(request), wait)


@router.post(AGENT_CALL_ROUTES["cancel"])
async def cancel_agent_call(
    request: Request, agent_id: str, run_id: str, body: CallCancelBody
) -> dict[str, Any]:
    """`{run_id, status, cancelled}`. 이미 끝난 실행은 그대로 두고 그 상태를 답한다."""
    return await _calls(request).cancel(agent_id, run_id, call_credentials(request), body.reason)
