"""허브 항목이 원본에서 가져오는 값들 (D49, D50).

`internal`·`published` 의 `tool_names` 는 저장하지 않는다. 사본을 두면 원본이 바뀔 때마다
어긋나고, 어긋난 것을 맞추는 장치를 두면 다시 그 장치와 사용자 쓰기의 경합을 관리해야 한다.
저장하지 않으면 둘 다 없다. D48 이 도구 스키마를 저장하지 않기로 한 것과 같은 논리이고,
여기는 원본이 우리 Mongo 안이라 더 쉽다.

도구 이름을 읽는 자리는 셋뿐이다. 응답(`api/hub.item_out`), Gateway 정의 검증
(`services/gateway_validation.GatewayValidator.validate_spec`), 배포 스냅샷 생성
(`services/gateways._snapshot_backends`)이다.

사람이 MCP 클라이언트에 붙여 넣는 주소(`external_url`)도 같은 자리에서 나온다. 허브 항목에는 노출
설정이 없고 원본(MCP Server·Gateway)의 것이라, 같은 일괄 조회로 함께 가져온다 (D50).

`remote`·`entry`·`image` 는 원본이 남의 서버라 우리가 계산할 수 없다. 저장된 값을 그대로 쓰고
`refresh-tools` 가 채운다.

**계산에 실패한 것과 도구가 없는 것은 다르다.** 원본을 못 찾으면 `None`("모른다")이고 빈 목록이
아니다. `advertised_names.plan_conflicts` 는 `tool_names` 가 비면 이름 규칙을 통째로 `prefix` 로
떨어뜨리므로, 실패를 빈 목록으로 돌려주면 배포된 도구 이름이 전부 `<workload>_<tool>` 로 바뀌는
조용한 사고가 난다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from daegam.services.advertised_names import advertised_tools as gateway_advertised_tools
from daegam.services.advertised_names import advertised_tools_of
from daegam.services.mcp_servers import version_refs_of
from daegam.services.platform_tools import tool_names as platform_tool_names
from daegam.store.backend_apis import BackendAPIStore
from daegam.store.gateways import MongoGatewayStore
from daegam.store.hub import PLATFORM_KIND, HubItem
from daegam.store.mcp_servers import MCPServer, MongoMCPServerStore

logger = logging.getLogger("daegam.hub")


@dataclass
class Origin:
    """한 항목이 원본에서 가져오는 것.

    `tool_names` 의 `None` 은 "모른다" 이고 빈 목록과 다르다. `external_url` 의 `None` 은 "밖으로
    열려 있지 않다" 이고, 그때 화면은 클러스터 안 주소만 보인다. `server` 는 internal 항목의 원본
    MCP Server 문서다. 상태(`HubService.status`)가 같은 문서를 항목마다 다시 읽지 않게 넘긴다.
    """

    tool_names: list[str] | None = None
    external_url: str | None = None
    server: MCPServer | None = None


def origin_server(resolved: Mapping[str, Origin], item: HubItem) -> MCPServer | None:
    """해석이 읽어 둔 internal 항목의 원본 MCP Server 문서. 해석하지 않았거나 없으면 None 이다."""
    origin = resolved.get(item.id)
    return origin.server if origin is not None else None


class HubToolNames:
    """허브 항목의 도구 이름 해석기. 항목 여럿을 한 번에 받아 질의 수를 상수로 묶는다."""

    def __init__(
        self,
        mcp_servers: MongoMCPServerStore,
        gateways: MongoGatewayStore,
        backend_apis: BackendAPIStore,
    ):
        self.mcp_servers = mcp_servers
        self.gateways = gateways
        self.backend_apis = backend_apis

    async def resolve(self, items: Sequence[HubItem]) -> dict[str, Origin]:
        """항목 id → 원본에서 가져온 것.

        결과에는 `internal`·`published`·`platform` 만 담는다. 나머지 종류를 호출부가 그대로 두게
        하려는 것이라, `.get(id)` 로 받아 없으면 저장된 값을 쓰면 된다.

        질의는 항목 수와 무관하게 셋이다. MCP Server 일괄, Gateway 일괄, backend 버전 일괄이다.
        """
        internal = {i.id: i.source.instance_id for i in items if i.kind == "internal"}
        published = {i.id: i.source.gateway_instance_id for i in items if i.kind == "published"}
        # 플랫폼 웹 Tool 의 이름은 계약과 플랫폼 앱이 정한다. 질의가 없다 (P39b 13절)
        platform = {
            i.id: Origin(tool_names=platform_tool_names(i))
            for i in items
            if i.kind == PLATFORM_KIND
        }
        if not internal and not published:
            return platform

        servers = await self.mcp_servers.get_many({v for v in internal.values() if v})
        gateways = await self.gateways.get_many({v for v in published.values() if v})

        # 필터 없는 backend 만 버전을 읽어야 한다. 짝을 모아 한 번에 읽는다
        refs: set[tuple[str, int]] = set()
        pinned = {}
        for server in servers.values():
            rev = _pinned_revision(server)
            if rev is None:
                continue
            pinned[server.id] = rev
            refs |= version_refs_of(rev)
        versions = await self.backend_apis.get_versions(refs)

        out: dict[str, Origin] = dict(platform)
        for item_id, server_id in internal.items():
            server = servers.get(server_id or "")
            rev = pinned.get(server.id) if server else None
            out[item_id] = Origin(
                tool_names=advertised_tools_of(rev, versions) if rev is not None else None,
                external_url=server.exposure.external_url if server else None,
                server=server,
            )
        for item_id, gateway_id in published.items():
            gateway = gateways.get(gateway_id or "")
            # Gateway 쪽은 배포 스냅샷에서 계산한다. 그 스냅샷은 적용한 vMCP CR 과 짝인 배포
            # 기록이라 얼어 있는 것이 맞고, published 항목이 말해야 하는 것이 바로 그 배포 계획이다
            out[item_id] = Origin(
                tool_names=(
                    gateway_advertised_tools(gateway)
                    if gateway is not None and gateway.deployed_revision is not None
                    else None
                ),
                external_url=gateway.exposure.external_url if gateway else None,
            )
        return out


def _pinned_revision(server):
    """배포가 핀한 리비전. 없으면 None 이다.

    `MCPServerService._pinned` 는 배포 전이면 409 를 던진다. 여기서는 예외가 아니라 "모른다" 라서
    같은 판단을 조용히 한다.
    """
    if server.deployed_revision is None:
        return None
    return server.get_revision(server.deployed_revision)
