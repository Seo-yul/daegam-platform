"""MCP Hub: 항목 생성(CR 렌더·apply), 상태, 삭제, 툴 목록 갱신, MCP Server 의 수동 게시(D23).

자기 CR 을 둔 항목(`owns_crs`)은 재적용 단계가 렌더 번호로 제자리에서 다시 렌더한다
(`rerender`, P50 1절, `services/hub_reapply.py`)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from pymongo.errors import DuplicateKeyError
from starlette.concurrency import run_in_threadpool

from daegam.auth.hub_access import (
    ACTIONS,
    CROSS_TENANT_KINDS,
    Access,
    AccessError,
    can,
    opens_other_tenants,
    validate_access,
    visible,
    widened_actions,
)
from daegam.auth.principal import Principal
from daegam.config.resolve import ProfileRuntime, image_digest, resolve_effective, with_registry
from daegam.errors import ServiceError, save_missed
from daegam.k8s.client import AccessDenied, ClusterUnreachable, KubeClient, parse_service_url
from daegam.k8s.gates import passing_features, require_tenant_secrets, toolhive_has
from daegam.k8s.labels import (
    APP_MCPSERVER,
    LABEL_APP_INSTANCE,
    LABEL_APP_NAME,
    LABEL_MCP_BACKEND,
    LABEL_PLATFORM_ID,
    LABEL_SOURCE_HASH,
    LABEL_SOURCE_ID,
    stable_hash,
)
from daegam.k8s.portforward import bearer_service_client
from daegam.k8s.templates import (
    TOOLHIVE_API,
    ImageContainer,
    proxy_mcp_url,
    proxy_service_url,
    render_image_server,
    render_remote_proxy,
    render_tool_config,
    vmcp_cluster_url,
)
from daegam.k8s.templates.authz import AUTHZ_CONFIG_KIND, authz_config_name, tenant_fence
from daegam.k8s.templates.common import owned_by
from daegam.k8s.templates.svc_token import TOKEN_BACKEND_KINDS
from daegam.k8s.templates.web_tools import render_fetch_clone
from daegam.mcp_client import AuthRefused, ToolSchema, ToolsResult
from daegam.services import hub_catalog, hub_env, outside_headers, platform_tools
from daegam.services.authz_configs import (
    AUTHZ_WAIT_SECONDS,
    REAPPLY_AUTHZ_WAIT_SECONDS,
    apply_in_order,
)
from daegam.services.clusters import ClusterService
from daegam.services.data_plane_auth import (
    KIND_HUB_ITEM,
    newer_rev,
    own_audience_rev,
    render_epoch,
    rules_rev,
    secret_env_rev,
)
from daegam.services.hub_images import (
    HubImageAdmission,
    image_warnings,
    refuse_refused,
    refused_items,
)
from daegam.services.hub_secret_env import SecretEnvSecrets
from daegam.services.proxy_accounts import proxy_service_account, require_proxy_account
from daegam.services.references import HUB_ITEM_DELETE, ReferenceService
from daegam.services.rule_compile import RuleScopes, closed_policies, item_policies
from daegam.services.svc_tokens import SvcTokens
from daegam.services.tenancy import TenancyError, TenancyService, group_name_for, k8s_name
from daegam.services.tool_rules import (
    RULES_NOT_ENFORCED,
    RULES_TOOL_MISSING,
    RULES_TOOLHIVE_OLD,
    RuleNotice,
    ToolRuleError,
    check_callers,
    check_cedar_size,
    check_rule_keys,
    coded_warning,
    notice_of,
    rule_warning,
    rules_toolhive_ok,
)
from daegam.store.access import stamp_access
from daegam.store.auth_reapply import AUTH_FIELDS, new_image_switch, switched_server_id
from daegam.store.base import (
    fields_of,
    iso_utc,
    read_after_miss,
    save_with_edit_claim,
    utcnow,
)
from daegam.store.gateways import Gateway
from daegam.store.hub import (
    CHECK_OK,
    CHECK_REFUSED,
    CHECK_UNREACHABLE,
    HUB_LIVE_STATES,
    HUB_NOT_DELETING,
    PLATFORM_KIND,
    HubAuth,
    HubAuthCheck,
    HubCatalogOrigin,
    HubItem,
    HubItemNewer,
    HubK8s,
    HubSource,
    MongoHubStore,
    StdioProbe,
    secret_env_name,
    unknown_keys,
)
from daegam.store.hub_env_secrets import MongoHubEnvSecretStore
from daegam.store.mcp_servers import MCPServer
from daegam.store.outside_header_secrets import MongoOutsideHeaderSecretStore
from daegam.store.settings import HubImagesSettings, PlatformSettings
from daegam.store.tool_rules import ItemToolRule, holds_rules, normalized_rules
from daegam.urls import is_http_url

if TYPE_CHECKING:
    # hub_tools 가 MCP Server 모듈을 거쳐 이 모듈을 import 하므로 타입으로만 가져온다
    from daegam.services.hub_tools import HubToolNames

logger = logging.getLogger("daegam.hub")

# (그 항목이 사는 클러스터의 클라이언트, MCP URL, 실을 헤더) → 툴 이름. 클러스터 안 Service 주소는
# 클라이언트의 service_route 를 지나므로(D21) 호출부는 home/원격을 모른다. 우리 MCPServer 는 서비스
# 토큰을 `Authorization` 으로 요구하고(D41) 그때 직행이 아니면 pods/portforward 다. 외부 서버 항목은
# 그 고정 헤더다 (P86 6.3절). 테스트는 이 자리에 fake 를 넣는다.
ToolLister = Callable[[KubeClient, str, Mapping[str, str] | None], Awaitable[list[str]]]
#: 같은 경로로 도구의 자세한 판까지 받는다. 이름만 쓰는 `ToolLister` 를 그대로 둔 것은 그 계약을
#: 목이 아홉 자리에서 쓰고 있어서다 (테스트가 이 자리에 fake 를 넣는다)
SchemaLister = Callable[[KubeClient, str, Mapping[str, str] | None], Awaitable["ToolsResult"]]
# internal 항목의 원본 MCP Server 가 자기 상태로 답하는 자리(`MCPServerService.item_status`). 둘째
# 인자는 부르는 쪽이 이미 읽은 원본 문서다. 원본이 없거나 draft 면 None 이다
ServerStatus = Callable[[HubItem, MCPServer | None], Awaitable[dict[str, Any] | None]]
TOOLS_LIST_TIMEOUT = 20.0

# 도구 스키마 응답의 상한. 저장하지 않고 그때그때 물어보는 값이지만 남의 MCP 서버가 얼마나 큰 것을
# 내놓을지는 우리가 정하지 못한다. petstore(도구 20개)가 7KB 인 것을 실측했으니 256KB 는 그만한 것
# 700개쯤이다. 스펙 작업 입력 상한(`spec_job_input_max_bytes`)과 같은 값이라 새 숫자가 아니다.
TOOL_SCHEMAS_MAX_BYTES = 256 * 1024
TOOL_SCHEMA_MAX_BYTES = 16 * 1024
TOOL_DESC_MAX = 2000


class HubError(ServiceError):
    pass


def refuse_platform(item: HubItem, action: str) -> None:
    """플랫폼 항목은 지우거나 도구를 새로 고치지 못한다. 정의는 Platform Settings 와 계약이다."""
    if item.kind == PLATFORM_KIND:
        raise HubError(
            422,
            "platform_item",
            f"{item.name} is a platform tool; turn it on or off in Platform Settings ({action})",
        )


def newer_item_error(item_id: Any, name: Any, fields: Sequence[str]) -> HubError:
    """뒤 판이 쓴 칸을 든 항목이나 원본 정의의 422 (P55 9절). 같은 문서는 판을 올리기 전까지 다시
    해도 같으므로 재적용이 그 문서를 막는다(`holds_reapply`)."""
    return HubError(
        422,
        "hub_item_newer",
        f"hub item {name} holds fields this platform version does not know "
        f"({', '.join(fields)}); upgrade the platform to deploy it or change its access",
        hub_item_id=item_id,
        fields=list(fields),
    )


def refuse_newer(item: HubItem) -> None:
    """뒤 판이 쓴 칸을 든 항목은 렌더하지도 접근 정책을 바꾸지도 않는다 (P55 9절). 렌더하면 모르는
    칸이 빠진 객체가 적용되고, 정책을 넓히면 뒤 판이 막는 공유가 열린다. 목록과 상세는 그대로
    보인다."""
    if item.newer_fields:
        raise newer_item_error(item.id, item.name, item.newer_fields)


# 적용과 삭제가 쓰는 생명주기 칸 (P68 2절)
_LIFECYCLE_FIELDS = {"state", "last_error", "updated_at"}
# Secret env 값을 바꿀 때 쓰는 칸 (P55 5절)
_SECRET_ENV_FIELDS = {
    "source.secret_env",
    "secret_env_rev",
    "secret_env_updated_by",
    "secret_env_updated_at",
    "updated_at",
}


def _creator_or_admin(
    item: HubItem, created_by: str | None, *, principal: Principal, tenant: str
) -> bool:
    """항목의 비밀 값을 바꿀 수 있는 사람 (P55 5절). 그 항목의 테넌트에서, 만든 사람이
    hub:write 를 가진 동안과 tenant-admin 이다. 모든 manage 가 아닌 것은 빈 기본 정책이 developer
    모두에게 manage 를 주기 때문이다."""
    if tenant != item.tenant:
        return False
    if created_by and principal.sub == created_by and "hub:write" in principal.scopes_in(tenant):
        return True
    return principal.is_tenant_admin(tenant)


# 항목이 가리키는 CR. entry 는 CR 없이 URL 만 보관한다 (사용 시 Gateway 그룹에 Entry 래퍼)
CR_KIND: dict[str, str | None] = {
    "internal": "MCPServer",
    "remote": "MCPRemoteProxy",
    "entry": None,
    "image": "MCPServer",
    "published": "VirtualMCPServer",
    # 플랫폼 항목 가운데 복제되는 것은 fetch 뿐이다 (P39b 13절)
    "platform": "MCPServer",
}
READY_PHASES = {"MCPServer": {"Ready"}, "MCPRemoteProxy": {"Ready"}, "VirtualMCPServer": {"Ready"}}
# stdio 항목이 Ready 인데 답하지 않는 상태 (P55 5절)
NOT_ANSWERING = "not_answering"
# 답하지 않는 stdio 항목의 프록시 파드 지우기 (P55 5절). 실패가 STDIO_CONFIRM 넘게 이어져야 처음
# 지우고(한 틱 넘게), 그 뒤로는 백오프다
STDIO_CONFIRM = timedelta(seconds=45)
STDIO_BACKOFF_FIRST = timedelta(minutes=2)
STDIO_BACKOFF_MAX = timedelta(minutes=30)
# 항목이 transport 를 적지 않았을 때 쓰는 값
DEFAULT_TRANSPORT = "streamable-http"


def hub_k8s_name(name: str) -> str:
    return k8s_name(name, prefix="hub")


def proxied(item: HubItem) -> bool:
    """MCPRemoteProxy 로 감싼 항목인가. remote 는 늘 그렇고 published 는 proxy 모드로 게시했을
    때다. 소비는 프록시를 거친다(정책·audit 계층)."""
    return item.kind == "remote" or (item.kind == "published" and item.k8s.kind == "MCPRemoteProxy")


def hub_item_rev(item: HubItem) -> str | None:
    """허브 항목의 판 첫 절반. 렌더가 인증 입력을 쓰지 않으므로 자기 audience 로 옮겨 간 image
    항목의 표시(P50 9절)와 Secret env 판(P55 5절)뿐이다. 그래서 값을 바꾼 뒤 적용이 실패하면
    재적용이 다시 렌더한다. 기대 판(`HubItemReapply.want_rev`)과 판을 적는
    자리(`HubService._stamp_epoch`)가 모두 이것을 쓴다."""
    return secret_env_rev(own_audience_rev(None, item.audience_switch), item.secret_env_rev)


def owns_crs(item: HubItem) -> bool:
    """항목이 자기 CR 을 렌더하는가. 프록시(remote·proxy 모드 게시)와 image 다. internal 의 CR 은
    MCP Server 의 것이고, entry 와 entry 모드 게시는 CR 이 없다(게시의 vMCP 는 Gateway 의 것)."""
    return proxied(item) or item.kind == "image"


# 복제본 이름에 붙이는 원본 항목 id 의 16진 자릿수. 두 원본의 복제본 이름이 한 네임스페이스에서
# 겹치지 않게 한다. 항목 이름 32자에 붙여도 ToolHive 가 만드는 가장 긴 이름
# `mcp-shared-<n>-remote-proxy` 가 63자에 든다 (docs/guide/names.md 2절)
CLONE_ID_DIGITS = 6
# 복제본 기록이 드는 원본 정의의 칸. 복제본의 렌더가 읽는 것만 든다
CLONE_SOURCE_FIELDS = (
    "id",
    "tenant",
    "name",
    "display_name",
    "kind",
    "source",
    "cluster_id",
    # image 항목의 Tool 규칙 (P52 1절). 복제본도 같은 규칙과 고정한 Tool 목록으로 렌더한다. 비어
    # 있으면 원본 정의에 싣지 않아 규칙이 없는 기록은 이 칸 전과 같다
    "tool_rules",
    "rules_tool_names",
)
# 복제본을 다시 렌더할지 가르는 칸. 규칙 칸은 있을 때만 판에 든다
_CLONE_RENDER_FIELDS = ("kind", "source", "cluster_id")
_CLONE_RULE_FIELDS = ("tool_rules", "rules_tool_names")


def shared_clone_name(item: Any) -> str:
    """다른 테넌트 네임스페이스에 만드는 복제본 CR 이름 `shared-<항목 이름>-<원본 id 6자>`
    (P50 1절). item 은 `id`·`name`·`kind` 를 가진 무엇이든 된다. 플랫폼 fetch 는
    `platform-web-fetch` 라 테넌트 항목의 복제본과 겹치지 않는다 (P39b 13절)."""
    if getattr(item, "kind", None) == PLATFORM_KIND:
        return platform_tools.FETCH_CLONE_NAME
    digits = item.id.replace("-", "")[:CLONE_ID_DIGITS]
    return k8s_name(f"{item.name}-{digits}", prefix="shared")


def pinned_tools(item: HubItem) -> list[str] | None:
    """image 항목의 MCPToolConfig toolsFilter. tools_filter 가 있으면 그것이고, 없고 누가 부를지의
    규칙이 있으면 규칙을 저장할 때의 목록(`rules_tool_names`)이다 (P52 1절). 둘 다 없으면 None 이고
    필터를 렌더하지 않는다."""
    if item.source.tools_filter:
        return list(item.source.tools_filter)
    if holds_rules(item.tool_rules.values()):
        return list(item.rules_tool_names)
    return None


def tool_config_name(cr_name: str) -> str:
    """image 항목과 그 복제본이 두는 도구 필터 CR(MCPToolConfig) 이름."""
    return f"{cr_name}-tools"


TOOL_CONFIG_KIND = "MCPToolConfig"


def image_side_objects(name: str, tool_config: str | None = None) -> list[tuple[str, str]]:
    """image 서버(항목이나 복제본) CR name 곁의 (종류, 이름). 테넌트 울타리 설정(P50 11절)과 도구
    필터 CR 이다. 둘 다 소유자 없이 두므로 이름으로 지운다. tool_config 는 항목이 저장한 필터
    이름이고, 없으면 규칙의 이름(`tool_config_name`)이다."""
    return [
        (AUTHZ_CONFIG_KIND, authz_config_name(name)),
        (TOOL_CONFIG_KIND, tool_config or tool_config_name(name)),
    ]


def owned_objects(item: HubItem) -> list[tuple[str, str]]:
    """항목이 그 네임스페이스(`k8s.namespace`)에 둔 ToolHive 객체의 (종류, 이름). 렌더하지 않고
    저장된 이름으로만 짓는다 (P55 9절). 그래서 이 판이 렌더하지 못하는 뒤 판의 항목도 이것으로
    지운다. image 의 곁 객체는 게이트가 닫힌 뒤나 필터 없는 항목에는 없을 수 있지만, 지우기는 없는
    객체를 넘어간다. 서버의 CR 이 없는 항목(internal·entry·entry 모드 게시)은 빈 목록이다."""
    k = item.k8s
    if not owns_crs(item) or k.kind is None:
        return []
    objects = [(k.kind, k.name)]
    if item.kind == "image":
        objects += image_side_objects(k.name, k.tool_config)
    return objects


def may_hold_secret_env(item: HubItem) -> bool:
    """항목에 Secret env 값의 Secret(`secret_env_name`)이 있을 수 있는가 (P55 5절). Secret env
    이름을 든 image 항목이다."""
    return item.kind == "image" and bool(item.source.secret_env)


def image_container(item: HubItem, tenant: str) -> ImageContainer:
    """image 항목(과 tenant 에 둔 복제본)의 컨테이너 입력 (P55 5절). Secret env 는 항목의
    테넌트에서만 렌더한다. 그런 항목은 다른 테넌트에 열 수 없어 복제본이 없지만, 어느 길로 렌더되든
    남의 네임스페이스에 없는 Secret 을 가리키지 않게 한다."""
    src = item.source
    own = tenant == item.tenant
    set_names = tuple(src.secret_names(only_set=True)) if own else ()
    return ImageContainer(
        stdio=bool(src.stdio),
        args=tuple(src.args or ()),
        env=tuple((e.name, e.value) for e in src.env or ()),
        secret_env=set_names,
        secret_name=secret_env_name(item.id) if set_names else None,
        secret_env_rev=item.secret_env_rev if own else None,
        writable_tmp=src.writable_tmp,
    )


def refuse_shared_secret_env(item_source: HubSource, access: Access, owner: str) -> None:
    """Secret env 를 든 항목은 다른 테넌트에 열지 않는다 (P55 5절). 열면 그 테넌트의
    복제본에는 Secret 이 없다. 422 secret_not_shareable 이다."""
    if item_source.secret_env and opens_other_tenants(access, owner):
        raise HubError(
            422,
            "secret_not_shareable",
            "an item with secret_env cannot be opened to other tenants",
        )


def clone_source(item: HubItem) -> dict[str, Any]:
    """복제본 기록이 드는 원본 정의다(`CLONE_SOURCE_FIELDS`). 복제본은 이것으로 렌더한다."""
    return item.model_dump(mode="json", include=set(CLONE_SOURCE_FIELDS))


def clone_source_hash(source: dict[str, Any]) -> str:
    """원본 정의 가운데 복제본의 렌더가 읽는 칸(종류, 출처, 원본 클러스터)의 판이다. 표시 이름이나
    접근 정책을 고친 것은 복제본을 다시 렌더하지 않는다."""
    picked = {k: source.get(k) for k in _CLONE_RENDER_FIELDS}
    picked.update({k: source[k] for k in _CLONE_RULE_FIELDS if source.get(k)})
    return stable_hash(picked, 16)


def item_from_clone_source(source: dict[str, Any]) -> HubItem:
    """복제본 기록이 든 원본 정의를 렌더할 수 있는 항목으로 되살린다. 원본 정의나 그 출처에 이 판이
    모르는 칸이 있으면 422 `hub_item_newer` 다 (P55 9절). 뒤 판이 쓴 정의를 그 칸 없이 렌더한
    복제본보다 멈춘 복제본이 안전하므로 일부러 버리지 않는다. Gateway 배포와 복제본 재적용이 살아
    있는 원본이든 동결된 기록이든 여기서 멈춘다."""
    unknown = unknown_keys(source, CLONE_SOURCE_FIELDS)
    if unknown:
        raise newer_item_error(source.get("id"), source.get("name"), unknown)
    picked = {k: source[k] for k in CLONE_SOURCE_FIELDS if k in source and k != "source"}
    k8s = HubK8s(kind=CR_KIND.get(source.get("kind", "")), name="", namespace="")
    src = source.get("source") or {}
    return HubItem.model_validate({**picked, "source": src, "k8s": k8s.model_dump()})


def source_hash(docs: list[dict[str, Any]]) -> str:
    """복제본 문서들의 spec 해시. 라벨 값 제한(63자) 때문에 sha256 앞 32자만 쓴다."""
    return stable_hash(
        [{"kind": d["kind"], "name": d["metadata"]["name"], "spec": d.get("spec")} for d in docs],
        32,
    )


async def _default_tool_lister(
    client: KubeClient, url: str, headers: Mapping[str, str] | None = None
) -> list[str]:
    """이름만 받는 판. 경로 고르기는 `_default_schema_lister` 가 한다."""
    return (await _default_schema_lister(client, url, headers)).tools


async def _default_schema_lister(
    client: KubeClient, url: str, headers: Mapping[str, str] | None = None
) -> ToolsResult:
    """`<svc>.<ns>.svc[.cluster.local]` 주소면 클러스터 클라이언트가 정한 경로(svc DNS 직행 또는
    API 서버 services/proxy)로, 그 밖의 URL(외부 MCP)은 그대로 tools/list 를 부른다.
    streamable-http 세션(POST 응답 SSE, Mcp-Session-Id)이 API 서버 프록시를 그대로 지나는 것은
    orbstack 에서 실측했다 (D21). 실은 헤더에 `Authorization` 이 있고 직행이 아니면
    pods/portforward 로 간다. API 서버 프록시는 Authorization 을 지우기 때문이다 (D41, 실측). 그
    판정은 에이전트 런타임 프록시와 같은 `bearer_service_client` 다. 외부 URL 에 헤더를 실으면
    리다이렉트를 따라가지 않는다 (`list_tools`, P86 6.5절)."""
    from daegam.mcp_client import list_tools

    parts = parse_service_url(url)
    if parts is None:
        return await list_tools(url, timeout=TOOLS_LIST_TIMEOUT, headers=headers)
    namespace, service, port, path = parts
    async with bearer_service_client(
        client,
        namespace,
        service,
        port,
        headers=headers,
        timeout=TOOLS_LIST_TIMEOUT,
        scheme=urlsplit(url).scheme or "http",
    ) as (hc, base_url):
        if headers:
            # 헤더가 항목의 URL 밖으로 나가지 않게 한다 (P86 6.5절)
            hc.follow_redirects = False
        return await list_tools(f"{base_url}{path}", timeout=TOOLS_LIST_TIMEOUT, http_client=hc)


# SSE 로만 말하는 외부 MCP 서버는 받지 않는다 (P54b 15절 3). 플랫폼의 MCP 클라이언트는
# streamable HTTP 만 쓰므로 Tool 목록과 Gateway 배포 전 확인이 실패하고, 원격 프록시는 서버가
# 알린 메시지 경로로 보내지 않는다. 만들기와 카탈로그 가져오기가 거절하고, 이미 있는 SSE 항목은
# 지우거나 바꾸지 않고 상태에 같은 코드의 경고를 보인다. 코드는 hub_catalog 의 것이다
OUTSIDE_SSE_UNSUPPORTED = hub_catalog.OUTSIDE_SSE_UNSUPPORTED
OUTSIDE_SSE_UNSUPPORTED_TEXT = (
    "outside MCP servers that speak only SSE are not supported; register the server's streamable "
    "HTTP address instead"
)
OUTSIDE_SSE_WARNING_TEXT = (
    "this hub item's server speaks only SSE, which the platform does not support: its Tools "
    "cannot be refreshed and Gateways that use it cannot be deployed"
)
# 허브 항목 상태 경고 (P86 6.4절). 플랫폼의 마지막 호출에서 외부 서버가 헤더 값을 거절했다
OUTSIDE_HEADER_REFUSED = "outside_header_refused"
OUTSIDE_HEADER_REFUSED_TEXT = (
    "the outside server refused this hub item's header value on the platform's last call"
)


def check_outcome(e: BaseException) -> str:
    """헤더를 실은 tools/list 의 실패를 확인의 답으로 옮긴다 (P86 6.3절). 401·403(`AuthRefused`)은
    header_refused, 그 밖은 unreachable 이다. anyio TaskGroup 이 감쌀 수 있어 가장 안쪽 예외까지
    본다."""
    return CHECK_REFUSED if isinstance(_leaf_exception(e), AuthRefused) else CHECK_UNREACHABLE


def sse_warnings(item: HubItem) -> list[str]:
    """SSE 로 등록된 remote·entry 항목이면 `outside_sse_unsupported` 다 (P54b 15절 3). 그 전에 만든
    항목만 해당한다."""
    if item.kind in ("remote", "entry") and item.source.transport == "sse":
        return [coded_warning(OUTSIDE_SSE_UNSUPPORTED, OUTSIDE_SSE_WARNING_TEXT)]
    return []


def header_warnings(item: HubItem) -> list[str]:
    """마지막 확인이 거절이면 `outside_header_refused` 다 (P86 6.4절)."""
    auth = item.header_auth
    if auth is not None and auth.check is not None and auth.check.outcome == CHECK_REFUSED:
        return [coded_warning(OUTSIDE_HEADER_REFUSED, OUTSIDE_HEADER_REFUSED_TEXT)]
    return []


def _json_bytes(v: Any) -> bytes:
    return json.dumps(v, ensure_ascii=False, default=str).encode("utf-8")


#: `_json_bytes` 가 배열 원소 사이에 쓰는 구분자 ", " 의 길이.
_SEP = 2


def _capped(schemas: list[ToolSchema]) -> tuple[list[dict[str, Any]], bool]:
    """응답 크기를 재고 넘치는 만큼만 덜어낸다. (도구 목록, 도구를 잘라냈는지)

    저장하지 않는 값이어도 남의 MCP 서버가 얼마나 큰 것을 내놓을지는 우리가 정하지 못한다.
    덜어내는 차례는 설명 자르기, 혼자 너무 큰 스키마 버리기, 그래도 넘치면 큰 스키마부터 버리기,
    마지막으로 도구 자체를 뒤에서부터 잘라내기다. 버린 스키마는 `schema_omitted` 로 드러낸다.

    도구를 잘라내는 것은 정말 마지막이다. 개수가 `tool_names` 와 어긋나 사람이 목록을 못 믿게
    되기 때문인데, 그렇다고 상한을 놓아 버리면 남의 서버가 우리 응답 크기를 정하게 된다. 이름은
    길이 제한이 없어 스키마를 다 버려도 상한을 넘길 수 있다. 그래서 잘라내되 잘랐다고 말한다.

    크기는 도구마다 한 번씩만 잰다. 고칠 때마다 전체를 다시 직렬화하면 도구 수의 제곱으로 늘어
    600개에서 7초가 걸렸다."""
    out: list[dict[str, Any]] = []
    for t in schemas:
        desc = t.description
        if desc and len(desc) > TOOL_DESC_MAX:
            desc = desc[:TOOL_DESC_MAX] + "…"
        schema = t.input_schema
        omitted = False
        if schema is not None and len(_json_bytes(schema)) > TOOL_SCHEMA_MAX_BYTES:
            schema, omitted = None, True
        out.append(
            {
                "name": t.name,
                "title": t.title,
                "description": desc,
                "input_schema": schema,
                "schema_omitted": omitted,
                "output_schema": t.output_schema,
            }
        )

    # `_json_bytes` 는 기본 구분자라 원소 사이가 ", " 두 바이트다. 여기에 대괄호 둘을 더한 것이
    # `len(_json_bytes(out))` 와 같아야 아래 계산이 실제 크기를 말한다
    sizes = [len(_json_bytes(d)) for d in out]
    total = sum(sizes) + _SEP * max(len(out) - 1, 0) + 2
    if total <= TOOL_SCHEMAS_MAX_BYTES:
        # 흔한 쪽이다. 여기서 끝내지 않으면 넘치지도 않는 목록을 매 요청 한 번 더 직렬화한다
        return out, False

    # 큰 스키마부터 버려 합을 상한 아래로 내린다
    heavy = sorted(
        range(len(out)), key=lambda i: len(_json_bytes(out[i]["input_schema"])), reverse=True
    )
    for i in heavy:
        if total <= TOOL_SCHEMAS_MAX_BYTES:
            break
        if out[i]["input_schema"] is None:
            continue
        out[i]["input_schema"], out[i]["schema_omitted"] = None, True
        after = len(_json_bytes(out[i]))
        total -= sizes[i] - after
        sizes[i] = after

    # 스키마를 다 버려도 넘치면 도구를 뒤에서부터 잘라낸다
    truncated = False
    while total > TOOL_SCHEMAS_MAX_BYTES and out:
        total -= sizes.pop()
        out.pop()
        if out:
            total -= _SEP  # 없어진 원소 앞의 구분자
        truncated = True
    return out, truncated


class HubService:
    # 만든 뒤에 붙이는 훅 (P72 2.4절). MCP Server 서비스가 이 서비스를 받아 만들어지므로 생성자로
    # 받을 수 없다. wiring 의 `_check_wiring` 이 기동 때 채워졌는지 본다
    late_hooks: ClassVar[frozenset[str]] = frozenset({"server_status", "rule_scopes"})
    # Tool 규칙을 컴파일할 테넌트의 사실 (P52 2절). wiring 이 단다. 없으면 규칙을 렌더하지 않는다
    rule_scopes: RuleScopes | None = None

    def __init__(
        self,
        store: MongoHubStore,
        tenancy: TenancyService,
        cluster_service: ClusterService,
        references: ReferenceService,
        *,
        tool_names: HubToolNames | None,
        tool_lister: ToolLister | None = None,
        schema_lister: SchemaLister | None = None,
        svc_tokens: SvcTokens,
        header_ledger: MongoOutsideHeaderSecretStore,
        env_ledger: MongoHubEnvSecretStore,
    ):
        self.store = store
        self.tenancy = tenancy
        self.cluster_service = cluster_service
        # 삭제 가드와 게시 항목 요약이 참조를 센다 (P75 2절)
        self.references = references
        self.tool_lister = tool_lister or _default_tool_lister
        # D49: internal·published 의 도구 이름을 원본에서 계산한다(`hub_tools.HubToolNames`). 그
        # 모듈이 Gateway·MCP Server 모듈을 import 해 여기서 만들 수 없으므로 wiring 이 만들어 넘긴다
        self.tool_names = tool_names
        self.schema_lister = schema_lister or _default_schema_lister
        # 플랫폼 자신이 우리 MCPServer 에 tools/list 할 때 낼 서비스 토큰의 발급 창구 (D41)
        self.svc_tokens = svc_tokens
        # internal 항목의 상태는 원본 MCP Server 자신의 판단이다 (T55). wiring 이 붙인다
        self.server_status: ServerStatus | None = None
        # entry 항목의 고정 헤더 값을 두는 관리 Secret (P86 6.1절). 정리는 리컨실의 한 걸음이다
        self.outside_headers = outside_headers.OutsideHeaderSecrets(
            cluster_service.secret_store,
            namespace=cluster_service.settings.namespace,
            ledger=header_ledger,
            live_ids=store.existing_ids,
        )
        # 새 image 항목의 허용 (P55 6절). 읽기 자격 증명은 플랫폼 네임스페이스의 관리 Secret 이다
        self.image_admission = HubImageAdmission(
            secrets=cluster_service.secret_store, namespace=cluster_service.settings.namespace
        )
        # image 항목의 Secret env 값을 둔 테넌트 Secret (P55 5절). 정리는 리컨실의 한 걸음이다
        self.secret_envs = SecretEnvSecrets(
            ledger=env_ledger,
            clients=self._cluster_client,
            live_ids=store.existing_ids,
            install_id=tenancy.platform_settings.install_id,
        )

    # -- 카탈로그 (P55 4절) ----------------------------------------------------------

    async def catalog(self) -> dict[str, Any]:
        """카탈로그 스냅숏의 답. 허브 이미지 설정(P55 6절)이 켜지기 전에는 카탈로그 이미지를 등록할
        수 없고(`hub_images_unset`) 옮긴 참조도 없다."""
        hub = await self.hub_images()
        return hub_catalog.catalog_out(
            hub_catalog.snapshot(),
            admission=hub.configured,
            prefix=hub.prefix if hub.configured else None,
        )

    async def platform_settings(self) -> PlatformSettings:
        """Platform Settings. 목록은 한 번 읽어 항목마다의 `status` 에 넘긴다."""
        return await self.tenancy.platform_settings.get()

    async def hub_images(self) -> HubImagesSettings:
        """Platform Settings 의 허브 이미지 설정 (P55 6절)."""
        return (await self.platform_settings()).hub_images

    async def refused_digests(self) -> frozenset[str]:
        """platform-admin 이 거부한 image digest (P55 6절)."""
        return frozenset((await self.hub_images()).refused_digests)

    # -- 판정 (D23) ------------------------------------------------------------------

    async def visible(self, principal: Principal, tenant: str) -> list[HubItem]:
        """tenant 에서 principal 에게 보이는 항목 (후보 → view 판정)."""
        return visible(principal, tenant, await self.store.list_candidates(tenant))

    async def get_visible(self, principal: Principal, tenant: str, item_id: str) -> HubItem | None:
        """없거나 view 가 없으면 None. 상세·manifest 의 404 가 존재를 드러내지 않는다. 뒤 판이 더한
        종류의 항목도 view 를 먼저 보고, 볼 수 있는 사람에게만 422 `hub_item_newer` 다 (P46 A0)."""
        try:
            item = await self.store.get_unscoped(item_id)
        except HubItemNewer as e:
            if not can(principal, tenant, e, "view"):
                return None
            raise
        if item is None or not can(principal, tenant, item, "view"):
            return None
        return item

    async def set_access(self, item: HubItem, access: Access, principal: Principal) -> HubItem:
        """정책 저장. 경계 확장은 hub:share, 그 밖은 manage 만으로 된다 (manage 는 호출부 몫).
        플랫폼 항목은 manage 경계를 비운다(P39b 13절). 뒤 판이 쓴 항목은 422 `hub_item_newer`
        다(P55 9절). 이 판은 그 판이 다른 테넌트에 열지 못하게 하는 항목을 가리지 못한다."""
        refuse_newer(item)
        if item.kind == PLATFORM_KIND:
            access = platform_tools.cleared_manage(access)
        validated = self._validated(
            access, current=item.access, owner=item.tenant, kind=item.kind, principal=principal
        )
        refuse_shared_secret_env(item.source, validated, item.tenant)
        if widened_actions(item.access, validated, item.tenant):
            await self._refuse_widening_a_refused_image(item)
        stamp_access(item, validated, principal)
        if not await self.store.save_access(item):
            raise await self._save_missed(item)
        return item

    async def refused_usage(self, digests: Sequence[str]) -> list[dict[str, Any]]:
        """거부한 digest 마다 그것을 든 항목과 그 항목을 백엔드로 쓰는 Gateway 의 수 (P55 6절).
        Platform Settings 카드가 읽는다. digest 로 고정한 image 항목을 읽어 여기서 맞춘다. 태그로만
        적은 항목은 digest 가 없어 들어가지 않는다."""
        items = refused_items(await self.store.digest_pinned_images(), digests)
        out = []
        for digest in digests:
            rows = []
            for item in items[digest]:
                gateways = await self.references.gateway_ids_using_hub_item(item.id)
                rows.append(
                    {
                        "id": item.id,
                        "tenant": item.tenant,
                        "name": item.name,
                        "gateways": len(gateways),
                    }
                )
            out.append({"digest": digest, "items": rows})
        return out

    async def _refuse_widening_a_refused_image(self, item: HubItem) -> None:
        """이미지가 거부된 image 항목은 다른 테넌트에 더 열 수 없다 (P55 6절). 422
        refused_image_not_shareable 이다."""
        digest = image_digest(item.source.image) if item.kind == "image" else None
        if digest is not None and digest in await self.refused_digests():
            raise HubError(
                422,
                "refused_image_not_shareable",
                f"a platform-admin refused image digest {digest}; this item cannot be opened "
                "further to other tenants",
            )

    async def _save_missed(self, item: HubItem) -> Exception:
        """사람의 저장이 맞지 않았을 때의 답 (P68 2절). 다시 읽어 없으면 404, 삭제 중이면 409
        deleting, 그 밖은 그사이 다른 저장이 지나간 것이라 409 revision_conflict 다."""
        fresh, deleting = await self._read_after_miss(item)
        return save_missed(
            HubError, "hub item", item.name, found=fresh is not None, deleting=deleting
        )

    async def _read_after_miss(self, item: HubItem) -> tuple[HubItem | None, bool]:
        return await read_after_miss(
            lambda: self.store.get_unscoped(item.id), lambda fresh: fresh.state == "deleting"
        )

    async def _set(
        self, item: HubItem, names: set[str], *, expect_state: frozenset[str] | None = None
    ) -> bool:
        """플랫폼이 쓰는 칸만 쓴다 (P68 2절). expect_state 가 있으면 그 상태일 때만이다."""
        return await self.store.set_fields(
            item.id, fields_of(item, names), expect_state=expect_state
        )

    @staticmethod
    def _validated(
        access: Access | None, *, current: Access, owner: str, kind: str, principal: Principal
    ) -> Access:
        """kind 를 다른 테넌트에 열 수 있는지와 hub:share 로 검증한다. 다른 테넌트에 열 수 없는
        kind 의 오류(`cross_tenant_unsupported`)에는 kind 를 싣는다."""
        try:
            return validate_access(
                access or Access(),
                current=current,
                owner_tenant=owner,
                cross_tenant=ACTIONS if kind in CROSS_TENANT_KINDS else (),
                share_scope="hub:share",
                principal=principal,
            )
        except AccessError as e:
            extra = {**e.extra, "kind": kind} if e.code == "cross_tenant_unsupported" else e.extra
            raise HubError(e.status, e.code, e.detail, **extra) from e

    def _initial_access(
        self, access: Access | None, *, owner: str, kind: str, principal: Principal
    ) -> Access:
        """생성·게시 시점의 정책. current 는 빈 정책이라 경계를 넓히면 hub:share 가 필요하다."""
        return self._validated(
            access, current=Access(), owner=owner, kind=kind, principal=principal
        )

    # -- 렌더링 -------------------------------------------------------------------

    async def render(self, item: HubItem) -> list[dict[str, Any]]:
        """항목이 소유한 CR 목록 (internal 은 인스턴스 소유라 빈 목록). 자기 audience 로 옮겨 간
        image 항목의 MCPServer 는 그 항목 하나의 audience 만 받는다 (P50 9절). 뒤 판이 쓴 항목은
        422 `hub_item_newer` 라 적용, 재적용, 매니페스트가 모두 멈춘다 (P55 9절)."""
        refuse_newer(item)
        k = item.k8s
        return await self._cr_docs(
            item,
            tenant=item.tenant,
            name=k.name,
            namespace=k.namespace,
            group=k.group or "",
            tool_config=k.tool_config,
            server_id=switched_server_id(item.audience_switch, item.id),
        )

    async def _cr_docs(
        self,
        item: HubItem,
        *,
        tenant: str,
        name: str,
        namespace: str,
        group: str,
        tool_config: str | None,
        server_id: str | None,
        cluster: Any = None,
    ) -> list[dict[str, Any]]:
        """항목이 띄우는 CR. 원본(`render`)과 공유 복제본(`shared_clone_docs`)이 테넌트·이름·
        네임스페이스·audience 만 달리해 같은 문서를 얻는다. 한쪽에만 인자를 더하면 복제본이 원본과
        다른 CR 로 조용히 뜨기 때문이다. 프록시는 MCPRemoteProxy 하나, image 는 [ToolConfig?,
        MCPAuthzConfig?, MCPServer] 이고 그 밖은 빈 목록이다. server_id 는 image 의 MCPServer 가
        자기 audience 로 검증하는 서버(항목이나 복제본 기록)의 id 이고, None 이면 테넌트
        audience 다.
        MCPAuthzConfig 는 그 테넌트(복제본이면 쓰는 테넌트)의 울타리이고, 울타리와 G8 게이트를 모두
        지난 클러스터에만 있다 (P50 11절). 적용은 그것을 서버보다 먼저 하고 받아들여지기를
        기다린다(`apply_in_order`)."""
        src = item.source
        transport = src.transport or DEFAULT_TRANSPORT
        if cluster is None:
            cluster = await self.tenancy.clusters.get(item.cluster_id)
        if item.kind == PLATFORM_KIND:
            return await self._web_tools_docs(item, tenant, name, namespace, group, cluster)
        if proxied(item):
            # 원격 MCP 를 프록시로 감싸는 까닭 하나가 감사다(docs/guide/hub-gateway.md 5절의 remote
            # 행). 끄면 원격 도구 호출이 MCP 감사에 남지 않는다. proxy 모드 게시도 같은 까닭이다(D5)
            return [
                render_remote_proxy(
                    tenant=tenant,
                    item_id=item.id,
                    name=name,
                    namespace=namespace,
                    url=src.url or "",
                    transport=transport,
                    audit=True,
                    # 역할 없는 ServiceAccount (P50 10절). 게이트를 지나지 못한 클러스터는 오늘의
                    # 꼴이다
                    service_account=proxy_service_account(cluster),
                )
            ]
        if item.kind != "image":
            return []
        effective = None
        if cluster is not None:
            effective = resolve_effective(
                await self.tenancy.platform_settings.get(),
                cluster,
                await self.tenancy.tenant_settings.get(tenant),
                ProfileRuntime(),
            )
        docs: list[dict[str, Any]] = []
        # 규칙이 있는 항목은 tools_filter 가 없어도 규칙을 저장할 때의 목록으로 toolsFilter 를
        # 렌더한다 (P52 1절)
        pinned = pinned_tools(item)
        if pinned is not None and tool_config is None:
            tool_config = tool_config_name(name)
        if tool_config:
            docs.append(
                render_tool_config(
                    tenant=tenant,
                    item_id=item.id,
                    name=tool_config,
                    namespace=namespace,
                    tools_filter=pinned or [],
                )
            )
        features = passing_features(cluster) if cluster is not None else frozenset()
        fence = tenant_fence(
            features,
            cr_name=name,
            namespace=namespace,
            tenant=tenant,
            id=item.id,
            rules=await self._item_rules(item, tenant, cluster),
        )
        if fence is not None and fence.config is not None:
            docs.append(fence.config)
        docs.append(
            render_image_server(
                tenant=tenant,
                item_id=item.id,
                name=name,
                namespace=namespace,
                group=group,
                image=src.image or "",
                transport=transport,
                port=src.port,
                tool_config=tool_config,
                effective=effective,
                server_id=server_id,
                fence=fence.spec if fence is not None else None,
                container=image_container(item, tenant),
            )
        )
        return docs

    async def item_rev(self, item: HubItem) -> str | None:
        """항목의 판 첫 절반(`hub_item_rev`)에 Tool 규칙의 지문을 얹은 것 (P52 2절, `rules_rev`).
        규칙을 컴파일할 판인지와 모드가 바뀌면 다시 렌더된다. 뒤 판이 쓴 칸을 든 항목은 그 칸
        이름을 더 얹어, 그 항목의 `hub_item_newer` 막힘이 그 칸을 드는 동안만 맞는다 (P55 9절,
        `newer_rev`)."""
        rev = rules_rev(hub_item_rev(item), await self._item_rules_mark(item))
        return newer_rev(rev, item.newer_fields)

    async def _item_rules_mark(self, item: HubItem) -> str | None:
        if item.kind != "image" or not holds_rules(item.tool_rules.values()):
            return None
        cluster = await self.tenancy.clusters.get(item.cluster_id)
        verdict = cluster is not None and rules_toolhive_ok(cluster)
        policies = await self._item_rules(item, item.tenant, cluster, cached=True)
        return stable_hash([verdict, policies, pinned_tools(item)], 16)

    async def _item_rules(
        self, item: HubItem, tenant: str, cluster: Any, *, cached: bool = False
    ) -> list[str]:
        """image 항목(과 tenant 에 둔 복제본) MCPServer 의 Tool 규칙 forbid (P52 2절). 항목의 역할은
        쓰는 테넌트에서 읽는다. 지문의 서버 id 는 항목 id 다. Gateway 의 Entry 토큰도 그 id 로
        짓는다. 0.53 아래이거나 판을 모르는 클러스터에서는 규칙이 있는 Tool 을 누구에게나 닫는다.
        적용과 다시 렌더는 그런 항목을 건너뛰지만, 어느 길로 렌더되든 열리지 않게 한다."""
        if self.rule_scopes is None or not holds_rules(item.tool_rules.values()):
            return []
        scope = await self.rule_scopes.scope(tenant, cached=cached)
        if cluster is None or not rules_toolhive_ok(cluster):
            return closed_policies(item.tool_rules, scope)
        return item_policies(item.tool_rules, scope, item_id=item.id)

    def rules_held_back(self, item: HubItem, cluster: Any) -> bool:
        """규칙을 든 image 항목(이나 복제본)을 이 클러스터에 적용하지 않는가 (P52 1절). 판이 0.53
        아래이거나 모르면 그 서버는 가진 spec 을 그대로 둔다(`rules_toolhive_old`)."""
        held = item.kind == "image" and holds_rules(item.tool_rules.values())
        return held and (cluster is None or not rules_toolhive_ok(cluster))

    async def _web_tools_docs(
        self, item: HubItem, tenant: str, name: str, namespace: str, group: str, cluster: Any
    ) -> list[dict[str, Any]]:
        """플랫폼 fetch 복제본 (P39b 13절). 이미지는 Platform Settings 의 기본값에 그 클러스터의
        레지스트리 접두사를 붙인 것이다. search 는 플랫폼이 내므로 CR 이 없다."""
        if not platform_tools.is_fetch(item) or cluster is None:
            return []
        settings = await self.tenancy.platform_settings.get()
        effective = resolve_effective(
            settings, cluster, await self.tenancy.tenant_settings.get(tenant), ProfileRuntime()
        )
        return render_fetch_clone(
            tenant=tenant,
            item_id=item.id,
            name=name,
            namespace=namespace,
            group=group,
            image=with_registry(
                platform_tools.fetch_image(settings, self.cluster_service.settings.web_tools_image),
                cluster.images.registry_prefix,
            ),
            fetch=settings.web_tools.fetch,
            ca_bundle=settings.network.ca_bundle,
            effective=effective,
            blocked=platform_tools.blocked_ranges(settings),
        )

    # -- 생성 --------------------------------------------------------------------

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        kind: str,
        source: HubSource,
        cluster_id: str | None,
        access: Access | None = None,
        principal: Principal,
        display_name: str | None = None,
        auth: outside_headers.HeaderInput | None = None,
        catalog: str | None = None,
        secret_values: Mapping[str, str] | None = None,
    ) -> HubItem:
        """항목을 만든다. entry 항목은 인증을 함께 고른다 (P86 6.1절). header 면 헤더 이름과 값을
        검사하고, 항목을 저장한 뒤 값을 관리 Secret 에 넣는다. 값을 넣지 못하면 항목을 지우고 실패를
        올린다.

        image 항목은 컨테이너 입력(stdio, args, env, Secret env)을 받는다 (P55 5절). Secret env 의
        값은 항목의 Secret 에 문서보다 먼저 쓰고, 문서를 쓰지 못하면 그 Secret 을 되돌린다. catalog
        는 항목이 온 카탈로그 서버다. remote·entry 는 주소와 전송이 스냅숏의 것과 같아야 하고,
        자격 증명이 필요한 원격은 그 서버의 헤더를 고정 헤더로 싣는 entry 여야 한다 (P86 A3).
        image 항목은 허브 이미지 설정이 켜져 있으면 허용을 거친다(`_admit`, P55 6절)."""
        if kind not in CROSS_TENANT_KINDS:
            raise HubError(422, "invalid_kind", f"kind {kind!r} cannot be created directly")
        header = self._header_input(kind, auth)
        # 정책 검증(hub:share 등)은 클러스터에 닿기 전에 끝낸다
        stamped = self._initial_access(access, owner=tenant, kind=kind, principal=principal)
        values = dict(secret_values or {})
        self._check_container(kind, source, values, stamped, tenant)
        if await self.store.get_by_name(tenant, name) is not None:
            raise HubError(409, "name_conflict", f"hub item {name!r} already exists")
        if kind in ("remote", "entry"):
            if not source.url or not is_http_url(source.url):
                raise HubError(422, "invalid_value", "source.url must be an http(s) URL")
            if kind == "entry" and not source.transport:
                raise HubError(422, "invalid_value", "entry needs source.transport")
            if source.transport == "sse":
                raise HubError(422, OUTSIDE_SSE_UNSUPPORTED, OUTSIDE_SSE_UNSUPPORTED_TEXT)
        elif not source.image:
            raise HubError(422, "invalid_value", "image needs source.image")
        source.transport = source.transport or DEFAULT_TRANSPORT
        if kind == "image":
            await self._admit(source, catalog, values, principal)
        elif catalog is not None:
            source.catalog = self._catalog_origin(
                kind, catalog, source, header_name=header[0] if header else None
            )
        try:
            cluster = await self.tenancy.resolve_cluster(tenant, cluster_id)
            namespace = await self.tenancy.ensure_tenant_namespace(tenant, cluster)
        except TenancyError as e:
            raise HubError(e.status, e.code, e.detail) from e
        except ClusterUnreachable as e:
            raise HubError(502, "cluster_unreachable", str(e)) from e
        if kind == "remote":
            # 새 원격 프록시는 역할 없는 ServiceAccount 로만 만든다 (P50 2절·10절)
            require_proxy_account(cluster, HubError)
        if header is not None:
            await self._require_https(tenant, cluster, source.url or "")
            # 값을 둘 곳이 없으면 항목을 저장하기 전에 멈춘다
            self.outside_headers.require_store(HubError)
        if source.secret_env:
            # 값을 둘 테넌트 Secret 을 쓸 수 없는 클러스터면 저장하기 전에 멈춘다
            require_tenant_secrets(cluster, HubError)
            for e in source.secret_env:
                e.set = e.name in values
        cr_name = hub_k8s_name(name)
        item = HubItem(
            tenant=tenant,
            name=name,
            display_name=display_name or name,
            kind=kind,  # type: ignore[arg-type]
            source=source,
            cluster_id=cluster.id,
            k8s=HubK8s(
                kind=CR_KIND[kind],
                name=cr_name,
                namespace=namespace,
                group=group_name_for(tenant) if kind == "image" else None,
                tool_config=tool_config_name(cr_name)
                if kind == "image" and source.tools_filter
                else None,
            ),
            tool_names=list(source.tools_filter or []) if kind == "image" else [],
            access=stamped,
            access_updated_by=principal.sub if access is not None else None,
            access_updated_at=utcnow() if access is not None else None,
            state="creating",
            # 새 image 항목은 옛 토큰을 든 소비자가 없어 처음부터 자기 audience 로 검증한다 (P50
            # 9절)
            audience_switch=new_image_switch(kind, utcnow()),
            auth=HubAuth(
                kind="header",
                header_name=header[0],
                set=True,
                rev=outside_headers.new_rev(),
                created_by=principal.sub,
            )
            if header is not None
            else None,
        )
        if source.secret_env:
            item.created_by = principal.sub
            item.secret_env_rev = 1
        await self._insert(item, values)
        if header is not None:
            await self._put_first_value(item, header[1])
        await self.apply(item)
        if header is not None:
            # 값을 실어 불러 본 답을 저장과 함께 돌려준다 (P86 6.3절)
            await self.check_header(item)
        return item

    @staticmethod
    def _check_container(
        kind: str, source: HubSource, values: Mapping[str, str], access: Access, tenant: str
    ) -> None:
        """컨테이너 입력은 image 항목만 받고 (P55 5절), 그 검사는 `hub_env` 다. Secret env 를 든
        항목은 다른 테넌트에 열지 않는다."""
        container = (source.stdio, source.args, source.env, source.secret_env)
        if kind != "image":
            if any(container) or values:
                raise HubError(
                    422, "invalid_value", "stdio, args, env and secret_env are for image items"
                )
            return
        hub_env.check_image_source(source, HubError)
        hub_env.check_secret_values(values, source.secret_names(), HubError)
        refuse_shared_secret_env(source, access, tenant)

    async def _admit(
        self,
        source: HubSource,
        catalog: str | None,
        values: Mapping[str, str],
        principal: Principal,
    ) -> None:
        """새 image 항목의 허용 (P55 6절). 허브 이미지 설정이 꺼져 있으면 카탈로그 서버를 적은
        요청만 422 hub_images_unset 이고 다른 항목은 지금처럼 만든다. 켜져 있으면
        서명을 모아 정하고, 이미지를 digest 로 고정한 참조로 바꾸며 `source.admission` 을 적는다.
        서명이 카탈로그 서버를 이름 대면 그 서버의 규칙이 서고(`hub_env.check_catalog_image`) 그
        서버가 `source.catalog` 다. 요청이 적은 카탈로그 서버가 서명이 정한 것과
        다르면 422 invalid_value 다."""
        settings = await self.tenancy.platform_settings.get()
        hub = settings.hub_images
        # 거부한 digest 는 허용이 꺼져 있어도 새 항목을 만들지 않는다 (P55 6절)
        refuse_refused(image_digest(source.image), hub.refused_digests, error_cls=HubError)
        if not hub.configured:
            if catalog is not None:
                raise HubError(
                    422,
                    "hub_images_unset",
                    "catalog images cannot be imported because image signature checks are not "
                    "set up",
                )
            return
        admitted = await self.image_admission.admit(
            source.image or "",
            hub,
            ca_bundle=settings.network.ca_bundle,
            error_cls=HubError,
        )
        server = admitted.catalog.server if admitted.catalog else None
        if catalog is not None and catalog != server:
            raise HubError(
                422,
                "invalid_value",
                f"catalog names {catalog}, but the image's signatures name "
                + (f"catalog server {server}" if server else "no catalog server"),
            )
        if server is not None:
            hub_env.check_catalog_image(
                hub_catalog.snapshot().server(server),
                source,
                values=values,
                platform_admin=principal.is_platform_admin,
                error_cls=HubError,
            )
        source.image = admitted.reference
        source.catalog = admitted.catalog
        source.admission = admitted.record(principal.sub)

    @staticmethod
    def _catalog_origin(
        kind: str, name: str, source: HubSource, *, header_name: str | None
    ) -> HubCatalogOrigin:
        """remote·entry 항목이 적은 카탈로그 서버 (P55 5절). 지금 스냅숏의 원격 서버와 주소·전송이
        같아야 하고, 자격 증명이 필요한 원격은 header_name(항목이 고정 헤더로 싣는 이름)이 그
        서버의 헤더여야 한다 (P86 A3). image 의 카탈로그 서버는 허용(서명)이 정한다(`_admit`)."""
        catalog = hub_catalog.snapshot()
        server = hub_catalog.catalog_remote(
            catalog,
            name,
            url=source.url,
            transport=source.transport,
            header_name=header_name,
            error_cls=HubError,
        )
        return HubCatalogOrigin(server=server.name, release=catalog.release)

    async def _insert(self, item: HubItem, values: Mapping[str, str]) -> None:
        """문서를 쓴다. 같은 이름이 그사이 생겼으면 409 name_conflict 다. Secret env 값이 있으면
        항목의 Secret 을 먼저 쓰고, 문서를 쓰지 못하면 그 Secret 을 되돌린다 (P55 5절)."""
        client = await self._client(item) if values else None
        if client is not None:
            await self.secret_envs.create(item, values, client)
        try:
            await self.store.create(item)
        except BaseException as e:
            if client is not None:
                await self.secret_envs.forget_written(item, client)
            if isinstance(e, DuplicateKeyError):
                raise HubError(
                    409, "name_conflict", f"hub item {item.name!r} already exists"
                ) from e
            raise

    @staticmethod
    def _header_input(
        kind: str, auth: outside_headers.HeaderInput | None
    ) -> tuple[str, str] | None:
        """만들기의 인증 입력 (P86 6.1절). header 면 (이름, 값)이고 none 이면 None 이다. 인증은
        entry 항목만 받고, oauth 는 422 outside_oauth_unavailable 이다(P86b)."""
        if auth is None or (
            auth.kind == "none" and auth.header_name is None and auth.header_value is None
        ):
            return None
        if kind != "entry":
            raise HubError(422, "invalid_value", "only entry items take auth")
        if auth.kind == "oauth":
            raise HubError(
                422,
                "outside_oauth_unavailable",
                "auth that connects each person's account is not available yet; use none or header",
            )
        if auth.kind != "header":
            raise HubError(
                422, "invalid_value", "header_name and header_value need auth kind header"
            )
        return (
            outside_headers.check_header_name(auth.header_name, HubError),
            outside_headers.check_header_value(auth.header_value, HubError),
        )

    async def _require_https(self, tenant: str, cluster: Any, url: str) -> None:
        """인증을 싣는 항목의 URL (P86 6.1절). 클러스터 안 Service 주소는 스킴과 상관없이 그
        테넌트의 네임스페이스여야 하고, 그 밖의 주소는 https 여야 한다. 사용자 정보(`user:pass@`)는
        받지 않는다. 헤더가 URL 에 든 다른 자격 증명과 함께 나가지 않게 한다."""
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise HubError(
                422, "invalid_value", "the URL of a hub item with auth holds no user info"
            )
        service = parse_service_url(url)
        if service is not None:
            if service[0] in await self.tenancy.allowed_namespaces(tenant, cluster):
                return
        elif parts.scheme == "https":
            return
        raise HubError(
            422,
            "outside_auth_not_https",
            "a hub item that carries auth needs an https URL, or a Service URL in one of the "
            "tenant's namespaces",
        )

    async def _put_first_value(self, item: HubItem, value: str) -> None:
        """만든 항목의 헤더 값을 넣는다. 못 넣으면 항목을 지운다. 값이 없는 header 항목을 남기지
        않는다."""
        try:
            await self.outside_headers.write(item.id, value, HubError)
        except BaseException:
            await self.store.delete(item.id)
            raise

    async def set_header_value(
        self, item: HubItem, value: str, *, principal: Principal, tenant: str
    ) -> HubItem:
        """header 항목의 값을 바꾸고 헤더 판을 올린다 (P86 6.1절). 그 항목의 테넌트에서, 만든 사람이
        hub:write 를 가진 동안(라우트가 본다)과 tenant-admin 만 된다. 새 판이 소비자의 표시를 바꿔
        재적용이 그 Gateway 를 다시 렌더하고 vMCP 파드를 굴린다 (6.2절)."""
        auth = item.header_auth
        if auth is None:
            raise HubError(422, "invalid_kind", "this hub item carries no fixed header")
        if not self.may_replace_header(item, principal=principal, tenant=tenant):
            raise HubError(
                403,
                "forbidden",
                "only the item's creator or a tenant-admin of its tenant replaces its header value",
            )
        value = outside_headers.check_header_value(value, HubError)
        await self.outside_headers.write(item.id, value, HubError)
        auth.set = True
        auth.rev = outside_headers.new_rev()
        item.updated_at = utcnow()
        if not await self._set(
            item, {"auth.set", "auth.rev", "updated_at"}, expect_state=HUB_NOT_DELETING
        ):
            raise await self._save_missed(item)
        return await self.check_header(item)

    @staticmethod
    def may_replace_header(item: HubItem, *, principal: Principal, tenant: str) -> bool:
        """header 항목의 값을 바꿀 수 있는가 (P86 6.1절, P55 5절). 그 항목의 테넌트에서
        만든 사람이 hub:write 를 가진 동안과 tenant-admin 이다. 응답의 `permissions.replace_header`
        도 이것이다."""
        auth = item.header_auth
        if auth is None:
            return False
        return _creator_or_admin(item, auth.created_by, principal=principal, tenant=tenant)

    @staticmethod
    def may_replace_secret_env(item: HubItem, *, principal: Principal, tenant: str) -> bool:
        """image 항목의 Secret env 값을 바꿀 수 있는가 (P55 5절). header 항목의 값과
        같은 사람들이다. 응답의 `permissions.replace_secret_env` 도 이것이다."""
        if not may_hold_secret_env(item):
            return False
        return _creator_or_admin(item, item.created_by, principal=principal, tenant=tenant)

    async def set_secret_env(
        self, item: HubItem, values: Mapping[str, str], *, principal: Principal, tenant: str
    ) -> HubItem:
        """image 항목의 Secret env 값을 바꾼다 (P55 5절). 보낸 이름만 merge patch 로 바꾸고 다른
        키는 읽지 않고 둔다. 항목의 편집 클레임 아래에서 Secret 을 쓰고, 판을 올리고 누가 언제
        바꿨는지 적은 뒤 렌더를 적용하고, 그다음에야 클레임을 끝낸다. 판은 `hub_item_rev` 에 들어
        적용이 실패하면 재적용이 다시 렌더한다. 클레임이 사는 동안 삭제는 409 revision_conflict
        다."""
        if not may_hold_secret_env(item):
            raise HubError(422, "invalid_kind", "this hub item holds no secret_env")
        if not self.may_replace_secret_env(item, principal=principal, tenant=tenant):
            raise HubError(
                403,
                "forbidden",
                "only the item's creator or a tenant-admin of its tenant replaces its Secret "
                "values",
            )
        if item.state == "deleting":
            raise HubError(409, "deleting", f"hub item {item.name} is being deleted")
        if item.state == "creating":
            raise HubError(409, "not_ready", f"hub item {item.name} is still being created")
        if not values:
            raise HubError(422, "invalid_value", "send at least one secret_env value")
        hub_env.check_secret_values(values, item.source.secret_names(), HubError)
        refuse_newer(item)

        async def prepare() -> KubeClient:
            cluster = await self._cluster(item)
            require_tenant_secrets(cluster, HubError)
            return await run_in_threadpool(self.cluster_service.client_for, cluster)

        async def write(client: KubeClient) -> None:
            # Secret 이 없어 아무것도 쓰지 않았으면 NothingWritten 이고 클레임이 풀린다
            await self.secret_envs.replace(item, values, client, HubError)
            for entry in item.source.secret_env or []:
                entry.set = entry.set or entry.name in values
            item.secret_env_rev = (item.secret_env_rev or 0) + 1
            item.secret_env_updated_by = principal.sub
            item.secret_env_updated_at = item.updated_at = utcnow()
            # 판을 적용보다 먼저 적는다. 적용이 실패해도 재적용이 새 판으로 다시 렌더한다. 플랫폼의
            # 칸이라 클레임과 상관없이 쓰고, 클레임은 사람의 저장과 삭제를 막는다
            if not await self._set(item, _SECRET_ENV_FIELDS, expect_state=HUB_NOT_DELETING):
                raise await self._save_missed(item)
            await self._apply_crs(item)
            # 지금 판으로 렌더했으니 적어 둔다. 재적용이 같은 렌더를 한 번 더 하지 않는다
            await self._stamp_epoch(item, expect_state=HUB_NOT_DELETING)

        await save_with_edit_claim(
            self.store,
            item,
            (),
            prepare=prepare,
            write=write,
            conflict=lambda: HubError(
                409,
                "revision_conflict",
                f"hub item {item.name} changed or started deleting meanwhile",
            ),
        )
        return item

    # -- shared 복제 (P6 F3b) ------------------------------------------------------

    async def shared_clone_docs(
        self,
        item: HubItem,
        *,
        tenant: str,
        namespace: str,
        name: str,
        server_id: str | None = None,
        cluster: Any = None,
    ) -> list[dict[str, Any]]:
        """shared 항목을 참조 테넌트의 네임스페이스에 복제할 CR 들. 이름은 복제본 기록의 것이다.
        cluster 는 복제본이 사는 Gateway 의 클러스터다. 그 클러스터의 이미지 설정과 배치가 복제본에
        간다(플랫폼 fetch 는 원본 클러스터가 없다, P39b 13절).
        entry 는 CR 이 없어 빈 목록이다. 복제본에는 원본 id 와 spec 해시, 이 설치의 라벨
        `daegam.devops.ai.kr/platform-id` 가 붙는다. 스윕은 다른 설치의 복제본을 건드리지 않는다
        (P50 1절). server_id 는 자기 audience 로 옮겨 간 image 복제본 기록의 id 다 (9절)."""
        docs = await self._cr_docs(
            item,
            tenant=tenant,
            name=name,
            namespace=namespace,
            group=group_name_for(tenant),
            tool_config=tool_config_name(name) if pinned_tools(item) is not None else None,
            server_id=server_id,
            cluster=cluster,
        )
        if not docs:
            return []
        h = source_hash(docs)
        install_id = await self.tenancy.platform_settings.install_id()
        for d in docs:
            labels = d["metadata"].setdefault("labels", {})
            labels[LABEL_SOURCE_ID] = item.id
            labels[LABEL_SOURCE_HASH] = h
            labels[LABEL_PLATFORM_ID] = install_id
        return docs

    async def apply(self, item: HubItem) -> HubItem:
        """렌더한 CR 을 그대로 적용한다. 허브 항목에는 Advanced 편집도 잠금 표도 없고 `overrides`
        를 쓰는 경로가 없다. 문서에 값이 남아 있어도 검사 없이 클러스터로 보내지 않는다. 플랫폼
        항목은 자기 CR 이 없다."""
        if item.kind == PLATFORM_KIND:
            return item
        try:
            await self._apply_crs(item)
        except (HubError, ClusterUnreachable) as e:
            item.state = "creating"
            item.last_error = f"{getattr(e, 'code', 'cluster_unreachable')}: {e}"
            item.updated_at = utcnow()
            await self._set(item, _LIFECYCLE_FIELDS, expect_state=HUB_NOT_DELETING)
            if isinstance(e, HubError):
                raise
            raise HubError(502, "cluster_unreachable", str(e)) from e
        item.state = "created"
        item.last_error = None
        item.updated_at = utcnow()
        # 그사이 삭제가 시작됐으면 created 를 쓰지 않는다 (P68 2절)
        if await self._set(item, _LIFECYCLE_FIELDS, expect_state=HUB_NOT_DELETING):
            # 지금 코드로 렌더했으니 지금 렌더 번호다
            await self._stamp_epoch(item, expect_state=HUB_NOT_DELETING)
        return item

    async def rerender(self, item: HubItem) -> bool:
        """재적용 단계의 제자리 다시 렌더 (P50 1절). 다시 읽어 아직 살아 있고(`HUB_LIVE_STATES`)
        자기 CR 을 둔 항목이면 `apply` 와 같은 렌더를 그때의 설정으로 다시 적용하고, 지금 렌더
        번호를 살아 있는 상태를 조건으로 적는다. 그 사이 삭제가 시작됐거나 문서가 없으면 적용하지
        않고 False 다. 상태(`state`·`last_error`)는 그대로 두고, 막힘이 맞대는 `updated_at` 도
        바꾸지 않는다. 실패는 그대로 올라가 재적용 단계가 백오프나 막힘으로 적는다."""
        fresh = await self.store.get_unscoped(item.id)
        if fresh is None or fresh.state not in HUB_LIVE_STATES or not owns_crs(fresh):
            return False
        await self._apply_crs(fresh, authz_wait=REAPPLY_AUTHZ_WAIT_SECONDS)
        return await self._stamp_epoch(fresh, expect_state=HUB_LIVE_STATES)

    async def _apply_crs(self, item: HubItem, *, authz_wait: float = AUTHZ_WAIT_SECONDS) -> None:
        """항목이 소유한 CR 을 렌더해 그 클러스터에 적용한다. `apply` 와 `rerender` 가 함께 쓴다.
        울타리 설정은 서버보다 먼저 적용하고 받아들여지기를 기다린다 (P50 11절)."""
        cluster = await self.tenancy.clusters.get(item.cluster_id)
        if self.rules_held_back(item, cluster):
            logger.warning(
                "hub item %s holds Tool rules on a cluster below ToolHive 0.53; its MCPServer "
                "keeps the spec it has (rules_toolhive_old)",
                item.id,
            )
            return
        client = await self._client(item)
        await apply_in_order(client, await self.render(item), HubError, wait=authz_wait)

    async def _stamp_epoch(
        self, item: HubItem, *, expect_state: frozenset[str] | None = None
    ) -> bool:
        """지금 판(`hub_item_rev` 와 렌더 번호)을 적는다. 재적용 칸만 쓰고 expect_state 가 있으면 그
        상태일 때만이다."""
        cluster = await self.tenancy.clusters.get(item.cluster_id)
        passing = passing_features(cluster) if cluster is not None else frozenset()
        item.mark_rendered(await self.item_rev(item), render_epoch(KIND_HUB_ITEM, passing))
        return await self.store.set_fields(
            item.id, fields_of(item, AUTH_FIELDS), expect_state=expect_state
        )

    async def _cluster(self, item: HubItem) -> Any:
        """항목의 클러스터. 없으면 422 unknown_cluster 다."""
        refuse_platform(item, "it has no cluster")
        cluster = await self.tenancy.clusters.get(item.cluster_id)
        if cluster is None:
            raise HubError(422, "unknown_cluster", f"cluster {item.cluster_id} missing")
        return cluster

    async def _client(self, item: HubItem) -> KubeClient:
        return await run_in_threadpool(self.cluster_service.client_for, await self._cluster(item))

    async def _cluster_client(self, cluster_id: str) -> KubeClient | None:
        """클러스터 id 의 클라이언트. 없는 클러스터는 None 이다. 리컨실의 정리가 쓴다."""
        cluster = await self.tenancy.clusters.get(cluster_id)
        if cluster is None:
            return None
        return await run_in_threadpool(self.cluster_service.client_for, cluster)

    # -- 상태 --------------------------------------------------------------------

    async def live_cr(self, item: HubItem) -> dict[str, Any] | None:
        if item.k8s.kind is None or item.kind == PLATFORM_KIND:
            return None
        client = await self._client(item)
        return await run_in_threadpool(
            client.get, TOOLHIVE_API, item.k8s.kind, item.k8s.name, item.k8s.namespace
        )

    def backend_url(self, item: HubItem, status_url: str | None = None) -> str:
        """Gateway 그룹의 Entry 래퍼가 가리킬 URL (kind 별 규칙, 실측 기반)."""
        k = item.k8s
        if item.kind in ("entry", PLATFORM_KIND):
            return item.source.url or ""
        if item.kind == "published" and not proxied(item):
            return item.source.url or vmcp_cluster_url(k.name, k.namespace)
        # 프록시(remote·proxy 모드 게시)는 MCPRemoteProxy 를, internal·image 는 MCPServer 프록시를
        # 거친다
        if status_url:
            return proxy_mcp_url(status_url, item.source.transport or DEFAULT_TRANSPORT)
        return proxy_service_url(k.name, k.namespace, remote=proxied(item))

    async def status(
        self,
        item: HubItem,
        *,
        server: MCPServer | None = None,
        settings: PlatformSettings | None = None,
    ) -> dict[str, Any]:
        """항목 상태와 그 항목의 경고. Tool 규칙(P52 7절), 고정 헤더(P86 6.4절), SSE 서버(P54b
        15절 3), image 의 허용과 거부(P55 6절)다. Platform Settings 는 플랫폼 항목의 상태와 image
        의 경고가 읽는다. 목록은 한 번 읽어 settings 로 넘기고, 없으면 필요할 때 읽는다."""
        if settings is None and item.kind in (PLATFORM_KIND, "image"):
            settings = await self.platform_settings()
        out = await self._status(item, server=server, settings=settings)
        out["warnings"] = (
            await self._rule_warnings(item) + header_warnings(item) + sse_warnings(item)
        )
        if item.kind == "image" and settings is not None:
            out["warnings"] += image_warnings(item, settings.hub_images)
        return out

    async def _rule_warnings(self, item: HubItem) -> list[str]:
        """image 항목의 Tool 규칙이 낳는 경고. header 모드, 0.53 아래 클러스터, 이미지가 더 이상
        내놓지 않는 규칙의 Tool 이다."""
        if item.kind != "image" or not holds_rules(item.tool_rules.values()):
            return []
        out: list[str] = []
        if not self.cluster_service.settings.token_auth:
            out.append(rule_warning(RULES_NOT_ENFORCED))
        if self.rules_held_back(item, await self.tenancy.clusters.get(item.cluster_id)):
            out.append(rule_warning(RULES_TOOLHIVE_OLD))
        missing = sorted(
            n for n, r in item.tool_rules.items() if item.tool_names and n not in item.tool_names
        )
        if missing:
            out.append(rule_warning(RULES_TOOL_MISSING, ", ".join(missing)))
        return out

    async def _status(
        self,
        item: HubItem,
        *,
        server: MCPServer | None = None,
        settings: PlatformSettings | None = None,
    ) -> dict[str, Any]:
        """CR status 요약. 예외 없이 error 필드로. 읽지 못한 이유는 MCP Server 와 Gateway 의 상태와
        같은 `코드: 설명` 한 줄이다(cluster_unreachable·cluster_forbidden).

        internal 항목은 원본 MCP Server 의 상태를 그대로 쓴다 (T55). server 는 부르는 쪽이 이미 읽은
        원본 문서다(`hub_tools.origin_server`). CR phase 만 보면 스펙을 바꾼
        직후 MCP Server 화면이 Rolling 이라고 하는 동안 오퍼레이터가 잠깐 낸 Failed 나 옛 파드의
        Ready 를 읽어, 허브 화면과 Gateway 배포 검사가 MCP Server 화면과 다르게 말한다."""
        out: dict[str, Any] = {
            "phase": None,
            "ready": False,
            "url": None,
            "message": None,
            "observed_at": iso_utc(utcnow()),
            "error": None,
        }
        if item.kind == PLATFORM_KIND:
            # 설정이 먼저 답한다. 켰으면 ready, 껐으면 platform_tool_disabled (P39b 13절)
            return platform_tools.status(item, settings or await self.platform_settings())
        if item.k8s.kind is None:
            # entry: CR 이 없다. URL 만 있으면 쓸 수 있다고 본다
            out.update(phase="Valid", ready=bool(item.source.url), url=item.source.url)
            return out
        if item.kind == "internal" and self.server_status is not None:
            st = await self.server_status(item, server)
            if st is not None:
                phase = st["phase"]
                out.update(
                    phase=phase,
                    ready=phase in READY_PHASES["MCPServer"],
                    url=self.backend_url(item, st["url"]) if st["url"] else None,
                    message=st["message"],
                    error=st["error"],
                )
                return out
        try:
            cr = await self.live_cr(item)
        except HubError as e:
            out["error"] = f"{e.code}: {e.detail}"
            return out
        except ClusterUnreachable as e:
            out["error"] = f"cluster_unreachable: {e}"
            return out
        except AccessDenied as e:
            # 권한이 모자란 원격 클러스터 하나가 목록과 Gateway 배포 검사를 500 으로 만들면 안 된다
            out["error"] = f"cluster_forbidden: {e.missing}"
            return out
        except Exception as e:  # noqa: BLE001 - 상태 조회는 예외를 던지지 않는다
            out["error"] = f"{type(e).__name__}: {e}"
            return out
        if cr is None:
            out["phase"] = "Missing"
            out["message"] = f"{item.k8s.kind} {item.k8s.namespace}/{item.k8s.name} not found"
            return out
        st = cr.get("status") or {}
        phase = st.get("phase")
        out["phase"] = phase
        out["ready"] = phase in READY_PHASES.get(item.k8s.kind, set())
        out["message"] = st.get("message")
        if out["ready"] and item.stdio_probe is not None:
            # CR 은 Ready 지만 stdio 프로세스가 답하지 않는다 (P55 5절). 플랫폼이 프록시 파드를 다시
            # 만드는 중이다
            out.update(
                phase=NOT_ANSWERING,
                ready=False,
                message="the backend pod runs but the item does not answer tools/list; the "
                "platform is recreating the proxy pod",
            )
        if item.kind == "published":
            # entry 모드는 CR(원본 vMCP)에 url 이 없어도 source.url 로 소비 주소를 안다
            out["url"] = self.backend_url(item, st.get("url"))
        else:
            out["url"] = self.backend_url(item, st.get("url")) if st.get("url") else None
        return out

    # -- stdio 항목의 점검 (P55 5절) ------------------------------------------------------

    async def probe_stdio(self, now: datetime | None = None) -> dict[str, Any]:
        """만들어진 stdio 항목마다 `svc:platform` 으로 tools/list 를 해 본다. ToolHive 0.53 은 stdio
        프로세스가 끝나면 백엔드 컨테이너를 다시 띄우지만 프록시가 새 프로세스에 붙지 않아, CR 이
        Ready 인 채로 모든 호출이 실패한다 (F0). 백엔드 파드가 Running 인데 답이 없으면 상태가
        `not_answering` 이 되고, 실패가 `STDIO_CONFIRM` 넘게 이어지면 그 점검이 지난 프록시 파드를
        그 uid 를 전제 조건으로 지워 ToolHive 가 다시 만들게 한다. 같은 항목은 2분에서 시작해
        30분까지 두 배로 늘어나는 백오프 뒤에만 다시 지운다. 리컨실 틱(60초)마다 돈다. 항목 하나의
        실패는 그 항목만 건너뛴다."""
        now = now or utcnow()
        restarted: list[str] = []
        for item in await self.store.stdio_items():
            if item.newer_fields:
                continue
            try:
                if await self._probe_one(item, now):
                    restarted.append(item.id)
            except Exception as e:  # noqa: BLE001 - 항목 하나가 점검 전체를 멈추지 않는다
                logger.info("stdio probe of hub item %s skipped: %s", item.id, type(e).__name__)
        return {"restarted": restarted} if restarted else {}

    async def _answers(self, item: HubItem) -> bool | None:
        """항목이 tools/list 에 답하는가. 요청 전의 실패(클러스터, 토큰)와 401·403 은 stdio
        프로세스의 일이 아니라 판정하지 않는다(None). 프록시가 답했기 때문이다."""
        try:
            client, headers = await self._client(item), await self._headers_for(item)
        except Exception as e:  # noqa: BLE001 - 요청을 보내지 못한 것은 점검이 아니다
            logger.info("stdio probe of hub item %s not sent: %s", item.id, type(e).__name__)
            return None
        try:
            await self.tool_lister(client, self.backend_url(item), headers)
        except Exception as e:  # noqa: BLE001 - 답하지 않는 까닭은 가리지 않는다
            return None if isinstance(_leaf_exception(e), AuthRefused) else False
        return True

    async def _probe_one(self, item: HubItem, now: datetime) -> bool:
        """항목 하나를 점검한다. 프록시 파드를 지웠으면 참이다."""
        answers = await self._answers(item)
        if answers is None:
            return False
        if answers:
            if item.stdio_probe is not None:
                await self.store.clear_stdio_probe(item.id)
            return False
        client = await self._client(item)
        k = item.k8s
        backend = await run_in_threadpool(
            client.list, "v1", "Pod", k.namespace, {LABEL_MCP_BACKEND: k.name}
        )
        if not any((p.get("status") or {}).get("phase") == "Running" for p in backend):
            # 백엔드가 뜨지 않은 것은 다른 문제다. 상태가 CR 의 phase 로 보인다
            return False
        probe = item.stdio_probe
        if probe is None:
            # 처음 본 실패는 적기만 한다. 한 번의 일시적인 실패로 파드를 지우지 않는다
            await self.store.note_stdio_failing(item.id, StdioProbe(failing_since=now))
            return False
        if now < probe.failing_since + STDIO_CONFIRM:
            return False
        if probe.next_delete is not None and now < probe.next_delete:
            return False
        pod = await self._stdio_proxy_pod(client, item)
        if pod is None:
            return False
        delay = min(STDIO_BACKOFF_FIRST * 2**probe.deletes, STDIO_BACKOFF_MAX)
        claimed = StdioProbe(
            failing_since=probe.failing_since,
            last_delete=now,
            next_delete=now + delay,
            deletes=probe.deletes + 1,
        )
        if not await self.store.claim_stdio_delete(item.id, probe.next_delete, claimed):
            return False
        meta = pod["metadata"]
        deleted = await run_in_threadpool(
            client.delete, "v1", "Pod", meta["name"], k.namespace, uid=meta["uid"]
        )
        if deleted:
            logger.info("proxy pod %s of stdio hub item %s deleted", meta["name"], item.id)
        return bool(deleted)

    async def _stdio_proxy_pod(self, client: KubeClient, item: HubItem) -> dict[str, Any] | None:
        """지울 프록시 파드. 이 설치가 그 테넌트에 둔 네임스페이스에서, 항목의 CR 이름으로 막 나열한
        프록시 파드 가운데 소유 사슬이 그 항목의 프록시 Deployment 에 닿는 하나다. RBAC 가 파드
        지우기를 라벨로 좁히지 못해 코드가 좁힌다. ReplicaSet 은 읽을 권한이 없어, 파드의 소유자
        ReplicaSet 이름이 `<Deployment>-<pod-template-hash>` 이고 그 Deployment 를 그 MCPServer 가
        소유하는지 본다. 프록시는 레플리카 하나라 점검이 지난 파드가 그것이다."""
        k = item.k8s
        cluster = await self._cluster(item)
        if k.namespace not in await self.tenancy.allowed_namespaces(item.tenant, cluster):
            return None
        deployment = await run_in_threadpool(
            client.get, "apps/v1", "Deployment", k.name, k.namespace
        )
        if not owned_by(deployment, "MCPServer", k.name):
            return None
        pods = await run_in_threadpool(
            client.list,
            "v1",
            "Pod",
            k.namespace,
            {LABEL_APP_NAME: APP_MCPSERVER, LABEL_APP_INSTANCE: k.name},
        )
        for pod in pods:
            meta = pod.get("metadata") or {}
            rs = f"{k.name}-{(meta.get('labels') or {}).get('pod-template-hash', '')}"
            if meta.get("uid") and owned_by(pod, "ReplicaSet", rs, controller=True):
                return pod
        return None

    async def resolve_origins(self, items: Sequence[HubItem]) -> dict[str, Any]:
        """`internal`·`published` 가 원본에서 가져오는 것 (D49, D50). 도구 이름과 MCP 주소다.

        해석기(`tool_names`)가 None 이면 빈 dict 다. 원본 이름이 필요 없는 테스트가 그렇게
        만들고, 그때 호출부는 저장된 값을 그대로 쓴다.
        """
        if self.tool_names is None or not items:
            return {}
        return await self.tool_names.resolve(items)

    async def _bearer_for(self, item: HubItem) -> str | None:
        """우리 MCPServer(internal·image)는 서비스 토큰을 요구하므로 호출마다 짧은 토큰을 낸다
        (D41). 프록시 없이 게시한 Gateway 는 vMCP 에 바로 묻으므로 vMCP 가 받는 토큰을
        낸다(local 모드뿐이다). 플랫폼이 제 이름으로 내는 것이라 부르는 사람의 권한이 이 토큰에
        실리지는 않는다."""
        tokens = self.svc_tokens
        if tokens is None:
            return None
        ours = item.kind in TOKEN_BACKEND_KINDS
        if not ours and not (
            item.kind == "published" and not proxied(item) and tokens.mints_gateway_tokens
        ):
            return None
        if not tokens.available:
            raise HubError(503, "signer_unavailable", "the platform signing key is not loaded")
        if ours:
            # 그 서버 하나의 audience 를 함께 싣는다 (P50 9절)
            return await tokens.platform_token(item.tenant, item.server_id)
        # code mode Gateway 의 상태가 vMCP 를 나열할 때와 같은 토큰이다 (`SvcTokens.vmcp_bearer`)
        return await tokens.vmcp_bearer(item.tenant)

    async def _headers_for(self, item: HubItem) -> dict[str, str]:
        """플랫폼이 그 항목을 부를 때 싣는 헤더. 외부 서버 항목은 그 고정 헤더이고(값이 없으면
        싣지 않아 서버가 거절한다, P86 6.3절), 우리 서버와 게시된 vMCP 는 서비스 토큰이다."""
        auth = item.header_auth
        if auth is not None:
            value = await self.outside_headers.read(item.id)
            return {auth.header_name: value} if value and auth.header_name else {}
        bearer = await self._bearer_for(item)
        return {"Authorization": f"Bearer {bearer}"} if bearer else {}

    async def list_tools_at(self, item: HubItem, url: str) -> list[str]:
        """항목이 사는 클러스터의 클라이언트로 url 에 tools/list (D21 경로 선택은 클라이언트 몫).
        헤더를 싣는 항목은 그 답을 확인으로 적는다(`_record_check`)."""
        client, headers = await self._client(item), await self._headers_for(item)
        try:
            names = await self.tool_lister(client, url, headers)
        except Exception as e:
            await self._record_check(item, check_outcome(e))
            raise
        await self._record_check(item, CHECK_OK)
        return names

    async def list_tool_schemas_at(self, item: HubItem, url: str) -> ToolsResult:
        """같은 경로로 이름·설명·입력 스키마까지 받는다. 확인을 적는 것도 같다."""
        client, headers = await self._client(item), await self._headers_for(item)
        try:
            res = await self.schema_lister(client, url, headers)
        except Exception as e:
            await self._record_check(item, check_outcome(e))
            raise
        await self._record_check(item, CHECK_OK)
        return res

    async def check_header(self, item: HubItem) -> HubItem:
        """헤더 값을 실어 그 항목의 URL 에 tools/list 를 불러 본다 (P86 6.3절). vMCP 의 점검은
        거절된 값을 정상으로 읽으므로 플랫폼이 헤더를 실어 부를 때마다 그 답을 적고(Tool 새로고침,
        Tools 탭, Gateway 배포의 도달 확인), 만들기와 값 바꾸기는 그 답을 저장과 함께 돌려준다."""
        if item.header_auth is None:
            return item
        try:
            await self.list_tools_at(item, item.source.url or "")
        except Exception:  # noqa: BLE001, S110 - 답은 `_record_check` 가 적었다
            pass
        return item

    async def _record_check(self, item: HubItem, outcome: str) -> None:
        """헤더를 싣는 항목이면 확인의 답을 적는다. 다른 항목은 하지 않는다."""
        auth = item.header_auth
        if auth is None:
            return
        auth.check = HubAuthCheck(outcome=outcome, at=utcnow())
        # 검사 칸만 쓴다. 그사이 바뀐 값의 판을 되돌리지 않는다
        await self._set(item, {"auth.check"}, expect_state=HUB_NOT_DELETING)

    async def tool_schemas(self, item: HubItem) -> dict[str, Any]:
        """도구 탭이 부르는 것. 그 자리에서 tools/list 를 해서 자세한 판을 돌려준다.

        **저장하지 않는다.** `tool_names` 를 쓰는 다섯 자리 중 라이브 조회를 거치는 것은
        `refresh_tools` 하나뿐이고 나머지 넷(프로필 게시, 재배포 동기화, `tools_filter`,
        Gateway 게시)은 이름만 다시 쓴다. 스키마를 문서에 담으면 재배포 한 번에 저장된 스키마가
        옛것이 되어 같은 화면이 서로 다른 것을 보인다. 그때그때 물어보면 그 어긋남 자체가 없다.

        못 물어보면 올리지 않고 까닭을 담아 돌려준다. 도구 탭은 그때 저장된 이름 칩으로 떨어진다.
        도구가 무엇인지 못 보는 것은 불편이지 오류가 아니라서 화면을 빨간 줄로 덮지 않는다.
        플랫폼 항목은 계약 파일과 플랫폼 앱의 정의를 그대로 낸다 (P39b 15절)."""
        if item.kind == PLATFORM_KIND:
            tools = [
                ToolSchema(
                    name=t["name"], description=t["description"], input_schema=t["input_schema"]
                )
                for t in platform_tools.tool_schemas(item)
            ]
            listed, truncated = _capped(tools)
            return {"tools": listed, "listed": True, "reason": None, "truncated": truncated}
        st = await self.status(item)
        if st["error"]:
            return {"tools": [], "listed": False, "reason": st["error"], "truncated": False}
        if not st["url"]:
            return {
                "tools": [],
                "listed": False,
                "reason": f"hub item has no URL yet (phase={st['phase']})",
                "truncated": False,
            }
        try:
            res = await self.list_tool_schemas_at(item, st["url"])
        except Exception as e:  # noqa: BLE001 - MCP 연결·프로토콜 오류 전부
            leaf = _leaf_exception(e)
            return {
                "tools": [],
                "listed": False,
                "reason": f"{type(leaf).__name__}: {leaf}",
                "truncated": False,
            }
        tools, truncated = _capped(res.tool_schemas)
        if truncated:
            logger.warning(
                "tool schemas truncated: item=%s kept=%s of=%s", item.id, len(tools), len(res.tools)
            )
        return {"tools": tools, "listed": True, "reason": None, "truncated": truncated}

    async def refresh_tools(self, item: HubItem) -> HubItem:
        """프록시 URL 로 tools/list.
        internal 은 프로필 tools 를 쓰므로 여기서는 라이브 조회만 한다. 플랫폼 항목은 422
        platform_item 이다."""
        refuse_platform(item, "refresh")
        st = await self.status(item)
        if st["error"]:
            raise HubError(502, "cluster_unreachable", st["error"])
        if not st["url"]:
            raise HubError(409, "not_ready", f"hub item has no URL yet (phase={st['phase']})")
        try:
            names = await self.list_tools_at(item, st["url"])
        except Exception as e:  # noqa: BLE001 - MCP 연결·프로토콜 오류 전부
            leaf = _leaf_exception(e)
            raise HubError(
                502, "tools_list_failed", f"{type(leaf).__name__}: {leaf} (url={st['url']})"
            ) from e
        item.tool_names = list(names)
        item.tool_names_at = utcnow()
        item.status = st
        item.updated_at = utcnow()
        # 도구 칸만 쓴다. 목록을 받는 사이 저장된 접근 정책을 되돌리지 않는다 (P68 2절)
        fields = {"tool_names", "tool_names_at", "status", "updated_at"}
        if not await self._set(item, fields, expect_state=HUB_NOT_DELETING):
            raise await self._save_missed(item)
        return item

    # -- Tool 규칙 (P52 1절) --------------------------------------------------------

    async def set_tool_rules(
        self, item: HubItem, rules: dict[str, ItemToolRule], *, oidc: bool
    ) -> tuple[HubItem, RuleNotice]:
        """image 항목의 규칙을 저장하고 항목을 다시 적용한다. 키는 지금 항목이 내놓는 Tool
        (tools_filter 가 있으면 그것, 없으면 tool_names)이어야 하고, 역할만 받는다.
        규칙이 있으면 그때의 Tool 목록을 rules_tool_names 로 남긴다. tools_filter 가 없는 항목은
        그 목록으로 toolsFilter 를 렌더해, 이미지가 더하거나 이름을 바꾼 Tool 은 다시 저장할
        때까지 내놓지 않는다. (항목, 알림). 저장 뒤의 적용이 렌더하므로 뒤 판이
        쓴 항목은 규칙을 쓰기 전에 422 `hub_item_newer` 다 (P55 9절)."""
        refuse_platform(item, "set tool rules")
        refuse_newer(item)
        if item.kind != "image":
            raise HubError(422, "invalid_kind", f"kind {item.kind!r} cannot hold Tool rules")
        rules = normalized_rules(rules)
        if holds_rules(rules.values()):
            cluster = await self.tenancy.clusters.get(item.cluster_id)
            if cluster is None or not rules_toolhive_ok(cluster):
                # 0.53 아래 클러스터의 항목 서버는 규칙을 실을 수 없다 (P52 1절)
                found = toolhive_has(cluster) if cluster is not None else "an unknown cluster"
                raise HubError(
                    422,
                    "rules_toolhive_old",
                    f"Tool rules need ToolHive 0.53.0; this item's cluster has {found}",
                    needed="ToolHive 0.53.0",
                    found=found,
                )
        offered = list(item.source.tools_filter or item.tool_names)
        try:
            check_rule_keys(rules, offered, code="rule_names_unknown_tool", hub_item_id=item.id)
            for name, rule in rules.items():
                check_callers(rule.callers, oidc=oidc, where=name, roles_only=True)
            if self.rule_scopes is not None and holds_rules(rules.values()):
                scope = await self.rule_scopes.scope(item.tenant)
                check_cedar_size(item_policies(rules, scope, item_id=item.id), object="MCPServer")
        except ToolRuleError as e:
            raise HubError(e.status, e.code, e.detail, **e.extra) from e
        notice = notice_of(item.tool_rules, rules)
        item.tool_rules = rules
        item.rules_tool_names = offered if holds_rules(rules.values()) else []
        item.updated_at = utcnow()
        fields = {
            "tool_rules": {k: v.model_dump(mode="json") for k, v in rules.items()},
            "rules_tool_names": list(item.rules_tool_names),
            "updated_at": item.updated_at,
        }
        if not await self.store.set_fields(item.id, fields, expect_state=HUB_NOT_DELETING):
            raise await self._save_missed(item)
        return await self.apply(item), notice

    # -- 삭제 --------------------------------------------------------------------

    async def delete(self, item: HubItem) -> None:
        """published 는 게시 해제다 (proxy 모드면 MCPRemoteProxy CR 도 지운다).
        internal 도 게시 해제다. CR 은 배포 소유라 문서만 지운다 (D23). 플랫폼 항목은 지우지
        못한다(Platform Settings 가 끈다).

        객체는 렌더하지 않고 저장된 이름으로 지운다(`owned_objects`). 그래서 뒤 판이 쓴 항목도
        지운다 (P55 9절). 그 항목이 Secret env 를 든 image 면 CR 다음에 그 Secret 도 이름으로
        지우고, 문서는 마지막에 지운다. 그때는 무엇이든 지우기 전에 테넌트 Secret 을 쓸 수 있는
        클러스터인지 본다(`require_tenant_secrets`). 삭제의 시작은 살아 있는 편집 클레임이 없을
        때만이다. Secret 값을 바꾸는 동안의 삭제는 409 revision_conflict 라, 바꾸기가 지운 MCPServer
        를 다시 만들지 않는다 (5절)."""
        refuse_platform(item, "delete")
        await self.references.refuse_any(
            "hub-item", item.id, HUB_ITEM_DELETE, HubError, "hub item is used by gateways"
        )
        if may_hold_secret_env(item):
            cluster = await self.tenancy.clusters.get(item.cluster_id)
            if cluster is not None:
                require_tenant_secrets(cluster, HubError)
        if item.state != "deleting":
            item.state = "deleting"
            item.updated_at = utcnow()
            if not await self.store.start_delete(item):
                fresh, deleting = await self._read_after_miss(item)
                if fresh is None:
                    return
                if not deleting:
                    # 살아 있는 편집 클레임이 있다 (Secret 값 바꾸기, P55 5절)
                    raise HubError(
                        409,
                        "revision_conflict",
                        f"hub item {item.name} is being changed; try the delete again",
                    )
                item = fresh  # 다른 삭제가 먼저 시작했다. 그 단계를 이어 간다
        # 이미 deleting 인 항목은 전이 없이 단계를 이어 간다. 클러스터에 닿지 못해 멈춘 삭제를
        # 다시 DELETE 해서 끝내는 길이다 (P68 2절)
        try:
            await self._delete_objects(item)
        except ClusterUnreachable as e:
            item.last_error = f"cluster_unreachable: {e}"
            await self._set(item, {"last_error"})
            raise HubError(502, "cluster_unreachable", str(e)) from e
        if item.auth is not None:
            # 헤더 값의 관리 Secret 이다 (P86 6.1절). 못 지우면 리컨실의 정리가 마저 지운다
            await self.outside_headers.delete(item.id)
        await self.store.delete(item.id)

    async def _delete_objects(self, item: HubItem) -> None:
        """항목의 CR, 곁 객체, Secret env 의 Secret 순으로 지운다. 서버를 먼저 지워 곁 객체를 잃은
        서버가 남지 않게 한다."""
        objects = owned_objects(item)
        if not objects:
            return
        client = await self._client(item)
        namespace = item.k8s.namespace
        for kind, name in objects:
            await run_in_threadpool(client.delete, TOOLHIVE_API, kind, name, namespace)
        if may_hold_secret_env(item):
            await self.secret_envs.delete(item, client)

    # -- internal (MCP Server 게시, docs/guide/hub-gateway.md 6절) ---------------------

    async def publish_instance(
        self,
        inst: MCPServer,
        *,
        name: str | None = None,
        display_name: str | None = None,
        access: Access | None = None,
        principal: Principal,
    ) -> tuple[HubItem, bool]:
        """배포된 MCP Server 를 `internal` 항목으로 게시한다. (항목, 새로 만들었는지).
        이미 게시돼 있으면 그대로 돌려준다(멱등). 이름 기본값은 문서 `name`, 표시 이름 기본값은
        문서 `display_name` 이고 다른 항목과 충돌하면 409 name_conflict 다. CR 은 만들지
        않는다 (문서의 MCPServer 를 가리킨다). `source.mcp_profile_id` 는 `instance_id` 와 같은
        값이다. internal 은 다른 테넌트에 열 수 없다 (422 cross_tenant_unsupported)."""
        existing = await self.store.get_by_instance(inst.id)
        if existing is not None:
            return existing, False
        stamped = self._initial_access(
            access, owner=inst.tenant, kind="internal", principal=principal
        )
        name = (name or inst.name).strip()
        if await self.store.get_by_name(inst.tenant, name) is not None:
            raise HubError(409, "name_conflict", f"hub item {name!r} already exists")
        item = HubItem(
            tenant=inst.tenant,
            name=name,
            display_name=display_name or inst.display_name,
            kind="internal",
            source=HubSource(instance_id=inst.id, mcp_profile_id=inst.id),
            cluster_id=inst.cluster_id,
            k8s=_instance_k8s(inst),
            access=stamped,
            access_updated_by=principal.sub if access is not None else None,
            access_updated_at=utcnow() if access is not None else None,
            state="created",
        )
        await self.store.create(item)
        return item, True

    async def publish_gateway(
        self,
        gw: Gateway,
        *,
        name: str | None = None,
        display_name: str | None = None,
        mode: str = "entry",
        access: Access | None = None,
        principal: Principal,
    ) -> HubItem:
        """배포된 Gateway 의 vMCP 를 `published` 항목으로 게시한다 (docs/guide/hub-gateway.md
        6절). 같은 이름의 항목이 이 Gateway 를 가리키면 그대로 돌려주고(멱등), 다른 것이면 409
        name_conflict 다. 이름 기본값은 문서 `name`, 표시 이름 기본값은 문서 `display_name` 이다
        (D24).

        entry 는 CR 없이 URL 만 둔다(소비자가 Entry 래퍼로 감싼다). proxy 는 MCPRemoteProxy 로
        감싸고 `apply` 가 CR 을 적용하며 실패를 last_error 로 남긴다. published 는 다른 테넌트에 열
        수 없다 (422 cross_tenant_unsupported). 상태·mode 검사는 부르는 쪽 몫이다."""
        k8s = gw.k8s
        assert k8s is not None  # 부르는 쪽이 deployed 를 확인했다
        hub_name = (name or gw.name).strip()
        existing = await self.store.get_by_name(gw.tenant, hub_name)
        if existing is not None:
            if existing.source.gateway_instance_id == gw.id:
                return existing  # 멱등
            raise HubError(409, "name_conflict", f"hub item {hub_name!r} already exists")
        stamped = self._initial_access(
            access, owner=gw.tenant, kind="published", principal=principal
        )
        proxy = mode == "proxy"
        if proxy:
            # 새 원격 프록시는 역할 없는 ServiceAccount 로만 만든다 (P50 2절·10절). 클러스터를
            # 모르면 게이트를 볼 수 없으므로 만들지 않는다
            cluster = await self.tenancy.clusters.get(gw.cluster_id or "")
            if cluster is None:
                raise HubError(422, "unknown_cluster", f"cluster {gw.cluster_id} missing")
            require_proxy_account(cluster, HubError)
        item = HubItem(
            tenant=gw.tenant,
            name=hub_name,
            display_name=display_name or gw.display_name,
            kind="published",
            source=HubSource(
                url=vmcp_cluster_url(k8s.name, k8s.namespace),
                transport=DEFAULT_TRANSPORT,
                gateway_instance_id=gw.id,
            ),
            cluster_id=gw.cluster_id,
            k8s=HubK8s(
                kind="MCPRemoteProxy" if proxy else "VirtualMCPServer",
                name=hub_k8s_name(hub_name) if proxy else k8s.name,
                namespace=k8s.namespace,
            ),
            access=stamped,
            access_updated_by=principal.sub if access is not None else None,
            access_updated_at=utcnow() if access is not None else None,
            state="creating" if proxy else "created",
        )
        await self.store.create(item)
        if proxy:
            await self.apply(item)
        return item

    async def sync_instance(self, inst: MCPServer) -> HubItem | None:
        """재배포 뒤 게시 항목의 CR 참조·클러스터를 맞춘다. 게시돼 있지 않으면 None.

        도구 이름은 맞추지 않는다. `internal` 의 그것은 저장된 사본이 아니라 원본에서 계산하는
        값이다 (D49).
        """
        item = await self.store.get_by_instance(inst.id)
        if item is None:
            return None
        item.source.mcp_profile_id = inst.id
        item.cluster_id = inst.cluster_id
        item.k8s = _instance_k8s(inst)
        item.updated_at = utcnow()
        await self._set(item, {"source", "cluster_id", "k8s", "updated_at"})
        return item

    async def instance_item(self, inst: MCPServer) -> HubItem:
        """inst 를 게시한 `internal` 항목. 게시돼 있지 않으면 404 not_published. 해제는 라우터가
        manage 를 본 뒤 `delete` 로 하므로 `DELETE /hub/{id}` 와 같은 판정이다."""
        item = await self.store.get_by_instance(inst.id)
        if item is None:
            raise HubError(404, "not_published", f"mcp server {inst.id} is not published")
        return item

    async def unregister_internal(self, instance_id: str) -> None:
        """인스턴스 삭제에 따라 게시 항목을 지운다. 참조 중이어도 지운다
        (docs/guide/hub-gateway.md 6절)."""
        item = await self.store.get_by_instance(instance_id)
        if item is not None:
            await self.store.delete(item.id)


def _instance_k8s(inst: MCPServer) -> HubK8s:
    if inst.k8s is None:
        raise HubError(409, "not_deployed", f"mcp server {inst.name} is not deployed")
    return HubK8s(
        kind="MCPServer", name=inst.k8s.name, namespace=inst.k8s.namespace, group=inst.k8s.group
    )


def _leaf_exception(e: BaseException) -> BaseException:
    """anyio TaskGroup 이 감싼 ExceptionGroup 에서 실제 원인을 꺼낸다."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e
