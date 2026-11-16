"""Agent 미리 보기 라우트 (P44b 6절, D107).

모든 라우트는 원본 Agent 아래에 있고 요청마다 그 테넌트의 `agent:deploy` 를 본다. oidc 모드에서
IdP 가 scope 를 거둬도 훅이 없으므로 이 검사가 막는다. 원본은 `get_agent_or_404` 로 찾으므로 미리
보기의 id 로는 원본 자리에 올 수 없다. 미리 보기는 원본과 호출자의 `sub` 로 찾으므로 시작한 사람만
자기 미리 보기를 쓴다. 다른 사람에게는 404 `preview_not_found` 다.

- `POST /preview` 는 시작하거나 저장한 최신 리비전으로 옮기고 202 다. `GET /preview` 는 그 상태다.
  없으면 404 이고 하루 안에 끝났으면 `ended` 가 까닭이다(`idle`, `max_age`, `ended_by_other`,
  `access_lost`, `source_deleted`, `disabled`). `DELETE /preview` 는 끝낸다.
- 채팅, 업로드, 세션 상세와 보류, 결정, 중지, 다시 생성, 세션 파일, 인용 원문은 Agent 라우트의 같은
  이름이고 몸통도 같다. 채팅, 결정, 다시 생성은 앞에서 원본의 일일 예산을 원장으로 본다(429
  `daily_budget_exceeded`). 세션 목록, 답변 피드백, 기억은 없다. 서랍이 새 세션을 열고 기억을
  남기지 않기 때문이다. 미리 보기에는 실행이 없다.
- `GET /previews` 는 그 Agent 에 열린 미리 보기 모두이고 `DELETE /previews/{preview_id}` 는 그
  가운데 하나를 끝낸다. 미리 보기는 원본으로서 쓰기 Tool 을 부르므로 `agent:deploy` 를 가진 누구나
  보고 끝낼 수 있다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from daegam.api._agent_proxy import get_agent_or_404, is_approver, owned_session
from daegam.api.agent_chat import (
    KNOWLEDGE_READ,
    SESSION_FILE_RESPONSES,
    SOURCE_RESPONSES,
    UPLOAD_RESPONSES,
    Sources,
    citation_source_of,
    regenerate_with,
    remove_upload,
    session_file_of,
    session_files_of,
    upload_to,
)
from daegam.api.agent_evaluation import Runs
from daegam.api.agents import (
    DEPLOY,
    SSE_RESPONSES,
    Agents,
    Choices,
    Service,
    Turns,
    Uploads,
    cancel_with,
    chat_with,
    decide_with,
    pending_of,
)
from daegam.api.deps import Tenant, User
from daegam.api.schemas import ERROR_RESPONSES, ErrorResponse
from daegam.api.schemas.agent_preview import AgentPreviewEndOut, AgentPreviewOut, AgentPreviewsOut
from daegam.api.schemas.agents import (
    AgentCancelBody,
    AgentCancelOut,
    AgentChatBody,
    AgentDecisionsBody,
    AgentPendingOut,
    AgentRegenerateBody,
    AgentSessionFilesOut,
    AgentSessionOut,
)
from daegam.api.schemas.chat_uploads import AgentUploadOut
from daegam.auth.principal import Principal
from daegam.services.agent_preview import AgentPreviews
from daegam.store.agents import Agent, MongoAgentStore

router = APIRouter(
    responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"], dependencies=[DEPLOY]
)

PREVIEW_PATH = "/{agent_id}/preview"
PREVIEW_SESSION_PATH = f"{PREVIEW_PATH}/sessions/{{session_id}}"
PREVIEWS_PATH = "/{agent_id}/previews"
START_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": "테넌트의 미리 보기 자리가 찼다(preview_limit), 플랫폼이 미리 보기를 껐다"
        "(preview_disabled), 앞의 미리 보기가 아직 끝나는 중이다(in_progress)",
    },
    429: {
        "model": ErrorResponse,
        "description": "오늘 시작하고 옮긴 수가 상한이다(preview_start_limit). Retry-After 가 "
        "다음 UTC 날까지다",
    },
}
TURN_RESPONSES: dict[int | str, dict[str, Any]] = {
    **SSE_RESPONSES,
    429: {
        "description": "원본의 오늘 원장 합계가 일일 예산에 닿았거나(daily_budget_exceeded, "
        "Retry-After) 런타임 한도다",
        "content": {"application/json": {"schema": {"type": "object"}}},
    },
}


def get_previews(request: Request) -> AgentPreviews:
    return request.app.state.agent_previews


Previews = Annotated[AgentPreviews, Depends(get_previews)]


async def _mine(
    store: MongoAgentStore, previews: AgentPreviews, tenant: str, user: Principal, agent_id: str
) -> Agent:
    """원본과 호출자로 찾은 미리 보기. 없으면 404 `preview_not_found` 다."""
    source = await get_agent_or_404(store, tenant, agent_id)
    return await previews.require_mine(source, user.sub)


# -- 미리 보기 하나 -----------------------------------------------------------------------------


@router.post(
    PREVIEW_PATH,
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentPreviewOut,
    responses=START_RESPONSES,
)
async def start_agent_preview(
    tenant: Tenant, user: User, store: Agents, previews: Previews, agent_id: str
) -> dict[str, Any]:
    """저장한 최신 리비전으로 내 미리 보기를 시작하거나, 이미 있으면 그 리비전으로 옮긴다. 옮기면
    파드가 굴러가 지금 대화가 사라지고 끝은 그대로다. 파드가 뜨기를 기다리지 않고 202 다.
    `memory` 없이 파드 하나에 상태를 두고 원본처럼 실제 Tool 을 부른다."""
    source = await get_agent_or_404(store, tenant, agent_id)
    return await previews.view(await previews.start(source, user))


@router.get(PREVIEW_PATH, response_model=AgentPreviewOut)
async def get_agent_preview(
    tenant: Tenant, user: User, store: Agents, previews: Previews, agent_id: str
) -> dict[str, Any]:
    """내 미리 보기. 없으면 404 `preview_not_found` 이고, 하루 안에 끝났으면 `ended`(까닭, 때,
    끝낸 사람)를 싣는다."""
    return await previews.view(await _mine(store, previews, tenant, user, agent_id))


@router.delete(PREVIEW_PATH, response_model=AgentPreviewEndOut)
async def end_agent_preview(
    tenant: Tenant, user: User, store: Agents, previews: Previews, agent_id: str
) -> dict[str, Any]:
    """내 미리 보기를 끝낸다. 턴을 멈추고 토큰을 회수하며 파드와 대화를 지운다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return {"id": doc.id, "ended": await previews.end(doc, "ended", by=user.sub)}


