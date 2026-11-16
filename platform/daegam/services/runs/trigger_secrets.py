"""웹훅 트리거의 서명 비밀 (P41 4절).

비밀은 `whsec_` 를 앞에 붙인 32바이트 키이고(Standard Webhooks), 만들 때와 바꿀 때 한 번만 보인다.
값은 Agent 마다 플랫폼이 관리하는 Secret 하나(`daegam-hooks-<agent id>`, 플랫폼 네임스페이스)에
트리거 id 로 둔다. 지금 비밀은 `<trigger id>`, 바꾸기 전 비밀은 `<trigger id>.previous` 이고 바꾼 뒤
24시간 받는다(그 끝은 트리거 기록의 `previous_until`). Provider 키(D56)처럼 Mongo 에는 지문
(`hash_token`)만 둔다. 키 하나를 고치는 일은 merge patch 하나라 같은 Agent 의 트리거를 함께 만들어도
잃는 키가 없다. Secret 저장소는 블로킹이라 스레드에서 부른다.

Secret 에 값을 쓰기 전에 그 Agent 를 장부(`store/hook_secrets.py`)에 적는다. 플랫폼은 Secret 목록을
보지 않으므로 리컨실의 쓸기(`trigger_sweep.py`)가 장부로 Secret 을 찾는다.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from anyio import to_thread

from daegam.k8s.labels import LABEL_ID, LABEL_RESOURCE
from daegam.secrets import SecretStore
from daegam.services.runs.errors import RunError
from daegam.store.base import hash_token, is_id, utcnow
from daegam.store.hook_secrets import MongoHookSecretStore

SECRET_PREFIX = "whsec_"
SECRET_BYTES = 32
# 바꾼 뒤 앞 비밀을 받는 동안 (4절)
ROTATION_GRACE = timedelta(hours=24)
# 기록과 Secret 이 어긋난 트리거를 버려진 새로 만들기로 볼 때까지. 기록을 바꾼 시각부터 센다.
# Secret 쓰기 한 번은 1분 가까이 걸릴 수 있고(k8s 쓰기 시간 제한) merge 는 세 번까지 부르므로 넉넉히
# 둔다. 리컨실의 쓸기도 쓰기를 시작한 지 이만큼 지난 Agent 만 본다
ROTATION_SETTLE = timedelta(minutes=15)
PREVIOUS = ".previous"
# 리컨실이 남은 키를 찾을 때 Secret 을 고르는 라벨 값
RESOURCE = "run-triggers"


def new_secret() -> str:
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode()


def fingerprint(secret: str) -> str:
    """Mongo 에 두는 지문. 값 자체는 두지 않는다."""
    return hash_token(secret)


def key_bytes(secret: str) -> bytes:
    """서명 키. `whsec_` 뒤의 base64 를 푼 것이다."""
    return base64.b64decode(secret.removeprefix(SECRET_PREFIX))


def secret_name(agent_id: str) -> str:
    """Agent 의 Secret 이름. Agent id 의 꼴(UUID)이 아니면 ValueError 라 다른 Secret 을 가리키지
    못한다."""
    if not is_id(agent_id):
        raise ValueError(f"not an agent id: {agent_id!r}")
    return f"daegam-hooks-{agent_id}"


@dataclass(frozen=True)
class TriggerKeys:
    """트리거 하나의 비밀. previous 는 바꾼 뒤 24시간만 있다."""

    current: str | None
    previous: str | None


def _pairs(trigger_id: str, keys: TriggerKeys) -> dict[str, str | None]:
    return {trigger_id: keys.current, trigger_id + PREVIOUS: keys.previous}


class TriggerSecrets:
    def __init__(
        self,
        store: Callable[[], SecretStore | None],
        *,
        namespace: str,
        ledger: MongoHookSecretStore,
    ):
        self._store = store
        self.namespace = namespace
        self.ledger = ledger

    @property
    def available(self) -> bool:
        return self._store() is not None

    def _required(self) -> SecretStore:
        store = self._store()
        if store is None:
            raise RunError(422, "no_secret_store", "webhook triggers need a secret store")
        return store

    async def put(self, agent_id: str, trigger_id: str, current: str, previous: str | None) -> None:
        """트리거의 지금 비밀과 앞 비밀(없으면 지운다)을 적는다. 그 전에 Agent 를 장부에 적는다."""
        store = self._required()
        await self.ledger.note_write(agent_id, utcnow())
        data = {trigger_id: current, trigger_id + PREVIOUS: previous}
        labels = {LABEL_RESOURCE: RESOURCE, LABEL_ID: agent_id}
        await to_thread.run_sync(
            lambda: store.merge(
                secret_name(agent_id), data, namespace=self.namespace, labels=labels
            )
        )

    async def read(self, agent_id: str, trigger_id: str) -> TriggerKeys:
        """트리거의 비밀. 저장소가 없거나 읽지 못하면 그대로 올린다(웹훅 경로는 503)."""
        store = self._required()
        found = await to_thread.run_sync(
            lambda: store.get(secret_name(agent_id), namespace=self.namespace)
        )
        data = found or {}
        return TriggerKeys(data.get(trigger_id), data.get(trigger_id + PREVIOUS))

    async def restore(
        self, agent_id: str, trigger_id: str, written: TriggerKeys, back: TriggerKeys
    ) -> bool:
        """트리거의 비밀이 아직 written 일 때만 back 으로 되돌린다. 그사이 다른 쓰기가 있었으면
        두고 거짓이다."""
        expect = _pairs(trigger_id, written)
        data = _pairs(trigger_id, back)
        return await self._swap(agent_id, {k: v for k, v in expect.items() if v is not None}, data)

    async def drop_if_unchanged(self, agent_id: str, seen: dict[str, str]) -> bool:
        """seen 의 키가 모두 읽은 값 그대로일 때만 뺀다(리컨실의 쓸기). 아니면 두고 거짓이다."""
        return await self._swap(agent_id, seen, dict.fromkeys(seen))

    async def _swap(
        self, agent_id: str, expect: dict[str, str], data: dict[str, str | None]
    ) -> bool:
        store = self._required()
        return await to_thread.run_sync(
            lambda: store.swap(secret_name(agent_id), expect, data, namespace=self.namespace)
        )

    async def drop(self, agent_id: str, trigger_id: str) -> None:
        """트리거를 지울 때 그 키를 뺀다."""
        await self.drop_keys(agent_id, [trigger_id, trigger_id + PREVIOUS])

    async def drop_keys(self, agent_id: str, keys: list[str]) -> None:
        """키를 뺀다. 다른 키는 둔다. Secret 이 없으면 아무것도 하지 않는다."""
        store = self._required()
        data: dict[str, str | None] = dict.fromkeys(keys)
        await to_thread.run_sync(
            lambda: store.merge(secret_name(agent_id), data, namespace=self.namespace)
        )

    async def all_keys(self, agent_id: str) -> dict[str, str] | None:
        """Agent 의 Secret 전부. 없으면 None 이다(리컨실의 쓸기)."""
        store = self._required()
        return await to_thread.run_sync(
            lambda: store.get(secret_name(agent_id), namespace=self.namespace)
        )

    async def drop_agent(self, agent_id: str) -> None:
        """Agent 를 지울 때 그 Secret 을 지운다. 저장소가 없으면 지울 것도 없다."""
        store = self._store()
        if store is None:
            return
        await to_thread.run_sync(
            lambda: store.delete(secret_name(agent_id), namespace=self.namespace)
        )


__all__ = [
    "PREVIOUS",
    "ROTATION_GRACE",
    "ROTATION_SETTLE",
    "TriggerKeys",
    "TriggerSecrets",
    "fingerprint",
    "key_bytes",
    "new_secret",
    "secret_name",
]
