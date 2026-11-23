"""Gateway API (D42). 정의와 vMCP 배포가 한 리소스 아래에 있다. 만들기는 저장이고 배포는 따로
누른다."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._advanced import overrides_manifest
from daegam.api._common import error, forbidden, not_found, or_404, references_out
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.hub import item_out_live
from daegam.api.mcp_servers import McpMetrics
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.gateways import (
    CloneRequest,
    DeployRequest,
    GatewayDeleteResponse,
    GatewayDeployed,
    GatewayOut,
    GatewayPreviewResponse,
    GatewayRevisionOut,
    GatewaySaved,
)
from daegam.api.schemas.hub import HubItemWithStatus
from daegam.api.schemas.mcp_metrics import GatewayMetricsOut, McpMetricsWindow
from daegam.api.schemas.mcp_servers import (
    DriftAbsorbResponse,
    ManifestResponse,
    OverridesResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.api.usage import Tz, checked_zone
from daegam.auth.cedar import MAX_POLICY_CHARS
from daegam.auth.hub_access import Access
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.services.exposure import ExposureInput
from daegam.services.gateway_timeouts import snapshot_timeout
from daegam.services.gateways import GatewayService, spec_of_revision
from daegam.store.base import iso_utc
from daegam.store.gateways import (
    ConflictResolution,
    FailureMode,
    Gateway,
    GatewayBackend,
    GatewayRuntime,
    GatewaySpec,
    GatewaySurface,
    MongoGatewayStore,
)

router = APIRouter(responses=ERROR_RESPONSES, prefix="/gateways", tags=["gateways"])


def get_store(request: Request) -> MongoGatewayStore:
    return request.app.state.gateways


def get_service(request: Request) -> GatewayService:
    return request.app.state.gateway_service


Gateways = Annotated[MongoGatewayStore, Depends(get_store)]
Service = Annotated[GatewayService, Depends(get_service)]
# viewer 는 읽기만, developer 이상이 저장·배포·재배포·해제·publish·overrides·drift 를 한다 (D23)
READ = Depends(require_scope("gateway:read"))
WRITE = Depends(require_scope("gateway:write"))
DEPLOY = Depends(require_scope("gateway:deploy"))


class GatewayBody(BaseModel):
    """POST 와 PUT 본문. PUT 의 name 은 저장값과 같아야 한다 (422 name_immutable, D24).
    display_name 은 POST 에서 없으면 name 이고 PUT 에서 없으면 그대로다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    backends: list[GatewayBackend] = Field(default_factory=list)
    conflict_resolution: ConflictResolution = "auto"
    failure_mode: FailureMode = "best_effort"
    # 사람이 쓴 Cedar 글. 길이 상한은 Cedar 엔진을 지키는 울타리 하나다(T63, auth/cedar.py)
    authz_policy: str | None = Field(default=None, max_length=MAX_POLICY_CHARS)
    runtime: GatewayRuntime = Field(default_factory=GatewayRuntime)
    # resources·prompts 를 내보일지 (P50 13절). POST 에서 없으면 둘 다 끄고 PUT 에서 없으면 그대로다
    surface: GatewaySurface | None = None
    # code mode (P56 6.2절). POST 에서 없으면 끄고 PUT 에서 없으면 그대로다
    code_mode: bool | None = None


class GatewayBasicPatch(BaseModel):
    """Basic 편집 (docs/design/resource-model.md 13절): 노출 설정 수정 + 재배포. draft 면 409
    not_deployed."""

    model_config = ConfigDict(extra="forbid")

    exposure: ExposureInput | None = None


class OverridesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    yaml: str | None = None


