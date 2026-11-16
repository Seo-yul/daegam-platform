"""비서 API (D31, P14 F1b). `/api/assistant/*`.

인가는 `require_user` 다. 별도 scope 를 두지 않는다. 도구가 각자의 scope 로 막히므로 viewer 는
읽기만 되는 비서가 된다. `settings.assistant.enabled` 가 꺼져 있으면 config 외 전부 404
`assistant_disabled` 다. 턴과 결정은 SSE(`text/event-stream`)이고 이벤트 계약은
docs/guide/assistant.md 9절(API)이 정본이다. 서비스 오류는 main.py 의 ServiceError 핸들러가
`{detail: {code, detail}}` 로 낸다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from daegam.api._common import error
from daegam.api.agents import review_out
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse
from daegam.api.schemas.assistant import (
    AssistantConfigOut,
    AssistantDecisionsBody,
    AssistantProposalOut,
    AssistantSessionCreate,
    AssistantSessionCreated,
    AssistantSessionListOut,
    AssistantSessionOut,
    AssistantTurnBody,
)
from daegam.services.agents import AgentService
from daegam.services.assistant.loop import Decision
from daegam.services.assistant.service import AssistantService, TurnHandle
from daegam.store.agents import AgentSpec
from daegam.store.assistant_sessions import AssistantSession
from daegam.store.base import iso_utc

router = APIRouter(prefix="/assistant", tags=["assistant"], responses=ERROR_RESPONSES)

SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "비서 SSE (token·tool_call·tool_result·interrupt·resource·ui·done·error)",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    },
}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_service(request: Request) -> AssistantService:
    return request.app.state.assistant


Service = Annotated[AssistantService, Depends(get_service)]


async def require_enabled(user: User, service: Service) -> None:
    """미인증 요청이 enabled 플래그를 알아내지 못하게 require_user 를 먼저 지난다."""
    if not await service.enabled():
        raise error(404, "assistant_disabled", "the assistant is disabled in platform settings")


Enabled = Depends(require_enabled)


def _session_out(s: AssistantSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "title": s.title,
        "state": s.state,
        "provider": s.provider,
        "pending": s.pending.model_dump() if s.pending is not None else None,
        "transcript": s.transcript,
        "turns": s.turns,
        "usage": s.usage.model_dump(),
        "updated_at": iso_utc(s.updated_at),
    }


def _request_id(request: Request) -> str:
    """턴·결정·다시 읽기가 실행기에 넘기는 요청 id. 도구 호출의 감사 행이 이 값에 `.n` 을 붙인다."""
    return str(getattr(request.state, "request_id", "") or "")


def _stream(service: AssistantService, handle: TurnHandle) -> StreamingResponse:
    return StreamingResponse(
        service.events(handle), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/config", response_model=AssistantConfigOut)
async def get_assistant_config(user: User, service: Service) -> dict[str, Any]:
    """패널을 그릴지 정하는 값. enabled 가 거짓이면 화면에 패널이 없다."""
    return await service.config()


@router.get("/sessions", response_model=AssistantSessionListOut, dependencies=[Enabled])
async def list_assistant_sessions(tenant: Tenant, user: User, service: Service) -> dict[str, Any]:
    """내 세션 목록(이 테넌트, 최근 것부터)."""
    return {"items": await service.list_sessions(tenant, user.sub)}


@router.post(
    "/sessions",
    status_code=201,
    response_model=AssistantSessionCreated,
    dependencies=[Enabled],
)
async def create_assistant_session(
    tenant: Tenant, user: User, service: Service, body: AssistantSessionCreate
) -> dict[str, Any]:
    """새 세션. 제목이 없으면 첫 메시지가 제목이 된다."""
    session = await service.create_session(tenant, user.sub, body.title)
    return {"id": session.id}


@router.get("/sessions/{session_id}", response_model=AssistantSessionOut, dependencies=[Enabled])
async def get_assistant_session(
    tenant: Tenant, user: User, service: Service, session_id: str
) -> dict[str, Any]:
    """복원용. 패널은 transcript 를 그대로 그리고 state 가 running 이면 폴링한다."""
    return _session_out(await service.get_session(session_id, tenant=tenant, sub=user.sub))


@router.get(
    "/sessions/{session_id}/proposal",
    response_model=AssistantProposalOut,
    dependencies=[Enabled, Depends(require_scope("agent:write"))],
)
async def get_assistant_proposal(
    request: Request, tenant: Tenant, user: User, service: Service, session_id: str
) -> dict[str, Any]:
    """기다리는 Agent 정의 제안을 저장해 둔 기준 리비전 위에 다시 짜 맞춘 것 (P48 6.1절). 세션을
    만든 사람만 보고, 기다리는 제안이 없으면 404 proposal_not_found 다. 대상 Agent 를 읽지 못하면
    그 404·403 을 그대로 낸다. 검증은 `POST /agents/validate` 와 같은 권한(agent:write)으로 이
    프로세스에서 하고 감사 행을 남기지 않는다. 화면이 읽는 것이 청지기의 일로 남지 않게 한다."""
    agents: AgentService = request.app.state.agent_service

    async def review(spec: AgentSpec) -> dict[str, Any]:
        return review_out(await agents.review(tenant, spec, principal=user))

    return await service.proposal(
        session_id,
        tenant=tenant,
        principal=user,
        request_id=_request_id(request),
        review=review,
    )


@router.delete("/sessions/{session_id}", response_model=DeleteResponse, dependencies=[Enabled])
async def delete_assistant_session(
    tenant: Tenant, user: User, service: Service, session_id: str
) -> dict[str, Any]:
    """세션을 지운다. 감사 행의 `via` 는 남는다."""
    await service.delete_session(session_id, tenant=tenant, sub=user.sub)
    return {"id": session_id, "deleted": True}


@router.post("/sessions/{session_id}/turns", responses=SSE_RESPONSES, dependencies=[Enabled])
async def post_assistant_turn(
    request: Request,
    tenant: Tenant,
    user: User,
    service: Service,
    session_id: str,
    body: AssistantTurnBody,
) -> StreamingResponse:
    """턴 하나를 시작하고 그 이벤트를 SSE 로 흘린다. 승인 대기 중이면 대기를 superseded 로 닫고
    새 메시지를 붙인다. 질문 대기 중이면 이 메시지가 답이다. 다른 턴이 돌고 있으면 409.
    `provider` 를 주면 그 대화가 그 provider 를 기억한다(D45). 등록되지 않은 이름은 422
    unknown_provider, 키가 없으면 provider_no_key 다."""
    handle = await service.start_turn(
        session_id,
        tenant=tenant,
        principal=user,
        message=body.message,
        ui_context=body.context.model_dump() if body.context is not None else None,
        request_id=_request_id(request),
        provider=body.provider,
    )
    return _stream(service, handle)


@router.post("/sessions/{session_id}/decisions", responses=SSE_RESPONSES, dependencies=[Enabled])
async def post_assistant_decisions(
    request: Request,
    tenant: Tenant,
    user: User,
    service: Service,
    session_id: str,
    body: AssistantDecisionsBody,
) -> StreamingResponse:
    """승인 대기를 결정으로 닫고 턴을 잇는다(같은 SSE). decisions 의 id 는 `pending.calls[].id` 와
    정확히 같아야 하고 개수가 다르면 422 decision_count_mismatch, 대기가 없으면 409 not_pending.
    Agent 정의 제안의 결정에 그 제안의 지문이 없거나 다르면 409 proposal_changed 다."""
    decisions = [
        Decision(
            id=d.id,
            type=d.type,
            message=d.message,
            proposal_digest=d.proposal_digest,
            canvas_saved=bool(d.canvas_saved),
        )
        for d in body.decisions
    ]
    handle = await service.decide(
        session_id,
        tenant=tenant,
        principal=user,
        decisions=decisions,
        request_id=_request_id(request),
    )
    return _stream(service, handle)
