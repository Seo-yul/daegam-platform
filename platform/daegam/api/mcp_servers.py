"""MCP Server API (D42). 정의와 배포가 한 리소스 아래에 있다. 만들기는 저장이고 배포는 따로
누른다."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._advanced import overrides_manifest
from daegam.api._common import (
    error,
    forbidden,
    not_found,
    or_404,
    patched_fields,
    references_out,
    unauthorized,
)
from daegam.api.deps import (
    BackendAPIs,
    Tenant,
    User,
    authenticate_api_token,
    bearer_token,
    require_scope,
)
from daegam.api.hub import item_out_live, require_hub_permission, unpublish_item
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse
from daegam.api.schemas.hub import HubItemWithStatus
from daegam.api.schemas.mcp_metrics import McpMetricsOut, McpMetricsWindow
from daegam.api.schemas.mcp_servers import (
    ApplyDraftRequest,
    CloneRequest,
    DeployRequest,
    DriftAbsorbResponse,
    ManifestResponse,
    MCPServerDeleteResponse,
    MCPServerDeployed,
    MCPServerOut,
    MCPServerRevisionOut,
    MCPServerSaved,
    OverridesResponse,
    PreviewResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.api.usage import Tz, checked_zone
from daegam.auth.hub_access import Access, permissions_dict
from daegam.error_codes import status_of
from daegam.k8s.preflight import preflight_warnings
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.pem import ca_sha256
from daegam.services.exposure import ExposureInput
from daegam.services.mcp_metrics.reports import McpMetricsReports
from daegam.services.mcp_servers import MCPServerService, redeploy_warnings, spec_of_revision
from daegam.services.mcp_spec import ProfileValidationError, render_profile_json, scope_for
from daegam.store.base import iso_utc
from daegam.store.mcp_servers import (
    BackendSelection,
    MCPServer,
    MCPServerRevision,
    MCPServerSpec,
    MongoMCPServerStore,
    ProfileRuntimeSpec,
    ToolOverride,
)
from daegam.store.tenants import Binding

router = APIRouter(responses=ERROR_RESPONSES, prefix="/mcp-servers", tags=["mcp-servers"])


def get_store(request: Request) -> MongoMCPServerStore:
    return request.app.state.mcp_servers


def get_service(request: Request) -> MCPServerService:
    return request.app.state.mcp_server_service


def get_mcp_metrics(request: Request) -> McpMetricsReports:
    return request.app.state.mcp_metrics_reports


Servers = Annotated[MongoMCPServerStore, Depends(get_store)]
Service = Annotated[MCPServerService, Depends(get_service)]
# MCP Server 와 Gateway 의 메트릭 (P51 4절). Gateway 라우터도 이것을 쓴다
McpMetrics = Annotated[McpMetricsReports, Depends(get_mcp_metrics)]


class MCPServerBody(BaseModel):
    """POST 와 PUT 본문. PUT 의 name 은 저장값과 같아야 한다 (422 name_immutable, D24).
    display_name 은 POST 에서 없으면 name 이고 PUT 에서 없으면 그대로다.

    backends 목록이 기본 형태다. 예전 단수 필드(backend_profile_id, spec_version,
    tools, tool_overrides)도 당분간 받아 한 항목 목록으로 정규화한다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    backends: list[BackendSelection] | None = None
    backend_profile_id: str | None = None
    spec_version: int | None = Field(default=None, ge=1)
    tools: list[str] = Field(default_factory=list)
    tool_overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    runtime: ProfileRuntimeSpec = Field(default_factory=ProfileRuntimeSpec)
    bindings: list[Binding] = Field(default_factory=list)