# -- 열린 미리 보기 모두 ------------------------------------------------------------------------


@router.get(PREVIEWS_PATH, response_model=AgentPreviewsOut)
async def list_agent_previews(
    tenant: Tenant, store: Agents, previews: Previews, agent_id: str
) -> dict[str, Any]:
    """그 Agent 에 열린 미리 보기 모두. 시작한 사람, 리비전, 시작, 끝, 마지막 활동이고 평가 실행이
    띄운 것은 그 실행(`run`)도 싣는다."""
    source = await get_agent_or_404(store, tenant, agent_id)
    return {"previews": [await previews.view(p) for p in await previews.of_source(source)]}


@router.delete(f"{PREVIEWS_PATH}/{{preview_id}}", response_model=AgentPreviewEndOut)
async def end_any_agent_preview(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    runs: Runs,
    agent_id: str,
    preview_id: str,
) -> dict[str, Any]:
    """그 Agent 의 미리 보기 하나를 끝낸다. 다른 사람의 것이면 그 사람의 서랍이 다른 사람이
    끝냈다고 말한다(`ended_by_other`). 평가 미리 보기면 그 실행을 취소한다. 없으면 404
    `preview_not_found` 다."""
    source = await get_agent_or_404(store, tenant, agent_id)
    found = await previews.require_of_source(source, preview_id)
    assert found.preview is not None
    # 평가 미리 보기를 끝내면 그 실행을 취소한다 (P45 9절)
    await runs.preview_ended(found, user.sub)
    reason = "ended" if found.preview.owner == user.sub else "ended_by_other"
    return {"id": found.id, "ended": await previews.end(found, reason, by=user.sub)}


# -- 채팅과 세션 (Agent 라우트의 같은 이름) ------------------------------------------------------


@router.post(f"{PREVIEW_PATH}/chat", responses=TURN_RESPONSES)
async def chat_with_agent_preview(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    uploads: Uploads,
    turns: Turns,
    choices: Choices,
    agent_id: str,
    body: AgentChatBody,
) -> StreamingResponse:
    """내 미리 보기와의 채팅. `POST /{agent_id}/chat` 과 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    await previews.check_budget(doc)
    return await chat_with(doc, user, service, uploads, turns, choices, body)


@router.post(
    f"{PREVIEW_PATH}/uploads",
    response_model=AgentUploadOut,
    status_code=status.HTTP_201_CREATED,
    responses=UPLOAD_RESPONSES,
)
async def upload_to_agent_preview(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    uploads: Uploads,
    agent_id: str,
    file: Annotated[UploadFile, File()],
) -> AgentUploadOut:
    """내 미리 보기와의 채팅에 붙일 파일 하나. `POST /{agent_id}/uploads` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await upload_to(request, doc, tenant, user, service, uploads, file)


