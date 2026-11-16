"""Agent 채팅의 세션 관리(이름 바꾸기·지우기), 다시 생성, 인용 원문, 내 장기 기억, 첨부 업로드,
세션 파일 라우트다(P36 4절·5절·7절·8절·9절·10절, D61).

채팅이 쓰는 런타임 프록시 가운데 세션 관리와 다시 생성을 `api/agents.py` 밖에 둔다. 모두
`agent:chat` 이다. 주체는 언제나 호출자의 sub 이고 승인자 헤더를 붙이지 않는다. 남의 대화를
지우거나 이름을 바꾸거나 다시 생성할 까닭이 없어서 `agent:approve` 가 있어도 남의 세션은 런타임의
403 not_session_owner 다.

세션 관리 두 라우트는 런타임 능력 `session_admin`, 다시 생성은 `regenerate` 에 기댄다. 이 에이전트의
기록에 그 이름이 없으면 런타임을 부르지 않고 409 runtime_feature_missing 이고, 롤링 중에 그 라우트를
모르는 옛 파드가 받으면 409 runtime_rolling 이다(`api/_agent_proxy.py`). 쓰기라 감사 미들웨어가
그대로 남긴다.

인용 원문은 읽기다. 주체는 세션 상세와 같이 소유자나 `agent:approve` 이고 `knowledge:read` 도
요구한다. 런타임 능력 `citations` 에 기대고, 판정은 `services/agent_citations.py` 가 한다. 테넌트
문서를 밖으로 내는 자리라 읽기인데도 감사에 남는다(`audit/middleware.py` 의 `AUDITED_READ_PATHS`).

세션 파일(`files`)은 그 세션에 업로드한 첨부와 에이전트가 파일 도구로 쓴 파일의 목록과 내려받기다.
주체는 세션 상세와 같이 소유자나 `agent:approve` 이고, 런타임이 쿼리의 sub 를 세션 소유자와 직접
대조한다(목록 응답에는 소유자를 확인할 세션 한 줄이 없다). 런타임 능력 `session_files` 에 기댄다.
런타임 URL 은 경로 조각마다 다시 인코딩하고, 내려받기 헤더는 런타임의 것을 버리고 `_download` 가
다시 만든다. 자기 세션 파일 읽기는 세션 상세처럼 감사에 남기지 않는다. 승인자로 부른 다운로드는 남이
업로드한 파일을 밖으로 낼 수 있어 성공해도 감사에 남긴다(`audit_read`).

내 장기 기억(`memories/me`)은 목록·읽기·하나 지우기이고 런타임 능력 `memory_files` 에 기댄다. 주체는
늘 호출자 sub 이고 런타임이 그 sub 의 네임스페이스만 읽으므로 tenant admin 도 남의 기억을 읽는 길이
없다. 파일 경로를 쿼리가 아니라 경로에 두는 것은 D34 와 같이 무엇을 지웠는지 감사에 남기려는
것이다. 런타임 URL 은 조각마다 다시 인코딩한다. 모두 지우기는 런타임 없이도 도는 D34 의
`DELETE /agents/{id}/memories/{user_sub}` 를 그대로 쓴다. 자기 기억 읽기는 세션 상세처럼 감사에
남기지 않는다.

첨부 업로드(`uploads`)는 보낼 파일을 미리 업로드해 두는 두 단계의 앞 단계이고 `agent:chat` 이다.
규칙은 `services/chat_uploads.py` 에 있다. 배포된 Agent 이고 기록에 런타임 능력 `attachments` 가
있어야 받는다. 옛 런타임은 첨부를 조용히 버리기 때문이다. 업로드는 업로드한 사람의 것이라 지우기도
그 사람의 것만 지우고, 없는 id 와 남의 id 를 가르지 않고 204 다. 쓰기라 감사 미들웨어가 남기고,
감사 행의 `resource_id` 는 Agent 가 아니라 업로드 id 라 업로드 행과 그 지우기 행이 같은 id 로
이어진다. Agent 는 `path_params` 에 남는다.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from daegam.api._agent_proxy import (
    expect_status,
    get_agent_or_404,
    is_approver,
    owned_session,
    proxied_download,
    proxied_sse,
    require_runtime_feature,
)
from daegam.api._common import audit_read, audit_target
from daegam.api._download import download_openapi, download_response
from daegam.api.agent_feedback import Feedback
from daegam.api.agents import CHAT, READ, SSE_RESPONSES, Agents, Choices, Service, Turns, Uploads
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, ErrorResponse
from daegam.api.schemas.agents import (
    AgentMemoriesOut,
    AgentMemoryFileDeleteOut,
    AgentMemoryFileOut,
    AgentRegenerateBody,
    AgentSessionDeleteOut,
    AgentSessionFilesOut,
    AgentSessionPatchOut,
    AgentSessionRenameBody,
)
from daegam.api.schemas.chat_uploads import AgentUploadOut
from daegam.auth.principal import Principal
from daegam.body_limit import MULTIPART_SLACK
from daegam.documents.formats import MAX_DOCUMENT_BYTES
from daegam.services.agent_citations import CitationSources
from daegam.services.agent_features import (
    ATTACHMENTS,
    CITATIONS,
    MEMORY_FILES,
    REGENERATE,
    SESSION_ADMIN,
    SESSION_FILES,
)
from daegam.services.agent_sleep import TURN
from daegam.services.agents import AgentService
from daegam.services.approval_choices import ApprovalChoiceService
from daegam.services.chat_uploads import ChatUploadService
from daegam.services.turn_person import TurnDelegation
from daegam.store.agents import Agent
from daegam.urls import API_PREFIX

router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])

SESSION_PATH = "/{agent_id}/sessions/{session_id}"
MY_MEMORIES_PATH = "/{agent_id}/memories/me"
MY_MEMORY_FILE_PATH = f"{MY_MEMORIES_PATH}/files/{{key:path}}"
KNOWLEDGE_READ = Depends(require_scope("knowledge:read"))
UPLOADS_PATH = "/{agent_id}/uploads"
# 첨부 업로드 본문의 상한이다. 미리 보기의 업로드(P44b 6절)도 같다. 앱 전체의 상한은 백엔드 API
# 업로드 크기라 이 경로에는 크다.
# FastAPI 가 폼을 라우트보다 먼저 읽으므로 본문 상한 미들웨어(`body_limit.py`)가 이 경로만 읽기
# 전에 끊는다. 상한 크기의 문서가 multipart 경계 때문에 끊기지 않게 여유를 더하고, 크기
# 판정(413 too_large)은 서비스가 한다
UPLOAD_BODY_LIMIT = (
    re.compile(rf"^{re.escape(API_PREFIX)}/agents/[^/]+(?:/preview)?/uploads$"),
    MAX_DOCUMENT_BYTES + MULTIPART_SLACK,
)
# 이름 없이 온 파일의 이름이다
UNNAMED_UPLOAD = "file"
UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorResponse, "description": "파일이 상한보다 크다 (too_large)"},
    429: {
        "model": ErrorResponse,
        "description": "업로드 남용 상한 (upload_rate_limited·too_many_uploads·upload_busy, "
        "문서 파싱 서비스가 바쁘면 extract_busy). Retry-After 가 있을 수 있다",
    },
}
SOURCE_RESPONSES = download_openapi("인용한 Knowledge 문서의 원문. 내려받기 헤더 다섯으로 온다")
SESSION_FILE_RESPONSES = download_openapi("세션 파일의 바이트. 내려받기 헤더 다섯으로 온다")


def get_citation_sources(request: Request) -> CitationSources:
    return request.app.state.citation_sources


Sources = Annotated[CitationSources, Depends(get_citation_sources)]


@router.patch(SESSION_PATH, response_model=AgentSessionPatchOut, dependencies=[CHAT])
async def rename_agent_session(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    agent_id: str,
    session_id: str,
    body: AgentSessionRenameBody,
) -> dict[str, Any]:
    """세션 이름을 바꾼다. 제목은 공백·제어 문자를 뺀 1~100자이고 아니면 422 invalid_title 이다.
    닫힌 세션도 된다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    require_runtime_feature(doc, SESSION_ADMIN)
    st, out = await service.proxy.rename_session(
        doc, session_id, user_sub=user.sub, title=body.title
    )
    expect_status(st, out, feature=SESSION_ADMIN)
    return out


