"""다른 Agent 에게 맡긴 작업의 기계 경로 (P46 6.7절). Agent Protocol 의 일부다.

- `POST /internal/agent-protocol/{agent_id}/threads`: 본문은 비어 있다. 작업을 만든다
- `GET  .../threads/{thread_id}`: 작업의 마지막 답을 `values.messages` 의 메시지 하나로 준다
- `POST .../threads/{thread_id}/runs`: 본문 `{assistant_id, input: {messages: [{role, content}]}}`.
  작업의 다음 실행을 시작한다. 앞 실행이 돌고 있으면 멈춘다
- `GET  .../threads/{thread_id}/runs/{run_id}`: `{run_id, thread_id, status, error?}`
- `POST .../threads/{thread_id}/runs/{run_id}/cancel`: 실행을 멈추고 본문 없이 202 로 답한다

경로가 호출받는 Agent 를 댄다. `threads.create` 의 본문이 비어 있기 때문이다. 부르는 Agent 의
런타임이 호출 경로(`api/agent_calls.py`)와 같은 자격으로 부르고, 작업은 만든 쪽에 묶여 그 밖의
요청에는 404 다. 판단은 `services/agent_task_runs.py` 에 있다. 본문의 그 밖의 칸(`stream_mode`,
`multitask_strategy` 등)과 취소의 쿼리(`wait`, `action`)는 읽지 않는다. 다음 실행은 언제나 앞 실행을
멈추고, 취소는 언제나 기다리지 않는다. 호출 경로처럼 OpenAPI 에 싣지 않고, 감사 미들웨어에서 빼고
스스로 감사 행을 쓰며, 본문 상한도 같다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from daegam.api.agent_calls import AGENT_CALL_BODY_LIMIT, call_credentials
from daegam.services.agent_task_runs import CANCEL_RUN, AgentTaskRuns
from daegam.urls import AGENT_PROTOCOL_PATHS, AGENT_PROTOCOL_ROUTES

router = APIRouter(include_in_schema=False)

# 실행의 메시지가 호출의 메시지와 같은 입력 한도를 따른다
AGENT_PROTOCOL_BODY_LIMIT = (AGENT_PROTOCOL_PATHS, AGENT_CALL_BODY_LIMIT[1])


class RunMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 부르는 Tool 은 실행 하나에 메시지 하나를 싣는다
    messages: list[RunMessage] = Field(min_length=1, max_length=1)


class RunCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assistant_id: str
    input: RunInput


def _tasks(request: Request) -> AgentTaskRuns:
    return request.app.state.agent_tasks


@router.post(AGENT_PROTOCOL_ROUTES["create_thread"])
async def create_thread(request: Request, agent_id: str) -> dict[str, Any]:
    """`{thread_id}`. 거절은 호출의 시작과 같다(401, 403 acts_as_unavailable, 409
    agent_calls_disabled·agent_source_unpublished·agent_call_cycle·agent_call_too_deep, 410
    turn_grant_ended). 호출받는 Agent 의 이미지가 작업을 모르면 409 runtime_feature_missing,
    호출 수를 다 쓴 턴이면 429 agent_call_fanout 이다."""
    return await _tasks(request).create_thread(agent_id, call_credentials(request))


@router.get(AGENT_PROTOCOL_ROUTES["get_thread"])
async def get_thread(request: Request, agent_id: str, thread_id: str) -> dict[str, Any]:
    """`{thread_id, values: {messages}}`. 이 요청에 묶인 작업이 아니면 404, 끝난 실행의 답을 아직
    읽지 못했으면 503 agent_task_answer_unread 다."""
    return await _tasks(request).get_thread(agent_id, thread_id, call_credentials(request))


@router.post(AGENT_PROTOCOL_ROUTES["create_run"])
async def create_run(
    request: Request, agent_id: str, thread_id: str, body: RunCreateBody
) -> dict[str, Any]:
    """`{run_id, thread_id, status}`. 실행의 끝을 기다리지 않는다. `assistant_id` 가 경로의 Agent 가
    아니면 404, 작업이 승인을 기다리면 409 agent_task_waiting, 그 승인이 결정 없이 끝났으면 409
    agent_task_closed 이고 그 밖의 거절은 작업 만들기와 호출의 시작과 같다."""
    return await _tasks(request).create_run(
        agent_id,
        thread_id,
        call_credentials(request),
        assistant_id=body.assistant_id,
        message=body.input.messages[0].content,
    )


@router.get(AGENT_PROTOCOL_ROUTES["get_run"])
async def get_run(request: Request, agent_id: str, thread_id: str, run_id: str) -> dict[str, Any]:
    """실행의 지금 상태다. `status` 는 pending, running, success, cancelled, error 가운데 하나다."""
    return await _tasks(request).get_run(agent_id, thread_id, run_id, call_credentials(request))


@router.post(AGENT_PROTOCOL_ROUTES["cancel_run"], status_code=CANCEL_RUN.ok_status)
async def cancel_run(request: Request, agent_id: str, thread_id: str, run_id: str) -> Response:
    """본문 없는 202 다. 이미 끝난 실행은 그대로 둔다."""
    await _tasks(request).cancel_run(agent_id, thread_id, run_id, call_credentials(request))
    return Response(status_code=CANCEL_RUN.ok_status)
