"""Agent API (D42). 캔버스 정의와 런타임 배포가 한 리소스 아래에 있다. 만들기는 저장이고 배포는
따로 누른다.

목록은 둘이다. `GET ""` 은 배포된 것마다 클러스터를 왕복해 지금 상태를 붙이고(런타임 수치는 직행
경로에서만), `GET /summary` 는 왕복 없이 id·이름·표시 이름·마지막으로 알려진 phase 만 준다(D35 F2).

프록시는 D21 대로 플랫폼이 인바운드 인증을 하고 런타임에는 인스턴스 토큰을 Bearer 로 붙인다. 직행이
아닌 경로는 pods/portforward 다(D41). SSE 응답은 끊기는 순간 본문을 닫는다.
세션 소유자는 호출자의 `sub` 이고, `agent:approve` scope 가 있으면 런타임에 `X-Daegam-Approver: 1`
을 보내 소유자가 아니어도 보류 세션을 보고 결정하고 진행 중 턴을 멈출 수 있다. 런타임 능력에 기대는
라우트(중지)는 이 에이전트의 기록에 그 이름이 있을 때만 런타임을 부른다(D65). 프록시 라우트가 함께
쓰는 도우미(소유자·승인자 판정, 오류 형식, 능력 가르기, SSE 넘기기)는 `api/_agent_proxy.py` 에 있다.
채팅과 결정 재개는 런타임을 부르기 전에 턴 위임을 열어 싣고 답을 보고 끝낸다
(`services/turn_person.py`, P53).

쓰지 않는 Agent 는 0 으로 줄어 잠든다(P44, `services/agent_sleep.py`). 채팅·결정·다시 생성은 로더
바로 뒤의 게이트가 깨우고 깨우는 동안 503 agent_waking 과 Retry-After 로 답한다. 위임을 열거나
업로드를 쓰기 전이라 다시 보내도 남는 것이 없다. `POST /sleep` 은 지금 재우고 `POST /wake` 는
깨운다. 읽기는 깨우지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._advanced import overrides_manifest
from daegam.api._agent_proxy import (
    APPROVE_SCOPE,
    check_owner,
    expect_status,
    get_agent_or_404,
    get_approval_choices,
    get_enabled_runs,
    get_turns,
    is_approver,
    owned_session,
    proxied_sse,
    require_runtime_feature,
    runtime_rolling,
    stream_error,
    turn_attachments,
)
from daegam.api._common import error, forbidden, not_found, references_out
from daegam.api.deps import Tenant, User, bearer_token, require_scope
from daegam.api.hub import item_out_live, unpublish_item
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse
from daegam.api.schemas.agents import (
    SESSION_TITLE_MAX_CHARS,
    AgentCancelBody,
    AgentCancelOut,
    AgentChatBody,
    AgentDecisionsBody,
    AgentDeleteResponse,
    AgentDeployed,
    AgentJson,
    AgentMemoriesDeleteResponse,
    AgentOut,
    AgentPendingOut,
    AgentRevisionOut,
    AgentSaved,
    AgentSessionOut,
    AgentSessionsOut,
    AgentSummary,
    AgentTemplateOut,
    AgentValidateResponse,
    CloneRequest,
    DeployRequest,
    RedeployRequest,
    RollbackRequest,
    SkipEvaluation,
)
from daegam.api.schemas.hub import HubItemWithStatus
from daegam.api.schemas.mcp_servers import (
    DriftAbsorbResponse,
    ManifestResponse,
    OverridesResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.api.schemas.skills import SkillBundleOut
from daegam.auth.hub_access import Access
from daegam.auth.principal import Principal
from daegam.k8s.preflight import preflight_warnings
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.services.agent_features import CANCEL, SESSION_ADMIN
from daegam.services.agent_graph import Issue, errors_of, issue_lines
from daegam.services.agent_rollback import spec_digest
from daegam.services.agent_sleep import TURN
from daegam.services.agent_templates import templates
from daegam.services.agents import AgentService, GraphReview
from daegam.services.approval_choices import ApprovalChoiceService, strip_remember
from daegam.services.chat_uploads import ChatUploadService
from daegam.services.runs.service import RunService
from daegam.services.telemetry.traces import AgentTraces
from daegam.services.turn_person import TURN_ID_HEADER, TurnDelegation
from daegam.store.agent_instance_tokens import MongoAgentInstanceTokenStore, pinned_agent
from daegam.store.agents import (
    Agent,
    AgentRevision,
    AgentSpec,
    Graph,
    MongoAgentStore,
    Point,
    ServiceSettings,
)
from daegam.store.base import iso_utc
from daegam.store.hub import AGENT_DESCRIPTION_MAX

logger = logging.getLogger("daegam.api.agents")

router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])

# SSE 응답 문서. 본문은 런타임 이벤트 스트림이라 JSON 모델이 없다
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "런타임 SSE (greeting·token·tool_call·tool_result·citations·interrupt·"
        "guardrail·model_fallback·tool_limit·done·error). greeting 은 새 세션의 첫 이벤트다",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    },
    429: {
        "description": "런타임 한도 (max_turns·max_concurrent_sessions·rate_limit). "
        "Retry-After 가 있을 수 있다",
        "content": {"application/json": {"schema": {"type": "object"}}},
    },
}


def get_store(request: Request) -> MongoAgentStore:
    return request.app.state.agents


def get_tokens(request: Request) -> MongoAgentInstanceTokenStore:
    return request.app.state.agent_instance_tokens


def get_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_traces(request: Request) -> AgentTraces:
    return request.app.state.agent_traces


def get_chat_uploads(request: Request) -> ChatUploadService:
    return request.app.state.chat_uploads


Agents = Annotated[MongoAgentStore, Depends(get_store)]
Tokens = Annotated[MongoAgentInstanceTokenStore, Depends(get_tokens)]
Service = Annotated[AgentService, Depends(get_service)]
Traces = Annotated[AgentTraces, Depends(get_traces)]
Uploads = Annotated[ChatUploadService, Depends(get_chat_uploads)]
Turns = Annotated[TurnDelegation, Depends(get_turns)]
# 기억한 승인 선택 (P43 5절). 세 턴 라우트가 싣고 결정이 기억하며 계정 화면이 보고 지운다
Choices = Annotated[ApprovalChoiceService, Depends(get_approval_choices)]
# 백그라운드 실행을 켰을 때만 있다 (P41)
EnabledRuns = Annotated[RunService | None, Depends(get_enabled_runs)]
# viewer 는 읽기와 채팅만, developer 이상이 저장·배포·재배포·해제를 한다 (D35)
READ = Depends(require_scope("agent:read"))
WRITE = Depends(require_scope("agent:write"))
DEPLOY = Depends(require_scope("agent:deploy"))
CHAT = Depends(require_scope("agent:chat"))
APPROVE = Depends(require_scope(APPROVE_SCOPE))
HUB_WRITE = Depends(require_scope("hub:write"))


class AgentGraphBody(BaseModel):
    """`POST /validate` 본문. 리비전에 들어가는 두 부분이다."""

    model_config = ConfigDict(extra="forbid")

    graph: Graph = Field(default_factory=Graph)
    service: ServiceSettings = Field(default_factory=ServiceSettings)

    def spec(self) -> AgentSpec:
        return AgentSpec(graph=self.graph, service=self.service)


class AgentBody(AgentGraphBody):
    """POST 본문이자 PUT 본문의 바탕. PUT 의 name 은 저장값과 같아야 한다 (422 name_immutable,
    D24). layout 은 리비전 비교에서 빠진다."""

    name: str
    display_name: str | None = None
    layout: dict[str, Point] = Field(default_factory=dict)


class AgentUpdateBody(AgentBody):
    """PUT 본문. `expect_revision` 은 고치기 시작할 때 본 최신 리비전 번호다. 그사이 다른 리비전이
    저장됐으면 409 revision_conflict 이고 본문의 `revision` 이 지금 번호다. 없으면 서버가 읽은
    리비전에 조건을 건다. layout 을 빼면 남는 노드의 저장된 배치를 지킨다 (P48 6.7절)."""

    expect_revision: int | None = Field(default=None, ge=1)


class OverridesBody(BaseModel):
    """patch(RFC 7386 + 키 리스트) / manifest(Deployment 전체 dict) / yaml 중 하나."""

    model_config = ConfigDict(extra="forbid")

    patch: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    yaml: str | None = None


def _issues_out(issues: list[Issue]) -> list[dict[str, Any]]:
    return [i.to_dict() for i in issues]


def _out(
    doc: Agent,
    status: dict[str, Any] | None = None,
    *,
    issues: list[Issue] | None = None,
    revision_created: bool | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    cur = doc.current
    d: dict[str, Any] = {
        "id": doc.id,
        "tenant": doc.tenant,
        "name": doc.name,
        "display_name": doc.display_name,
        "state": doc.state,
        "revision": doc.revision,
        "deployed_revision": doc.deployed_revision,
        "graph": cur.graph.model_dump(mode="json"),
        "service": cur.service.model_dump(mode="json"),
        "layout": {k: v.model_dump() for k, v in doc.layout.items()},
        "revisions": [r.revision for r in doc.revisions],
        "cluster_id": doc.cluster_id,
        "k8s": doc.k8s.model_dump(mode="json") if doc.k8s is not None else None,
        "replicas": doc.replicas,
        "resources": doc.resources,
        "scale_to_zero": doc.scale_to_zero,
        "bindings": [b.model_dump(mode="json") for b in doc.bindings],
        "token_id": doc.token_id,
        "llm_secrets": list(doc.llm_secrets),
        "llm_route": doc.llm_route.model_dump(include={"route", "reason"}),
        "exposure": doc.exposure.model_dump(mode="json"),
        "k8s_uid": doc.k8s_uid,
        "svc_token_rev": doc.svc_token_rev,
        "effective_config": doc.effective_config,
        "overrides": doc.overrides,
        "last_phase": doc.last_phase,
        "last_error": doc.last_error,
        "last_spec_change": iso_utc(doc.last_spec_change),
        "deploy_history": [r.model_dump(mode="json") for r in doc.deploy_history],
        "evaluation_gate": (
            doc.evaluation_gate.model_dump(mode="json") if doc.evaluation_gate is not None else None
        ),
        "status": status,
        "created_at": iso_utc(doc.created_at),
        "updated_at": iso_utc(doc.updated_at),
        "updated_by": doc.updated_by,
    }
    if issues is not None:
        d["issues"] = _issues_out(issues)
    if revision_created is not None:
        d["revision_created"] = revision_created
    if warnings is not None:
        d["warnings"] = warnings
    return d


def review_out(review: GraphReview) -> dict[str, Any]:
    """검증의 답 `POST /validate`. 비서의 제안 다시 읽기도 같은 모양을 쓴다(`api/assistant.py`)."""
    return {
        "ok": not errors_of(review.issues),
        "issues": _issues_out(review.issues),
        "tool_effects": review.tools.effects,
        "tool_origins": review.tools.origins,
        "tool_marked": review.tools.marked,
        "tool_safety": True,
    }


def _skip(skip: SkipEvaluation | None) -> str | None:
    """배포 게이트를 건너뛰는 이유 (P45 12절)."""
    return skip.reason if skip is not None else None


async def _deployed_out(service: AgentService, doc: Agent, warnings: list[str]) -> dict[str, Any]:
    """배포, 재배포, 되돌리기의 답. 클러스터 preflight 경고를 동작의 경고 앞에 붙인다."""
    cluster = await service.tenancy.clusters.get(doc.cluster_id)
    preflight = preflight_warnings(cluster.status.preflight if cluster else None)
    return _out(doc, await service.status(doc), warnings=preflight + warnings)


def _revision_out(rev: AgentRevision) -> dict[str, Any]:
    """리비전과 그 내용의 지문 (P45 6절)."""
    return {**rev.model_dump(mode="json"), "spec_digest": spec_digest(rev)}


# -- 정의 --------------------------------------------------------------------------------


@router.get("/templates", response_model=list[AgentTemplateOut], dependencies=[READ])
async def list_agent_templates() -> list[dict[str, Any]]:
    """캔버스 템플릿. 단일 상담 에이전트 하나다."""
    return templates()


@router.post("/validate", response_model=AgentValidateResponse, dependencies=[WRITE])
async def validate_agent_graph(
    tenant: Tenant,
    service: Service,
    store: Agents,
    body: AgentGraphBody,
    user: User,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """저장 없이 그래프를 검사한다. 저장·배포도 같은 규칙이다. 보이는데 consume 이 없는 Skill 을
    붙였으면 422 `skill_not_consumable` 이다(D64). `agent_id` 는 캔버스가 고치는 Agent 이고, 없는
    Agent 면 404 다. 검사는 그것에 따라 바뀌지 않는다."""
    if agent_id:
        await get_agent_or_404(store, tenant, agent_id)
    return review_out(await service.review(tenant, body.spec(), principal=user))


@router.get("/summary", response_model=list[AgentSummary], dependencies=[READ])
async def list_agent_summaries(tenant: Tenant, store: Agents) -> list[dict[str, Any]]:
    """상태 왕복 없는 가벼운 목록 (D35 F2). 홈 챗의 에이전트 고르개가 첫 요청으로 쓴다. 배포된
    것만 든다(draft 는 부를 수 없다).

    경로가 `/{agent_id}` 보다 먼저 선언돼야 `summary` 가 문서 id 로 읽히지 않는다."""
    return [
        {"id": a.id, "name": a.name, "display_name": a.display_name, "phase": a.last_phase}
        for a in await store.list(tenant)
    ]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentSaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def create_agent(
    tenant: Tenant, service: Service, body: AgentBody, user: User
) -> dict[str, Any]:
    """정의만 받아 draft 로 저장한다. 배포는 `POST /{id}/deploy` 다."""
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    doc, issues = await service.create(
        tenant,
        name=name,
        display_name=display_name,
        spec=body.spec(),
        layout=body.layout,
        principal=user,
    )
    return _out(doc, issues=issues, revision_created=True)


@router.get(
    "", response_model=list[AgentOut], response_model_exclude_unset=True, dependencies=[READ]
)
async def list_agents(tenant: Tenant, store: Agents, service: Service) -> list[dict[str, Any]]:
    """draft 를 포함한 전부. status 는 배포된 것만 매 요청 Deployment 를 왕복하고, 런타임 수치는
    직행 경로에서만 싣는다(원격 클러스터에서 에이전트 수만큼 터널을 열지 않는다)."""
    docs = await store.list(tenant, include_draft=True)
    states = await asyncio.gather(*(service.status(d, listing=True) for d in docs))
    return [_out(d, st) for d, st in zip(docs, states, strict=True)]


@router.get(
    "/{agent_id}", response_model=AgentOut, response_model_exclude_unset=True, dependencies=[READ]
)
async def get_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    doc = await get_agent_or_404(store, tenant, agent_id)
    return _out(doc, await service.status(doc))


@router.get("/{agent_id}/references", response_model=ReferencesResponse, dependencies=[READ])
async def agent_references(
    tenant: Tenant, store: Agents, agent_id: str, request: Request
) -> dict[str, Any]:
    """이 Agent 의 참조 (D43). 이 Agent 를 게시한 허브 항목과, 그 항목을 거쳐 이 Agent 를 Tool 로
    쓰는 Agent 다 (P46 6.1절)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await references_out(request, "agent", doc.id, tenant=tenant)


