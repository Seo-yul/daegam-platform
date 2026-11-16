"""Agent 답변의 피드백 라우트 (P36 6절, D61).

세션 소유자는 `agent:chat` 으로 끝난 턴마다 평가를 만들고 고치고 거두며, 다시 연 세션의 단추 상태를
자기 평가로 되살린다. 승인자라도 남의 턴은 평가하지 못한다. 그래서 런타임 세션 상세를 승인자 헤더
없이 호출자 sub 로 부르고 그 전사에서 턴을 찾는다. `agent:approve` 는 남의 전사를 읽는 사람과 같은
범위로 모든 평가를 목록으로 보고 NDJSON 으로 내보낸다. 내보내기는 남의 평가를 밖으로 내는 자리라
읽기인데도 감사에 남는다(`audit/middleware.py` 의 `AUDITED_READ_PATHS`).

피드백은 플랫폼 Mongo 에 있어 런타임 능력 이름이 없다. `turn_id` 가 없는 옛 전사의 턴은
404 turn_not_found 이고 화면은 그 턴에 엄지를 세우지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from daegam.api._agent_proxy import get_agent_or_404, owned_session
from daegam.api._download import NDJSON, download_response
from daegam.api.agents import APPROVE, CHAT, Agents, Service
from daegam.api.audit import parse_ts
from daegam.api.deps import Tenant, User
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.agent_feedback import (
    AgentFeedbackBody,
    AgentFeedbackListOut,
    AgentFeedbackOut,
    AgentSessionFeedbackOut,
)
from daegam.services.agent_feedback import PAGE_MAX, AgentFeedbackService, FeedbackInput
from daegam.store.agent_feedback import FeedbackTag, Rating

router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])

TURN_FEEDBACK_PATH = "/{agent_id}/sessions/{session_id}/turns/{turn_id}/feedback"
EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "평가 한 건이 한 줄인 NDJSON 내려받기. 사람 식별자는 싣지 않는다",
        "content": {NDJSON: {"schema": {"type": "string"}}},
    },
}


def get_feedback(request: Request) -> AgentFeedbackService:
    return request.app.state.agent_feedback_service


Feedback = Annotated[AgentFeedbackService, Depends(get_feedback)]


@router.put(TURN_FEEDBACK_PATH, response_model=AgentFeedbackOut, dependencies=[CHAT])
async def put_turn_feedback(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    feedback: Feedback,
    agent_id: str,
    session_id: str,
    turn_id: str,
    body: AgentFeedbackBody,
) -> dict[str, Any]:
    """끝난 턴의 평가를 만들거나 고친다. 세션 소유자만 되고, 런타임 전사에 그 `turn_id` 로 끝난
    턴이 없으면 404 turn_not_found 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    session = await owned_session(service, doc, session_id, user, approver=False)
    value = FeedbackInput(
        rating=body.rating,
        tags=list(body.tags),
        comment=body.comment,
        ephemeral=body.ephemeral,
        evaluation_use=bool(body.evaluation_use),
    )
    fb = await feedback.rate(
        doc, session, session_id=session_id, turn_id=turn_id, user_sub=user.sub, value=value
    )
    return fb.model_dump()


@router.delete(TURN_FEEDBACK_PATH, status_code=204, response_class=Response, dependencies=[CHAT])
async def delete_turn_feedback(
    tenant: Tenant,
    user: User,
    store: Agents,
    feedback: Feedback,
    agent_id: str,
    session_id: str,
    turn_id: str,
) -> Response:
    """자기 평가를 거둔다. 거둘 것이 없으면 404 feedback_not_found 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    await feedback.withdraw(doc, session_id=session_id, turn_id=turn_id, user_sub=user.sub)
    return Response(status_code=204)


@router.get(
    "/{agent_id}/sessions/{session_id}/feedback",
    response_model=AgentSessionFeedbackOut,
    dependencies=[CHAT],
)
async def list_session_feedback(
    tenant: Tenant, user: User, store: Agents, feedback: Feedback, agent_id: str, session_id: str
) -> dict[str, Any]:
    """그 세션의 내 평가와 설치의 평가 보존 기간이다. 다시 연 화면이 엄지 상태를 되살리고, 의견
    창의 동의 안내가 보존 기간을 적는다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    rows = await feedback.mine(doc, session_id, user_sub=user.sub)
    return {"items": [fb.model_dump() for fb in rows], "retention_days": feedback.retention.days}


@router.get("/{agent_id}/feedback", response_model=AgentFeedbackListOut, dependencies=[APPROVE])
async def list_agent_feedback(
    tenant: Tenant,
    store: Agents,
    feedback: Feedback,
    agent_id: str,
    rating: Rating | None = None,
    tag: FeedbackTag | None = None,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 100,
) -> dict[str, Any]:
    """이 Agent 의 평가를 새것부터 본다. 다음 쪽은 응답의 `next_before` 를 `before` 로 준다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    rows, next_before = await feedback.page(doc, rating=rating, tag=tag, before=before, limit=limit)
    return {
        "items": [fb.model_dump() for fb in rows],
        "next_before": next_before,
        "last_7_days": await feedback.recent_counts(doc),
    }


@router.get("/{agent_id}/feedback/export", responses=EXPORT_RESPONSES, dependencies=[APPROVE])
async def export_agent_feedback(
    tenant: Tenant,
    store: Agents,
    feedback: Feedback,
    agent_id: str,
    since: str | None = None,
    until: str | None = None,
    rating: Rating | None = None,
) -> Response:
    """평가를 NDJSON 으로 내려받는다. 구간은 기본이 지금까지 90일이고 최대 90일, 1만 줄이다.
    파일 이름은 Agent 이름과 구간이다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    start, end = parse_ts(since, "since"), parse_ts(until, "until")
    lines = await feedback.export(doc, since=start, until=end, rating=rating)
    return download_response(lines, _export_name(doc.name, start, end), media_type=NDJSON)


def _export_name(agent_name: str, since: datetime | None, until: datetime | None) -> str:
    span = "-".join(t.strftime("%Y%m%d") for t in (since, until) if t is not None)
    return f"feedback-{agent_name}{f'-{span}' if span else ''}.ndjson"
