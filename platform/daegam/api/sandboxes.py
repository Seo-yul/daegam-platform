"""코드 실행 샌드박스의 기계 경로 (P40 5절, D95).

- `POST /agents/{agent_id}/sandboxes {session_id, wait_seconds?}`: 세션의 샌드박스를 연다. 열린 것이
  있으면 그것이다. 답은 `{sandbox_id, generation, ready, address, token, unchecked, restarts,
  last_termination}` 이다
- `GET /agents/{agent_id}/sandboxes?session_id=`: 열린 샌드박스를 찾는다. 만들지 않는다
- `POST /agents/{agent_id}/sandboxes/touch {session_id}`: 쓰기를 알린다
- `POST /agents/{agent_id}/sandboxes/close {session_id, reason}`: 닫는다

모두 Agent 의 인스턴스 토큰을 Bearer 로 받는다. 판단은 `services/sandboxes.py` 에 있다. 사람도
비서도 부르지 않으므로 OpenAPI 에 싣지 않고, 기계의 쓰기라 감사에서도 뺀다
(`audit/middleware.py` 의 `UNAUDITED_PATHS`). 본문 상한은 4 KiB 다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from daegam.api.deps import bearer_token
from daegam.services.sandboxes import RuntimeCloseReason
from daegam.urls import SANDBOX_PATHS, SANDBOX_ROUTES

router = APIRouter(include_in_schema=False)

SANDBOX_BODY_LIMIT = (SANDBOX_PATHS, 4 << 10)
SESSION_ID_MAX = 256


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=SESSION_ID_MAX)


class OpenBody(SessionBody):
    # 부르는 쪽이 기다릴 수 있는 초(턴의 남은 시간). 없으면 플랫폼의 시한(150초)이다
    wait_seconds: float | None = Field(default=None, ge=0, le=600)


class CloseBody(SessionBody):
    reason: RuntimeCloseReason


@router.post(SANDBOX_ROUTES["open"])
async def open_sandbox(request: Request, agent_id: str, body: OpenBody) -> dict[str, Any]:
    """거절은 401(인스턴스 토큰), 429 sandbox_rate_limited·sandbox_limit(`cap`), 409 not_deployed·
    sandbox_not_enabled·sandbox_gate_closed(`reason`), 503 sandbox_namespace_policy(`policy`)·
    sandbox_open_failed(`reason`)·sandbox_starting 이다."""
    return await request.app.state.sandboxes.open(
        agent_id, bearer_token(request), body.session_id, wait=body.wait_seconds
    )


@router.get(SANDBOX_ROUTES["open"])
async def lookup_sandbox(
    request: Request,
    agent_id: str,
    session_id: str = Query(min_length=1, max_length=SESSION_ID_MAX),
) -> dict[str, Any]:
    """없으면 404 sandbox_not_found 와 그 세션의 마지막 기록이 닫힌 까닭(`closed_reason`)이다."""
    return await request.app.state.sandboxes.lookup(agent_id, bearer_token(request), session_id)


@router.post(SANDBOX_ROUTES["touch"])
async def touch_sandbox(request: Request, agent_id: str, body: SessionBody) -> dict[str, Any]:
    """열린 기록이 없으면 404 sandbox_not_found 다. 런타임은 그때 회수·잃음을 바로 안다."""
    return await request.app.state.sandboxes.touch(agent_id, bearer_token(request), body.session_id)


@router.post(SANDBOX_ROUTES["close"])
async def close_sandbox(request: Request, agent_id: str, body: CloseBody) -> dict[str, bool]:
    """`{closed}`. 열린 것이 없었으면 거짓이다."""
    return await request.app.state.sandboxes.close_session(
        agent_id, bearer_token(request), body.session_id, body.reason
    )