@router.put(
    "/{agent_id}",
    response_model=AgentSaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def put_agent(
    tenant: Tenant,
    store: Agents,
    service: Service,
    agent_id: str,
    body: AgentUpdateBody,
    user: User,
) -> dict[str, Any]:
    """전체 교체. graph·service 가 바뀌면 새 리비전이고 layout 만 바뀌면 리비전 없이 저장된다.
    배포 핀은 그대로라 "r5 저장됨, r2 배포됨" 이 된다. name 은 바꿀 수 없다 (422 name_immutable).
    `expect_revision` 이 최신 리비전이 아니면 409 revision_conflict{revision} 이다. layout 을 빼면
    지금 저장된 배치 가운데 남는 노드의 것을 그대로 둔다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    check_name_unchanged(body.name, doc.name)
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    # 보내지 않은 layout 과 빈 layout 을 가른다. 빈 것은 배치를 모두 비운다
    layout = body.layout if "layout" in body.model_fields_set else None
    doc, issues, created = await service.save(
        doc,
        display_name=display_name,
        spec=body.spec(),
        layout=layout,
        principal=user,
        expect_revision=body.expect_revision,
    )
    return _out(doc, await service.status(doc), issues=issues, revision_created=created)


@router.delete("/{agent_id}", response_model=AgentDeleteResponse, dependencies=[WRITE])
async def delete_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str, user: User
) -> dict[str, Any]:
    """정의와 클러스터의 것을 모두 지운다. draft 는 agent:write 로 되고, 배포 쪽이 있으면
    agent:deploy 도 있어야 한다 (403 의 모양은 require_scope 와 같다)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    if doc.is_deployed and "agent:deploy" not in user.scopes_in(tenant):
        raise forbidden("scope agent:deploy required", scope="agent:deploy", tenant=tenant)
    state_before = doc.state
    k8s = doc.k8s.model_dump(mode="json") if doc.k8s is not None else None
    # 남이 쥔 클레임에 일을 남겼으면 수가 None 이고 문서는 deleting 이다 (P66 2.6절)
    doc, revoked = await service.delete(doc)
    return {
        "id": doc.id,
        "deleted": revoked is not None,
        "state_before": state_before,
        "revoked_tokens": revoked or 0,
        "k8s": k8s,
    }


@router.get(
    "/{agent_id}/revisions",
    response_model=list[AgentRevisionOut],
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def list_revisions(tenant: Tenant, store: Agents, agent_id: str) -> list[dict[str, Any]]:
    doc = await get_agent_or_404(store, tenant, agent_id)
    return [_revision_out(r) for r in doc.revisions]


@router.get(
    "/{agent_id}/revisions/{n}",
    response_model=AgentRevisionOut,
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def get_revision(tenant: Tenant, store: Agents, agent_id: str, n: int) -> dict[str, Any]:
    doc = await get_agent_or_404(store, tenant, agent_id)
    rev = doc.get_revision(n)
    if rev is None:
        raise not_found(f"revision {n} not found")
    return _revision_out(rev)


@router.post(
    "/{agent_id}/clone",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentSaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def clone_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str, body: CloneRequest, user: User
) -> dict[str, Any]:
    """현재 리비전의 정의를 베낀 draft. exposure 를 주면 배포 설정으로 남는다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    copy, issues = await service.clone(
        doc, name=name, display_name=display_name, exposure=body.exposure, principal=user
    )
    return _out(copy, issues=issues, revision_created=True)


# -- 배포 --------------------------------------------------------------------------------


@router.post(
    "/{agent_id}/deploy",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentDeployed,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def deploy_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str, body: DeployRequest, user: User
) -> dict[str, Any]:
    """현재 리비전을 핀해 런타임을 띄운다. 클러스터 객체가 새로 생기므로 201 이다. draft 가
    아니면 409 already_deployed."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    doc, issues = await service.deploy(
        doc,
        cluster_id=body.cluster_id,
        namespace=body.namespace,
        replicas=body.replicas,
        resources=body.resources,
        resources_sent="resources" in body.model_fields_set,
        exposure=body.exposure,
        exposure_sent="exposure" in body.model_fields_set,
        scale_to_zero=body.scale_to_zero,
        principal=user,
        skip_evaluation=_skip(body.skip_evaluation),
    )
    doc = await service.provision(doc)
    return await _deployed_out(service, doc, issue_lines(issues))


@router.post(
    "/{agent_id}/redeploy",
    response_model=AgentDeployed,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def redeploy_agent(
    tenant: Tenant,
    store: Agents,
    service: Service,
    agent_id: str,
    user: User,
    body: RedeployRequest | None = None,
) -> dict[str, Any]:
    """핀을 현재 리비전으로 옮기고 provider 키를 다시 복사해 재적용한다. 본문에 exposure·replicas·
    resources 가 있으면 그것도 바꾼다. 보류 세션이 있는데 리비전이 바뀌면 warnings 에 적는다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    sent = body.model_fields_set if body is not None else set()
    doc, warnings = await service.redeploy(
        doc,
        exposure=body.exposure if body is not None else None,
        exposure_sent="exposure" in sent,
        replicas=body.replicas if body is not None else None,
        resources=body.resources if body is not None else None,
        resources_sent="resources" in sent,
        scale_to_zero=body.scale_to_zero if body is not None else None,
        principal=user,
        skip_evaluation=_skip(body.skip_evaluation if body is not None else None),
    )
    return await _deployed_out(service, doc, warnings)


@router.post(
    "/{agent_id}/rollback",
    response_model=AgentDeployed,
    response_model_exclude_unset=True,
    dependencies=[WRITE, DEPLOY],
)
async def rollback_agent(
    tenant: Tenant,
    store: Agents,
    service: Service,
    agent_id: str,
    body: RollbackRequest,
    user: User,
) -> dict[str, Any]:
    """리비전 `revision` 의 정의를 새 리비전으로 저장하고 그것을 재배포한다 (P45 6절). 그 내용이
    현재 리비전과 같으면 리비전을 만들지 않는다. 배포됐거나 실패한 Agent 에서만 된다. 없는 리비전은
    404, 배포된 내용과 같으면 409 `nothing_to_roll_back` 이다. 저장의 검증이 막는 422
    `invalid_graph` 만 아무것도 바꾸지 않는다. 저장 뒤 재배포의 검사(`cluster_mismatch`, 샌드박스,
    네임스페이스, 런타임 기능)가 막으면 새 리비전은 저장된 채 남는다. 배포는 오늘의 런타임 이미지,
    provider 설정, Tool 목록으로 돈다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    doc, warnings = await service.rollback(
        doc,
        revision=body.revision,
        principal=user,
        skip_evaluation=_skip(body.skip_evaluation),
    )
    return await _deployed_out(service, doc, warnings)


@router.post(
    "/{agent_id}/undeploy",
    response_model=AgentOut,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def undeploy_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    """클러스터에서 내리고 정의는 남긴다. 장기 기억은 남는다 (같은 id 로 다시 배포된다)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    doc = await service.undeploy(doc)
    return _out(doc, None)


@router.post(
    "/{agent_id}/sleep",
    response_model=AgentOut,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def sleep_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    """쓰지 않는 Agent 를 지금 0 으로 줄인다 (P44 7절). 쉬는 창과 상관없고, 0 으로 줄이기를
    적용한 뒤 상태로 답한다. 잠들 수 없으면 409 agent_sleep_ineligible 이고 `ineligible` 이 그
    까닭이다. 턴이 리스를 쥐고 있으면 409 agent_busy 다. 이미 잠들었으면 그대로 답한다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    await service.sleep.sleep_now(doc)
    return _out(doc, await service.status(doc))


@router.post(
    "/{agent_id}/wake",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentOut,
    response_model_exclude_unset=True,
    dependencies=[CHAT],
)
async def wake_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    """잠든 Agent 의 파드를 다시 띄운다 (P44 8절). 파드가 뜨기를 기다리지 않고 상태로 답한다.
    깨어 있는 Agent 도 같은 답이고 활동만 적힌다. 홈 챗이 사람이 잠든 Agent 를 고를 때 한 번
    부른다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    service.proxy.require_deployed(doc, action="wake")
    await service.sleep.wake(doc)
    return _out(doc, await service.status(doc))


# -- 허브 게시 (P46 6.1절) ----------------------------------------------------------------------


class AgentPublishBody(BaseModel):
    """허브 게시 본문. name 기본값은 문서 name, display_name 기본값은 문서 display_name 이다
    (D24). description 은 이 Agent 를 Tool 로 부르는 Agent 의 모델이 읽는 Tool 설명이다.
    calls_trusted 가 참이면 호출의 메시지를 사람이 쓴 것으로 다룬다. access 는 생략하면 빈 정책이고,
    agent 항목은 다른 테넌트에 열 수 없다 (422 cross_tenant_unsupported)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    display_name: str | None = None
    description: str = Field(min_length=1, max_length=AGENT_DESCRIPTION_MAX)
    calls_trusted: bool = False
    access: Access | None = None


@router.post(
    "/{agent_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE, HUB_WRITE],
)
async def publish_agent(
    tenant: Tenant,
    store: Agents,
    service: Service,
    agent_id: str,
    body: AgentPublishBody,
    response: Response,
    user: User,
) -> dict[str, Any]:
    """허브에 `agent` 항목으로 게시한다. 같은 테넌트의 다른 Agent 가 캔버스에서 이 항목을 Tool 로
    붙인다. 이미 게시돼 있으면 200 으로 그 항목을 돌려준다. 턴을 받는 Agent 만 되고(409
    not_deployed·in_progress·deleting), 배포된 리비전에 memory 노드와 세션 예산이 있어야 하며(409
    agent_source_runs_unmet), 이벤트 수집이 켜져 있고(409 runs_need_events) 런타임 이미지가
    `runs` 와 `turn_tokens` 를 알려야 한다(409 runtime_feature_missing). Agent 호출이 꺼져 있으면
    409 agent_calls_disabled 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    name = validate_name(body.name) if body.name is not None else None
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    description = body.description.strip()
    if not description:
        raise error(422, "invalid_value", "description must not be blank")
    item, created = await service.publishing.publish(
        doc,
        name=name,
        display_name=display_name,
        description=description,
        calls_trusted=body.calls_trusted,
        access=body.access,
        principal=user,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return await item_out_live(service.publishing.hub, item, principal=user, tenant=tenant)


@router.delete(
    "/{agent_id}/publish", response_model=DeleteResponse, dependencies=[WRITE, HUB_WRITE]
)
async def unpublish_agent(
    tenant: Tenant, store: Agents, service: Service, agent_id: str, user: User
) -> dict[str, Any]:
    """게시 해제. `DELETE /hub/{id}` 와 같은 판정이다. 항목의 manage 가 없으면 403, 게시돼 있지
    않으면 404 not_published 다. 이 항목을 Tool 로 쓰는 Agent 가 있어도 해제되고, 그 Agent 의 저장과
    배포와 호출이 그때부터 거절된다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    hub = service.publishing.hub
    return await unpublish_item(hub, user, tenant, await hub.agent_item(doc))


# -- Advanced 편집 (F7a 공통) -------------------------------------------------------------------


@router.get("/{agent_id}/manifest", response_model=ManifestResponse, dependencies=[READ])
async def get_agent_manifest(
    tenant: Tenant, store: Agents, service: Service, agent_id: str
) -> dict[str, Any]:
    """{documents: [Deployment(편집 대상), Service, NetworkPolicy(전부 잠김)]}. draft 면 409."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await service.manifest(doc)


@router.put("/{agent_id}/overrides", response_model=OverridesResponse, dependencies=[DEPLOY])
async def put_agent_overrides(
    tenant: Tenant, store: Agents, service: Service, agent_id: str, body: OverridesBody
) -> dict[str, Any]:
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await service.set_overrides(doc, patch=body.patch, manifest=overrides_manifest(body))


@router.post(
    "/{agent_id}/drift/{action}",
    response_model=ManifestResponse | DriftAbsorbResponse,
    dependencies=[DEPLOY],
)
async def agent_drift_action(
    tenant: Tenant,
    store: Agents,
    service: Service,
    agent_id: str,
    action: Literal["reset", "absorb"],
) -> dict[str, Any]:
    doc = await get_agent_or_404(store, tenant, agent_id)
    if action == "reset":
        return await service.drift_reset(doc)
    return await service.drift_absorb(doc)


# -- 런타임 프록시 (D21) ------------------------------------------------------------------------


@router.post("/{agent_id}/chat", responses=SSE_RESPONSES, dependencies=[CHAT])
async def chat(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    uploads: Uploads,
    turns: Turns,
    choices: Choices,
    agent_id: str,
    body: AgentChatBody,
) -> StreamingResponse:
    """런타임 `/chat` 으로 SSE 프록시. `{session_id?, message, ephemeral?, attachments?}` 에
    호출자의 sub 를 user_sub 로 넣는다. 런타임의 상태 코드·본문·Retry-After 는 그대로 전달된다.
    첨부는 업로드를 런타임 모양으로 바꿔 싣고, 런타임이 스트림을 열면 그 업로드를 지운다. 부른
    사람의 턴 위임을 열어 싣는다(P53, `services/turn_person.py`). 실어야 할 토큰을 만들지 못하면
    런타임을 부르지 않고 503 turn_tokens_unavailable 이다. 그 사람이 세션 소유자로 턴을 열면 그가
    기억한 승인 선택을 싣는다(P43 5절). 잠든 Agent 는 위임과 업로드 전에 깨우고 503
    agent_waking 이다(P44 8절)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await chat_with(doc, user, service, uploads, turns, choices, body)


async def chat_with(
    doc: Agent,
    user: Principal,
    service: AgentService,
    uploads: ChatUploadService,
    turns: TurnDelegation,
    choices: ApprovalChoiceService,
    body: AgentChatBody,
) -> StreamingResponse:
    """찾은 문서와의 채팅 턴. Agent 라우트와 미리 보기 라우트(`api/agent_preview.py`)가 함께
    쓴다."""
    await service.sleep.admit(doc, TURN)
    attachments = await turn_attachments(service, uploads, doc, user.sub, body.attachments)
    plan = await turns.chat(doc, user, body.session_id)
    async with turns.preparing(plan):
        delivered = await choices.for_turn(plan)
    proxied = await turns.send(
        plan,
        lambda turn_grant: service.proxy.chat(
            doc,
            user_sub=user.sub,
            message=body.message,
            session_id=body.session_id,
            ephemeral=body.ephemeral,
            attachments=attachments,
            turn_grant=turn_grant,
            approval_choices=delivered,
        ),
    )
    response = await proxied_sse(proxied)
    try:
        await uploads.consume(doc, user.sub, body.attachments)
    except Exception as e:  # noqa: BLE001 - 런타임이 이미 턴을 받았다
        # 본문을 닫아도 턴은 끝나지 않으므로(P78 6절) 응답을 그대로 내고, 남은 업로드는 기한이
        # 지운다
        logger.warning("Agent %s 의 채팅 첨부를 지우지 못했다: %s", doc.id, e)
    except BaseException:
        # 요청이 취소돼 응답을 내지 못한다. 본문을 닫아 런타임 연결을 놓는다
        await response.close_body()
        raise
    return response


@router.get("/{agent_id}/sessions", response_model=AgentSessionsOut, dependencies=[READ])
async def list_agent_sessions(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    traces: Traces,
    run_service: EnabledRuns,
    agent_id: str,
    status: Literal["active", "pending", "closed"] | None = None,
    all_users: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    q: Annotated[str | None, Query(min_length=1, max_length=SESSION_TITLE_MAX_CHARS)] = None,
    runs: Literal["include", "only"] | None = None,
) -> dict[str, Any]:
    """세션 색인 목록. 기본은 자기 세션이고 all_users 는 agent:approve 가 있을 때만 전체다. 세션마다
    원장의 금액(`cost_usd`)이 붙고 원장에 행이 없으면 null 이다(P37 12절). q 는
    제목 검색이라 기록에 `session_admin` 이 있어야 한다(P36). 롤링 중에는 기록에 이름이 있어도 q 를
    모르는 옛 파드가 그 쿼리를 버리고 거르지 않은 목록을 200 으로 줄 수 있다. 그래서 런타임이 받은
    q 를 되돌려주지 않았으면 409 runtime_rolling 이다.

    runs 는 백그라운드 실행의 세션을 넣거나(`include`) 그것만 본다(`only`, P41 7절). 실행을 켰으면
    줄마다 그 실행(`run`: id, 상태, 마감)을 붙이고, `only` 는 실행 세션만, 그 가운데 `pending` 은
    승인을 기다리는 실행의 세션만 남긴다. 승인자의 받은 결정함이 `all_users` 와 함께 쓴다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    if q is not None:
        require_runtime_feature(doc, SESSION_ADMIN)
    st, body = await service.proxy.sessions(
        doc,
        user_sub=user.sub,
        approver=is_approver(user, tenant),
        status=status,
        limit=limit,
        all_users=all_users,
        q=q,
        runs=runs,
    )
    expect_status(st, body)
    if q is not None and body.get("q") != q:
        raise runtime_rolling(SESSION_ADMIN)
    sessions = body.get("sessions") or []
    if runs is not None and run_service is not None:
        sessions = body["sessions"] = await run_service.approvals.mark_sessions(
            tenant, agent_id, sessions, runs=runs, status=status
        )
    costs = await traces.session_costs(doc, [s["session_id"] for s in sessions])
    for s in sessions:
        s["cost_usd"] = costs.get(s["session_id"])
    return body


@router.get(
    "/{agent_id}/sessions/{session_id}", response_model=AgentSessionOut, dependencies=[READ]
)
async def get_agent_session(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    agent_id: str,
    session_id: str,
    view: Literal["progress"] | None = None,
) -> dict[str, Any]:
    """세션 상세(대화 요약·토큰·상태). 소유자 또는 agent:approve. `view=progress` 는 진행 중 턴을
    따라가는 가벼운 읽기다. 체크포인트와 메시지 요약을 읽지 않고, 화면이 다시 읽을 간격
    `poll_seconds` 를 싣는다(P78 2·4절)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await owned_session(
        service, doc, session_id, user, approver=is_approver(user, tenant), view=view
    )


@router.get(
    "/{agent_id}/sessions/{session_id}/pending",
    response_model=AgentPendingOut,
    dependencies=[READ],
)
async def get_agent_pending(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    choices: Choices,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """보류 승인 요청 (action_requests·review_configs). 소유자 또는 agent:approve. 부른 사람이
    기록된 세션 소유자면 요청마다 기억할 수 있는 선택(`remember_options`)과 그 기한
    (`remember_days`)을 더한다(P43 5절). 승인 카드가 그 선택만 보인다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await pending_of(doc, tenant, user, service, choices, session_id)


async def pending_of(
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    choices: ApprovalChoiceService,
    session_id: str,
) -> dict[str, Any]:
    """찾은 문서의 보류 승인 요청. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    approver = is_approver(user, tenant)
    st, body = await service.proxy.pending(doc, session_id, user_sub=user.sub, approver=approver)
    expect_status(st, body)
    check_owner(body, user.sub, approver)
    options = await choices.card_options(doc, session_id, user.sub, body)
    if options is not None:
        body = {**body, "remember_options": options, "remember_days": choices.days}
    return body


@router.post(
    "/{agent_id}/sessions/{session_id}/decisions",
    responses=SSE_RESPONSES,
    dependencies=[CHAT],
)
async def post_agent_decisions(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    turns: Turns,
    choices: Choices,
    run_service: EnabledRuns,
    agent_id: str,
    session_id: str,
    body: AgentDecisionsBody,
) -> StreamingResponse:
    """보류 세션을 재개한다 (`/chat` 과 같은 SSE). 소유자 또는 agent:approve 가 결정하고 결정자의
    sub 가 런타임 이벤트에 남는다. 승인자가 입력 없이 결정하면 소유자의, 그 밖에는 결정자 자신의 턴
    위임을 연다(P53 5.3절).

    백그라운드 실행의 세션이면 플랫폼이 결과를 정한다(P41 7절, `services/runs/approvals.py`). 묶인
    사람도 승인자도 아니면 아무것도 쓰기 전에 403 이다. 런타임을 부르기 전에 승인 대기 실행을
    `running` 으로 옮기고, 이미 기다리지 않거나 마감이 지났거나 중지 표시가 있으면 409 run_closed
    다. 결정은 실행 id 와 함께 ephemeral 로 보낸다. 턴이 열리지 않았으면 승인 대기로 되돌리고,
    받아들였는데 그사이 중지 표시가 남았으면 그 턴을 멈춘다.

    결정마다의 `remember` 는 세션 소유자가 기억할 선택이다(P43 5절, `services/approval_choices.py`).
    받을 수 없으면 아무것도 보내기 전에 422 remember_refused 이고, 런타임이 결정을 받은 뒤에 적는다.
    재개가 세션 소유자의 이름으로 돌면 그가 기억한 선택을 싣고, 실행 세션에는 거부만 싣는다.
    잠든 Agent 는 실행을 옮기거나 위임을 열기 전에 깨우고 503 agent_waking 이다(P44 8절)."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await decide_with(
        doc, tenant, user, service, turns, choices, run_service, session_id, body
    )


async def decide_with(
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    turns: TurnDelegation,
    choices: ApprovalChoiceService,
    run_service: RunService | None,
    session_id: str,
    body: AgentDecisionsBody,
) -> StreamingResponse:
    """찾은 문서의 보류 세션 재개. Agent 라우트와 미리 보기 라우트가 함께 쓴다. 미리 보기에는
    실행이 없어 run_service 가 None 이다."""
    await service.sleep.admit(doc, TURN)
    approver = is_approver(user, tenant)
    approvals = run_service.approvals if run_service is not None else None
    run = None
    if approvals is not None:
        run = await approvals.begin(tenant, doc.id, session_id, sub=user.sub, approver=approver)

    async def read_pending() -> dict[str, Any]:
        st, pending = await service.proxy.pending(
            doc, session_id, user_sub=user.sub, approver=approver
        )
        expect_status(st, pending)
        return pending

    try:
        plan = await turns.decide(doc, user, session_id, body.decisions, approver=approver)
        async with turns.preparing(plan):
            to_save = await choices.to_remember(plan, body.decisions, read_pending)
            delivered = await choices.for_turn(plan, run=run is not None)
    except BaseException:
        # 런타임을 부르기 전이라 턴이 열리지 않았다. 요청이 취소됐어도 막이 안에서 되돌린다
        if run is not None and approvals is not None:
            with anyio.CancelScope(shield=True):
                await approvals.not_sent(run)
        raise
    proxied = await turns.send(
        plan,
        lambda turn_grant: service.proxy.decisions(
            doc,
            session_id,
            user_sub=user.sub,
            decisions=strip_remember(body.decisions),
            approver=approver,
            ephemeral=body.ephemeral or run is not None,
            turn_grant=turn_grant,
            run_id=run.id if run is not None else None,
            approval_choices=delivered,
        ),
    )
    if proxied.status < 400 and to_save:
        # 런타임이 결정을 받았다. 요청이 취소돼도 적고 아래가 스트림을 넘긴다
        with anyio.CancelScope(shield=True):
            await choices.save(to_save)
    if run is None or approvals is None:
        return await proxied_sse(proxied)
    if proxied.status >= 400:
        refusal = await stream_error(proxied)
        detail = refusal.detail if isinstance(refusal.detail, dict) else {}
        await approvals.refused(run, proxied.status, detail.get("code"))
        raise refusal
    try:
        await approvals.accepted(run, proxied.headers.get(TURN_ID_HEADER))
    except Exception as e:  # noqa: BLE001 - 턴은 이미 다시 열렸다. 표시와 이벤트가 끝낸다
        logger.warning("run %s: the stop check after a decision failed: %s", run.id, e)
    except BaseException:
        # 요청이 취소돼 응답을 내지 못한다. 본문을 닫아 런타임 연결을 놓는다
        await proxied.body.aclose()
        raise
    return await proxied_sse(proxied)


@router.post(
    "/{agent_id}/sessions/{session_id}/cancel",
    response_model=AgentCancelOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[CHAT],
)
async def cancel_agent_turn(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    agent_id: str,
    session_id: str,
    body: AgentCancelBody | None = None,
) -> dict[str, Any]:
    """진행 중 턴을 멈춘다(D65). 결정과 같은 주체(소유자 또는 agent:approve)이고 감사에 남는다.
    스트림은 `error{code: turn_cancelled}` 로 끝난다. 본문은 없어도 되고, `turn_id` 를 주면 그
    시도일 때만 멈춘다(P78 2절). 진행 중 턴이 없거나 다른 시도면 409 no_turn_in_progress, 기록에
    `cancel` 이 없으면 런타임을 부르지 않고 409 runtime_feature_missing 이다. 롤링 중에 `cancel` 을
    모르는 옛 파드가 받으면 409 runtime_rolling 이다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await cancel_with(doc, tenant, user, service, session_id, body)


async def cancel_with(
    doc: Agent,
    tenant: str,
    user: Principal,
    service: AgentService,
    session_id: str,
    body: AgentCancelBody | None,
) -> dict[str, Any]:
    """찾은 문서의 진행 중 턴 멈추기. Agent 라우트와 미리 보기 라우트가 함께 쓴다."""
    require_runtime_feature(doc, CANCEL)
    st, out = await service.proxy.cancel(
        doc,
        session_id,
        user_sub=user.sub,
        approver=is_approver(user, tenant),
        turn_id=body.turn_id if body is not None else None,
    )
    expect_status(st, out, ok=status.HTTP_202_ACCEPTED, feature=CANCEL)
    return out


# -- 장기 기억 (D34) --------------------------------------------------------------------------


@router.delete(
    "/{agent_id}/memories/{user_sub}",
    response_model=AgentMemoriesDeleteResponse,
    dependencies=[CHAT],
)
async def delete_agent_memories(
    tenant: Tenant, user: User, store: Agents, service: Service, agent_id: str, user_sub: str
) -> dict[str, Any]:
    """한 사람의 장기 기억을 지운다. 자기 것이거나 tenant admin 이다 (D34).

    대상을 쿼리가 아니라 경로에 두는 것은 쿼리로 받으면 감사 기록에 무엇을 지웠는지 안 남기
    때문이다. 감사는 미들웨어가 자동으로 남긴다. 요구하는 scope 를 `agent:chat` 으로 둔 것은
    기억을 남기는 것이 대화이고, 자기가 남긴 것은 자기가 지울 수 있어야 하기 때문이다(viewer 도
    `agent:chat` 을 갖는다, D35).
    """
    if user_sub != user.sub and not user.is_tenant_admin(tenant):
        raise error(
            403,
            "not_memory_owner",
            "long-term memory belongs to its user; only that user or a tenant admin can delete it",
        )
    doc = await get_agent_or_404(store, tenant, agent_id)
    deleted = await service.delete_memories(doc, user_sub=user_sub)
    return {"agent_id": doc.id, "deleted": deleted}


# -- initContainer 가 읽는 agent.json 과 Skill -------------------------------------------------


async def _pinned_agent(
    store: MongoAgentStore, tokens: MongoAgentInstanceTokenStore, agent_id: str, request: Request
) -> Agent:
    """요청의 Bearer 로 `pinned_agent` 를 본다."""
    return await pinned_agent(store, tokens, agent_id, bearer_token(request))


@router.get("/{agent_id}/agent.json", response_model=AgentJson)
async def read_agent_json(
    store: Agents,
    tokens: Tokens,
    service: Service,
    agent_id: str,
    request: Request,
    llm: Literal["gateway"] | None = None,
    gateway_auth: Literal["bearer", "oauth"] | None = None,
) -> dict[str, Any]:
    """initContainer 용. 응답은 agent-runtime/schema/agent.schema.json 을 따르는 compile 결과다.
    이미지가 모르는 `required` 기능이 있으면 409 runtime_image_too_old 다(D65). init 이 실은 이름
    헤더(D65)는 인증된 뒤에 서비스가 읽는다. `llm` 은 그 파드 템플릿의 LLM 경로이고 없으면 직접
    경로다(P38 6절). `gateway_auth` 는 그 템플릿이 env 로 실은 Gateway 소스 인증이고 없으면 지금의
    모드가 정한다. 다른 값은 422 다."""
    doc = await _pinned_agent(store, tokens, agent_id, request)
    return await service.fetch_config(
        doc, request.headers, gateway=llm == "gateway", gateway_auth=gateway_auth
    )


@router.get("/{agent_id}/skills/{name}", response_model=SkillBundleOut)
async def read_agent_skill(
    store: Agents, tokens: Tokens, service: Service, agent_id: str, name: str, request: Request
) -> dict[str, Any]:
    """initContainer 의 Skill 받기다(D64). agent.json 과 같은 토큰과 핀 scope 를 보고, 핀된 리비전이
    그 이름으로 붙인 Skill 리비전 하나를 준다. 핀에 없는 이름은 404 다."""
    doc = await _pinned_agent(store, tokens, agent_id, request)
    return await service.skill_bundle(doc, name)
