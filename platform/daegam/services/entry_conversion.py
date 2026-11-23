"""프록시를 두던 허브 항목을 Gateway 의 Entry 가 주소를 직접 부르는 꼴로 옮긴다 (P54b 5·6·7절).

remote 항목과 proxy 모드로 게시한 항목은 `MCPRemoteProxy hub-<이름>` 을 하나씩 돌리고, 다른 테넌트의
Gateway 는 remote 항목의 복제본을 자기 네임스페이스에 둔다. vMCP 가 호출마다 사람과 Entry 를 감사에
남기므로 그 프록시가 더하는 것이 없다. 리컨실의 한 단계(`EntryConversion.run`)가 남은 항목을 틱마다
조금씩 옮긴다. 새 배포는 처음부터 주소를 직접 부르므로(`store/hub.py` `as_entry`) 옮길 것은 전에
배포한 스냅샷 행과 전에 만든 항목뿐이고, 새 설치에는 옮길 것이 없다.

항목 하나는 세 걸음으로 간다.

1. 그 항목을 쓰는 Gateway 마다 배포 스냅샷의 행을 주소를 직접 부르는 행으로 고쳐 쓰고 렌더 번호를
   다시 렌더 표시로 바꾼다. 같은 틱의 재적용이 그 스냅샷으로 다시 렌더한다. Entry 이름, Tool 이름,
   priority 순서와 접두사는 그대로다. 실패한 Gateway 의 행도 고쳐 써서 다음 재시도가 주소를
   렌더한다.
2. 클러스터의 살아 있는 Entry 가운데 그 프록시나 복제본의 Service 를 이름 댄 것이 없으면 프록시와
   P50 전에 만들어져 남아 있는 러너 RoleBinding 을 지우고 항목을 저장소에서 옮긴다. 항목의 편집
   클레임 안에서 한다. remote 는 entry 항목이 되고, 게시한 항목은 `k8s` 가 원본 Gateway 의
   VirtualMCPServer 를 가리킨다.
3. 복제본은 공유 복제본 스윕이 같은 규칙으로 지운다(`services/shared_clones.py` `_ClusterSweep`).

렌더를 적용하지 못하는 Gateway 의 Entry 는 프록시나 복제본을 계속 부르므로 그 프록시, 그 복제본,
항목의 종류가 그대로 남고 상태가 그 Gateway 를 이름 댄다. 옮기지 않고 둔 항목과 그 까닭은
`HoldReason` 이고 `GET /settings/data-plane-auth` 가 보인다(`status`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import KubeClient
from daegam.k8s.templates.common import TOOLHIVE_API, proxy_service_name
from daegam.services.claims import tick_item
from daegam.services.entry_address import ENTRY_ADDRESS_REFUSED, EntryAddressRefused
from daegam.services.gateway_entries import EntryServices, entry_services
from daegam.services.hub import (
    CR_KIND,
    PROXY_KEPT_AUDIT,
    PROXY_KEPT_SSE,
    HubService,
    owned_objects,
    proxied,
    proxy_kept,
    shared_clone_name,
)
from daegam.services.instance_base import EDIT_STATES
from daegam.services.proxy_accounts import drop_runner_binding
from daegam.store.auth_reapply import MongoAuthReapplyStore
from daegam.store.base import NothingWritten, fields_of, save_with_edit_claim, utcnow
from daegam.store.clusters import Cluster
from daegam.store.gateways import Gateway, GatewayBackendSnapshot
from daegam.store.hub import (
    DEFAULT_TRANSPORT,
    HUB_NOT_DELETING,
    PUBLISHED_KIND,
    HubItem,
    HubK8s,
    as_entry,
)
from daegam.store.shared_clones import MongoSharedCloneStore, SharedClone

if TYPE_CHECKING:
    from daegam.services.gateways import GatewayService

logger = logging.getLogger("daegam.entry_conversion")

# 한 틱이 클러스터 하나에서 손대는 항목 수. 항목 하나가 그것을 쓰는 Gateway 를 모두 다시 렌더하게
# 하므로 재적용의 몫(`services/data_plane_auth.py` `REAPPLY_PER_NAMESPACE`)처럼 작게 둔다
ITEMS_PER_CLUSTER = 3
# 옮긴 항목이 저장하는 칸. 출처는 전송만 쓴다. 프록시가 없으니 적용을 기다리는 상태도 끝난다
CONVERTED_FIELDS = frozenset(
    {"kind", "k8s", "source.transport", "state", "last_error", "updated_at"}
)
# 주소를 직접 부르는 행에는 복제본이 없다
NO_CLONE = {"shared_source_id": None, "clone_kind": None, "clone_name": None}
# 상태 API 가 클러스터 하나의 Entry 를 읽는 데 쓰는 시한(초). 느리거나 죽은 클러스터가 응답을
# Kubernetes 클라이언트의 시한만큼 잡아 두지 않는다. 넘기면 그 항목들은 `cluster_unreachable` 이다
STATUS_READ_SECONDS = 5.0


class HoldReason(StrEnum):
    """항목을 옮기지 않고 둔 까닭. 값은 상태 API 의 `reason` 이다."""

    # 클러스터가 vMCP 감사의 게이트를 지나지 못한다. 그 아래에서는 프록시만 이 호출을 감사에 남긴다
    AUDIT_OLD = PROXY_KEPT_AUDIT
    # 뒤 판이 쓴 칸을 든 항목이다
    HUB_ITEM_NEWER = "hub_item_newer"
    CLUSTER_UNREACHABLE = "cluster_unreachable"
    # 플랫폼이 그 주소를 직접 부르지 못한다. 프록시가 있는 동안은 Tool 새로고침이 프록시로 간다
    ENTRY_ADDRESS_REFUSED = ENTRY_ADDRESS_REFUSED
    # SSE 만 말하는 서버다. 플랫폼의 MCP 클라이언트가 SSE 주소를 확인하지 못한다
    SSE_ENTRY = PROXY_KEPT_SSE
    # proxy 모드로 게시한 항목의 원본 Gateway 가 배포돼 있지 않아 가리킬 vMCP 가 없다
    SOURCE_NOT_DEPLOYED = "source_not_deployed"
    # 그 항목을 쓰는 Gateway 가 주소를 직접 부르는 렌더를 아직 적용하지 않았다
    CONSUMER_NOT_APPLIED = "consumer_not_applied"


@dataclass(frozen=True)
class Held:
    """옮기지 않고 둔 항목 하나. gateway_ids 는 `consumer_not_applied` 일 때 기다리는 Gateway 다."""

    item: HubItem
    reason: HoldReason
    gateway_ids: tuple[str, ...] = ()

    def row(self) -> dict[str, Any]:
        return {
            "hub_item_id": self.item.id,
            "name": self.item.name,
            "tenant": self.item.tenant,
            "reason": self.reason,
            "gateway_ids": list(self.gateway_ids),
        }


@dataclass(frozen=True)
class Candidate:
    """조건을 모두 지난 항목과 옮기는 데 쓰는 재료. converted 는 프록시를 뗀 항목이고 url 은 Entry
    가 직접 부를 주소다. stale 은 행을 아직 고쳐 쓰지 않은 Gateway, callers 는 그 프록시나 복제본의
    Service 를 이름 댄 살아 있는 Entry 들의 주인 id 다(라벨이 없는 Entry 는 None)."""

    item: HubItem
    converted: HubItem
    url: str
    callers: frozenset[str | None]
    stale: tuple[Gateway, ...] = ()

    @property
    def applied(self) -> bool:
        """그 항목을 쓰는 모든 Gateway 가 주소를 직접 부르는 렌더를 적용했는가."""
        return not self.stale and not self.callers

    def waiting_on(self) -> tuple[str, ...]:
        ids = {g.id for g in self.stale} | {c for c in self.callers if c}
        return tuple(sorted(ids))

    def row_of(self, row: GatewayBackendSnapshot) -> GatewayBackendSnapshot:
        """스냅샷 행을 주소를 직접 부르는 꼴로 옮긴 것. 다른 항목의 행은 그대로다. Entry 이름과
        Tool 이름, 필터, 타임아웃은 건드리지 않는다."""
        if row.hub_item_id != self.item.id:
            return row
        return row.model_copy(
            update={
                "kind": self.converted.kind,
                "url": self.url,
                # 게시한 항목은 transport 를 적지 않은 것이 있다. remote 는 `as_entry` 가 채웠다
                "transport": self.converted.source.transport or DEFAULT_TRANSPORT,
                **NO_CLONE,
            }
        )

    def rows_of(self, gateway: Gateway) -> list[GatewayBackendSnapshot]:
        return [self.row_of(b) for b in gateway.deployed_backends]


@dataclass
class Survey:
    """클러스터 하나에 남은 것. items 는 프록시를 둔 항목 수, clones 는 remote 항목의 복제본 기록
    수다. held 는 옮기지 않고 둔 항목, ready 는 조건을 지난 항목이다."""

    items: int = 0
    clones: int = 0
    held: list[Held] = field(default_factory=list)
    ready: list[Candidate] = field(default_factory=list)

    def left(self) -> bool:
        return bool(self.items or self.clones)

    def all_held(self) -> list[Held]:
        """옮기지 않고 둔 항목 전부. 조건은 지났지만 Gateway 를 기다리는 항목도 든다."""
        waiting = [
            Held(c.item, HoldReason.CONSUMER_NOT_APPLIED, c.waiting_on())
            for c in self.ready
            if not c.applied
        ]
        return self.held + waiting


def proxy_callers(
    item: HubItem, records: list[SharedClone], named: EntryServices
) -> frozenset[str | None]:
    """살아 있는 Entry 가운데 항목의 프록시나 그 복제본의 Service 를 이름 댄 것들의 주인 id. 복제본
    이름은 원본 id 를 담고 있어 어느 네임스페이스에서든 그 항목의 것이다. 기록이 든 이름도 함께
    본다."""
    own = (item.k8s.namespace, proxy_service_name(item.k8s.name, remote=True))
    clones = {shared_clone_name(item), *(r.name for r in records if r.source_item_id == item.id)}
    services = {proxy_service_name(name, remote=True) for name in clones}
    callers: set[str | None] = set()
    for place, owners in named.items():
        if place == own or place[1] in services:
            callers |= owners
    return frozenset(callers)


class _Changed(Exception):
    """항목의 편집 클레임을 잡지 못했거나 마지막 저장이 맞지 않았다. 그 사이 사람이 고쳤거나 삭제가
    시작됐다."""


class EntryConversion:
    """리컨실의 옮기기 단계와 그 상태. hub 의 저장소·주소 규칙·클러스터 클라이언트를 쓴다. Gateway
    의 스냅샷은 그 문서의 클레임 안에서(`BaseInstanceService.claimed`), 항목은 그 편집 클레임
    안에서(`save_with_edit_claim`) 쓴다."""

    def __init__(
        self,
        hub: HubService,
        gateways: GatewayService,
        clones: MongoSharedCloneStore,
        auth_states: MongoAuthReapplyStore,
    ):
        self.hub = hub
        self.gateways = gateways
        self.clones = clones
        # 인증 재적용이 적는 클러스터 상태. 연결하지 못해 미뤄 둔 클러스터는 읽지 않는다
        self.auth_states = auth_states

    # -- 리컨실의 한 단계 ----------------------------------------------------------------------

    async def run(self, now: datetime | None = None) -> dict[str, list[str]]:
        """클러스터마다 남은 항목을 읽고 조건을 지난 항목을 한 걸음씩 옮긴다. 한 틱이 클러스터
        하나에서 손대는 항목은 `ITEMS_PER_CLUSTER` 개까지이고 남은 것은 다음 틱이 잇는다. 한
        항목이나 한 클러스터의 실패가 다른 것을 막지 않는다. 남은 것이 없으면 결과 키도 없다."""
        result: dict[str, list[str]] = {
            key: [] for key in ("rewritten", "converted", "held", "deferred", "failed")
        }
        backing_off = await self.auth_states.backing_off(now or utcnow())
        for cluster in await self.hub.tenancy.clusters.list():
            # 틱의 리스를 늘리고 잃었으면 멈춘다
            await tick_item()
            try:
                await self._run_cluster(cluster, cluster.id in backing_off, result)
            except Exception as e:  # noqa: BLE001 - 한 클러스터의 실패가 단계를 멈추면 안 된다
                logger.warning("entry conversion on cluster %s: %s", cluster.id, e)
                result["failed"].append(f"cluster:{cluster.id}")
        return {key: ids for key, ids in result.items() if ids}

    async def _run_cluster(
        self, cluster: Cluster, backing_off: bool, result: dict[str, list[str]]
    ) -> None:
        survey, client = await self._survey(cluster, backing_off)
        result["held"] += [f"{h.item.id}:{h.reason}" for h in survey.held]
        touched = 0
        for candidate in survey.ready:
            await tick_item()
            if touched >= ITEMS_PER_CLUSTER:
                result["deferred"].append(candidate.item.id)
                continue
            assert client is not None  # 조건을 지난 항목은 클러스터를 읽은 뒤에만 있다
            try:
                touched += await self._advance(candidate, client, result)
            except Exception as e:  # noqa: BLE001 - 한 항목의 실패가 다른 항목을 막지 않는다
                logger.warning("entry conversion of hub item %s: %s", candidate.item.id, e)
                result["failed"].append(candidate.item.id)

    async def _advance(
        self, candidate: Candidate, client: KubeClient, result: dict[str, list[str]]
    ) -> bool:
        """항목 하나를 한 걸음 옮긴다. 무엇인가 썼으면 참이다. 행을 고쳐 쓸 Gateway 가 있으면 그것이
        먼저이고, 모든 Gateway 가 적용한 뒤에야 항목을 옮긴다."""
        item = candidate.item
        if candidate.applied:
            if await self._convert(candidate, client):
                result["converted"].append(item.id)
            return True
        rewritten = [g.id for g in candidate.stale if await self._rewrite(g, candidate)]
        result["rewritten"] += rewritten
        result["held"].append(f"{item.id}:{HoldReason.CONSUMER_NOT_APPLIED}")
        return bool(rewritten)

    async def _rewrite(self, gateway: Gateway, candidate: Candidate) -> bool:
        """Gateway 하나의 스냅샷 행을 고쳐 쓰고 렌더 번호를 다시 렌더 표시로 바꾼다. 그 문서의
        클레임 안에서, 배포 쪽을 고칠 수 있는 상태(`EDIT_STATES`)일 때만이다. 배포 중이거나 내리는
        중인 문서와 남이 클레임을 쥔 문서는 건너뛰고 다음 틱이 다시 본다. 렌더는 재적용이 한다.
        그래서 렌더가 실패한 Gateway 는 재적용의 백오프와 막힘을 따른다."""
        if gateway.state not in EDIT_STATES:
            return False

        async def work() -> bool:
            rows = candidate.rows_of(gateway)
            if rows == gateway.deployed_backends:
                return False
            gateway.deployed_backends = rows
            gateway.updated_at = utcnow()
            return await self.gateways.instances.mark_stale(
                gateway.id,
                fields_of(gateway, {"deployed_backends", "updated_at"}),
                expect_state=EDIT_STATES,
            )

        return await self.gateways.claimed(
            gateway, EDIT_STATES, action="point backends at their address", work=work
        )

    async def _convert(self, candidate: Candidate, client: KubeClient) -> bool:
        """프록시와 P50 전에 만들어져 남아 있는 러너 RoleBinding 을 지우고 항목을 저장소에서
        옮긴다. 항목의 편집 클레임 안에서 하므로 사람의 저장이나 삭제와 겹치지 않는다. 프록시를
        먼저 지운다. 저장이 먼저면 지우기가 실패했을 때 그 프록시를 기억하는 문서가 없다. 지우기는
        되풀이해도 같고 문서를 바꾸지 않으므로, 실패하면 클레임을 풀고 다음 틱이 두 일을 다시
        한다. 클레임을 잡지 못했거나 그 사이 삭제가 시작된 항목은 쓰지 않고 False 다."""
        item = candidate.item
        converted = candidate.converted.model_copy(
            update={"state": "created", "last_error": None, "updated_at": utcnow()}
        )

        async def delete_proxy(_prepared: None) -> None:
            try:
                for kind, name in owned_objects(item):
                    await run_in_threadpool(
                        client.delete, TOOLHIVE_API, kind, name, item.k8s.namespace
                    )
                    await drop_runner_binding(client, name, item.k8s.namespace)
            except Exception as e:
                raise NothingWritten from e

        async def nothing() -> None:
            return None

        try:
            await save_with_edit_claim(
                self.hub.store,
                converted,
                CONVERTED_FIELDS,
                prepare=nothing,
                write=delete_proxy,
                conflict=_Changed,
            )
        except _Changed:
            return False
        logger.info("hub item %s (%s) no longer runs a remote proxy", item.id, item.kind)
        return True

    # -- 남은 것 읽기 --------------------------------------------------------------------------

    async def _survey(
        self, cluster: Cluster, backing_off: bool, read_seconds: float | None = None
    ) -> tuple[Survey, KubeClient | None]:
        """클러스터 하나에 남은 항목과 복제본 기록을 읽고 항목마다 둔 까닭이나 옮길 재료를 정한다.
        저장소만으로 정해지는 까닭이 먼저다. 조건을 지난 항목이 있을 때만 클러스터의 Entry 를 읽고,
        읽지 못하거나 read_seconds 안에 읽지 못하면 그 항목들은 `cluster_unreachable` 이다. 남은
        항목이 없는 클러스터는 부르지 않는다."""
        records = [
            r
            for r in await self.clones.live_on_cluster(cluster.id)
            if r.source.get("kind") == "remote"
        ]
        items = [
            i
            for i in await self.hub.store.owning_cr_on_cluster(cluster.id)
            if proxied(i) and i.state in HUB_NOT_DELETING
        ]
        survey = Survey(items=len(items), clones=len(records))
        passing: list[tuple[HubItem, HubItem]] = []
        for item in items:
            held_or_converted = await self._held_or_converted(item, cluster, backing_off)
            if isinstance(held_or_converted, HoldReason):
                survey.held.append(Held(item, held_or_converted))
            else:
                passing.append((item, held_or_converted))
        if not passing:
            return survey, None
        try:
            async with asyncio.timeout(read_seconds):
                client = await run_in_threadpool(self.hub.cluster_service.client_for, cluster)
                named = await entry_services(client)
        except Exception as e:  # noqa: BLE001 - 읽지 못한 클러스터의 항목은 그대로 둔다
            logger.info("entry conversion: cluster %s unread: %r", cluster.id, e)
            survey.held += [Held(i, HoldReason.CLUSTER_UNREACHABLE) for i, _ in passing]
            return survey, None
        for item, converted in passing:
            survey.ready.append(await self._candidate(item, converted, records, named))
        return survey, client

    async def _held_or_converted(
        self, item: HubItem, cluster: Cluster, backing_off: bool
    ) -> HoldReason | HubItem:
        """클러스터를 읽지 않고 정해지는 까닭이 있으면 그것, 없으면 프록시를 뗀 항목이다."""
        reason = await self._stored_reason(item, cluster, backing_off)
        if reason is not None:
            return reason
        return await self._converted(item) or HoldReason.SOURCE_NOT_DEPLOYED

    async def _stored_reason(
        self, item: HubItem, cluster: Cluster, backing_off: bool
    ) -> HoldReason | None:
        """클러스터를 읽지 않고 정해지는 까닭. 클러스터에 연결하지 못하는 것이 먼저이고, 프록시를
        떼지 못하는 까닭(감사 게이트, SSE: `proxy_kept`), 뒤 판의 칸, 주소 규칙이 뒤따른다. 게시한
        항목의 주소는 플랫폼이 만든 vMCP 의 Service 라 주소 규칙을 보지 않는다."""
        if backing_off:
            return HoldReason.CLUSTER_UNREACHABLE
        kept = proxy_kept(item, cluster)
        if kept is not None:
            return HoldReason(kept)
        if item.newer_fields:
            return HoldReason.HUB_ITEM_NEWER
        if item.kind != PUBLISHED_KIND and await self._address_refused(item, cluster):
            return HoldReason.ENTRY_ADDRESS_REFUSED
        return None

    async def _address_refused(self, item: HubItem, cluster: Cluster) -> bool:
        """entry 항목이 되면 플랫폼이 그 주소를 직접 부른다. 주소 규칙이 거절하는가."""
        try:
            await self.hub.checked_entry_url(item, item.source.url or "", cluster)
        except EntryAddressRefused:
            return True
        return False

    async def _candidate(
        self, item: HubItem, converted: HubItem, records: list[SharedClone], named: EntryServices
    ) -> Candidate:
        candidate = Candidate(
            item=item,
            converted=converted,
            url=self.hub.backend_url(converted),
            callers=proxy_callers(item, records, named),
        )
        users = await self.gateways.instances.deployed_over([item.id])
        stale = [g for g in users if candidate.rows_of(g) != g.deployed_backends]
        return replace(candidate, stale=tuple(stale))

    async def _converted(self, item: HubItem) -> HubItem | None:
        """프록시를 뗀 항목. remote 는 entry 항목이고(`as_entry`), proxy 모드로 게시한 항목은 `k8s`
        가 원본 Gateway 의 VirtualMCPServer 를 가리킨다. 그 Gateway 가 없거나 배포돼 있지 않으면
        None 이다."""
        if item.kind != PUBLISHED_KIND:
            return as_entry(item)
        source = await self.gateways.instances.get(item.source.gateway_instance_id or "")
        if source is None or source.k8s is None:
            return None
        k8s = HubK8s(
            kind=CR_KIND[PUBLISHED_KIND], name=source.k8s.name, namespace=source.k8s.namespace
        )
        return item.model_copy(update={"k8s": k8s})

    # -- 상태 --------------------------------------------------------------------------------

    async def status(self, clusters: list[Cluster], now: datetime) -> dict[str, dict[str, Any]]:
        """클러스터 id 마다 남은 항목 수와 remote 복제본 기록 수, 옮기지 않고 둔 항목과 그 까닭.
        지금 읽어 계산하고 아무것도 쓰지 않는다. 클러스터의 Entry 는 `STATUS_READ_SECONDS` 안에
        읽은 것만 쓴다. 남은 것이 없는 클러스터는 빠진다."""
        backing_off = await self.auth_states.backing_off(now)
        out: dict[str, dict[str, Any]] = {}
        for cluster in clusters:
            survey, _client = await self._survey(
                cluster, cluster.id in backing_off, STATUS_READ_SECONDS
            )
            if survey.left():
                out[cluster.id] = {
                    "items": survey.items,
                    "clones": survey.clones,
                    "held": [h.row() for h in survey.all_held()],
                }
        return out
