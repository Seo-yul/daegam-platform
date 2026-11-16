"""SecretStore 의 인메모리 테스트 대역 (P75, D87). 블로킹 메서드를 이벤트 루프 위에서 부르면
거부한다(P69, D81)."""

from __future__ import annotations

import copy

from tests.helpers.loop_guard import guard_methods


@guard_methods(("put", "create", "get", "delete", "merge", "swap"))
class FakeSecretStore:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], dict[str, str]] = {}
        self.labels: dict[tuple[str, str], dict[str, str]] = {}

    def put(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.secrets[(namespace, name)] = dict(data)
        self.labels[(namespace, name)] = dict(labels or {})

    def create(
        self,
        name: str,
        data: dict[str, str],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> bool:
        if (namespace, name) in self.secrets:
            return False
        self.put(name, data, namespace=namespace, labels=labels)
        return True

    def get(self, name: str, *, namespace: str) -> dict[str, str] | None:
        d = self.secrets.get((namespace, name))
        return copy.deepcopy(d) if d is not None else None

    def delete(self, name: str, *, namespace: str) -> bool:
        self.labels.pop((namespace, name), None)
        return self.secrets.pop((namespace, name), None) is not None

    def swap(
        self,
        name: str,
        expect: dict[str, str],
        data: dict[str, str | None],
        *,
        namespace: str,
    ) -> bool:
        stored = self.secrets.get((namespace, name))
        if stored is None or any(stored.get(k) != v for k, v in expect.items()):
            return False
        if any(v is None and k not in stored for k, v in data.items()):
            return False
        self.merge(name, data, namespace=namespace)
        return True

    def merge(
        self,
        name: str,
        data: dict[str, str | None],
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        stored = self.secrets.get((namespace, name))
        if stored is None:
            present = {k: v for k, v in data.items() if v is not None}
            if present:
                self.put(name, present, namespace=namespace, labels=labels)
            return
        for key, value in data.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value