def _spec_from(body: MCPServerBody, current: MCPServerSpec | None = None) -> MCPServerSpec:
    """본문의 정의. backend 가 timeout_seconds 를 보내지 않으면 PUT 은 같은 Backend API 의 저장된
    값을 그대로 둔다. 그래야 타임아웃을 모르는 클라이언트의 저장이 그 값을 기본값으로 되돌리지
    않는다 (P50 7절)."""
    if body.backends:
        if body.backend_profile_id is not None or body.spec_version is not None:
            raise error(
                422,
                "invalid_backends",
                "send either backends or the legacy singular fields, not both",
            )
        backends = body.backends
    else:
        if body.backend_profile_id is None or body.spec_version is None:
            raise error(422, "invalid_backends", "backends must have at least one entry")
        backends = [
            BackendSelection(
                backend_profile_id=body.backend_profile_id,
                spec_version=body.spec_version,
                tools=body.tools,
                tool_overrides=body.tool_overrides,
            )
        ]
    if current is not None:
        # timeout_seconds 와 tool_rules 를 보내지 않은 backend 는 같은 Backend API 의 저장된 값을
        # 그대로 둔다. 규칙을 모르는 클라이언트의 저장이 규칙을 지우지 않는다 (P52 1절)
        stored = {b.backend_profile_id: b for b in current.backends}
        kept = ("timeout_seconds", "tool_rules")
        backends = [
            b.model_copy(
                update={
                    f: getattr(stored[b.backend_profile_id], f)
                    for f in kept
                    if f not in b.model_fields_set
                }
            )
            if b.backend_profile_id in stored
            else b
            for b in backends
        ]
    return MCPServerSpec(backends=backends, runtime=body.runtime, bindings=body.bindings)


def _rev_out(r: MCPServerRevision) -> dict[str, Any]:
    return r.model_dump(mode="json")


