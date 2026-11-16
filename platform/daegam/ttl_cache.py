"""파드 로컬의 작은 TTL 캐시 (코어). 레플리카마다 따로 들고 파드 사이에 나누지 않는다.

키마다 값 하나와 만료 시각을 둔다. 만료한 값은 읽을 때 지운다. 잠그지 않으므로 여러 스레드가 함께
쓰는 쪽은 자기 잠금으로 감싼다(k8s/client.py 의 ClusterClientCache). 시계는 테스트가 바꿔 끼우는
자리다.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class TTLCache[K, V]:
    def __init__(
        self,
        ttl: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int | None = None,
    ):
        """max_entries 를 주면 넘칠 때 가장 먼저 넣은 값부터 버린다."""
        self.ttl = ttl
        self._clock = clock
        self._max = max_entries
        self._entries: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        """살아 있는 값. 없거나 만료했으면 None 이다."""
        hit = self._entries.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at <= self._clock():
            del self._entries[key]
            return None
        return value

    def put(self, key: K, value: V) -> None:
        self._entries.pop(key, None)
        self._entries[key] = (self._clock() + self.ttl, value)
        if self._max is not None and len(self._entries) > self._max:
            del self._entries[next(iter(self._entries))]

    def pop(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()