@router.delete(
    f"{PREVIEW_PATH}/uploads/{{upload_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_agent_preview_upload(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    uploads: Uploads,
    agent_id: str,
    upload_id: str,
) -> Response:
    """보내지 않은 업로드를 지운다. `DELETE /{agent_id}/uploads/{upload_id}` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await remove_upload(request, doc, tenant, user, uploads, upload_id)


@router.get(PREVIEW_SESSION_PATH, response_model=AgentSessionOut)
async def get_agent_preview_session(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    agent_id: str,
    session_id: str,
    view: Literal["progress"] | None = None,
) -> dict[str, Any]:
    """미리 보기의 세션 상세. `GET /{agent_id}/sessions/{session_id}` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await owned_session(
        service, doc, session_id, user, approver=is_approver(user, tenant), view=view
    )


@router.get(f"{PREVIEW_SESSION_PATH}/pending", response_model=AgentPendingOut)
async def get_agent_preview_pending(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    choices: Choices,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """미리 보기 세션의 보류 승인 요청. `GET /{agent_id}/sessions/{session_id}/pending` 과 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await pending_of(doc, tenant, user, service, choices, session_id)


@router.post(f"{PREVIEW_SESSION_PATH}/decisions", responses=TURN_RESPONSES)
async def decide_in_agent_preview(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    turns: Turns,
    choices: Choices,
    agent_id: str,
    session_id: str,
    body: AgentDecisionsBody,
) -> StreamingResponse:
    """미리 보기의 보류 세션을 재개한다. `POST /{agent_id}/sessions/{session_id}/decisions` 와
    같고 실행은 없다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    await previews.check_budget(doc)
    return await decide_with(doc, tenant, user, service, turns, choices, None, session_id, body)


@router.post(
    f"{PREVIEW_SESSION_PATH}/cancel",
    response_model=AgentCancelOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_agent_preview_turn(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    agent_id: str,
    session_id: str,
    body: AgentCancelBody | None = None,
) -> dict[str, Any]:
    """미리 보기의 진행 중 턴을 멈춘다. `POST /{agent_id}/sessions/{session_id}/cancel` 과 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await cancel_with(doc, tenant, user, service, session_id, body)


@router.post(f"{PREVIEW_SESSION_PATH}/regenerate", responses=TURN_RESPONSES)
async def regenerate_in_agent_preview(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    turns: Turns,
    choices: Choices,
    agent_id: str,
    session_id: str,
    body: AgentRegenerateBody,
) -> StreamingResponse:
    """미리 보기의 마지막 답변을 다시 생성한다. `POST /{agent_id}/sessions/{session_id}/regenerate`
    와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    await previews.check_budget(doc)
    return await regenerate_with(doc, user, service, turns, choices, session_id, body)


@router.get(f"{PREVIEW_SESSION_PATH}/files", response_model=AgentSessionFilesOut)
async def list_agent_preview_session_files(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """미리 보기 세션의 파일 목록. `GET /{agent_id}/sessions/{session_id}/files` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await session_files_of(doc, tenant, user, service, session_id)


@router.get(
    f"{PREVIEW_SESSION_PATH}/files/{{path:path}}",
    response_class=Response,
    responses=SESSION_FILE_RESPONSES,
)
async def download_agent_preview_session_file(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    agent_id: str,
    session_id: str,
    path: str,
) -> Response:
    """미리 보기 세션의 파일 하나. `GET /{agent_id}/sessions/{session_id}/files/{path}` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await session_file_of(request, doc, tenant, user, service, session_id, path)


@router.get(
    f"{PREVIEW_SESSION_PATH}/citations/{{n}}/source",
    response_class=Response,
    responses=SOURCE_RESPONSES,
    dependencies=[KNOWLEDGE_READ],
)
async def download_agent_preview_citation_source(
    tenant: Tenant,
    user: User,
    store: Agents,
    previews: Previews,
    service: Service,
    sources: Sources,
    agent_id: str,
    session_id: str,
    n: Annotated[int, Path(ge=1)],
) -> Response:
    """미리 보기 세션의 답이 인용한 원문.
    `GET /{agent_id}/sessions/{session_id}/citations/{n}/source` 와 같다."""
    doc = await _mine(store, previews, tenant, user, agent_id)
    return await citation_source_of(doc, tenant, user, service, sources, session_id, n)