def _effective_out(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """API 가 내는 배포 스냅샷. CA 묶음(PEM, 64 KiB 까지)은 목록의 항목마다 실리지 않게 지문
    `ca_bundle_sha256` 으로 바꾼다 (D88)."""
    if not config or "ca_bundle" not in config:
        return config
    out = {k: v for k, v in config.items() if k != "ca_bundle"}
    out["ca_bundle_sha256"] = ca_sha256(config["ca_bundle"])
    return out


def _out(
    doc: MCPServer,
    status: dict[str, Any] | None = None,
    hub_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cur = doc.current
    return {
        "id": doc.id,
        "tenant": doc.tenant,
        "name": doc.name,
        "display_name": doc.display_name,
        "state": doc.state,
        "revision": doc.revision,
        "deployed_revision": doc.deployed_revision,
        "backends": [b.model_dump(mode="json") for b in cur.backends],
        "runtime": cur.runtime.model_dump(mode="json"),
        "bindings": [b.model_dump(mode="json") for b in cur.bindings],
        "revisions": [r.revision for r in doc.revisions],
        "cluster_id": doc.cluster_id,
        "k8s": doc.k8s.model_dump(mode="json") if doc.k8s is not None else None,
        "exposure": doc.exposure.model_dump(mode="json"),
        "effective_config": _effective_out(doc.effective_config),
        "spec_token_id": doc.spec_token_id,
        "spec_token_ids": list(doc.spec_token_ids),
        "k8s_uid": doc.k8s_uid,
        "svc_token_rev": doc.svc_token_rev,
        "overrides": doc.overrides,
        "last_error": doc.last_error,
        "last_spec_change": iso_utc(doc.last_spec_change),
        "status": status,
        "hub_item": hub_item,
        "created_at": iso_utc(doc.created_at),
        "updated_at": iso_utc(doc.updated_at),
        "updated_by": doc.updated_by,
    }


async def _hub_refs(
    service: MCPServerService, docs: list[MCPServer], *, principal: Any, tenant: str
) -> dict[str, dict[str, Any]]:
    """문서 id → 게시 항목 요약 (hub_item). 허브 항목은 한 번에 읽고 참조는 병렬로 센다
    (docs/guide/hub-gateway.md 6절)."""
    hub = service.hub
    if hub is None or not docs:
        return {}
    items = await hub.store.by_instances([d.id for d in docs])
    users = await asyncio.gather(
        *(service.references.gateway_ids_using_hub_item(item.id) for item in items.values())
    )
    return {
        doc_id: {
            "id": item.id,
            "name": item.name,
            "display_name": item.display_name,
            "permissions": permissions_dict(principal, tenant, item),
            "gateway_ids": used_by,
        }
        for (doc_id, item), used_by in zip(items.items(), users, strict=True)
    }


async def _out_with_hub(
    service: MCPServerService, doc: MCPServer, *, principal: Any, tenant: str
) -> dict[str, Any]:
    refs = await _hub_refs(service, [doc], principal=principal, tenant=tenant)
    return _out(doc, await service.status(doc), refs.get(doc.id))


async def _get_or_404(store: MongoMCPServerStore, tenant: str, server_id: str) -> MCPServer:
    return or_404(await store.get(server_id, tenant=tenant), "mcp server", server_id)


# -- 정의 --------------------------------------------------------------------------------


@router.post(
    "",
    response_model=MCPServerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def create_server(
    tenant: Tenant, user: User, service: Service, body: MCPServerBody
) -> dict[str, Any]:
    """정의만 받아 draft 로 저장한다. 배포는 `POST /{id}/deploy` 다."""
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    doc = await service.create(
        tenant, name=name, display_name=display_name, spec=_spec_from(body), user_sub=user.sub
    )
    return _out(doc)


@router.get(
    "",
    response_model=list[MCPServerOut],
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def list_servers(
    tenant: Tenant, store: Servers, service: Service, user: User
) -> list[dict[str, Any]]:
    """draft 를 포함한 전부. status 는 배포된 것만 매 요청 CR 을 조회한다."""
    docs = await store.list(tenant, include_draft=True)
    refs = await _hub_refs(service, docs, principal=user, tenant=tenant)
    states = await asyncio.gather(*(service.status(d) for d in docs))
    return [_out(d, st, refs.get(d.id)) for d, st in zip(docs, states, strict=True)]


@router.get(
    "/{server_id}",
    response_model=MCPServerOut,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def get_server(
    tenant: Tenant, store: Servers, service: Service, server_id: str, user: User
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, server_id)
    return await _out_with_hub(service, doc, principal=user, tenant=tenant)


@router.get(
    "/{server_id}/references",
    response_model=ReferencesResponse,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def server_references(
    tenant: Tenant, store: Servers, server_id: str, request: Request
) -> dict[str, Any]:
    """이 MCP Server 의 참조 (D43). 허브 항목과 그 항목을 묶은 Gateway, Tool 소스로 쓰는
    Agent 다."""
    doc = await _get_or_404(store, tenant, server_id)
    return await references_out(request, "mcp-server", doc.id, tenant=tenant)


@router.get(
    "/{server_id}/metrics",
    response_model=McpMetricsOut,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def server_metrics(
    tenant: Tenant,
    store: Servers,
    reports: McpMetrics,
    server_id: str,
    window: McpMetricsWindow = "24h",
    tz: Tz = "UTC",
) -> dict[str, Any]:
    """이 MCP Server 프록시를 지난 Tool 호출의 수·결과·걸린 시간 (P51 4절). 감사 이벤트의 시간
    묶음에서 읽는다. 창은 24h·7d·30d 이고 날은 tz 로 자른다. 같은 (문서, 창, 시간대)는 15초
    기억한다."""
    checked_zone(tz)
    doc = await _get_or_404(store, tenant, server_id)
    return await reports.mcp_server(doc, window=window, tz=tz)


@router.put(
    "/{server_id}",
    response_model=MCPServerSaved,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def put_server(
    tenant: Tenant,
    user: User,
    store: Servers,
    service: Service,
    server_id: str,
    body: MCPServerBody,
) -> dict[str, Any]:
    """전체 교체 = 새 리비전. 배포 핀은 그대로라 "r5 저장됨, r2 배포됨" 이 된다. name 은 바꿀 수
    없고 (422 name_immutable) display_name 은 바뀐다 (D24)."""
    doc = await _get_or_404(store, tenant, server_id)
    check_name_unchanged(body.name, doc.name)
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    before = spec_of_revision(doc.current)
    doc = await service.save(
        doc, display_name=display_name, spec=_spec_from(body, doc.current), user_sub=user.sub
    )
    out = await _out_with_hub(service, doc, principal=user, tenant=tenant)
    out["rules_notice"] = (await service.rules_notice(doc, before)).to_dict()
    return out


@router.delete(
    "/{server_id}",
    response_model=MCPServerDeleteResponse,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def delete_server(
    tenant: Tenant, user: User, store: Servers, service: Service, server_id: str
) -> dict[str, Any]:
    """정의와 클러스터의 것을 모두 지운다. draft 는 mcp:write 로 되고, 배포 쪽이 있으면
    mcp:deploy 도 있어야 한다 (403 의 모양은 require_scope 와 같다). 게시 항목도 함께 지우므로
    게시돼 있으면 그 항목의 manage 를 게시 해제와 같이 본다."""
    doc = await _get_or_404(store, tenant, server_id)
    if doc.is_deployed and "mcp:deploy" not in user.scopes_in(tenant):
        raise forbidden("scope mcp:deploy required", scope="mcp:deploy", tenant=tenant)
    item = await service.hub_item(doc)
    if item is not None:
        require_hub_permission(user, tenant, item, "manage")
    state_before = doc.state
    token_ids = doc.token_ids
    k8s = doc.k8s.model_dump(mode="json") if doc.k8s is not None else None
    # 남이 쥔 클레임에 일을 남겼으면 deleted 가 거짓이고 문서는 deleting 이다 (P66 2.6절)
    deleted = await service.delete(doc)
    return {
        "id": doc.id,
        "deleted": deleted,
        "state_before": state_before,
        "revoked_token_ids": token_ids if deleted else [],
        "k8s": k8s,
    }


@router.post(
    "/{server_id}/apply-draft",
    response_model=MCPServerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def apply_draft(
    tenant: Tenant,
    user: User,
    store: Servers,
    service: Service,
    server_id: str,
    body: ApplyDraftRequest,
    request: Request,
) -> JSONResponse:
    """overrides job 의 changes 중 accepted_ids(비면 전부) 를 심어 새 리비전을 만든다 (D37).
    채택 결과가 지금과 같으면 200 이고 리비전은 늘지 않는다."""
    doc = await _get_or_404(store, tenant, server_id)
    job = await request.app.state.spec_jobs.get(body.job_id, tenant=tenant)
    if job is None:
        raise error(422, "job_not_found", f"spec job {body.job_id} not found")
    doc, created = await service.apply_draft(
        doc,
        job=job,
        job_files=request.app.state.spec_job_files,
        accepted_ids=body.accepted_ids,
        user_sub=user.sub,
    )
    out = await _out_with_hub(service, doc, principal=user, tenant=tenant)
    return JSONResponse(status_code=201 if created else 200, content=out)


@router.get(
    "/{server_id}/revisions",
    response_model=list[MCPServerRevisionOut],
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def list_revisions(tenant: Tenant, store: Servers, server_id: str) -> list[dict[str, Any]]:
    doc = await _get_or_404(store, tenant, server_id)
    return [_rev_out(r) for r in doc.revisions]


@router.get(
    "/{server_id}/revisions/{n}",
    response_model=MCPServerRevisionOut,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def get_revision(tenant: Tenant, store: Servers, server_id: str, n: int) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, server_id)
    rev = doc.get_revision(n)
    if rev is None:
        raise not_found(f"revision {n} not found")
    return _rev_out(rev)


@router.post(
    "/{server_id}/revisions/{n}/preview",
    response_model=PreviewResponse,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def preview_revision(
    tenant: Tenant,
    store: Servers,
    service: Service,
    server_id: str,
    n: int,
    cluster_id: str | None = None,
) -> dict[str, Any]:
    """이 리비전을 배포하면 생길 MCPServer CR (apply 없음). F7a."""
    doc = await _get_or_404(store, tenant, server_id)
    rev = doc.get_revision(n)
    if rev is None:
        raise not_found(f"revision {n} not found")
    out = await service.preview(doc, rev, cluster_id)
    out["effective_config"] = _effective_out(out["effective_config"])
    return out


@router.post(
    "/{server_id}/clone",
    response_model=MCPServerOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def clone_server(
    tenant: Tenant,
    user: User,
    store: Servers,
    service: Service,
    server_id: str,
    body: CloneRequest,
) -> dict[str, Any]:
    """현재 리비전의 정의를 베낀 draft. exposure 를 주면 배포 설정으로 남는다."""
    doc = await _get_or_404(store, tenant, server_id)
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    copy = await service.clone(
        doc, name=name, display_name=display_name, exposure=body.exposure, user_sub=user.sub
    )
    return _out(copy)


# -- initContainer 가 읽는 profile.json ----------------------------------------------------


async def _profile_json(
    backend: BackendAPIs, doc: MCPServer | None, n: int, presented: str
) -> dict[str, Any]:
    """토큰만 본다(테넌트 헤더 없음). 요청한 리비전(없으면 현재 스펙)의 backend 로 토큰을 인증하고,
    그 토큰의 scope 가 이 문서를 가리켜야 한다. 존재 여부를 토큰 없이 드러내지 않으므로 문서가
    없거나 토큰이 맞지 않으면 전부 401 이다."""
    if doc is None:
        raise unauthorized("invalid or revoked token")
    rev = doc.get_revision(n)
    bp_ids: list[str] = []
    for sel in rev.backends if rev else doc.spec.backends:
        if sel.backend_profile_id not in bp_ids:
            bp_ids.append(sel.backend_profile_id)
    tok = None
    for bp_id in bp_ids:
        try:
            tok = await authenticate_api_token(backend, bp_id, presented)
            break
        except HTTPException:
            continue
    if tok is None or scope_for(doc.id) not in tok.scope:
        raise unauthorized("invalid or revoked token")
    if rev is None:
        raise not_found(f"revision {n} not found")
    pairs = []
    for sel in rev.backends:
        bp = await backend.get(sel.backend_profile_id)
        version = (
            await backend.get_version(sel.backend_profile_id, sel.spec_version) if bp else None
        )
        if bp is None or version is None:
            raise error(500, "integrity_error", "backend api or version missing")
        pairs.append((bp, version))
    try:
        return render_profile_json(rev, pairs)
    except ProfileValidationError as e:
        raise HTTPException(status_of(e.code, 422), e.to_dict()) from e


@router.get("/{server_id}/revisions/{n}/profile.json")
async def read_profile_json(
    store: Servers, backend: BackendAPIs, server_id: str, n: int, request: Request
) -> dict[str, Any]:
    """initContainer 용. Backend API 토큰의 scope 에 mcp-profile:{id} 가 있어야 한다."""
    presented = bearer_token(request)
    return await _profile_json(backend, await store.get(server_id), n, presented)


# -- 배포 --------------------------------------------------------------------------------


@router.post(
    "/{server_id}/deploy",
    response_model=MCPServerDeployed,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def deploy_server(
    tenant: Tenant,
    user: User,
    store: Servers,
    service: Service,
    server_id: str,
    body: DeployRequest,
) -> dict[str, Any]:
    """현재 리비전을 핀해 클러스터에 띄운다. 클러스터 객체가 새로 생기므로 201 이다. draft 가
    아니면 409 already_deployed."""
    doc = await _get_or_404(store, tenant, server_id)
    doc, warnings = await service.deploy(
        doc,
        cluster_id=body.cluster_id,
        namespace=body.namespace,
        exposure=body.exposure,
        exposure_sent="exposure" in body.model_fields_set,
    )
    doc = await service.provision(doc)
    out = await _out_with_hub(service, doc, principal=user, tenant=tenant)
    # F3a: 클러스터가 NetworkPolicy 를 집행하지 않으면 프록시 무인증이 격리를 보장하지 않는다 (D16)
    cluster = await service.tenancy.clusters.get(doc.cluster_id)
    out["warnings"] = preflight_warnings(cluster.status.preflight if cluster else None) + warnings
    return out


@router.post(
    "/{server_id}/redeploy",
    response_model=MCPServerDeployed,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def redeploy_server(
    tenant: Tenant, store: Servers, service: Service, server_id: str, user: User
) -> dict[str, Any]:
    """핀을 현재 리비전으로 옮기고 다시 적용한다."""
    doc = await _get_or_404(store, tenant, server_id)
    before = doc.deployed_inputs
    doc = await service.redeploy(doc)
    out = await _out_with_hub(service, doc, principal=user, tenant=tenant)
    out["warnings"] = redeploy_warnings(doc, before)
    return out


@router.post(
    "/{server_id}/undeploy",
    response_model=MCPServerOut,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def undeploy_server(
    tenant: Tenant, store: Servers, service: Service, server_id: str, user: User
) -> dict[str, Any]:
    """클러스터에서 내리고 정의는 남긴다. 게시돼 있으면 409 published."""
    doc = await _get_or_404(store, tenant, server_id)
    doc = await service.undeploy(doc)
    return await _out_with_hub(service, doc, principal=user, tenant=tenant)


class PublishBody(BaseModel):
    """허브 게시 본문 (docs/guide/hub-gateway.md 6절). name 기본값은 문서 name, display_name
    기본값은 문서 display_name (D24), access 는 생략하면 빈 정책이다. internal 은 다른 테넌트에
    열 수 없다 (422 cross_tenant_unsupported)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    display_name: str | None = None
    access: Access | None = None


@router.post(
    "/{server_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("hub:write"))],
)
async def publish_server(
    tenant: Tenant,
    store: Servers,
    service: Service,
    server_id: str,
    body: PublishBody,
    response: Response,
    user: User,
) -> dict[str, Any]:
    """허브에 `internal` 항목으로 게시한다. 이미 게시돼 있으면 200 으로 그 항목을 돌려준다.
    `deployed` 만 된다 (409 not_deployed)."""
    doc = await _get_or_404(store, tenant, server_id)
    name = validate_name(body.name) if body.name is not None else None
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    item, created = await service.publish(
        doc, name=name, display_name=display_name, access=body.access, principal=user
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return await item_out_live(service.hub, item, principal=user, tenant=tenant)


@router.delete(
    "/{server_id}/publish",
    response_model=DeleteResponse,
    dependencies=[Depends(require_scope("hub:write"))],
)
async def unpublish_server(
    tenant: Tenant, store: Servers, service: Service, server_id: str, user: User
) -> dict[str, Any]:
    """게시 해제. `DELETE /hub/{id}` 와 같은 판정이다. 항목의 manage 가 없으면 403, 참조 중이면
    409 in_use, 게시돼 있지 않으면 404 not_published."""
    doc = await _get_or_404(store, tenant, server_id)
    item = await service.hub.instance_item(doc)
    return await unpublish_item(service.hub, user, tenant, item)


@router.get(
    "/{server_id}/manifest",
    response_model=ManifestResponse,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def get_manifest(
    tenant: Tenant, store: Servers, service: Service, server_id: str
) -> dict[str, Any]:
    """{documents: [{kind, name, rendered, overrides, final, live, drift, locked, ...}]}.
    draft 면 409 not_deployed."""
    doc = await _get_or_404(store, tenant, server_id)
    return await service.manifest(doc)


class OverridesBody(BaseModel):
    """patch(RFC 7386 + 키 리스트) / manifest(CR 전체 dict) / yaml(CR 전체 YAML) 중 하나."""

    model_config = ConfigDict(extra="forbid")

    patch: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    yaml: str | None = None


@router.put(
    "/{server_id}/overrides",
    response_model=OverridesResponse,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def put_overrides(
    tenant: Tenant, store: Servers, service: Service, server_id: str, body: OverridesBody
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, server_id)
    return await service.set_overrides(doc, patch=body.patch, manifest=overrides_manifest(body))


class BasicPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replicas: int | None = Field(default=None, ge=1, le=10)
    resources: dict[str, Any] | None = None
    image: str | None = None
    bindings: list[Binding] | None = None
    # 노출 설정 수정 + 재배포 (docs/design/resource-model.md 13절 Basic)
    exposure: ExposureInput | None = None


@router.patch(
    "/{server_id}",
    response_model=MCPServerDeployed,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def patch_server(
    tenant: Tenant,
    store: Servers,
    service: Service,
    server_id: str,
    patch: BasicPatch,
    user: User,
) -> dict[str, Any]:
    """배포 쪽 Basic 편집: 정의 필드는 새 리비전, 노출은 설정 갱신, 그 뒤 redeploy. draft 면
    409 not_deployed (정의는 PUT 으로 고친다)."""
    doc = await _get_or_404(store, tenant, server_id)
    sent = patched_fields(patch)
    if "image" in sent and patch.image is not None and not patch.image.strip():
        raise error(422, "invalid_value", "image cannot be empty")
    before = doc.deployed_inputs
    doc = await service.basic_patch(
        doc,
        replicas=patch.replicas,
        resources=patch.resources,
        image=patch.image.strip() if patch.image else patch.image,
        bindings=patch.bindings,
        exposure=patch.exposure,
        sent=sent,
        user_sub=user.sub,
    )
    out = await _out_with_hub(service, doc, principal=user, tenant=tenant)
    out["warnings"] = redeploy_warnings(doc, before)
    return out


@router.post(
    "/{server_id}/drift/{action}",
    response_model=ManifestResponse | DriftAbsorbResponse,
    dependencies=[Depends(require_scope("mcp:deploy"))],
)
async def drift_action(
    tenant: Tenant,
    store: Servers,
    service: Service,
    server_id: str,
    action: Literal["reset", "absorb"],
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, server_id)
    if action == "reset":
        return await service.drift_reset(doc)
    return await service.drift_absorb(doc)
