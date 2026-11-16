"""Gateway 의 Entry 마다의 서비스 토큰 객체 (P72 2.2절, D41, P50 9절). 토큰을 받는 Entry 마다
그 Entry 가 부르는 서버의 audience 를 실은 Secret 과 MCPExternalAuthConfig 가 하나씩이다.
`GatewayTokens` 는 서비스가 생성자에서 만들고 이름 묶음을 콜러블로 넘긴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.auth.service_token import InternalIssuer
from daegam.auth.signer import PLATFORM_SERVER_IDS
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import daegam_labels
from daegam.k8s.templates.common import TOOLHIVE_API, rolled_out, secret_refs
from daegam.k8s.templates.svc_token import (
    RESOURCE_GATEWAY_SVC_TOKEN,
    TOKEN_BACKEND_KINDS,
    entry_token_objects,
    is_header_object,
    svc_token_refs,
    svc_token_rev,
)
from daegam.services.svc_tokens import SvcTokens
from daegam.store.base import from_epoch, utcnow
from daegam.store.gateways import Gateway, GatewayBackendSnapshot, MongoGatewayStore
from daegam.store.hub import MongoHubStore
from daegam.store.shared_clones import MongoSharedCloneStore

logger = logging.getLogger("daegam.gateways")


class GatewayTokens:
    """Entry 토큰을 내고, 서버 id 를 맞추고, 쓰지 않게 된 토큰 객체를 거둔다."""

    def __init__(
        self,
        *,
        svc_tokens: SvcTokens,
        gateways: MongoGatewayStore,
        hub_items: MongoHubStore,
        clone_records: MongoSharedCloneStore,
        k8s: Callable[[Gateway], Any],
        server_id: Callable[[Any, str], str | None],
    ):
        self.svc_tokens = svc_tokens
        # 게시된 Gateway 의 `bound_until` 을 묶인 토큰을 내기 전에 올린다 (P57 6.2절)
        self.gateways = gateways
        self.hub_items = hub_items
        self.clone_records = clone_records
        self.k8s = k8s
        # 이 테넌트 항목의 서버 id (`gateways.backend_server_id`). 다른 테넌트의 internal 은 422 라
        # Gateway 오류 클래스를 쓰는 함수를 서비스가 넘긴다
        self.server_id = server_id

    async def issue(
        self, doc: Gateway, client: KubeClient, *, fresh: bool = False
    ) -> dict[str, str]:
        """토큰을 받는 Entry 마다의 서비스 토큰 (Entry 이름 → 토큰, P50 9절). 각 토큰은 그 Entry 가
        부르는 서버의 audience 를 싣는다. Secret 에 있고 audience 가 맞으면 그대로 쓰고, fresh 면
        새로 낸다(회전). 토큰의 판과 자기 audience 를 실은 서버 목록을 문서에 적는다. 게시된
        백엔드의 Entry 는 그 Gateway 에 묶인 토큰이고, 그 만료를 토큰이 Secret 에 들어가기 전에 그
        Gateway 의 `bound_until` 로 올린다 (P57 6.1·6.2절)."""
        k8s = self.k8s(doc)
        rows = {b.workload: b for b in doc.deployed_backends}
        tokens: dict[str, str] = {}
        servers: set[str] = set()
        now = utcnow()
        for workload, svc_name in entry_token_objects(k8s.name, doc.deployed_backends).items():
            row = rows[workload]
            if row.bound_gateway_id:
                tokens[workload] = token = await self.svc_tokens.ensure_bound(
                    client,
                    secret_name=svc_name,
                    namespace=k8s.namespace,
                    name=k8s.name,
                    tenant=doc.tenant,
                    gateway_id=row.bound_gateway_id,
                    rules=row.rules_digest,
                    now=now,
                    fresh=fresh,
                )
                await self._raise_bound_until(row.bound_gateway_id, token)
                continue
            tokens[workload] = await self.svc_tokens.ensure(
                client,
                secret_name=svc_name,
                namespace=k8s.namespace,
                kind="vmcp",
                name=k8s.name,
                tenant=doc.tenant,
                server_id=row.server_id,
                fresh=fresh,
                rules=row.rules_digest,
            )
            # 플랫폼 웹 Tool audience 의 열쇠는 우리 서버 id 가 아니라 G9 의 대기 목록에 들지 않는다
            if row.server_id and row.server_id not in PLATFORM_SERVER_IDS:
                servers.add(row.server_id)
        doc.svc_token_rev = svc_token_rev(*tokens.values())
        doc.svc_token_servers = sorted(servers)
        return tokens

    async def _raise_bound_until(self, gateway_id: str, token: str) -> None:
        """묶인 토큰의 만료까지 그 Gateway 가 소비자 forbid 를 렌더하게 한다. 이미 Secret 에 있던
        토큰도 다시 올린다. 값은 앞으로만 간다(`MongoGatewayStore.raise_bound_until`)."""
        peek = InternalIssuer.peek(token)
        if peek is not None and peek.exp is not None:
            await self.gateways.raise_bound_until(gateway_id, from_epoch(peek.exp))

    async def _server_id_of(self, doc: Gateway, b: GatewayBackendSnapshot) -> str | None:
        """토큰을 받는 백엔드가 지금 부르는 우리 서버의 id (P50 9절). 다른 테넌트의 image 는 이
        네임스페이스의 공유 복제본 기록이고, 그 밖은 `backend_server_id` 다(다른 테넌트의 internal
        은 422). 항목이나 기록이 없으면 None 이다."""
        if b.shared_source_id:
            record = await self.clone_records.at(
                b.shared_source_id, doc.cluster_id or "", self.k8s(doc).namespace
            )
            return record.id if record is not None and record.tenant == doc.tenant else None
        item = await self.hub_items.get_unscoped(b.hub_item_id)
        return self.server_id(item, doc.tenant) if item else None

    async def current_server_ids(self, doc: Gateway) -> dict[str, str | None]:
        """토큰을 받는 Entry 마다(Entry 이름 → 서버 id) 지금 찾은 서버. 찾지 못하면 스냅샷의 id
        그대로다. 문서는 바꾸지 않는다. 다시 렌더하기(`resolve_servers`)가 부른다."""
        return {
            b.workload: await self._server_id_of(doc, b) or b.server_id
            for b in doc.deployed_backends
            if b.kind in TOKEN_BACKEND_KINDS
        }

    async def resolve_servers(self, doc: Gateway) -> None:
        """토큰을 받는 백엔드의 서버 id 를 지금 서버로 맞춘다 (P50 9절). 비어 있던 것을 채우고,
        다시 만들어진 공유 복제본 기록처럼 지금 서버와 다른 것을 고친다. 항목과 그 서버의 관계는
        바뀌지 않으므로 이 조회가 다시 렌더하기를 지금 정의에 묶지 않는다. 서버를 찾지 못한 Entry 는
        그대로 두고, 비어 남으면 그 토큰은 테넌트 audience 하나라 경고 로그를 남긴다."""
        found = await self.current_server_ids(doc)
        for b in doc.deployed_backends:
            if b.workload not in found:
                continue
            b.server_id = found[b.workload]
            if b.server_id is None:
                logger.warning(
                    "gateway %s: backend %s has no known server, its token keeps the tenant "
                    "audience alone",
                    doc.id,
                    b.workload,
                )

    async def labeled_objects(self, doc: Gateway, client: KubeClient) -> set[str]:
        """이 Gateway 가 만든 토큰 객체의 이름. 라벨로 찾은 MCPExternalAuthConfig 로 센다(같은
        이름의 Secret 이 짝이다)."""
        k8s = self.k8s(doc)
        labels = daegam_labels(tenant=doc.tenant, resource=RESOURCE_GATEWAY_SVC_TOKEN, id=doc.id)
        configs = await run_in_threadpool(
            client.list, TOOLHIVE_API, "MCPExternalAuthConfig", k8s.namespace, labels
        )
        return {c["metadata"]["name"] for c in configs}

    @staticmethod
    async def drop_objects(client: KubeClient, namespace: str, names: Iterable[str]) -> None:
        """토큰 객체(같은 이름의 Secret 과 MCPExternalAuthConfig)를 이름으로 지운다. 없으면
        넘어간다."""
        for name in names:
            for api_version, kind, ref in svc_token_refs(name):
                await run_in_threadpool(client.delete, api_version, kind, ref, namespace)

    async def settle(self, doc: Gateway, client: KubeClient) -> bool:
        """지금 렌더가 쓰지 않는 토큰 객체를 지운다 (P50 9절). 재배포가 뺀 Entry 의 것이다.
        오퍼레이터는 그 Secret 을 vMCP Deployment 의
        secretKeyRef env 로 싣고 그 키가 없으면 파드가 뜨지 못하므로, vMCP Deployment 가 다
        굴러갔고(`rolled_out`) 그 템플릿이 가리키지 않는 것만 지운다. 서비스 토큰 스윕이 부른다.
        지웠으면 참이다. 외부 서버 헤더의 사본(P86 6.2절)은 적용하는 렌더와 리컨실의 한 걸음이
        거두므로(`EntryHeaders`) 여기서 건드리지 않는다."""
        k8s = self.k8s(doc)
        wanted = set(entry_token_objects(k8s.name, doc.deployed_backends).values())
        stale = {
            n
            for n in await self.labeled_objects(doc, client) - wanted
            if not is_header_object(k8s.name, n)
        }
        if not stale:
            return False
        deployment = await run_in_threadpool(
            client.get, "apps/v1", "Deployment", k8s.name, k8s.namespace
        )
        if deployment is None or not rolled_out(deployment):
            return False
        stale -= secret_refs((deployment.get("spec") or {}).get("template"))
        await self.drop_objects(client, k8s.namespace, sorted(stale))
        return bool(stale)
