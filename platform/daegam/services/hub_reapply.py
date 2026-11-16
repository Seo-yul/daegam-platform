"""허브 항목 재적용 (P50 1절). 자기 CR 을 둔 허브 항목을 인증 재적용의 한 종류로 싣는다.

`DataPlaneAuth` 는 종류마다 인스턴스 서비스와 같은 모양을 본다. `auth_kind`·`profile_kind`·
`live_states`, 스토어 `instances`(`live_on_cluster`·`set_fields`), 그리고 `reapply_auth` 다. 허브
항목에는 인증 입력이 없으므로(`consumer_parts` 가 비어 있다) 판의 인증 절반은 자기 audience 로
옮겨 간 image 항목의 표시(P50 9절)뿐이고, 그 밖에는 렌더 번호 `RENDER_EPOCHS[KIND_HUB_ITEM]` 이
오를 때만 다시 렌더된다. 다시 렌더는 `HubService.rerender`
가 그때의 설정으로 한다. 공유 복제본은 이 종류가 아니다 (리컨실의 `shared` 단계).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from daegam.services.data_plane_auth import KIND_HUB_ITEM
from daegam.services.hub import HubService, owns_crs
from daegam.store.hub import HUB_LIVE_STATES, HubItem, MongoHubStore


class _HubItems:
    """재적용 단계가 보는 스토어 모양. 그 클러스터에 자기 CR 을 둔 항목만 낸다."""

    def __init__(self, store: MongoHubStore):
        self.store = store

    async def live_on_cluster(self, cluster_id: str) -> list[HubItem]:
        """그 클러스터에 자기 CR 을 둔 항목. 상태는 거르지 않는다. 재적용 단계가 `live_states` 로
        거르고, 그 밖의 항목도 네임스페이스 입양 판단에 쓴다."""
        return [i for i in await self.store.owning_cr_on_cluster(cluster_id) if owns_crs(i)]

    async def set_fields(
        self,
        item_id: str,
        fields: dict[str, Any],
        *,
        expect_state: Collection[str] | None = None,
    ) -> bool:
        return await self.store.set_fields(item_id, fields, expect_state=expect_state)


class HubItemReapply:
    """허브 항목 종류. wiring 이 `DataPlaneAuth` 에 한 종류로 넘긴다."""

    auth_kind = KIND_HUB_ITEM
    profile_kind = "hub item"
    live_states = HUB_LIVE_STATES

    def __init__(self, hub: HubService):
        self.hub = hub
        self.instances = _HubItems(hub.store)

    async def want_rev(self, doc: HubItem, ts: Any) -> str | None:
        return await self.hub.item_rev(doc)

    async def paced_revs(self, doc: Any, ts: Any) -> frozenset[str | None]:
        return frozenset()

    async def reapply_auth(self, item: HubItem) -> bool:
        return await self.hub.rerender(item)
