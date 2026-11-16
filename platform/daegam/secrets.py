"""SecretStore: 비밀 값은 K8s Secret 에만 둔다. MongoDB 에는 Secret 이름만 들어간다."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from starlette.concurrency import run_in_threadpool

from daegam.k8s.client import KubeClient, decode_secret_data, encode_secret_data
from daegam.k8s.labels import LABEL_MANAGED_BY, MANAGED_BY


@runtime_checkable
class SecretStore(Protocol):
    def put(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None: ...

    def get(self, name: str, *, namespace: str) -> dict[str, str] | None: ...
    def delete(self, name: str, *, namespace: str) -> bool: ...

    def merge(
        self,
        name: str,
        data: dict[str, str | None],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        """키를 더하거나 바꾸고(값) 뺀다(None). 다른 키는 둔다. 없으면 값이 있는 키로 만든다."""
        ...

    def swap(
        self,
        name: str,
        expect: dict[str, str],
        data: dict[str, str | None],
        *,
        namespace: str,
    ) -> bool:
        """expect 의 키가 모두 그 값일 때만 키를 바꾸고(값) 뺀다(None). 아니면 False 다."""
        ...

    def create(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> bool:
        """있으면 만들지 않고 False 다.

        put(SSA)은 덮어쓰므로 먼저 쓴 쪽이 이겨야 하는 자리에 쓴다."""
        ...


async def read_secret_data(
    store: SecretStore | None, name: str, *, namespace: str
) -> dict[str, str]:
    """관리 Secret 하나의 값 전체. 저장소가 없거나 Secret 이 없으면 빈 dict 다. 읽기의 실패는 그대로
    올라간다. 허브 이미지의 읽기 자격 증명(사용자와 비밀번호)이 쓴다."""
    if store is None:
        return {}
    return dict(await run_in_threadpool(store.get, name, namespace=namespace) or {})


async def read_secret_key(
    store: SecretStore | None, name: str, key: str, *, namespace: str
) -> str | None:
    """관리 Secret 하나의 키 값. 저장소가 없거나 Secret 이나 키가 없으면 None 이다. 읽기의 실패는
    그대로 올라간다. 웹 검색의 키, 외부 서버의 헤더 값, Teams 봇의 Client Secret 이 함께 쓴다."""
    return (await read_secret_data(store, name, namespace=namespace)).get(key) or None


def _secret_manifest(
    name: str, data: dict[str, str], *, namespace: str, labels: dict[str, str] | None
) -> dict[str, Any]:
    """플랫폼이 쓰는 Opaque Secret. managed-by 라벨은 늘 붙는다."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {LABEL_MANAGED_BY: MANAGED_BY, **(labels or {})},
        },
        "type": "Opaque",
        "data": encode_secret_data(data),
    }


class K8sSecretStore:
    """대상 클러스터(보통 home)의 Secret 을 SSA 로 쓰고 읽는다."""

    def __init__(self, client: KubeClient):
        self.client = client

    def put(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.client.apply(_secret_manifest(name, data, namespace=namespace, labels=labels))

    def create(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> bool:
        return self.client.create(_secret_manifest(name, data, namespace=namespace, labels=labels))

    def get(self, name: str, *, namespace: str) -> dict[str, str] | None:
        obj = self.client.get("v1", "Secret", name, namespace)
        if obj is None:
            return None
        return decode_secret_data(obj.get("data"))

    def delete(self, name: str, *, namespace: str) -> bool:
        return self.client.delete("v1", "Secret", name, namespace)

    def swap(
        self,
        name: str,
        expect: dict[str, str],
        data: dict[str, str | None],
        *,
        namespace: str,
    ) -> bool:
        """JSON Patch 하나의 test 라 그 사이에 다른 쓰기가 끼지 못한다."""
        return self.client.swap_secret_keys(name, namespace, expect, data)

    def merge(
        self,
        name: str,
        data: dict[str, str | None],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        """merge patch 하나라 같은 Secret 의 다른 키를 고치는 요청과 겹쳐도 잃지 않는다. 없으면
        만들고, 그사이 다른 요청이 만들었으면 다시 고친다."""
        if self.client.merge_secret_keys(name, namespace, data):
            return
        present = {k: v for k, v in data.items() if v is not None}
        if not present:
            return
        if not self.create(name, present, namespace=namespace, labels=labels):
            self.client.merge_secret_keys(name, namespace, data)
