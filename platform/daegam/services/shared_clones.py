"""공유 복제본의 기록과 그 적용 (P50 1절, D23).

다른 테넌트가 연 shared image 항목과 플랫폼 fetch 를 쓰는 Gateway 는 자기 네임스페이스에 복제본 CR
을 둔다. 복제본의 기록(`store/shared_clones.py`)이 마지막으로 렌더한 원본 정의를 든다. remote 항목의
복제본(MCPRemoteProxy)은 만들지도 다시 렌더하지도 않는다. 전에 만든 것과 그 기록은 그 항목이 entry
항목으로 옮겨 갈 때까지(`services/entry_conversion.py`) 적용된 그대로 남고, 아래 스윕이 지운다.

- 배포 경로: Gateway 의 provision 이 `apply` 로 복제본을 렌더해 적용하고 기록을 맞춘다. 쓰는
  테넌트가 consume 경계 밖이면 기록의 원본으로 렌더한다.
- 재적용의 한 종류(`SharedCloneReapply`): 경계 안이면 판의 첫 절반이 지금 원본 정의의 판이라, 원본을
  고치면 다음 재적용이 복제본을 다시 렌더한다. 경계 밖이거나 원본이 없으면 기록이 원본을 더 받지
  않아, 동결된 복제본은 마지막 원본으로 렌더하되 렌더 번호가 오르면 다시 렌더한다.
- 리컨실의 스윕(`sweep`): 손으로 지운 복제본을 재적용이 다시 만들게 표시하고, 쓰는 Gateway 가 없는
  복제본과 기록, 이 설치의 버려진 복제본을 지운다. 살아 있는 MCPServerEntry 가 그 복제본의 Service
  를 이름 대는 동안은 지우지 않는다. 스냅샷에서 복제본을 뺀 Gateway 가 그 렌더를 적용하기 전에는
  Entry 가 아직 복제본을 부르기 때문이다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, NamedTuple

from starlette.concurrency import run_in_threadpool

from daegam.auth.hub_access import tenant_in_boundary
from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.gates import Feature, passing_features
from daegam.k8s.labels import LABEL_PLATFORM_ID, LABEL_RESOURCE, LABEL_SOURCE_ID
from daegam.k8s.templates.common import RESOURCE_HUB_ITEM, TOOLHIVE_API, proxy_service_name
from daegam.k8s.templates.svc_token import TOKEN_BACKEND_KINDS
from daegam.k8s.templates.web_tools import web_config_hash
from daegam.services.authz_configs import (
    AUTHZ_WAIT_SECONDS,
    REAPPLY_AUTHZ_WAIT_SECONDS,
    apply_in_order,
)
from daegam.services.budget import Budget, resumed
from daegam.services.claims import tick_item
from daegam.services.data_plane_auth import (
    KIND_CLONE,
    newer_rev,
    own_audience_rev,
    render_epoch,
)
from daegam.services.gateway_entries import EntryServices, entry_services
from daegam.services.gateway_plan import point_at_clone
from daegam.services.hub import (
    CLONE_SOURCE_FIELDS,
    CR_KIND,
    HubService,
    clone_source,
    clone_source_hash,
    image_side_objects,
    item_from_clone_source,
    newer_item_error,
    refuse_newer,
    shared_clone_name,
)
from daegam.services.platform_tools import (
    FETCH_CLONE_NAME,
    FETCH_SIDE_OBJECTS,
    enabled,
    fetch_image,
)
from daegam.services.tool_rules import rules_toolhive_ok
from daegam.store.auth_reapply import (
    AUTH_FIELDS,
    STALE_EPOCH,
    MongoAuthReapplyStore,
    is_stale_epoch,
    new_image_switch,
    own_audience,
    switched_server_id,
)
from daegam.store.base import fields_of, utcnow
from daegam.store.gateways import Gateway, MongoGatewayStore
from daegam.store.hub import (
    CLONED_KINDS,
    KEPT_CLONE_KINDS,
    PLATFORM_KIND,
    HubItem,
    unknown_keys,
)
from daegam.store.shared_clones import CLONE_LIVE_STATES, MongoSharedCloneStore, SharedClone
from daegam.store.tenants import TenantSettings

logger = logging.getLogger("daegam.shared_clones")

# 한 틱의 스윕이 쓰는 시간(초). 리컨실 리스(`store/leases.py` 의 `LEASE_TTL`) 안에서 다른 단계의
# 몫을 남긴다. 다 못 본 클러스터는 다음 틱이 본다
CLONE_SWEEP_SECONDS = 20.0
# 렌더할 때마다 기록에 함께 적는 칸. 재적용 칸(`AUTH_FIELDS`)이 판을 든다
_RENDER_FIELDS = frozenset({*AUTH_FIELDS, "source", "frozen", "tenant", "updated_at"})

# (네임스페이스, CR 종류, 이름) 마다 복제본 CR 의 metadata
LiveClones = dict[tuple[str, str, str], dict[str, Any]]


class WantedSource(NamedTuple):
    """복제본 기록이 렌더할 원본 정의. newer 는 그 원본에서 이 판이 모르는 칸이다 (P55 9절). 살아
    있는 원본이면 항목의 `newer_fields` 라 원본 정의에 싣지 않는 최상위 칸도 들고, 기록의 원본이면
    그 정의의 모르는 칸이다."""

    source: dict[str, Any]
    frozen: bool
    newer: tuple[str, ...]


class SharedClones:
    """복제본 기록의 배포 경로, 재적용, 스윕. Gateway 서비스를 들지 않는다 (P72 2.4절)."""

    def __init__(
        self,
        store: MongoSharedCloneStore,
        hub: HubService,
        gateways: MongoGatewayStore,
        cluster_service: Any,
        tenancy: Any,
        auth_states: MongoAuthReapplyStore,
    ):
        self.store = store
        self.hub = hub
        self.gateways = gateways
        self.cluster_service = cluster_service
        self.tenancy = tenancy
        # 인증 재적용이 적는 클러스터 상태. 연결하지 못해 미뤄 둔 클러스터는 스윕도 건너뛴다
        self.auth_states = auth_states
        # 이 레플리카가 클러스터마다 스윕을 마지막으로 시작한 때와, 시간이 모자라 멈춘 기록. 리스가
        # 다른 레플리카로 옮기면 처음부터 다시 센다
        self.started: dict[str, float] = {}
        self.resume_after: dict[str, str] = {}

    # -- 배포 경로 ---------------------------------------------------------------------------

    async def apply(
        self,
        item: HubItem,
        client: KubeClient,
        *,
        cluster_id: str,
        namespace: str,
        tenant: str,
        error_cls: type[ServiceError],
    ) -> SharedClone | None:
        """복제본을 렌더해 적용하고 기록을 맞춘 뒤 저장된 기록을 돌려준다. 이름은 기록이 있으면 그
        이름이고, 없으면 새 이름이다. 기록의 id 는 image 복제본에 Gateway 가 낼 토큰의 audience 다
        (P50 9절). 기록이 없으면 먼저 만든다. 새 image 기록은 옛 토큰을 든 소비자가 없어 처음부터 그
        id 의 audience 로 검증하고, 두 배포가 함께 만들면 먼저 넣은 기록이 남으므로 CR 은 저장된
        기록의 id 로 렌더해야 하기 때문이다. 쓰는 테넌트가 consume 경계 밖이면 기록의 원본으로
        렌더하고, 기록도 없으면 렌더할 원본이 없어 None 이다(부르는 쪽이 거절한다). 꺼진 플랫폼
        fetch 는 409 `platform_tool_disabled` 다. 끈 동안 스윕이 지운 복제본을 Gateway 의 재적용이
        되살리지 않게 한다. 뒤 판이 쓴 원본이면 기록을 만들기 전에 422 `hub_item_newer` 다 (P55
        9절)."""
        if item.kind == PLATFORM_KIND and not enabled(
            item, await self.tenancy.platform_settings.get()
        ):
            raise error_cls(409, "platform_tool_disabled", f"platform tool {item.name} is off")
        record = await self.store.at(item.id, cluster_id, namespace)
        if tenant_in_boundary(tenant, item, "consume"):
            # 원본 정의는 최상위의 일부 칸만 실으므로 항목 자신의 모르는 칸을 먼저 본다
            refuse_newer(item)
            source, frozen = clone_source(item), False
        elif record is not None:
            source, frozen = record.source, True
        else:
            return None
        clone = item_from_clone_source(source)
        if record is None:
            draft = SharedClone(
                source_item_id=item.id,
                cluster_id=cluster_id,
                namespace=namespace,
                tenant=tenant,
                name=shared_clone_name(item),
                source=source,
                audience_switch=new_image_switch(item.kind, utcnow()),
            )
            record = await self.store.upsert(draft, ())
        fields = _RENDER_FIELDS
        if item.kind == "image" and not own_audience(record.audience_switch):
            # 2.0.0-rc1 이 테넌트 audience 로 입양했거나 소비자를 기다리던 image 기록이다. 이 판의
            # 소비자는 모두 두 audience 를 실으므로 기다리지 않고 자기 audience 로 옮긴다
            record.audience_switch = new_image_switch(item.kind, utcnow())
            fields = _RENDER_FIELDS | {"audience_switch"}
        await self._apply_docs(client, record, clone, tenant=tenant, error_cls=error_cls)
        cluster = await self.tenancy.clusters.get(cluster_id)
        passing = passing_features(cluster) if cluster is not None else frozenset()
        web = await self._web_rev(source, record)
        rendered = _rendered(record, source, frozen=frozen, tenant=tenant, passing=passing, web=web)
        return await self.store.upsert(rendered, fields)

    async def apply_for(
        self,
        doc: Gateway,
        client: KubeClient,
        *,
        k8s: Any,
        error_cls: type[ServiceError],
    ) -> list[str]:
        """문서가 참조하는 다른 테넌트 항목의 복제본 CR 을 이 테넌트 네임스페이스에 적용하고 그
        기록을 맞춘다 (P50 1절, `services/shared_clones.py`). 복제본의 이름은 기록의 것이라 그
        백엔드를 기록의 이름과 주소로 맞춘다. 복제본은 Gateway 사이에 공유되므로 ownerReference 없이
        둔다. 쓰는 Gateway 가 없어진 복제본은 리컨실의 스윕이 지운다. consume 판정은 배포·재배포의
        validate_spec 이 먼저 했다. remote 행의 복제본은 렌더하지 않는다. 전에 만든 기록이 있으면
        그 이름으로 백엔드를 맞추기만 하고, 없으면 그 행을 그대로 둔다.

        k8s(Gateway 의 이름 묶음)와 error_cls(Gateway 의 오류 클래스)를 부를 때마다 받는다. 이
        모듈이 Gateway 서비스를 들지 않게 하려는 것이다. 들면 서로를 잇는 뒷줄이 되살아나고(P72
        2.4절), 오류 클래스를 import 하면 gateways 와 순환이 된다."""
        records: dict[str, SharedClone] = {}
        for b in doc.deployed_backends:
            if not b.shared_source_id:
                continue
            if b.kind in KEPT_CLONE_KINDS:
                kept = await self.store.at(b.shared_source_id, doc.cluster_id or "", k8s.namespace)
                if kept is not None:
                    point_at_clone(b, kept.name, k8s.namespace)
                continue
            if b.shared_source_id not in records:
                item = await self.hub.store.get_unscoped(b.shared_source_id)
                if item is None:
                    raise error_cls(
                        422, "unknown_hub_item", f"shared hub item {b.shared_source_id} is gone"
                    )
                record = await self.apply(
                    item,
                    client,
                    cluster_id=doc.cluster_id or "",
                    namespace=k8s.namespace,
                    tenant=doc.tenant,
                    error_cls=error_cls,
                )
                if record is None:
                    raise error_cls(
                        422,
                        "hub_item_not_consumable",
                        f"hub item {item.name} is no longer open to tenant {doc.tenant}",
                    )
                records[b.shared_source_id] = record
            record = records[b.shared_source_id]
            point_at_clone(b, record.name, k8s.namespace)
            if b.kind in TOKEN_BACKEND_KINDS:
                # image 복제본은 이 네임스페이스의 우리 서버다. Entry 의 토큰이 그 기록의 audience
                # 를 싣는다 (P50 9절)
                b.server_id = record.id
        return [r.name for r in records.values()]

    async def delete_source(self, item_id: str) -> list[str]:
        """한 원본의 복제본을 모두 지운다. 꺼진 플랫폼 fetch 의 정리다(P39b 13절). 지운 것을
        `<네임스페이스>/<이름>` 으로 돌려준다. 하나의 실패가 다른 것을 막지 않는다."""
        out: list[str] = []
        for record in await self.store.of_source(item_id):
            await tick_item()
            try:
                cluster = await self.tenancy.clusters.get(record.cluster_id)
                if cluster is not None:
                    client = await run_in_threadpool(self.cluster_service.client_for, cluster)
                    await _delete_clone(client, _clone_kind(record), record.name, record.namespace)
                await self.store.delete_unchanged(record.id, record.updated_at)
                out.append(f"{record.namespace}/{record.name}")
            except Exception as e:  # noqa: BLE001 - 하나의 실패가 다른 것을 막지 않는다
                logger.warning("clone %s not deleted: %s", record.id, e)
        return out

    async def restore(self, item: HubItem) -> list[str]:
        """원본을 백엔드로 배포한 Gateway 가운데 그 네임스페이스에 복제본 기록이 없는 것에 복제본을
        다시 만든다. 다시 켠 플랫폼 fetch 다(P39b 13절). 만든 것을 `<네임스페이스>/<이름>` 으로
        돌려준다."""
        out: list[str] = []
        for gw in await self.gateways.using_hub_item(item.id):
            uses = any(b.shared_source_id == item.id for b in gw.deployed_backends)
            if gw.state != "deployed" or gw.k8s is None or gw.cluster_id is None or not uses:
                continue
            namespace = gw.k8s.namespace
            if await self.store.at(item.id, gw.cluster_id, namespace) is not None:
                continue
            await tick_item()
            try:
                cluster = await self.tenancy.clusters.get(gw.cluster_id)
                client = await run_in_threadpool(self.cluster_service.client_for, cluster)
                record = await self.apply(
                    item,
                    client,
                    cluster_id=gw.cluster_id,
                    namespace=namespace,
                    tenant=gw.tenant,
                    error_cls=ServiceError,
                )
            except Exception as e:  # noqa: BLE001 - 다음 틱에 다시 본다
                logger.warning("clone of %s for gateway %s not restored: %s", item.name, gw.id, e)
                continue
            if record is not None:
                out.append(f"{namespace}/{record.name}")
        return out

    async def _apply_docs(
        self,
        client: KubeClient,
        record: SharedClone,
        item: HubItem,
        *,
        tenant: str,
        error_cls: type[ServiceError] = ServiceError,
        authz_wait: float = AUTHZ_WAIT_SECONDS,
    ) -> None:
        """기록의 이름과 네임스페이스에 item 으로 렌더한 복제본을 적용한다. item 은 원본 정의에서
        되살린 것이다(`item_from_clone_source`). 자기 audience 로 옮겨 간 기록이면 그 id 의
        audience 로 검증한다. image 복제본의 울타리 설정은 서버보다 먼저 적용하고
        받아들여지기를 기다린다 (P50 11절). Tool 규칙을 든 image 원본의 복제본은 그 클러스터가 0.53
        아래이거나 판을 모르면 적용하지 않는다(P52 1절). 복제본은 가진 spec 을 그대로 둔다."""
        cluster = await self.tenancy.clusters.get(record.cluster_id)
        if self.hub.rules_held_back(item, cluster):
            logger.warning(
                "shared clone %s holds Tool rules on a cluster below ToolHive 0.53; it keeps the "
                "spec it has (rules_toolhive_old)",
                record.id,
            )
            return
        docs = await self.hub.shared_clone_docs(
            item,
            tenant=tenant,
            namespace=record.namespace,
            name=record.name,
            server_id=switched_server_id(record.audience_switch, record.id),
            cluster=cluster,
        )
        await apply_in_order(client, docs, error_cls, wait=authz_wait)

    # -- 재적용 -----------------------------------------------------------------------------

    async def wanted_source(self, record: SharedClone) -> WantedSource:
        """렌더할 원본 정의와 동결 여부, 그 원본의 모르는 칸. 쓰는 테넌트가 경계 안이면 지금
        원본이고, 경계 밖이거나 원본이 없으면 기록이 든 원본이다. 원본의 종류가 기록의 것과
        달라졌어도(entry 가 된 remote 항목) 기록의 원본이다. 그 복제본은 스윕이 지울 때까지 렌더한
        꼴로 남는다. 기대 판도 이것을 읽으므로 모르는 칸이 있어도 멈추지 않는다. 멈추는 것은 다시
        렌더다(`rerender`)."""
        item = await self.hub.store.get_unscoped(record.source_item_id)
        if item is not None and item.kind != record.source.get("kind"):
            item = None
        if item is not None and tenant_in_boundary(record.tenant, item, "consume"):
            return WantedSource(clone_source(item), False, item.newer_fields)
        source = record.source
        return WantedSource(source, True, unknown_keys(source, CLONE_SOURCE_FIELDS))

    async def wanted_hash(self, record: SharedClone) -> str | None:
        """판의 첫 절반. 렌더할 원본으로 본 `clone_rev` 다. 플랫폼 fetch 는 복제본이 렌더하는 웹
        Tool 설정의 판도 싣는다. 그래서 설정을 바꾸면 재적용이 모든 fetch 복제본을 다시 렌더한다
        (P39b 13절). 원본에 이 판이 모르는 칸이 있으면 그 칸 이름을 얹는다 (P55 9절)."""
        wanted = await self.wanted_source(record)
        web = await self._web_rev(wanted.source, record)
        return clone_rev(record, wanted.source, web, newer=wanted.newer)

    async def _web_rev(self, source: dict[str, Any], record: SharedClone) -> str | None:
        """플랫폼 fetch 복제본이 렌더하는 설정(env, CA, 이미지)의 판. Tool 규칙을 든 image 원본은
        그 복제본의 클러스터가 규칙을 실을 수 있는지의 판정이다(P52 1절). 판정이 바뀌면 다시
        렌더되어 0.53 에 이른 클러스터의 복제본이 규칙을 싣는다. 다른 원본은 None 이다."""
        if source.get("kind") == "image" and source.get("tool_rules"):
            cluster = await self.tenancy.clusters.get(record.cluster_id)
            ok = cluster is not None and rules_toolhive_ok(cluster)
            return "rules-ok" if ok else "rules-held"
        if source.get("kind") != PLATFORM_KIND:
            return None
        settings = await self.tenancy.platform_settings.get()
        image = fetch_image(settings, self.cluster_service.settings.web_tools_image)
        return web_config_hash(settings.web_tools.fetch, settings.network.ca_bundle, image)

    async def rerender(self, record: SharedClone) -> bool:
        """렌더할 원본으로 복제본을 지금 코드로 다시 렌더해 적용하고 기록을 맞춘다. 그 사이 기록이
        사라졌거나 클러스터가 등록에서 빠졌으면 False 다."""
        record = await self.store.get(record.id)
        if record is None:
            return False
        cluster = await self.tenancy.clusters.get(record.cluster_id)
        if cluster is None:
            return False
        source, frozen, newer = await self.wanted_source(record)
        if newer:
            raise newer_item_error(record.source_item_id, source.get("name"), newer)
        clone = item_from_clone_source(source)
        client = await run_in_threadpool(self.cluster_service.client_for, cluster)
        await self._apply_docs(
            client, record, clone, tenant=record.tenant, authz_wait=REAPPLY_AUTHZ_WAIT_SECONDS
        )
        web = await self._web_rev(source, record)
        rendered = _rendered(
            record,
            source,
            frozen=frozen,
            tenant=record.tenant,
            passing=passing_features(cluster),
            web=web,
        )
        fields = fields_of(rendered, _RENDER_FIELDS)
        return await self.store.set_fields(record.id, fields, expect_state=CLONE_LIVE_STATES)

    # -- 스윕 -------------------------------------------------------------------------------

    async def sweep(self) -> dict[str, list[str]]:
        """리컨실의 한 단계(리스 안). 클러스터마다 기록을 맞추고 지운다. 한 클러스터나 한 기록의
        실패가 다른 것을 막지 않는다. 스윕을 시작한 지 오래된 클러스터부터 보고, 인증
        재적용이 연결하지 못해 미뤄 둔 클러스터는 건너뛴다. 한 틱의 시간(`CLONE_SWEEP_SECONDS`)을
        넘기면 남은 것은 다음 틱이 잇는다."""
        result: dict[str, list[str]] = {
            key: [] for key in ("missing", "deleted", "deferred", "unreachable", "failed")
        }
        budget = Budget(CLONE_SWEEP_SECONDS)
        now = utcnow()
        install_id = await self.tenancy.platform_settings.install_id()
        backing_off = await self.auth_states.backing_off(now)
        clusters = await self.tenancy.clusters.list()
        clusters.sort(key=lambda c: self.started.get(c.id, 0.0))
        for cluster in clusters:
            # 틱의 리스를 늘리고 잃었으면 멈춘다 (P67 2절)
            await tick_item()
            if cluster.id in backing_off:
                result["unreachable"].append(f"cluster:{cluster.id}")
                continue
            if budget.out_of_time():
                result["deferred"].append(f"cluster:{cluster.id}")
                continue
            self.started[cluster.id] = time.monotonic()
            try:
                client = await run_in_threadpool(self.cluster_service.client_for, cluster)
                sweep = _ClusterSweep(self, cluster, client, install_id, result, budget)
                await sweep.run()
            except Exception as e:  # noqa: BLE001 - 한 클러스터의 실패가 스윕을 멈추면 안 된다
                logger.warning("shared clone sweep on cluster %s: %s", cluster.id, e)
                result["failed"].append(f"cluster:{cluster.id}")
        return {key: ids for key, ids in result.items() if ids}


class _ClusterSweep:
    """클러스터 하나의 스윕. 실물, 기록, Gateway 순으로 읽는다. 배포는 Gateway 의 스냅샷을 먼저
    저장하고 provision 에서 복제본과 기록을 만들므로, 먼저 읽은 복제본과 기록을 쓰는 Gateway 가 뒤에
    읽는 목록에서 빠지지 않는다. 지우기 직전에는 Gateway 를 다시 읽는다. 실물과 함께 읽은 Entry 가
    이름 댄 Service 의 복제본은 스냅샷이 쓰지 않아도 지우지 않는다(`_named`)."""

    def __init__(
        self,
        clones: SharedClones,
        cluster: Any,
        client: KubeClient,
        install_id: str,
        result: dict[str, list[str]],
        budget: Budget,
    ):
        self.clones = clones
        self.store = clones.store
        self.cluster = cluster
        self.client = client
        self.install_id = install_id
        self.result = result
        self.budget = budget
        self.seen_at = utcnow()  # 실물을 읽기 시작한 시각
        # 살아 있는 Entry 가 이름 댄 Service (`entry_services`). `run` 이 실물과 함께 읽는다
        self.entry_services: EntryServices = {}

    def _out_of_time(self) -> bool:
        return self.budget.out_of_time()

    async def run(self) -> None:
        live = await _live_clones(self.client)
        self.entry_services = await entry_services(self.client)
        records = await self.store.live_on_cluster(self.cluster.id)
        gateways = await self.clones.gateways.live_on_cluster(self.cluster.id)
        # 버려진 복제본을 기록마다의 일보다 먼저 지운다. 기록이 많아 한 틱에 다 보지 못해도 그
        # 정리가 밀리지 않는다
        await self._delete_orphans(records, gateways, live)
        resume = self.clones.resume_after
        for record in resumed(records, resume.get(self.cluster.id), key=_record_id, wrap=True):
            await tick_item()
            if self._out_of_time():
                self.result["deferred"].append(f"cluster:{self.cluster.id}")
                return
            try:
                await self._sweep_record(record, gateways, live)
            except Exception as e:  # noqa: BLE001 - 한 기록의 실패가 다른 기록을 막지 않는다
                logger.warning("shared clone %s: %s", record.id, e)
                self.result["failed"].append(record.id)
            resume[self.cluster.id] = record.id
        resume.pop(self.cluster.id, None)

    # -- 기록 하나 ----------------------------------------------------------------------------

    async def _sweep_record(
        self, record: SharedClone, gateways: list[Gateway], live: LiveClones
    ) -> None:
        users = _users(gateways, record)
        if not users:
            await self._delete_unused(record)
            return
        await self._check_live(record, live)

    async def _delete_unused(self, record: SharedClone) -> None:
        """쓰는 Gateway 가 없으면 기록과 복제본을 지운다. 살아 있는 Entry 가 그 복제본의 Service 를
        이름 대면 둔다. 지우기 직전에 Gateway 를 다시 읽고 기록을 먼저 지우며, 그 사이 배포가
        기록을 고쳤으면 지우지 않는다. 기록을 지운 바로 뒤에 배포가 기록을 다시 만들면 그 복제본은
        다음 틱의 실물 확인과 재적용이 되살린다."""
        if self._named(_clone_kind(record), record.name, record.namespace):
            return
        fresh = await self.clones.gateways.live_on_cluster(self.cluster.id)
        if _users(fresh, record):
            return
        if not await self.store.delete_unchanged(record.id, record.updated_at):
            return
        await self._delete_crs(record, [record.name])
        self.result["deleted"].append(f"{record.namespace}/{record.name}")

    def _named(self, kind: str, name: str, namespace: str) -> bool:
        """살아 있는 Entry 가 그 복제본 CR 의 프록시 Service 를 이름 대는가."""
        service = proxy_service_name(name, remote=kind == CR_KIND["remote"])
        return (namespace, service) in self.entry_services

    async def _check_live(self, record: SharedClone, live: LiveClones) -> None:
        """쓰이는 복제본이 실물에 없으면(손으로 지웠다) 렌더 번호를 -1 로 적어 재적용이 다시 만들게
        하고, 동결 표시를 원본의 consume 경계에 맞춘다. 둘을 한 번에 적는다. 복제본 기록은 턴 위임
        게이트가 읽지 않으므로 바꾼 번호를 담는 표시(`stale_mark`)가 아니라 -1 그대로다. 렌더하지
        않는 종류(remote)의 복제본은 재적용이 다시 만들지 못하므로 표시하지 않는다."""
        fields: dict[str, Any] = {}
        key = (record.namespace, _clone_kind(record), record.name)
        # 실물을 읽은 뒤에 렌더한 기록은 그 실물 목록으로 판정하지 않는다
        rendered_since = record.updated_at >= self.seen_at
        rendered = record.source.get("kind") not in KEPT_CLONE_KINDS
        if (
            rendered
            and key not in live
            and not rendered_since
            and not is_stale_epoch(record.render_epoch)
        ):
            fields["render_epoch"] = STALE_EPOCH
            self.result["missing"].append(f"{record.namespace}/{record.name}")
        frozen = (await self.clones.wanted_source(record)).frozen
        if frozen != record.frozen:
            fields["frozen"] = frozen
        if fields:
            await self.store.set_fields(record.id, fields)

    async def _delete_crs(self, record: SharedClone, names: list[str]) -> list[str]:
        """기록의 원본 종류인 복제본 CR 을 이름으로 지우고 지운 이름을 돌려준다. 같은 이름이라도
        다른 원본 라벨을 단 것은 지우지 않는다. image 복제본이면 울타리 설정과 도구 필터 CR 도
        지우고, 복제본이 먼저 사라졌으면 남은 둘만 원본을 확인해 지운다."""
        kind = _clone_kind(record)
        deleted: list[str] = []
        for name in names:
            cr = await run_in_threadpool(
                self.client.get, TOOLHIVE_API, kind, name, record.namespace
            )
            if cr is None and kind == CR_KIND["image"]:
                for side_kind, side in image_side_objects(name):
                    left = await run_in_threadpool(
                        self.client.get, TOOLHIVE_API, side_kind, side, record.namespace
                    )
                    if _source_of(left) == record.source_item_id:
                        await run_in_threadpool(
                            self.client.delete, TOOLHIVE_API, side_kind, side, record.namespace
                        )
            elif _source_of(cr) == record.source_item_id:
                await _delete_clone(self.client, kind, name, record.namespace)
                deleted.append(name)
        return deleted

    # -- 버려진 복제본 ------------------------------------------------------------------------

    async def _delete_orphans(
        self, records: list[SharedClone], gateways: list[Gateway], live: LiveClones
    ) -> None:
        """기록도 없고 가리키는 Gateway 도 없는 이 설치의 복제본을 지운다. 이 설치의 라벨이 있는
        것만이다. 다른 설치의 복제본과 라벨이 없는 복제본은 건드리지 않는다. 그 원본을 쓰는 Gateway
        가 그 네임스페이스에서 배포 중이거나 실패했으면 남기고(`_unsettled`), 살아 있는 Entry 가 그
        Service 를 이름 대도 남긴다(`_named`)."""
        named = {(r.namespace, r.name) for r in records}
        named |= _gateway_clone_names(gateways)
        for (namespace, kind, name), meta in sorted(live.items()):
            if (namespace, name) in named or self._out_of_time():
                continue
            if self._named(kind, name, namespace):
                continue
            await tick_item()
            labels = meta.get("labels") or {}
            source_id = labels.get(LABEL_SOURCE_ID, "")
            if labels.get(LABEL_PLATFORM_ID) != self.install_id:
                continue
            if _unsettled(gateways, namespace, source_id):
                continue
            await _delete_clone(self.client, kind, name, namespace)
            self.result["deleted"].append(f"{namespace}/{name}")


def clone_rev(
    record: SharedClone,
    source: dict[str, Any],
    web: str | None = None,
    *,
    newer: Sequence[str] = (),
) -> str | None:
    """기록을 source 로 렌더했을 때의 판 첫 절반. 원본 정의의 판이고, 자기 audience 로 옮겨 간
    기록이면 그 표시를 함께 싣는다 (P50 9절). web 은 플랫폼 fetch 의 설정 판이다(P39b 13절). 기대
    판(`SharedClones.wanted_hash`)과 판을 적는 자리(`_rendered`)가 모두 이것을 쓴다. newer 는 원본의
    모르는 칸이다(`newer_rev`, P55 9절). 렌더한 원본에는 없으므로 기대 판만 싣는다."""
    base = clone_source_hash(source)
    rev = own_audience_rev(f"{base}:{web}" if web else base, record.audience_switch)
    return newer_rev(rev, newer)


def _rendered(
    record: SharedClone,
    source: dict[str, Any],
    *,
    frozen: bool,
    tenant: str,
    passing: frozenset[Feature],
    web: str | None = None,
) -> SharedClone:
    """렌더한 뒤의 기록. 원본 정의와 동결, 판(`clone_rev` 와 그 클러스터에서의 이 종류의 렌더
    번호)을 적는다. passing 은 그 클러스터가 지나는 기능이다."""
    record.source, record.frozen, record.tenant, record.updated_at = (
        source,
        frozen,
        tenant,
        utcnow(),
    )
    record.mark_rendered(clone_rev(record, source, web), render_epoch(KIND_CLONE, passing))
    return record


def _clone_kind(record: SharedClone) -> str:
    return CR_KIND[record.source["kind"]] or ""


async def _delete_clone(client: KubeClient, kind: str, name: str, namespace: str) -> None:
    """복제본 CR 하나를 지운다. image 복제본이면 그 곁의 객체도(`image_side_objects`), 플랫폼 fetch
    복제본이면 곁의 NetworkPolicy 도(`FETCH_SIDE_OBJECTS`) 지운다. CA 는 env 로 실려 곁의 객체가
    없다."""
    await run_in_threadpool(client.delete, TOOLHIVE_API, kind, name, namespace)
    if name == FETCH_CLONE_NAME:
        for api, side_kind, side in FETCH_SIDE_OBJECTS:
            await run_in_threadpool(client.delete, api, side_kind, side, namespace)
        return
    if kind == CR_KIND["image"]:
        for side_kind, side in image_side_objects(name):
            await run_in_threadpool(client.delete, TOOLHIVE_API, side_kind, side, namespace)


def _source_of(cr: dict[str, Any] | None) -> str | None:
    """CR 의 원본 id 라벨. CR 이 없거나 라벨이 없으면 None 이다."""
    return (((cr or {}).get("metadata") or {}).get("labels") or {}).get(LABEL_SOURCE_ID)


def _record_id(record: SharedClone) -> str:
    return record.id


def _unsettled(gateways: list[Gateway], namespace: str, source_id: str) -> bool:
    """그 네임스페이스에서 그 원본을 쓰는 Gateway 가운데 deployed 가 아닌(배포 중이거나 실패한)
    것이 있는가. 그 배포가 아직 기록을 만들지 않았을 수 있어 그 원본의 버려진 복제본을 지우지
    않는다."""
    return any(
        g.state != "deployed"
        for g in gateways
        if g.k8s is not None
        and g.k8s.namespace == namespace
        and any(b.shared_source_id == source_id for b in g.deployed_backends)
    )


def _users(gateways: list[Gateway], record: SharedClone) -> list[Gateway]:
    """그 기록의 원본을 쓰는, draft 가 아닌 Gateway 가운데 같은 네임스페이스의 것."""
    return [
        g
        for g in gateways
        if g.k8s is not None
        and g.k8s.namespace == record.namespace
        and any(b.shared_source_id == record.source_item_id for b in g.deployed_backends)
    ]


def _gateway_clone_names(gateways: list[Gateway]) -> set[tuple[str, str]]:
    """Gateway 들이 가리키는 (네임스페이스, 복제본 이름). 원본을 가리지 않는다."""
    return {
        (g.k8s.namespace, b.clone_name)
        for g in gateways
        if g.k8s is not None
        for b in g.deployed_backends
        if b.clone_name
    }


async def _live_clones(client: KubeClient) -> LiveClones:
    """클러스터의 복제본 CR 의 metadata 를 (네임스페이스, 종류, 이름)마다 둔다. 허브 항목 CR 가운데
    원본 id 라벨이 있는 것이 복제본이다. 종류마다 목록 하나다. 전에 만든 remote 복제본도 읽어 버려진
    것을 지운다."""
    out: LiveClones = {}
    for kind in sorted({CR_KIND[k] or "" for k in (*CLONED_KINDS, *KEPT_CLONE_KINDS)}):
        items = await run_in_threadpool(
            client.list_metadata, TOOLHIVE_API, kind, labels={LABEL_RESOURCE: RESOURCE_HUB_ITEM}
        )
        for item in items:
            meta = item.get("metadata") or {}
            if (meta.get("labels") or {}).get(LABEL_SOURCE_ID):
                out[(meta.get("namespace") or "", kind, meta.get("name") or "")] = meta
    return out


class SharedCloneReapply:
    """재적용 단계가 다루는 종류 하나로서의 복제본 기록 (`services/data_plane_auth.py`
    `ReapplyKind`)."""

    auth_kind = KIND_CLONE
    profile_kind = "shared clone"
    live_states = CLONE_LIVE_STATES

    def __init__(self, clones: SharedClones):
        self.clones = clones
        self.instances = clones.store

    async def want_rev(self, doc: SharedClone, ts: TenantSettings) -> str | None:
        return await self.clones.wanted_hash(doc)

    async def paced_revs(self, doc: Any, ts: Any) -> frozenset[str | None]:
        return frozenset()

    async def reapply_auth(self, doc: SharedClone) -> bool:
        return await self.clones.rerender(doc)