@router.delete(SESSION_PATH, response_model=AgentSessionDeleteOut, dependencies=[CHAT])
async def delete_agent_session(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    feedback: Feedback,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """세션을 지운다. 체크포인트, 전사, 색인 행과 그 세션의 답변 피드백이 사라지고 장기 기억과
    감사 기록은 남는다. 도는 턴이 있으면 409 turn_in_progress 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    require_runtime_feature(doc, SESSION_ADMIN)
    st, out = await service.proxy.delete_session(doc, session_id, user_sub=user.sub)
    expect_status(st, out, feature=SESSION_ADMIN)
    await feedback.forget_session(doc, session_id)
    return out


@router.post(f"{SESSION_PATH}/regenerate", responses=SSE_RESPONSES, dependencies=[CHAT])
async def regenerate_agent_turn(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    turns: Turns,
    choices: Choices,
    agent_id: str,
    session_id: str,
    body: AgentRegenerateBody,
) -> StreamingResponse:
    """마지막 턴의 답변을 다시 생성한다. 응답은 `/chat` 과 같은 SSE 다. `message` 가 있으면
    마지막 질문을 그 글로 바꿔 보낸다. 마지막 턴이 없으면 409 nothing_to_regenerate, 승인 대기
    중이면 409 session_pending 이다. 채팅처럼 부른 사람의 턴 위임을 열고 그가 기억한 승인 선택을
    싣는다(P53, P43 5절). 잠든 Agent 는 위임 전에 깨우고 503 agent_waking 이다(P44 8절)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await regenerate_with(doc, user, service, turns, choices, session_id, body)


async def regenerate_with(
    doc: Agent,
    user: Principal,
    service: AgentService,
    turns: TurnDelegation,
    choices: ApprovalChoiceService,
    session_id: str,
    body: AgentRegenerateBody,
) -> StreamingResponse:
    """찾은 문서의 다시 생성. Agent 라우트와 미리 보기 라우트(`api/agent_preview.py`)가 함께
    쓴다."""
    await service.sleep.admit(doc, TURN)
    require_runtime_feature(doc, REGENERATE)
    plan = await turns.regenerate(doc, user, session_id)
    async with turns.preparing(plan):
        delivered = await choices.for_turn(plan)
    proxied = await turns.send(
        plan,
        lambda turn_grant: service.proxy.regenerate(
            doc,
            session_id,
            user_sub=user.sub,
            message=body.message,
            ephemeral=body.ephemeral,
            turn_grant=turn_grant,
            approval_choices=delivered,
        ),
    )
    return await proxied_sse(proxied, feature=REGENERATE)


@router.get(
    f"{SESSION_PATH}/citations/{{n}}/source",
    response_class=Response,
    responses=SOURCE_RESPONSES,
    dependencies=[READ, KNOWLEDGE_READ],
)
async def download_citation_source(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    sources: Sources,
    agent_id: str,
    session_id: str,
    n: Annotated[int, Path(ge=1)],
) -> Response:
    """그 세션의 답이 인용한 번호 `n` 의 Knowledge 문서 원문을 내려받는다. 인용되지 않은 번호와
    배포된 리비전에 없는 Knowledge Base 는 404 citation_not_found, Tool 이나 출력 위치에 가림
    가드레일이 있는 에이전트는 403 source_masked 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await citation_source_of(doc, tenant, user, service, sources, session_id, n)


async def citation_source_of(
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    sources: CitationSources,
    session_id: str,
    n: int,
) -> Response:
    """찾은 문서의 세션이 인용한 원문. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    require_runtime_feature(doc, CITATIONS)
    session = await owned_session(
        service, doc, session_id, user, approver=is_approver(user, tenant)
    )
    source = await sources.source(doc, session, n)
    return download_response(source.body, source.name)


@router.get(f"{SESSION_PATH}/files", response_model=AgentSessionFilesOut, dependencies=[READ])
async def list_session_files(
    tenant: Tenant, user: User, store: Agents, service: Service, agent_id: str, session_id: str
) -> dict[str, Any]:
    """세션 파일 목록이다. 업로드한 파일(`origin: upload`), 코드 실행 Agent 의 작업 공간 파일
    (`origin: workspace`)과 에이전트가 파일 Tool 로 쓴 텍스트 파일(`origin: agent`)이다. 소유자 또는
    agent:approve 이고, 닫힌 세션은 파일이 지워져 410 session_closed 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await session_files_of(doc, tenant, user, service, session_id)


async def session_files_of(
    doc: Agent, tenant: str, user: Principal, service: AgentService, session_id: str
) -> dict[str, Any]:
    """찾은 문서의 세션 파일 목록. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    require_runtime_feature(doc, SESSION_FILES)
    approver = is_approver(user, tenant)
    st, out = await service.proxy.session_files(
        doc, session_id, user_sub=user.sub, approver=approver
    )
    expect_status(st, out, feature=SESSION_FILES)
    return out


@router.get(
    f"{SESSION_PATH}/files/{{path:path}}",
    response_class=Response,
    responses=SESSION_FILE_RESPONSES,
    dependencies=[READ],
)
async def download_session_file(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    agent_id: str,
    session_id: str,
    path: str,
) -> Response:
    """세션 파일 하나를 내려받는다. path 는 목록의 경로에서 앞 `/` 를 뗀 것이다. 없으면 404
    file_not_found, 10MiB 를 넘으면 413 file_too_large, `.`·`..` 조각이 든 경로는 422 invalid_path
    다. 작업 공간 파일 가운데 열면 코드가 돌 수 있는 형식은 403 file_refused_executable 이다(P40
    6절). 어떤 형식도 문서로 열리지 않는 첨부로 온다. 승인자로 부른 다운로드는 감사에 남는다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await session_file_of(request, doc, tenant, user, service, session_id, path)


async def session_file_of(
    request: Request,
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    session_id: str,
    path: str,
) -> Response:
    """찾은 문서의 세션 파일 하나. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    require_runtime_feature(doc, SESSION_FILES)
    approver = is_approver(user, tenant)
    if approver:
        audit_read(request)
    proxied = await service.proxy.session_file(
        doc, session_id, path, user_sub=user.sub, approver=approver
    )
    return await proxied_download(proxied, path, feature=SESSION_FILES)


@router.get(MY_MEMORIES_PATH, response_model=AgentMemoriesOut, dependencies=[CHAT])
async def list_my_memories(
    tenant: Tenant, user: User, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    """이 에이전트가 기억하는 내 파일 목록이다. 장기 기억이 꺼져 있으면 404 long_term_off 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    require_runtime_feature(doc, MEMORY_FILES)
    st, out = await service.proxy.memories(doc, user_sub=user.sub)
    expect_status(st, out, feature=MEMORY_FILES)
    return out


@router.get(MY_MEMORY_FILE_PATH, response_model=AgentMemoryFileOut, dependencies=[CHAT])
async def read_my_memory(
    tenant: Tenant, user: User, store: Agents, service: Service, agent_id: str, key: str
) -> dict[str, Any]:
    """내 기억 파일 하나의 내용이다. key 는 `/memories/` 밑의 경로다. 1MiB 를 넘으면 413
    file_too_large, 없으면 404 memory_not_found, `.`·`..` 조각이 든 경로는 422 invalid_path 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    require_runtime_feature(doc, MEMORY_FILES)
    st, out = await service.proxy.memory_file(doc, key, user_sub=user.sub)
    expect_status(st, out, feature=MEMORY_FILES)
    return out


@router.delete(MY_MEMORY_FILE_PATH, response_model=AgentMemoryFileDeleteOut, dependencies=[CHAT])
async def delete_my_memory(
    tenant: Tenant, user: User, store: Agents, service: Service, agent_id: str, key: str
) -> dict[str, Any]:
    """내 기억 파일 하나를 지운다. 그 경로로 시작하는 다른 파일은 남는다. 없으면 404
    memory_not_found 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    require_runtime_feature(doc, MEMORY_FILES)
    st, out = await service.proxy.delete_memory_file(doc, key, user_sub=user.sub)
    expect_status(st, out, feature=MEMORY_FILES)
    return out


@router.post(
    UPLOADS_PATH,
    response_model=AgentUploadOut,
    status_code=status.HTTP_201_CREATED,
    responses=UPLOAD_RESPONSES,
    dependencies=[CHAT],
)
async def upload_chat_attachment(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    uploads: Uploads,
    agent_id: str,
    file: Annotated[UploadFile, File()],
) -> AgentUploadOut:
    """채팅에 붙일 파일 하나를 업로드한다(문서 10MiB, 이미지 2MiB). 문서는 지금 글을 뽑아 쪽 수와
    글자 수를 돌려주고, 보낼 때는 `id` 만 `/chat` 의 `attachments` 에 싣는다. 1시간 안에 보내지
    않으면 사라진다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await upload_to(request, doc, tenant, user, service, uploads, file)


async def upload_to(
    request: Request,
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    uploads: ChatUploadService,
    file: UploadFile,
) -> AgentUploadOut:
    """찾은 문서에 채팅 첨부 하나를 올린다. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    service.proxy.require_deployed(doc, action="upload")
    require_runtime_feature(doc, ATTACHMENTS)
    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    upload = await uploads.upload(
        doc,
        user_sub=user.sub,
        name=file.filename or UNNAMED_UPLOAD,
        content_type=file.content_type,
        data=data,
    )
    audit_target(request, upload.id, tenant)
    return AgentUploadOut.of(upload)


@router.delete(
    f"{UPLOADS_PATH}/{{upload_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[CHAT],
)
async def delete_chat_attachment(
    request: Request,
    tenant: Tenant,
    user: User,
    store: Agents,
    uploads: Uploads,
    agent_id: str,
    upload_id: str,
) -> Response:
    """보내지 않은 업로드를 지운다. 입력칸에서 칩을 지운 자리다. 이미 보냈거나 만료됐어도 204 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await remove_upload(request, doc, tenant, user, uploads, upload_id)


async def remove_upload(
    request: Request,
    doc: Agent,
    tenant: str,
    user: Principal,
    uploads: ChatUploadService,
    upload_id: str,
) -> Response:
    """찾은 문서의 보내지 않은 업로드를 지운다. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    audit_target(request, upload_id, tenant)
    await uploads.remove(doc, user.sub, upload_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
