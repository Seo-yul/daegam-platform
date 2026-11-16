"""MCP Hub API. GET 은 hub:read, 쓰기는 hub:write 이고 그 뒤에 access 를 판정한다 (D23)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from daegam.api._common import (
    error,
    forbidden,
    not_found,
    references_out,
)
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES, DeleteResponse
from daegam.api.schemas.hub import (
    HubCatalogResponse,
    HubItemWithStatus,
    HubManifestResponse,
    HubToolRulesSaved,
    HubToolsResponse,
)
from daegam.api.schemas.references import ReferencesResponse
from daegam.auth.hub_access import (
    Access,
    Action,
    can,
    narrowed,
    permissions_dict,
    scope_of,
)
from daegam.auth.principal import Principal
from daegam.names import validate_display_name, validate_name
from daegam.services.hub import HubService
from daegam.services.hub_tools import origin_server
from daegam.services.outside_headers import HeaderInput
from daegam.store.hub import HubEnvVar, HubItem, HubSecretEnvVar, HubSource, is_derived
from daegam.store.tool_rules import ItemToolRule
from daegam.urls import EXPORTED_CLASSES, outside_address

router = APIRouter(responses=ERROR_RESPONSES, prefix="/hub", tags=["hub"])


def get_service(request: Request) -> HubService:
    return request.app.state.hub_service


Service = Annotated[HubService, Depends(get_service)]
READ = Depends(require_scope("hub:read"))
WRITE = Depends(require_scope("hub:write"))


class HubAuthIn(BaseModel):
    """entry 항목의 인증 (P86 6.1절). 만들 때만 고르고 바꾸지 않는다. 값의 검사는 서비스가 하고
    `outside_header_value_invalid` 로 답한다."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["none", "header", "oauth"] = "none"
    header_name: str | None = None
    header_value: str | None = None


class HubHeaderValue(BaseModel):
    """헤더 값 바꾸기 (P86 6.1절)."""

    model_config = ConfigDict(extra="forbid")

    value: str


class HubEnvIn(BaseModel):
    """image 항목의 env 하나 (P55 5절). 값은 그 항목을 볼 수 있는 모든 사람에게 보인다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str


class HubSecretEnvIn(BaseModel):
    """image 항목의 Secret env 하나 (P55 5절). 값은 그 항목의 Secret 에만 두고 다시 보이지 않는다.
    값 없이 이름만 둘 수 있고, 값은 나중에 `PUT /hub/{id}/secret-env` 로 넣는다. 값의 검사는 한
    곳(`services/hub_env.py` `check_secret_values`)에 있다. 요청 검증의 422 는 어느 라우트든
    입력값을 싣지 않는다 (T77)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | None = None


