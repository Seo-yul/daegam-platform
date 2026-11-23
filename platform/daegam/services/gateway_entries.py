"""Gateway 의 MCPServerEntry 이름이 누구의 것인지 (D82). Entry 이름 `<gateway>-<item>` 은 두
이름을 하이픈으로 잇기만 하므로 Gateway `shop` 의 항목 `a-orders` 와 Gateway `shop-a` 의 항목
`orders` 가 같은 이름이 된다. 이름 규칙을 바꾸면 prefix 모드의 도구 이름이 바뀌므로 규칙은 두고
겹치는 배포를 막는다.

막는 자리는 셋이다. 배포 전 검사(`require_free`)는 다른 Gateway 의 배포 스냅샷을 읽는다. 선점
(`claimed`·`claim`)은 (클러스터, 네임스페이스, 이름) 마다 유일한 기록이라 함께 도는 두 배포를
가른다. 적용 직전 막이(`require_own`)는 클러스터의 Entry 라벨을 보고 기록 없는 옛 Entry 를
덮어쓰지 않는다. `GatewayEntries` 는 서비스가 생성자에서 만든다.

노출한 MCP Server 의 vMCP 앞단도 Entry 이름 `front-<k8s.name>` 을 같은 기록에 선점한다 (P54 5절).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient, parse_service_url
from daegam.k8s.labels import LABEL_ID
from daegam.k8s.templates.common import TOOLHIVE_API
from daegam.services.claims import tick_item
from daegam.store.gateway_entry_names import (
    EntryNameClaim,
    MongoEntryNameStore,
    entry_claim_id,
)
from daegam.store.gateways import Gateway, GatewayBackendSnapshot, GatewayK8s, MongoGatewayStore
from daegam.store.mcp_servers import MongoMCPServerStore
from daegam.store.name_claims import CLAIM_GRACE

logger = logging.getLogger("daegam.gateways")

ENTRY_KIND = "MCPServerEntry"


async def entry_owners(client: KubeClient, namespace: str) -> dict[str, str | None]:
    """그 네임스페이스의 MCPServerEntry 이름 → 라벨 daegam.devops.ai.kr/id 의 값(없으면 None). Entry
    이름은 네임스페이스 안에서 하나뿐이고 force SSA 는 남의 Entry 도 덮어쓰므로, 적용하기 전에
    누구의 것인지 본다. metadata 만 읽는다."""
    listed = await run_in_threadpool(client.list_metadata, TOOLHIVE_API, ENTRY_KIND, namespace)
    return {
        item["metadata"]["name"]: (item["metadata"].get("labels") or {}).get(LABEL_ID)
        for item in listed
    }


# (네임스페이스, Service 이름) 마다 그 Service 를 이름 댄 Entry 들의 라벨 daegam.devops.ai.kr/id 값
EntryServices = dict[tuple[str, str], set[str | None]]


async def entry_services(client: KubeClient) -> EntryServices:
    """클러스터의 지워지는 중이 아닌 MCPServerEntry 가 `spec.remoteUrl` 로 이름 댄 클러스터 안
    Service. 바깥 주소를 가리키는 Entry 는 들지 않는다. 프록시나 복제본을 지워도 되는지는 저장된
    스냅샷이 아니라 클러스터가 도는 이것으로 본다. 렌더를 적용하지 못한 Gateway 의 Entry 가 아직
    그 Service 를 부르기 때문이다."""
    named: EntryServices = {}
    for entry in await run_in_threadpool(client.list, TOOLHIVE_API, ENTRY_KIND):
        meta = entry.get("metadata") or {}
        service = parse_service_url((entry.get("spec") or {}).get("remoteUrl") or "")
        if service is None or meta.get("deletionTimestamp"):
            continue
        named.setdefault(service[:2], set()).add((meta.get("labels") or {}).get(LABEL_ID))
    return named


class GatewayEntries:
    """Entry 이름의 배포 전 검사, 선점과 그 놓기, 적용 직전 막이, 자기 Entry 만 지우기. 노출한 MCP
    Server 의 vMCP 앞단도 Entry 이름 `front-<k8s.name>` 을 같은 기록에 선점한다 (P54 5절). servers
    는 쓸기가 그 이름을 쓰고 있는 것으로 읽는 MCP Server 의 스토어다."""

    def __init__(
        self,
        *,
        names: MongoEntryNameStore,
        gateways: MongoGatewayStore,
        servers: MongoMCPServerStore,
        k8s: Callable[[Gateway], GatewayK8s],
        conflict: Callable[..., ServiceError],
    ):
        self.names = names
        self.gateways = gateways
        self.servers = servers
        self._k8s = k8s
        # 서비스의 409 k8s_name_conflict 생성 (`BaseInstanceService._name_conflict`)
        self._conflict = conflict

    async def conflict(self, namespace: str, name: str, holder_id: str | None) -> ServiceError:
        """다른 Gateway 가 쥔 Entry 이름의 409 k8s_name_conflict. gateway_ids 가 화면의 참조 줄에
        막는 Gateway 를 보인다. 그 id 의 문서가 없으면(지운 Gateway 의 라벨이 붙은 Entry) id 로
        적는다."""
        if holder_id is None:
            return self._conflict(ENTRY_KIND, namespace, name, "another gateway")
        holder = await self.gateways.get(holder_id)
        who = f"gateway {holder.name}" if holder else f"document {holder_id}"
        return self._conflict(ENTRY_KIND, namespace, name, who, gateway_ids=[holder_id])

    async def require_free(
        self,
        doc: Gateway,
        cluster_id: str,
        namespace: str,
        backends: list[GatewayBackendSnapshot],
    ) -> None:
        """배포·재배포 전 검사. 이 배포의 Entry 이름을 같은 클러스터·네임스페이스의 다른 Gateway 가
        배포 스냅샷에 두고 쥐고 있으면 저장하기 전에 409 다. 실패한 문서는 세지 않는다. 적용한
        이름은 선점이 남아 막고, 적용하지 못한 이름은 실패할 때 놓았다(`release_unapplied`)."""
        names = {b.workload for b in backends}
        clash = await self.gateways.holding_entries(cluster_id, namespace, names, exclude_id=doc.id)
        if clash is not None:
            name = min(names & {b.workload for b in clash.deployed_backends})
            raise await self.conflict(namespace, name, clash.id)

    async def claim(self, doc: Gateway) -> list[EntryNameClaim]:
        """배포 스냅샷의 Entry 이름을 모두 선점하고 새로 넣은 기록을 돌려준다. 자기 기록은 새
        nonce 로 다시 선점한다. 다른 Gateway 가 쥔 이름이 있으면 이 호출이 넣은 것을 지우고 409
        다."""
        return await self.claim_names(doc, {b.workload for b in doc.deployed_backends})

    async def claim_names(self, doc: Any, names: Collection[str]) -> list[EntryNameClaim]:
        """`claim` 의 이름을 받는 꼴. 노출한 MCP Server 가 vMCP 앞단의 Entry 이름 하나를 자기 것으로
        선점할 때도 쓴다 (P54 5절). doc 은 id·tenant·cluster_id 와 이 서비스가 아는 k8s 를 가진
        문서다."""
        namespace = self._k8s(doc).namespace
        inserted: list[EntryNameClaim] = []
        for name in sorted(names):
            try:
                claim, new = await self._claim_one(doc, namespace, name)
            except ServiceError:
                await self._remove(inserted)
                raise
            if new:
                inserted.append(claim)
        return inserted

    async def _claim_one(self, doc: Any, namespace: str, name: str) -> tuple[EntryNameClaim, bool]:
        """이름 하나를 선점한다. 다른 Gateway 가 쥐고 있으면 그 Gateway 를 gateway_ids 로 실은 409
        다. 선점에 진 뒤 기록을 읽기 전에 쥔 쪽이 놓았으면 한 번 더 선점해 본다. 그래도 쥔 쪽을 읽지
        못할 때만 누구인지 없이 답한다."""
        for _ in range(2):
            got = await self.names.claim(
                self._key(doc, name),
                resource_id=doc.id,
                tenant=doc.tenant,
                cluster_id=doc.cluster_id,
                namespace=namespace,
                name=name,
            )
            if got is not None:
                return got
            holder = await self.holder(doc, name)
            if holder is not None:
                raise await self.conflict(namespace, name, holder.resource_id)
        raise await self.conflict(namespace, name, None)

    def _key(self, doc: Any, name: str) -> str:
        """그 문서의 클러스터·네임스페이스에서 Entry 이름 하나의 선점 기록 id."""
        return entry_claim_id(doc.cluster_id or "", self._k8s(doc).namespace, name)

    async def holder(self, doc: Any, name: str) -> EntryNameClaim | None:
        """그 문서의 클러스터·네임스페이스에서 Entry 이름 하나를 쥔 선점 기록. 없으면 None 이다."""
        return await self.names.get(self._key(doc, name))

    def claimed(self, doc: Gateway) -> AbstractAsyncContextManager[None]:
        """deploying 으로 옮기는 저장 동안 배포 스냅샷의 Entry 이름을 선점한다(`claimed_names`)."""
        return self.claimed_names(doc, {b.workload for b in doc.deployed_backends})

    @asynccontextmanager
    async def claimed_names(self, doc: Any, names: Collection[str]) -> AsyncIterator[None]:
        """블록 동안 names 를 선점한다. 공개 host 의 선점(`ExposureValidationMixin._host_claimed`)과
        같이, 블록 안에서 무엇이든 실패하면 이 호출이 새로 넣은 기록만 지운다. vMCP 앞단은 적용 전
        라벨 검사를 이 블록 안에서 한다 (P54 5절)."""
        inserted = await self.claim_names(doc, names)
        try:
            yield
        except BaseException:
            await self._remove(inserted)
            raise

    async def _remove(self, claims: list[EntryNameClaim]) -> None:
        for claim in claims:
            await self.names.remove(claim.id, claim.nonce)

    async def release_except(self, doc: Gateway, keep: Collection[str]) -> None:
        """이 문서의 선점 가운데 지금 클러스터·네임스페이스의 keep 이름이 아닌 것을 읽은 nonce 로
        놓는다. 재배포가 뺀 이름과 실패한 배포가 적용하지 못한 이름이다."""
        kept = {self._key(doc, name) for name in keep}
        await self._remove([c for c in await self.names.of_resource(doc.id) if c.id not in kept])

    async def release_unapplied(
        self, doc: Gateway, client_for: Callable[[Gateway], Awaitable[KubeClient]]
    ) -> None:
        """실패한 배포의 선점 가운데 적용하지 못한 이름의 것을 놓는다. 그래야 적용한 적 없는 이름을
        실패한 문서가 쥐고 있지 않다. 클러스터에 이 문서의 라벨로 살아 있는 Entry 의 이름은 쥔 채
        둔다. 클러스터를 읽지 못하면 무엇을 적용했는지 모르므로 모두 쥔 채 둔다."""
        try:
            owners = await entry_owners(await client_for(doc), self._k8s(doc).namespace)
        except Exception as e:  # noqa: BLE001 - 실패를 덮지 않고 선점만 남긴다
            logger.warning("gateway %s: entry name claims kept, cluster not read: %s", doc.id, e)
            return
        await self.release_except(doc, [n for n, owner in owners.items() if owner == doc.id])

    async def foreign(self, doc: Gateway, client: KubeClient) -> dict[str, str]:
        """이 문서가 적용할 Entry 가운데 다른 문서의 라벨(daegam.devops.ai.kr/id)을 단 채 살아 있는
        것의 이름 → 그 문서 id. 선점이 생기기 전에 겹친 Entry 와, 지운 Gateway 의 라벨이 남은 Entry
        다."""
        return await self.foreign_names(doc, client, [b.workload for b in doc.deployed_backends])

    async def foreign_names(
        self, doc: Any, client: KubeClient, names: Collection[str]
    ) -> dict[str, str]:
        """`foreign` 의 이름을 받는 꼴. names 가운데 다른 문서의 라벨을 단 채 살아 있는 Entry 의
        이름 → 그 문서 id 다."""
        owners = await entry_owners(client, self._k8s(doc).namespace)
        return {name: owner for name in names if (owner := owners.get(name)) not in (None, doc.id)}

    async def skip_foreign(
        self, doc: Gateway, client: KubeClient, rendered: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """rendered 에서 다른 문서의 것인 Entry 를 뺀다. 다시 렌더하기와 공유 복제본 옮기기가 쓰고,
        그 Entry 는 덮어쓰지 않고 나머지를 적용한다. 여기서 멈추면 토큰을 갈거나 복제본을 옮기지
        못해 Gateway 전체가 멈추므로 배포처럼 409 로 끝내지 않는다."""
        foreign = await self.foreign(doc, client)
        if foreign:
            logger.warning(
                "gateway %s: Entries held by other documents are not applied: %s",
                doc.id,
                ", ".join(sorted(foreign)),
            )
        return [
            d
            for d in rendered
            if not (d["kind"] == ENTRY_KIND and d["metadata"]["name"] in foreign)
        ]

    async def require_own(self, doc: Gateway, client: KubeClient) -> None:
        """적용 직전 막이. 이 문서가 적용할 Entry 가 다른 문서의 것이면 force SSA 로 덮어쓰지 않게
        아무것도 쓰기 전에 409 로 멈춘다."""
        await self.require_own_names(doc, client, [b.workload for b in doc.deployed_backends])

    async def require_own_names(self, doc: Any, client: KubeClient, names: Collection[str]) -> None:
        """`require_own` 의 이름을 받는 꼴. 노출한 MCP Server 의 vMCP 앞단도 적용 전에 쓴다 (P54
        5절)."""
        foreign = await self.foreign_names(doc, client, names)
        if foreign:
            name = min(foreign)
            raise await self.conflict(self._k8s(doc).namespace, name, foreign[name])

    async def delete_own(self, doc: Gateway, client: KubeClient, names: Collection[str]) -> None:
        """names 가운데 이 문서의 라벨(daegam.devops.ai.kr/id)이 붙은 Entry 만 이름으로 지운다. 같은
        이름의 Entry 를 다른 Gateway 가 쓰고 있으면 그것은 그쪽 것이다."""
        namespace = self._k8s(doc).namespace
        own = await run_in_threadpool(
            client.list_metadata, TOOLHIVE_API, ENTRY_KIND, namespace, labels={LABEL_ID: doc.id}
        )
        for name in sorted(set(names) & {item["metadata"]["name"] for item in own}):
            await run_in_threadpool(client.delete, TOOLHIVE_API, ENTRY_KIND, name, namespace)

    async def sweep(self, now: datetime) -> dict[str, list[str]]:
        """리컨실 단계. 유예(`CLAIM_GRACE`)가 지난 선점 가운데 그 Gateway 나 노출한 MCP Server 가
        더는 쓰지 않는 이름의 것을 읽은 nonce 로 지운다. 전이를 저장하기 전에 죽은 배포와, 재배포가
        이름을 빼거나 노출을 끄고 놓기 전에 죽은 자리가 남긴 기록이다."""
        in_use = await self.gateways.entry_names_in_use()
        in_use |= await self.servers.front_entries_in_use()
        released: list[str] = []
        for claim in await self.names.all():
            # 리컨실 리스를 항목마다 늘린다 (P67)
            await tick_item()
            used = (claim.resource_id, claim.cluster_id, claim.namespace, claim.name) in in_use
            if now - claim.claimed_at < CLAIM_GRACE or used:
                continue
            if await self.names.remove(claim.id, claim.nonce):
                released.append(claim.id)
        return {"released": released}
