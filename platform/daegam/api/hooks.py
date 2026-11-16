"""웹훅 트리거의 공개 경로 (P41 4절).

- `POST /hooks/{trigger_id}`: 다른 시스템이 Bearer 없이 Standard Webhooks 서명으로 부른다. 받으면
  202 `{run_id}` 이고 런타임을 기다리지 않는다. 판단은 `services/runs/webhooks.py` 에 있다.

사람도 비서도 부르지 않으므로 OpenAPI 에 싣지 않는다. 경로가 스스로 감사 행을 쓰므로 감사
미들웨어에서 뺀다(`audit/middleware.py` 의 `UNAUDITED_PATHS`). 본문은 트리거를 찾은 뒤에 읽으며
64 KiB 에서 끊는다. 실행을 켜기 전에는(`DAEGAM_RUNS_ENABLED`) 404 다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from daegam.api.agent_runs import require_runs_enabled
from daegam.request_body import body_upto
from daegam.services.runs.webhooks import BODY_CAP
from daegam.urls import HOOK_ROUTE

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_runs_enabled)])


@router.post(HOOK_ROUTE, status_code=status.HTTP_202_ACCEPTED)
async def receive_hook(request: Request, trigger_id: str) -> dict[str, Any]:
    """`{run_id}`. 같은 delivery id 로 다시 온 호출은 같은 실행 id 다."""
    run_id = await request.app.state.webhooks.receive(
        trigger_id, request.headers, lambda: body_upto(request, BODY_CAP)
    )
    return {"run_id": run_id}