class HubSecretEnvValues(BaseModel):
    """Secret env 값 바꾸기 (P55 5절). 보낸 이름만 바뀐다."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, str]


class HubCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None  # 없으면 name (D24)
    kind: Literal["remote", "entry", "image"]
    # 접근 정책 (D23). 생략하면 빈 정책(게시 테넌트 소속 전원이 보고 developer 이상이 고르고
    # 관리한다). 다른 테넌트나 `*` 를 넣는 것은 hub:share(platform-admin)다
    access: Access | None = None
    cluster_id: str | None = None
    url: str | None = None
    transport: Literal["streamable-http", "sse"] | None = None
    image: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    tools_filter: list[str] | None = None
    # entry 항목의 인증 (P86 6.1절). 없으면 none 이다
    auth: HubAuthIn | None = None
    # image 항목의 컨테이너 (P55 5절)
    stdio: bool | None = None
    args: list[str] | None = None
    env: list[HubEnvIn] | None = None
    secret_env: list[HubSecretEnvIn] | None = None
    # 항목이 온 카탈로그 서버의 이름 (P55 4·5절). 스냅숏의 이름이다
    catalog: str | None = None


def _mcp_url(item: HubItem, origin: Any | None) -> dict[str, Any]:
    """이 항목을 MCP 로 쓸 때의 주소(`external_url`)와 등록한 주소의 갈래(`address_class`).

    남의 서버를 가리키는 항목은 등록한 문자열을 서버가 가른다 (P55a 1절). 밖에서 닿거나 사설망인
    주소만 `external_url` 로 내보내고 갈래는 늘 싣는다. 화면은 URL 을 직접 가르지 않는다. 우리가
    만든 것은 원본의 노출 주소이고 갈래가 없다. 주소가 없으면 둘 다 None 이다."""
    if item.kind in ("remote", "entry"):
        if not item.source.url:
            return {"external_url": None, "address_class": None}
        cls = outside_address(item.source.url)
        url = item.source.url if cls in EXPORTED_CLASSES else None
        return {"external_url": url, "address_class": cls}
    return {
        "external_url": origin.external_url if origin is not None else None,
        "address_class": None,
    }


def item_out(
    item: HubItem,
    status: dict[str, Any] | None,
    *,
    principal: Principal,
    tenant: str,
    resolved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """허브 항목 응답. 다른 라우터의 publish 응답도 이 모양이다 (HubItemWithStatus).
    permissions 는 요청 주체의 판정 결과, scope 는 access 에서 파생한 읽기 전용 값이다.

    `resolved` 는 `internal`·`published` 의 도구 이름을 원본에서 계산한 것이다 (D49). 그 둘은
    저장된 값을 쓰지 않는다. 계산에 실패한 것(`None`)은 빈 목록으로 내보내고 `tool_names_at` 을
    null 로 두어 지금까지 "아직 모름" 이던 것과 같은 길로 간다. `tool_names_at` 은 베낀 시각이라
    계산한 값에는 뜻이 없고, 그래서 그 둘에서는 늘 null 이다.
    """
    d = item.model_dump(mode="json")
    origin = resolved.get(item.id) if resolved else None
    if origin is not None:
        d["tool_names"] = list(origin.tool_names or [])
        d["tool_names_at"] = None
    if status is not None:
        # 사람이 MCP 클라이언트에 붙여 넣는 주소다 (D50). 우리가 만든 것은 원본의 노출 설정에서,
        # 남의 서버를 가리키는 것은 등록할 때 적은 주소가 밖에서 닿을 때 그것이다. 밖으로 열지
        # 않았으면 없다
        status = {**status, **_mcp_url(item, origin)}
        d["status"] = status
    d["permissions"] = permissions_dict(principal, tenant, item)
    # header 항목의 값 바꾸기 (P86 6.1절)와 Secret env 값 바꾸기 (P55 5절). 서비스의 판정과 같은
    # 함수다
    d["permissions"]["replace_header"] = HubService.may_replace_header(
        item, principal=principal, tenant=tenant
    )
    d["permissions"]["replace_secret_env"] = HubService.may_replace_secret_env(
        item, principal=principal, tenant=tenant
    )
    d["scope"] = scope_of(item)
    return d


async def item_out_live(
    hub: HubService, item: HubItem, *, principal: Principal, tenant: str
) -> dict[str, Any]:
    """항목 하나의 응답. 원본을 해석하고 상태를 읽어 `item_out` 으로 만든다. 상태는 해석이 읽은
    원본 문서를 받는다. 목록은 원본을 한 번에 해석하고 상태를 모아 읽으므로 `item_out` 을 바로
    쓴다."""
    resolved = await hub.resolve_origins([item])
    return item_out(
        item,
        await hub.status(item, server=origin_server(resolved, item)),
        principal=principal,
        tenant=tenant,
        resolved=resolved,
    )


async def _visible_or_404(
    service: HubService, principal: Principal, tenant: str, item_id: str
) -> HubItem:
    item = await service.get_visible(principal, tenant, item_id)
    if item is None:
        raise not_found(f"hub item {item_id} not found")
    return item


def require_hub_permission(
    principal: Principal, tenant: str, item: HubItem, action: Action
) -> None:
    """항목의 action 권한이 없으면 403 forbidden. 본문의 `action` 이 무엇이 모자랐는지 말한다.
    MCP Server 삭제도 게시 항목을 함께 지우므로 이것으로 manage 를 본다."""
    if not can(principal, tenant, item, action):
        raise forbidden(f"{action} permission on this hub item required", action=action)


async def unpublish_item(
    service: HubService, principal: Principal, tenant: str, item: HubItem
) -> dict[str, Any]:
    """게시 해제. manage 가 없으면 403, 참조 중이면 409 in_use. `DELETE /hub/{id}` 와 MCP Server
    쪽 해제가 함께 불러 두 길이 같은 판정을 낸다."""
    require_hub_permission(principal, tenant, item, "manage")
    await service.delete(item)
    return {"id": item.id, "deleted": True}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def create_item(
    tenant: Tenant, service: Service, body: HubCreate, user: User
) -> dict[str, Any]:
    name = validate_name(body.name)
    display_name = validate_display_name(body.display_name, default=name)
    # 플랫폼만 쓰는 칸(`set`, `catalog` 의 릴리스)은 요청에서 받지 않는다. 라우터가 출처를 칸마다
    # 짓는다 (P55 5절)
    source = HubSource(
        url=body.url.strip() if body.url else None,
        transport=body.transport,
        image=body.image.strip() if body.image else None,
        port=body.port,
        tools_filter=body.tools_filter,
        stdio=body.stdio or None,
        args=body.args or None,
        env=[HubEnvVar(name=e.name, value=e.value) for e in body.env] if body.env else None,
        secret_env=[HubSecretEnvVar(name=e.name) for e in body.secret_env]
        if body.secret_env
        else None,
    )
    secret_values = {e.name: e.value for e in body.secret_env or [] if e.value is not None}
    auth = body.auth
    item = await service.create(
        tenant,
        name=name,
        display_name=display_name,
        kind=body.kind,
        source=source,
        cluster_id=body.cluster_id,
        access=body.access,
        principal=user,
        auth=HeaderInput(auth.kind, auth.header_name, auth.header_value) if auth else None,
        catalog=body.catalog,
        secret_values=secret_values,
    )
    return await item_out_live(service, item, principal=user, tenant=tenant)


@router.get(
    "",
    response_model=list[HubItemWithStatus],
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def list_items(tenant: Tenant, service: Service, user: User) -> list[dict[str, Any]]:
    """view 로 거른 목록."""
    items = await service.visible(user, tenant)
    # 항목마다 해석하면 N+1 이다. 목록 전체를 한 번에 계산한다 (D49)
    resolved = await service.resolve_origins(items)
    # Platform Settings 는 목록에 한 번 읽는다 (플랫폼 항목의 상태, P55 6절의 image 경고)
    settings = await service.platform_settings()
    # 상태는 항목마다 CR 을 읽는다. 차례로 기다리면 목록 한 장이 왕복 수만큼 느려진다. 목록 셋
    # (Gateway·Agent·MCP Server)이 쓰는 것과 같은 방식이다
    states = await asyncio.gather(
        *(service.status(i, server=origin_server(resolved, i), settings=settings) for i in items)
    )
    return [
        item_out(i, st, principal=user, tenant=tenant, resolved=resolved)
        for i, st in zip(items, states, strict=True)
    ]


@router.get("/catalog", response_model=HubCatalogResponse, dependencies=[READ])
async def get_catalog(service: Service) -> dict[str, Any]:
    """공개 카탈로그의 스냅숏 (P55 4절). 이 플랫폼 판과 함께 실린 것 전체를 한 번에 돌려준다.
    `GET /hub/{item_id}` 보다 먼저 선언해 항목 id 로 읽히지 않게 한다."""
    return await service.catalog()


@router.get(
    "/{item_id}",
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[READ],
)
async def get_item(tenant: Tenant, service: Service, item_id: str, user: User) -> dict[str, Any]:
    item = await _visible_or_404(service, user, tenant, item_id)
    return await item_out_live(service, item, principal=user, tenant=tenant)


@router.get("/{item_id}/references", response_model=ReferencesResponse, dependencies=[READ])
async def item_references(
    tenant: Tenant, service: Service, item_id: str, user: User, request: Request
) -> dict[str, Any]:
    """이 허브 항목 의 참조 (D43). 이 항목을 백엔드로 묶은 Gateway 다."""
    item = await _visible_or_404(service, user, tenant, item_id)
    return await references_out(request, "hub-item", item.id, tenant=tenant)


@router.delete("/{item_id}", response_model=DeleteResponse, dependencies=[WRITE])
async def delete_item(tenant: Tenant, service: Service, item_id: str, user: User) -> dict[str, Any]:
    """삭제 = 게시 해제. manage 가 없으면 403, 참조 중이면 409 in_use."""
    item = await _visible_or_404(service, user, tenant, item_id)
    return await unpublish_item(service, user, tenant, item)


@router.post(
    "/{item_id}/refresh-tools",
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def refresh_tools(
    tenant: Tenant, service: Service, item_id: str, user: User
) -> dict[str, Any]:
    item = await _visible_or_404(service, user, tenant, item_id)
    require_hub_permission(user, tenant, item, "manage")
    # internal·published 의 도구 이름은 우리 리비전에서 계산한다 (D49). 라이브 값을 저장할 자리가
    # 없고, 저장하면 연산자가 새 리비전을 아직 안 굴린 과도기의 값이 굳어 멀쩡한 Gateway 정의를
    # 422 로 막는다. 라이브를 보는 일은 도구 탭이 저장 없이 한다 (D48)
    if is_derived(item.kind):
        raise error(
            422,
            "derived_tools",
            f"{item.kind} items take their tool names from the platform document, not from a "
            "live listing; the tools tab shows what the server answers right now",
            kind=item.kind,
        )
    item = await service.refresh_tools(item)
    return item_out(
        item,
        item.status,
        principal=user,
        tenant=tenant,
        resolved=await service.resolve_origins([item]),
    )


@router.put(
    "/{item_id}/access",
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def set_access(
    tenant: Tenant, service: Service, item_id: str, body: Access, user: User, request: Request
) -> dict[str, Any]:
    """정책 저장 (D23). manage 가 없으면 403, 경계를 넓히면 hub:share, internal·published 에
    다른 테넌트를 넣으면 422 cross_tenant_unsupported. header 항목의 consume 경계에서 테넌트를
    빼면 그 테넌트의 헤더 사본이 지워지므로 감사 action 이 `set_access_drop_header_copies` 다 (P86
    6.4절)."""
    item = await _visible_or_404(service, user, tenant, item_id)
    require_hub_permission(user, tenant, item, "manage")
    before = item.access
    item = await service.set_access(item, body, user)
    if item.header_auth is not None and narrowed(before, item.access, item.tenant, "consume"):
        request.state.audit_action = "set_access_drop_header_copies"
    return await item_out_live(service, item, principal=user, tenant=tenant)


@router.put(
    "/{item_id}/auth/header",
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def set_header_value(
    tenant: Tenant, service: Service, item_id: str, body: HubHeaderValue, user: User
) -> dict[str, Any]:
    """header 항목의 값을 바꾼다 (P86 6.1절). 그 항목의 테넌트에서 만든 사람과 tenant-admin 만
    되고(403 forbidden), 헤더 판이 올라 그 항목을 쓰는 Gateway 가 재적용의 속도로 다시 렌더된다.
    값은 응답·감사·Mongo 에 남지 않는다. 감사 action 은 핸들러 이름이다."""
    item = await _visible_or_404(service, user, tenant, item_id)
    item = await service.set_header_value(item, body.value, principal=user, tenant=tenant)
    return await item_out_live(service, item, principal=user, tenant=tenant)


@router.put(
    "/{item_id}/secret-env",
    response_model=HubItemWithStatus,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def set_secret_env(
    tenant: Tenant, service: Service, item_id: str, body: HubSecretEnvValues, user: User
) -> dict[str, Any]:
    """image 항목의 Secret env 값을 바꾼다 (P55 5절). 그 항목의 테넌트에서 만든 사람(hub:write 를
    가진 동안)과 tenant-admin 만 되고(403 forbidden), 보낸 이름만 바뀌며 그 항목의 백엔드 파드가
    다시 뜬다. Secret 이 없으면 이름을 모두 보내야 한다(409 secret_missing). 값은 응답·감사·Mongo 에
    남지 않는다."""
    item = await _visible_or_404(service, user, tenant, item_id)
    item = await service.set_secret_env(item, body.values, principal=user, tenant=tenant)
    return await item_out_live(service, item, principal=user, tenant=tenant)


class HubToolRulesBody(BaseModel):
    """image 항목의 Tool 규칙 전체 (P52 1절). 키는 Tool 이름이다."""

    model_config = ConfigDict(extra="forbid")

    rules: dict[str, ItemToolRule] = Field(default_factory=dict)


@router.put(
    "/{item_id}/tool-rules",
    response_model=HubToolRulesSaved,
    response_model_exclude_unset=True,
    dependencies=[WRITE],
)
async def set_tool_rules(
    tenant: Tenant,
    service: Service,
    item_id: str,
    body: HubToolRulesBody,
    user: User,
    request: Request,
) -> dict[str, Any]:
    """image 항목의 Tool 규칙을 통째로 저장하고 항목을 다시 적용한다 (P52 1절). manage 가 없으면
    403 이다. 역할만 받고, 키는 지금 항목이 내놓는 Tool 이어야 한다(422
    rule_names_unknown_tool)."""
    item = await _visible_or_404(service, user, tenant, item_id)
    require_hub_permission(user, tenant, item, "manage")
    oidc = request.app.state.settings.auth_mode == "oidc"
    item, notice = await service.set_tool_rules(item, body.rules, oidc=oidc)
    out = await item_out_live(service, item, principal=user, tenant=tenant)
    out["rules_notice"] = notice.to_dict()
    return out


@router.get("/{item_id}/tools", response_model=HubToolsResponse, dependencies=[READ])
async def get_tools(tenant: Tenant, service: Service, item_id: str, user: User) -> dict[str, Any]:
    """Tool 탭이 부르는 것. 그 자리에서 tools/list 를 해서 이름·설명·입력 스키마를 돌려준다.

    **읽기인데도 `consume` 을 요구한다.** 내용만 보면 `view` 로 충분하다. Tool 이름은 이미 상세
    응답에 실리고 같은 문턱의 매니페스트가 렌더된 CR 을 통째로 보인다. 문턱을 올린 것은 내용이
    아니라 행동 때문이다. 이 호출은 사용자가 등록 때 적은 임의의 주소로 플랫폼이 나가게 하고,
    `internal`·`image` 면 그 테넌트를 audience 로 하는 서비스 토큰 발급까지 일으킨다 (D41).
    읽기 권한만 가진 주체에게 밖으로 나가는 발신 능력을 새로 주지 않는다."""
    item = await _visible_or_404(service, user, tenant, item_id)
    require_hub_permission(user, tenant, item, "consume")
    return await service.tool_schemas(item)


@router.get("/{item_id}/manifest", response_model=HubManifestResponse, dependencies=[READ])
async def get_manifest(
    tenant: Tenant, service: Service, item_id: str, user: User
) -> dict[str, Any]:
    item = await _visible_or_404(service, user, tenant, item_id)
    return {
        "documents": [
            {
                "kind": d["kind"],
                "name": d["metadata"]["name"],
                "rendered": d,
                "overrides": item.overrides,
            }
            for d in await service.render(item)
        ]
    }