class PublishBody(BaseModel):
    """name 기본값은 문서 name 그대로, display_name 기본값은 문서 display_name (D24)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    display_name: str | None = None
    # 게시 방식은 entry 하나다. 다른 값은 서비스가 422 invalid_value 로 답하도록 문자열로 받는다
    mode: str = Field(default="entry", json_schema_extra={"enum": ["entry"]})
    # 접근 정책 (D23). published 는 다른 테넌트에 열 수 없다 (422 cross_tenant_unsupported)
    access: Access | None = None


def _spec(body: GatewayBody, current: GatewaySpec | None = None) -> GatewaySpec:
    """본문의 정의. surface 를 보내지 않으면 PUT 은 저장된 값을 그대로 두고, POST 는 둘 다 끈다.
    그래야 표면을 모르는 클라이언트의 저장이 열린 표면을 닫지 않는다. code_mode 도 같다. backend 가
    timeout_seconds 를 보내지 않으면 PUT 은 같은 허브 항목의 저장된 값을 그대로 둔다."""
    if body.surface is not None:
        surface: GatewaySurface = body.surface
    else:
        surface = current.surface if current is not None else GatewaySurface()
    if body.code_mode is not None:
        code_mode = body.code_mode
    else:
        code_mode = current.code_mode if current is not None else False
    backends = body.backends
    if current is not None:
        # tool_rules 도 같다. 규칙을 모르는 클라이언트의 저장이 규칙을 지우지 않는다 (P52 1절)
        stored = {b.hub_item_id: b for b in current.backends}
        kept = ("timeout_seconds", "tool_rules")
        backends = [
            b.model_copy(
                update={
                    f: getattr(stored[b.hub_item_id], f)
                    for f in kept
                    if f not in b.model_fields_set
                }
            )
            if b.hub_item_id in stored
            else b
            for b in backends
        ]
    return GatewaySpec(
        backends=backends,
        conflict_resolution=body.conflict_resolution,
        failure_mode=body.failure_mode,
        authz_policy=body.authz_policy,
        runtime=body.runtime,
        surface=surface,
        code_mode=code_mode,
    )


def _out(
    doc: Gateway,
    service: GatewayService,
    status: dict[str, Any] | None = None,
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
        **cur.model_dump(mode="json", exclude={"revision", "created_at"}),
        "auth": service.auth_boundary(),
        "revisions": [r.revision for r in doc.revisions],
        "cluster_id": doc.cluster_id,
        "k8s": doc.k8s.model_dump(mode="json") if doc.k8s is not None else None,
        # 타임아웃은 렌더하는 값이다. 이 필드가 생기기 전의 스냅샷도 고정값으로 채운다 (P50 7절)
        "deployed_backends": [
            {**b.model_dump(mode="json"), "timeout_seconds": snapshot_timeout(b)}
            for b in doc.deployed_backends
        ],
        "deployed_conflict_resolution": doc.deployed_conflict_resolution,
        "tool_overrides": doc.tool_overrides,
        "exposure": doc.exposure.model_dump(mode="json"),
        "k8s_uid": doc.k8s_uid,
        "svc_token_rev": doc.svc_token_rev,
        "effective_config": doc.effective_config,
        "overrides": doc.overrides,
        "last_error": doc.last_error,
        "last_spec_change": iso_utc(doc.last_spec_change),
        "status": status,
        "created_at": iso_utc(doc.created_at),
        "updated_at": iso_utc(doc.updated_at),
        "updated_by": doc.updated_by,
    }
    if warnings is not None:
        d["warnings"] = warnings
    return d


async def _get_or_404(store: MongoGatewayStore, tenant: str, gateway_id: str) -> Gateway:
    return or_404(await store.get(gateway_id, tenant=tenant), "gateway", gateway_id)


# -- 정의 --------------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=GatewaySaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def create_gateway(
    tenant: Tenant, service: Service, body: GatewayBody, user: User
) -> dict[str, Any]:
    """정의만 받아 draft 로 저장한다. 배포는 `POST /{id}/deploy` 다."""
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    doc, warnings = await service.create(
        tenant,
        name=name,
        display_name=display_name,
        spec=_spec(body),
        principal=user,
        user_sub=user.sub,
    )
    return _out(doc, service, warnings=warnings)


@router.get(
    "",
    response_model=list[GatewayOut],
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def list_gateways(tenant: Tenant, store: Gateways, service: Service) -> list[dict[str, Any]]:
    """draft 를 포함한 전부. status 는 배포된 것만 매 요청 CR 을 조회한다."""
    docs = await store.list(tenant, include_draft=True)
    states = await asyncio.gather(*(service.status(d) for d in docs))
    return [_out(d, service, st) for d, st in zip(docs, states, strict=True)]


@router.get(
    "/{gateway_id}",
    response_model=GatewayOut,
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def get_gateway(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, gateway_id)
    return _out(doc, service, await service.status(doc))


@router.get("/{gateway_id}/references", response_model=ReferencesResponse, dependencies=[READ])
async def gateway_references(
    tenant: Tenant, store: Gateways, gateway_id: str, request: Request
) -> dict[str, Any]:
    """이 Gateway 의 참조 (D43). Tool 소스로 쓰는 Agent 와 이 Gateway 가 허브에 올린 항목이다."""
    doc = await _get_or_404(store, tenant, gateway_id)
    return await references_out(request, "gateway", doc.id, tenant=tenant)


@router.get("/{gateway_id}/metrics", response_model=GatewayMetricsOut, dependencies=[READ])
async def gateway_metrics(
    tenant: Tenant,
    store: Gateways,
    reports: McpMetrics,
    gateway_id: str,
    window: McpMetricsWindow = "24h",
    tz: Tz = "UTC",
) -> dict[str, Any]:
    """이 Gateway 의 vMCP 를 지난 Tool 호출의 수·결과·걸린 시간과 백엔드별 행 (P51 4절). 감사
    이벤트의 시간 묶음에서 읽는다. 창은 24h·7d·30d 이고 날은 tz 로 자른다. 같은 (문서, 창,
    시간대)는 15초 기억한다."""
    checked_zone(tz)
    doc = await _get_or_404(store, tenant, gateway_id)
    return await reports.gateway(doc, window=window, tz=tz)


@router.put(
    "/{gateway_id}",
    response_model=GatewaySaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def put_gateway(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    body: GatewayBody,
    user: User,
) -> dict[str, Any]:
    """전체 교체 = 새 리비전. 배포 핀은 그대로라 "r5 저장됨, r2 배포됨" 이 된다. name 은 바꿀 수
    없고 (422 name_immutable) display_name 은 바뀐다 (D24)."""
    doc = await _get_or_404(store, tenant, gateway_id)
    check_name_unchanged(body.name, doc.name)
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    before = spec_of_revision(doc.current)
    doc, warnings = await service.save(
        doc,
        display_name=display_name,
        spec=_spec(body, doc.current),
        principal=user,
        user_sub=user.sub,
    )
    out = _out(doc, service, await service.status(doc), warnings)
    out["rules_notice"] = (await service.rules_notice(doc, before)).to_dict()
    if closed := await service.consumer_notice(doc, before):
        out["consumer_notice"] = {"closed": closed}
    return out


@router.delete("/{gateway_id}", response_model=GatewayDeleteResponse, dependencies=[WRITE])
async def delete_gateway(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str, user: User
) -> dict[str, Any]:
    """정의와 클러스터의 것을 모두 지운다. draft 는 gateway:write 로 되고, 배포 쪽이 있으면
    gateway:deploy 도 있어야 한다 (403 의 모양은 require_scope 와 같다)."""
    doc = await _get_or_404(store, tenant, gateway_id)
    if doc.is_deployed and "gateway:deploy" not in user.scopes_in(tenant):
        raise forbidden("scope gateway:deploy required", scope="gateway:deploy", tenant=tenant)
    state_before = doc.state
    k8s = doc.k8s.model_dump(mode="json") if doc.k8s is not None else None
    # 남이 쥔 클레임에 일을 남겼으면 deleted 가 거짓이고 문서는 deleting 이다 (P66 2.6절)
    deleted = await service.delete(doc)
    return {"id": doc.id, "deleted": deleted, "state_before": state_before, "k8s": k8s}


@router.get(
    "/{gateway_id}/revisions",
    response_model=list[GatewayRevisionOut],
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def list_revisions(tenant: Tenant, store: Gateways, gateway_id: str) -> list[dict[str, Any]]:
    doc = await _get_or_404(store, tenant, gateway_id)
    return [r.model_dump(mode="json") for r in doc.revisions]


@router.get(
    "/{gateway_id}/revisions/{n}",
    response_model=GatewayRevisionOut,
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def get_revision(tenant: Tenant, store: Gateways, gateway_id: str, n: int) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, gateway_id)
    rev = doc.get_revision(n)
    if rev is None:
        raise not_found(f"revision {n} not found")
    return rev.model_dump(mode="json")


@router.get(
    "/{gateway_id}/revisions/{n}/preview",
    response_model=GatewayPreviewResponse,
    dependencies=[READ],
)
async def preview_revision(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    n: int,
    user: User,
    cluster_id: str | None = None,
) -> dict[str, Any]:
    """이 리비전을 배포하면 생길 문서 묶음 (apply 없음). draft 에서도 된다."""
    doc = await _get_or_404(store, tenant, gateway_id)
    rev = doc.get_revision(n)
    if rev is None:
        raise not_found(f"revision {n} not found")
    return await service.preview(doc, rev, cluster_id, principal=user)


@router.post(
    "/{gateway_id}/clone",
    status_code=status.HTTP_201_CREATED,
    response_model=GatewaySaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def clone_gateway(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    body: CloneRequest,
    user: User,
) -> dict[str, Any]:
    """현재 리비전의 정의를 베낀 draft. exposure 를 주면 배포 설정으로 남는다."""
    doc = await _get_or_404(store, tenant, gateway_id)
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    copy, warnings = await service.clone(
        doc,
        name=name,
        display_name=display_name,
        exposure=body.exposure,
        principal=user,
        user_sub=user.sub,
    )
    return _out(copy, service, warnings=warnings)


# -- 배포 --------------------------------------------------------------------------------


@router.post(
    "/{gateway_id}/deploy",
    status_code=status.HTTP_201_CREATED,
    response_model=GatewayDeployed,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def deploy_gateway(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    body: DeployRequest,
    user: User,
) -> dict[str, Any]:
    """현재 리비전을 핀해 vMCP 를 띄운다. 클러스터 객체가 새로 생기므로 201 이다. draft 가
    아니면 409 already_deployed."""
    doc = await _get_or_404(store, tenant, gateway_id)
    doc, warnings = await service.deploy(
        doc,
        cluster_id=body.cluster_id,
        namespace=body.namespace,
        exposure=body.exposure,
        exposure_sent="exposure" in body.model_fields_set,
        principal=user,
    )
    doc = await service.provision(doc)
    return _out(doc, service, await service.status(doc), warnings)


@router.post(
    "/{gateway_id}/redeploy",
    response_model=GatewayDeployed,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def redeploy_gateway(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str, user: User
) -> dict[str, Any]:
    """핀을 현재 리비전으로 옮기고 스냅샷을 다시 만들어 적용한다."""
    doc = await _get_or_404(store, tenant, gateway_id)
    doc, warnings = await service.redeploy(doc, principal=user)
    return _out(doc, service, await service.status(doc), warnings)


@router.post(
    "/{gateway_id}/undeploy",
    response_model=GatewayOut,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def undeploy_gateway(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str
) -> dict[str, Any]:
    """클러스터에서 내리고 정의는 남긴다. 게시돼 있으면 409 published, Agent 가 참조 중이면 409
    in_use."""
    doc = await _get_or_404(store, tenant, gateway_id)
    doc = await service.undeploy(doc)
    return _out(doc, service, None)


@router.patch(
    "/{gateway_id}",
    response_model=GatewayDeployed,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def patch_gateway(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    patch: GatewayBasicPatch,
    user: User,
) -> dict[str, Any]:
    """배포 쪽 Basic 편집: 노출 설정을 바꾸고 재배포한다. draft 면 409 not_deployed."""
    doc = await _get_or_404(store, tenant, gateway_id)
    if "exposure" not in patch.model_fields_set:
        raise error(422, "empty_patch", "no fields to update")
    doc, warnings = await service.set_exposure(doc, patch.exposure, principal=user)
    return _out(doc, service, await service.status(doc), warnings)


@router.post(
    "/{gateway_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[DEPLOY],
)
async def publish_gateway(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    body: PublishBody,
    user: User,
) -> dict[str, Any]:
    """이 문서의 vMCP 를 허브 published 항목으로 게시한다 (docs/guide/hub-gateway.md 6절).
    `deployed` 만 된다."""
    doc = await _get_or_404(store, tenant, gateway_id)
    name = validate_name(body.name) if body.name is not None else None
    display_name = validate_display_name(body.display_name, default=doc.display_name)
    item = await service.publish(
        doc,
        name=name,
        display_name=display_name,
        mode=body.mode,
        access=body.access,
        principal=user,
    )
    return await item_out_live(service.hub, item, principal=user, tenant=tenant)


@router.get("/{gateway_id}/manifest", response_model=ManifestResponse, dependencies=[READ])
async def get_manifest(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str
) -> dict[str, Any]:
    """문서 묶음. draft 면 409 not_deployed."""
    doc = await _get_or_404(store, tenant, gateway_id)
    return await service.manifest(doc)


@router.put("/{gateway_id}/overrides", response_model=OverridesResponse, dependencies=[DEPLOY])
async def put_overrides(
    tenant: Tenant, store: Gateways, service: Service, gateway_id: str, body: OverridesBody
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, gateway_id)
    return await service.set_overrides(doc, patch=body.patch, manifest=overrides_manifest(body))


@router.post(
    "/{gateway_id}/drift/{action}",
    response_model=ManifestResponse | DriftAbsorbResponse,
    dependencies=[DEPLOY],
)
async def drift_action(
    tenant: Tenant,
    store: Gateways,
    service: Service,
    gateway_id: str,
    action: Literal["reset", "absorb"],
) -> dict[str, Any]:
    doc = await _get_or_404(store, tenant, gateway_id)
    if action == "reset":
        return await service.drift_reset(doc)
    return await service.drift_absorb(doc)
