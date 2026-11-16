"""플랫폼 웹 Tool 의 허브 항목과 Gateway 규칙 (P39b 13절).

두 항목 `web-fetch`·`web-search` 는 종류 `platform` 이고 주인 테넌트도 클러스터도 없다. Platform
Settings 가 켜면 만들고, 리컨실 단계가 틱마다 설정에 맞춘다. 끄면 항목은 남아 상태가
`platform_tool_disabled` 가 되고, fetch 의 복제본과 그 기록은 리컨실이 지운다.

`require_same_cluster` 는 같은 클러스터 판정이다(클러스터 없는 항목은 건너뛴다). `check_backends` 는
Gateway 의 웹 백엔드 규칙이다. 노출한 Gateway 에 플랫폼 Tool 을 넣지 않고(관리자가 바깥
클라이언트를 허용하면 넣는다), 웹 Tool 에 닿는 Gateway 는 모든 백엔드의 도구 이름을 알아야 한다.
`GatewayService` 가 부르고 `HubService` 는 부르지 않으므로 순환이 없다.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from daegam.auth.hub_access import ALL_TENANTS, Access, Subjects
from daegam.contracts import web_tools_contract
from daegam.errors import ServiceError
from daegam.k8s.templates.common import NETWORK_POLICY_API
from daegam.k8s.templates.web_tools import FETCH_POLICY_NAME
from daegam.services.web_search import SEARCH_TOOL, WEB_SEARCH_ITEM
from daegam.store.base import iso_utc, utcnow
from daegam.store.hub import (
    PLATFORM_KIND,
    HubItem,
    HubK8s,
    HubSource,
    MongoHubStore,
    other_cluster,
)
from daegam.store.settings import PlatformSettings
from daegam.urlfetch import BLOCKED_V4, BLOCKED_V6
from daegam.urls import WEB_SEARCH_MCP_PATH

logger = logging.getLogger("daegam.platform_tools")

WEB_FETCH_ITEM = "web-fetch"
# Gateway 네임스페이스마다 하나인 fetch 복제본의 CR 이름. 테넌트 항목의
# 복제본(`shared-<이름>-<6자>`) 과 접두사가 달라 겹치지 않는다
FETCH_CLONE_NAME = "platform-web-fetch"
PLATFORM_ITEMS = (WEB_FETCH_ITEM, WEB_SEARCH_ITEM)
# fetch 복제본 곁의 객체 (apiVersion, kind, 이름). 복제본을 지울 때 함께 지운다
FETCH_SIDE_OBJECTS = ((NETWORK_POLICY_API, "NetworkPolicy", FETCH_POLICY_NAME),)
DISPLAY_NAMES = {WEB_FETCH_ITEM: "Web Fetch", WEB_SEARCH_ITEM: "Web Search"}


class PlatformToolError(ServiceError):
    pass


def is_fetch(item: Any) -> bool:
    return getattr(item, "kind", None) == PLATFORM_KIND and item.name == WEB_FETCH_ITEM


def enabled(item: HubItem, settings: PlatformSettings) -> bool:
    web = settings.web_tools
    return web.fetch.enabled if is_fetch(item) else web.search.enabled


def tool_schemas(item: HubItem) -> list[dict[str, Any]]:
    """항목의 도구. fetch 는 계약 파일, search 는 플랫폼 앱의 정의다 (D49 의 계산하는 종류)."""
    if is_fetch(item):
        tools = web_tools_contract()["tools"]
    else:
        tools = [SEARCH_TOOL]
    return [
        {
            "name": t["name"],
            "description": t.get("description"),
            "input_schema": t.get("inputSchema") or {},
        }
        for t in tools
    ]


def tool_names(item: HubItem) -> list[str]:
    return [t["name"] for t in tool_schemas(item)]


def _contract_names() -> list[str]:
    return [t["name"] for t in web_tools_contract()["tools"]]


def status(item: HubItem, settings: PlatformSettings) -> dict[str, Any]:
    """`HubService.status` 와 같은 모양. 켜졌으면 ready 이고 search 는 플랫폼 주소가 URL 이다. fetch
    의 URL 은 Gateway 네임스페이스의 복제본이라 스냅샷이 정한다."""
    on = enabled(item, settings)
    return {
        "phase": "Ready" if on else "Disabled",
        "ready": on,
        "url": item.source.url if on else None,
        "message": None,
        "observed_at": iso_utc(utcnow()),
        "error": None if on else f"platform_tool_disabled: {item.name} is turned off",
    }


def fetch_image(settings: PlatformSettings, pinned: str | None) -> str:
    """fetch 복제본의 이미지. 배포가 못 박은 값(`Settings.web_tools_image`)이 먼저다."""
    return pinned or settings.default_web_tools_image


def blocked_ranges(settings: PlatformSettings) -> list[str]:
    """fetch 복제본의 NetworkPolicy 가 빼는 범위. urlfetch 의 차단 표와 관리자가 막은 CIDR 이다."""
    nets = [*BLOCKED_V4, *BLOCKED_V6]
    nets += [ipaddress.ip_network(n) for n in settings.web_tools.fetch.blocked_cidrs]
    return [str(n) for n in nets]


def initial_access() -> Access:
    """처음 만들 때의 정책. 모든 테넌트가 보고 Gateway 에 넣을 수 있고, manage 경계는 비어
    있다(관리는 platform-admin 만, `can`)."""
    everyone = Subjects(tenants=[ALL_TENANTS])
    return Access(view=everyone, consume=everyone)


def cleared_manage(access: Access) -> Access:
    """플랫폼 항목의 정책 저장은 manage 경계를 비운다. 그래야 consume 경계 하나로 판정이 끝난다."""
    return access.model_copy(update={"manage": Subjects()})


def _item(name: str, issuer_url: str) -> HubItem:
    search = name == WEB_SEARCH_ITEM
    return HubItem(
        tenant="",
        name=name,
        display_name=DISPLAY_NAMES[name],
        kind=PLATFORM_KIND,
        source=HubSource(
            url=f"{issuer_url.rstrip('/')}{WEB_SEARCH_MCP_PATH}" if search else None,
            transport="streamable-http",
        ),
        cluster_id="",
        k8s=HubK8s(kind=None if search else "MCPServer", name=name, namespace=""),
        tool_names=[SEARCH_TOOL["name"]] if search else _contract_names(),
        access=initial_access(),
    )


class PlatformTools:
    """두 항목을 설정에 맞추고, 꺼진 fetch 의 복제본을 지운다."""

    def __init__(self, store: MongoHubStore, settings_store: Any, clones: Any, *, issuer_url: str):
        self.store = store
        self.settings_store = settings_store
        # 복제본의 배포 경로(`SharedClones`). fetch 를 끄면 복제본을 지우고 켜면 되살린다
        self.clones = clones
        self.issuer_url = issuer_url

    async def sync(self, settings: PlatformSettings | None = None) -> list[str]:
        """켜진 Tool 의 항목이 없으면 만들고, search 의 주소가 바뀌었으면 고친다. 만든 이름을
        돌려준다.
        꺼진 Tool 의 항목은 그대로 둔다(상태가 꺼짐을 말한다)."""
        settings = settings or await self.settings_store.get()
        made: list[str] = []
        for name in PLATFORM_ITEMS:
            want = _item(name, self.issuer_url)
            have = await self.store.get_platform(name)
            if have is None:
                if not enabled(want, settings):
                    continue
                try:
                    await self.store.create(want)
                    made.append(name)
                except Exception as e:  # noqa: BLE001 - 다른 레플리카가 먼저 만들었을 수 있다
                    logger.info("platform item %s not created: %s", name, e)
                continue
            if have.source.url != want.source.url or have.tool_names != want.tool_names:
                # 저장된 출처에 이 판의 칸만 덮어 뒤 판이 쓴 칸을 남긴다 (P55 9절)
                kept, known = (
                    have.source.model_dump(mode="json"),
                    want.source.model_dump(mode="json"),
                )
                source = {**kept, **known}
                await self.store.set_fields(
                    have.id,
                    {"source": source, "tool_names": want.tool_names, "updated_at": utcnow()},
                )
        return made

    async def sweep(self, now: datetime | None = None) -> dict[str, list[str]]:
        """리컨실의 단계 `platform_tools`. 항목을 맞추고, fetch 가 꺼졌으면 그 복제본과 기록을
        지운다. 그래야 재적용이 다시 렌더하지 않는다. 켜져 있으면 그것을 백엔드로 배포한 Gateway
        가운데 복제본이 없는 것(끈 동안 지운 것)에 다시 만든다. now 는 리컨실 단계의 꼴이라 받기만
        한다."""
        del now
        settings = await self.settings_store.get()
        result = {"created": await self.sync(settings), "deleted": [], "restored": []}
        item = await self.store.get_platform(WEB_FETCH_ITEM)
        if item is not None and not settings.web_tools.fetch.enabled:
            result["deleted"] = await self.clones.delete_source(item.id)
        elif item is not None:
            result["restored"] = await self.clones.restore(item)
        return {k: v for k, v in result.items() if v}


def _reachable_platform(items: Iterable[HubItem]) -> list[HubItem]:
    return [i for i in items if i.kind == PLATFORM_KIND]


def require_same_cluster(
    items: Mapping[str, HubItem], cluster_id: str | None, error_cls: type[ServiceError]
) -> None:
    """백엔드 항목은 한 클러스터(cluster_id 를 주면 그 클러스터)에 있어야 한다. 클러스터 없는 항목은
    빼고 본다. 422 cluster_mismatch."""
    mixed = other_cluster(items.values(), cluster_id)
    if mixed is not None:
        raise error_cls(422, "cluster_mismatch", f"hub item {mixed.name} is on another cluster")


async def check_backends(
    items: Mapping[str, HubItem],
    *,
    exposed: bool,
    settings: PlatformSettings,
    reachable: Iterable[HubItem],
    names: Mapping[str, list[str]],
    error_cls: type[ServiceError],
) -> None:
    """Gateway 웹 백엔드의 규칙 (P39b 13절). reachable 은 백엔드와, 게시 항목을 원본 Gateway 까지
    따라 내려가 닿는 항목이다.

    - 노출: 노출한 Gateway 가 플랫폼 Tool 에 닿으면, 관리자가 `web_tools.external_clients` 를 켜지
      않은 한 422 platform_tool_not_exposable.
    - 웹 백엔드: 플랫폼 Tool 에 닿는 Gateway 는 모든 백엔드의 도구 이름을 알아야 한다(422
      web_gateway_unknown_names). 도구마다의 효과를 이름으로 정하고, 그 효과로 바깥 내용 뒤의 쓰기를
      묻기 때문이다(14절 4부, P43).
    names 는 항목 id 마다 해석한 도구 이름이다."""
    platform = _reachable_platform(reachable)
    if exposed and platform and not settings.web_tools.external_clients:
        raise error_cls(
            422,
            "platform_tool_not_exposable",
            "platform web tools cannot be in an exposed gateway unless an admin allows external "
            "clients",
            items=sorted({i.name for i in platform}),
        )
    if platform:
        unknown = sorted(i.name for i in items.values() if not names.get(i.id))
        if unknown:
            raise error_cls(
                422,
                "web_gateway_unknown_names",
                "a gateway with platform web tools must know every backend's tool names; "
                "refresh them first",
                items=unknown,
            )
