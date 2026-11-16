"""image 항목의 Secret env 값을 두는 Secret 과 그 장부 (P55 5절, D97 초안).

항목마다 Secret 하나(`store/hub.py` `secret_env_name`)를 그 항목의 클러스터, 테넌트 네임스페이스에
둔다. 값은 사람이 화면에서 넣고 플랫폼은 다시 읽지 않는다. 문서는 이름마다 값이 있는지(`set`)만
안다. Secret 은 항목의 라벨과 이 설치의 `daegam.devops.ai.kr/platform-id` 를 단다. 플랫폼은 테넌트
네임스페이스의 Secret 목록을 볼 수 없으므로(`daegam-tenant-objects` 에 `list` 가 없다), 쓰기 전에
장부(`store/hub_env_secrets.py`)에 항목과 자리를 적고, 리컨실의 정리가 항목 없는 행의 Secret 을
이름으로 지운다. 장부와 정리의 규칙은 `store/secret_ledger.py` 에 있다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from daegam.errors import ServiceError
from daegam.k8s.client import KubeClient
from daegam.k8s.labels import LABEL_PLATFORM_ID
from daegam.k8s.templates.common import hub_labels
from daegam.secrets import K8sSecretStore
from daegam.store.base import NothingWritten, utcnow
from daegam.store.hub import HubItem, secret_env_name
from daegam.store.hub_env_secrets import MongoHubEnvSecretStore
from daegam.store.secret_ledger import SecretLedgerRow, sweep_ledger

logger = logging.getLogger("daegam.hub")


class SecretEnvSecrets:
    """항목마다의 Secret env Secret 과 그 장부. clients 는 클러스터 id 로 그 클러스터의 클라이언트를
    준다(없는 클러스터는 None). live_ids 는 정리가 남길 항목 id 를 찾는 허브 저장소의 조회다."""

    def __init__(
        self,
        *,
        ledger: MongoHubEnvSecretStore,
        clients: Callable[[str], Awaitable[KubeClient | None]],
        live_ids: Callable[[list[str]], Awaitable[set[str]]],
        install_id: Callable[[], Awaitable[str]],
    ):
        self.ledger = ledger
        self.clients = clients
        self.live_ids = live_ids
        self.install_id = install_id

    async def _labels(self, item: HubItem) -> dict[str, str]:
        return {
            **hub_labels(item.tenant, item.id, "image"),
            LABEL_PLATFORM_ID: await self.install_id(),
        }

    async def _note(self, item: HubItem) -> None:
        """Secret 에 쓰기 전에 장부에 그 자리를 적는다."""
        await self.ledger.note_write(
            item.id, utcnow(), cluster_id=item.cluster_id, namespace=item.k8s.namespace
        )

    async def create(self, item: HubItem, values: Mapping[str, str], client: KubeClient) -> None:
        """만들기의 Secret. 장부에 먼저 적고 쓴다. 항목 id 는 새로 지은 것이라 남의 Secret 일 수
        없다."""
        await self._note(item)
        await run_in_threadpool(
            K8sSecretStore(client).put,
            secret_env_name(item.id),
            dict(values),
            namespace=item.k8s.namespace,
            labels=await self._labels(item),
        )

    async def replace(
        self,
        item: HubItem,
        values: Mapping[str, str],
        client: KubeClient,
        error_cls: type[ServiceError],
    ) -> None:
        """보낸 이름의 값만 merge patch 로 바꾸고 다른 키는 읽지 않고 둔다(`K8sSecretStore.merge`).
        이름을 모두 보내지 않았는데 Secret 이 없으면 409 secret_missing 이고, 아무것도 쓰지
        않았으므로 `NothingWritten` 으로 알린다. 원래 오류는 그 `__cause__` 다."""
        await self._note(item)
        name, namespace = secret_env_name(item.id), item.k8s.namespace
        patch: dict[str, str | None] = dict(values)
        missing = sorted(set(item.source.secret_names()) - set(values))
        if not missing:
            await run_in_threadpool(
                K8sSecretStore(client).merge,
                name,
                patch,
                namespace=namespace,
                labels=await self._labels(item),
            )
            return
        if not await run_in_threadpool(client.merge_secret_keys, name, namespace, patch):
            raise NothingWritten from error_cls(
                409,
                "secret_missing",
                "the Secret env Secret of this item is missing; send a value for every name",
                env_names=missing,
            )

    async def delete(self, item: HubItem, client: KubeClient) -> None:
        """Secret 과 장부의 행을 지운다. 행은 Secret 을 지운 뒤에만 지워, 실패하면 정리가 마저
        지운다."""
        await run_in_threadpool(
            K8sSecretStore(client).delete, secret_env_name(item.id), namespace=item.k8s.namespace
        )
        await self.ledger.forget(item.id)

    async def forget_written(self, item: HubItem, client: KubeClient) -> None:
        """만들기가 문서를 쓰지 못했을 때 막 쓴 Secret 을 되돌린다. 실패하면 정리가 마저 지운다."""
        try:
            await self.delete(item, client)
        except Exception as e:  # noqa: BLE001 - 원래 오류를 덮지 않는다
            logger.warning(
                "Secret env Secret of hub item %s not deleted: %s", item.id, type(e).__name__
            )

    async def _delete_row(self, row: SecretLedgerRow) -> bool:
        """장부 행 하나의 Secret 을 지운다. 클러스터가 없어졌으면 지울 Secret 도 없어 행만
        지운다."""
        if not row.namespace:
            return False
        client = await self.clients(row.cluster_id or "")
        if client is not None:
            await run_in_threadpool(
                K8sSecretStore(client).delete, secret_env_name(row.id), namespace=row.namespace
            )
        return True

    async def sweep(self, now: datetime | None = None) -> dict[str, Any]:
        """장부에 있고 항목이 없는 Secret 을 지운다(`sweep_ledger`). 닿지 못하거나 권한이 없는
        클러스터의 행은 남겨 다음 차례에 다시 본다."""
        dropped = await sweep_ledger(self.ledger, now or utcnow(), self.live_ids, self._delete_row)
        if dropped:
            logger.info("Secret env Secrets without a hub item deleted: %s", dropped)
        return {"dropped": dropped} if dropped else {}
