"""Data Service API. 테넌트 범위.

라우터는 본문을 읽고 이름을 검사해 응답을 만든다. 쓰기 규칙(Secret 과 문서, 참조 가드)은
`services/data_services.py` 의 `DataServiceService` 에 있다 (P75 2절).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._common import not_null, or_404, patched_fields, patched_values, references_out
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.data_services import (
    DataServiceCreated,
    DataServiceDeleteResponse,
    DataServiceOut,
    DataServicePlainConsumers,
    DataServiceVerifyResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.names import check_name_unchanged, validate_display_name, validate_name
from daegam.services.data_services import DataServiceInput, DataServiceService, TlsInput
from daegam.services.redis_tls_consumers import plain_redis_consumers, plain_row
from daegam.store.base import iso_utc
from daegam.store.data_services import DataService, MongoDataServiceStore

router = APIRouter(responses=ERROR_RESPONSES, prefix="/data-services", tags=["data-services"])


def get_ds_store(request: Request) -> MongoDataServiceStore:
    return request.app.state.data_services


def get_ds_service(request: Request) -> DataServiceService:
    return request.app.state.data_service_service


DataServices = Annotated[MongoDataServiceStore, Depends(get_ds_store)]
DataServiceSvc = Annotated[DataServiceService, Depends(get_ds_service)]


class DataServiceTlsInput(BaseModel):
    """Redis 의 TLS 끝점 (P61 2절). ca 는 서버 인증서를 확인할 CA 의 PEM 이다. 만들 때는 꼭 있어야
    하고, 고칠 때 비우면 지금 CA 를 그대로 둔다. 응답에는 PEM 대신 그 Secret 이름이 온다."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(ge=1, le=65535)
    ca: str | None = None


class DataServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None  # 없으면 name (D24)
    cluster_id: str | None = None
    kind: Literal["postgres", "redis"]
    host: str
    port: int = Field(ge=1, le=65535)
    database: str | None = None
    db: int | None = Field(default=None, ge=0)
    username: str | None = None
    password: str | None = None  # 무인증 Redis 는 생략 가능
    tls: DataServiceTlsInput | None = None  # redis 만


class DataServicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None  # 저장값과 같아야 한다 (422 name_immutable, D24)
    display_name: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    db: int | None = Field(default=None, ge=0)
    username: str | None = None
    password: str | None = None
    tls: DataServiceTlsInput | None = None  # null 이면 TLS 끝점을 지운다


def _out(ds: DataService, *, namespace: str | None = None) -> dict[str, Any]:
    d = ds.model_dump(mode="json")
    if namespace is not None:
        d["namespace"] = namespace
    return d


async def _get_or_404(store: MongoDataServiceStore, tenant: str, ds_id: str) -> DataService:
    return or_404(await store.get(ds_id, tenant=tenant), "data service", ds_id)


@router.post(
    "",
    response_model=DataServiceCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def create_data_service(
    tenant: Tenant, service: DataServiceSvc, body: DataServiceCreate, user: User
) -> dict[str, Any]:
    name = validate_name(body.name)
    fields = body.model_dump(exclude={"name", "display_name", "tls"})
    ds, namespace = await service.create(
        tenant,
        DataServiceInput(
            name=name,
            display_name=validate_display_name(body.display_name, default=name),
            tls=TlsInput(body.tls.port, body.tls.ca) if body.tls is not None else None,
            **fields,
        ),
        user_sub=user.sub,
    )
    return _out(ds, namespace=namespace)


@router.get(
    "", response_model=list[DataServiceOut], dependencies=[Depends(require_scope("mcp:read"))]
)
async def list_data_services(tenant: Tenant, store: DataServices) -> list[dict[str, Any]]:
    return [_out(d) for d in await store.list(tenant)]


@router.get(
    "/{ds_id}", response_model=DataServiceOut, dependencies=[Depends(require_scope("mcp:read"))]
)
async def get_data_service(tenant: Tenant, store: DataServices, ds_id: str) -> dict[str, Any]:
    return _out(await _get_or_404(store, tenant, ds_id))


@router.get(
    "/{ds_id}/plain-consumers",
    response_model=DataServicePlainConsumers,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def data_service_plain_consumers(
    tenant: Tenant, store: DataServices, ds_id: str, request: Request
) -> dict[str, Any]:
    ds = await _get_or_404(store, tenant, ds_id)
    if ds.kind != "redis" or ds.tls is None:
        return {"consumers": []}
    state = request.app.state
    rows = await plain_redis_consumers(
        state.agents, state.mcp_servers, store, state.clusters, tenant=tenant, data_service_id=ds.id
    )
    return {"consumers": [plain_row(r) for r in rows]}


@router.get(
    "/{ds_id}/references",
    response_model=ReferencesResponse,
    dependencies=[Depends(require_scope("mcp:read"))],
)
async def data_service_references(
    tenant: Tenant, store: DataServices, ds_id: str, request: Request
) -> dict[str, Any]:
    """이 Database 의 참조 (D43). 바인딩한 MCP Server·Agent 와 벡터 테이블을 둔 Knowledge
    Base 다. 읽기 scope 는 목록·상세와 같은 `mcp:read` 다."""
    ds = await _get_or_404(store, tenant, ds_id)
    return await references_out(request, "data-service", ds.id, tenant=tenant)


@router.patch(
    "/{ds_id}", response_model=DataServiceOut, dependencies=[Depends(require_scope("mcp:write"))]
)
async def patch_data_service(
    tenant: Tenant,
    user: User,
    store: DataServices,
    service: DataServiceSvc,
    ds_id: str,
    patch: DataServicePatch,
) -> dict[str, Any]:
    ds = await _get_or_404(store, tenant, ds_id)
    sent = patched_fields(patch)
    if "name" in sent:
        # name 은 생성 뒤 바꿀 수 없다 (D24). 같은 값이면 통과, 다르면 422 name_immutable
        check_name_unchanged(not_null(patch.name, "name", code="invalid_name"), ds.name)
    changes = patched_values(patch, sent)
    if patch.tls is not None:
        changes["tls"] = TlsInput(patch.tls.port, patch.tls.ca)
    if "display_name" in sent:
        changes["display_name"] = validate_display_name(
            not_null(patch.display_name, "display_name", code="invalid_display_name"),
            default=ds.name,
        )
    return _out(await service.update(ds, changes, user_sub=user.sub))


@router.delete(
    "/{ds_id}",
    response_model=DataServiceDeleteResponse,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def delete_data_service(
    tenant: Tenant, store: DataServices, service: DataServiceSvc, ds_id: str
) -> dict[str, Any]:
    ds = await _get_or_404(store, tenant, ds_id)
    await service.delete(ds)
    return {"id": ds.id, "deleted": True, "secret_name": ds.secret_name}


@router.post(
    "/{ds_id}/verify",
    response_model=DataServiceVerifyResponse,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("mcp:write"))],
)
async def verify_data_service(
    tenant: Tenant, store: DataServices, service: DataServiceSvc, ds_id: str
) -> dict[str, Any]:
    ds = await _get_or_404(store, tenant, ds_id)
    result = await service.verify(ds)
    if not result.ok:
        return {"id": ds.id, "ok": False, "error": result.error, "detail": result.detail}
    out: dict[str, Any] = {
        "id": ds.id,
        "ok": True,
        "verified_at": iso_utc(ds.verified_at),
        "detail": result.detail,
    }
    if result.extensions is not None:
        out["extensions"] = result.extensions
    if result.redis is not None:
        out["redis"] = result.redis
    return out
