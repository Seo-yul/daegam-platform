"""턴 위임의 기계 경로 (P53 4절).

- `POST /agents/{agent_id}/turn-grants/{grant_id}/renew`: 런타임이 토큰 수명의 3분의 1이 남으면
  부른다. 답은 `{tokens, expires_in}` 이다
- `POST /agents/{agent_id}/turn-grants/{grant_id}/end`: 런타임이 턴을 마쳤거나 파드가 내려갈 때
  부른다

둘 다 Agent 의 인스턴스 토큰을 Bearer 로, 위임의 비밀을 본문으로 받는다. 판단은
`services/turn_grants.py` 에 있다. 사람도 비서도 부르지 않으므로 OpenAPI 에 싣지 않고, 몇 분마다
오는 기계의 쓰기라 감사에서도 뺀다(`audit/middleware.py` 의 `UNAUDITED_PATHS`). 본문 상한은 4 KiB
다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from daegam.api.deps import bearer_token
from daegam.store.turn_grants import RuntimeEndReason
from daegam.urls import TURN_GRANT_PATHS, TURN_GRANT_ROUTES

router = APIRouter(include_in_schema=False)

TURN_GRANT_BODY_LIMIT = (TURN_GRANT_PATHS, 4 << 10)


class GrantSecretBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    secret: str = Field(min_length=1, max_length=256)


class GrantEndBody(GrantSecretBody):
    reason: RuntimeEndReason


@router.post(TURN_GRANT_ROUTES["renew"])
async def renew_turn_grant(
    request: Request, agent_id: str, grant_id: str, body: GrantSecretBody
) -> dict[str, Any]:
    """같은 대상의 새 토큰들과 새 수명이다. 거절은 401(인스턴스 토큰이나 비밀), 404
    turn_grant_not_found, 410 turn_grant_ended, 403 turn_grant_refused, 429 turn_grant_rate_limited
    다."""
    return await request.app.state.turn_grants.renew(
        agent_id, grant_id, secret=body.secret, presented=bearer_token(request)
    )


@router.post(TURN_GRANT_ROUTES["end"])
async def end_turn_grant(
    request: Request, agent_id: str, grant_id: str, body: GrantEndBody
) -> dict[str, bool]:
    """`{ended}`. 이미 끝난 위임이면 거짓이고 처음 적힌 끝이 남는다."""
    return await request.app.state.turn_grants.end_by_runtime(
        agent_id, grant_id, secret=body.secret, presented=bearer_token(request), reason=body.reason
    )
