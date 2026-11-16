"""리스를 잡아 한 건씩 처리하는 워커의 공통 수명. Spec Generator 워커와 Knowledge 인덱서가 쓴다.
쉬기 워커와 미리 보기의 끝내기 워커는 그 위의 `QueuedLeaseWorker` 를 쓴다.

둘은 같은 실행 모델이다. 주기마다 `acquire()` 로 잡을 수 있는 것을 전부 하나씩 잡아 `process()` 에
넘기고, 처리하는 동안 `heartbeating()` 이 리스를 갱신한다. 무엇을 잡고 어떻게 갱신하는지, 처리의
실패를 어떻게 접는지는 워커마다 달라 하위 클래스가 정한다. 리스 자체(`LeaseLost`·소유자 검사)는
스토어 쪽에 그대로 둔다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from functools import partial
from typing import ClassVar, Protocol

from daegam.services.periodic import cancel_task, renewing_every, run_periodic
from daegam.store.base import utcnow
from daegam.store.leases import Lease


def default_owner() -> str:
    return f"{os.environ.get('HOSTNAME') or socket.gethostname()}-{uuid.uuid4().hex[:6]}"


class LeaseWorker[T]:
    # 하위 클래스가 정한다. 태스크 이름의 앞머리, 루프 실패 로그의 이름, 하트비트 로그의 항목 이름
    task_prefix: ClassVar[str]
    tick_label: ClassVar[str]
    item_label: ClassVar[str]
    log: ClassVar[logging.Logger]

    def __init__(self, *, owner: str | None, interval: float, lease_seconds: float):
        self.owner = owner or default_owner()
        self.interval = interval
        self.lease_seconds = lease_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- 하위 클래스가 채운다 --------------------------------------------------------

    async def acquire(self) -> T | None:
        """잡을 수 있는 것 하나에 리스를 걸어 돌려준다. 없으면 None 이다."""
        raise NotImplementedError

    async def process(self, item: T) -> None:
        """잡은 것 하나를 끝까지 처리한다. 실패는 여기서 기록하고 올리지 않는다."""
        raise NotImplementedError

    async def renew(self, item_id: str) -> bool:
        """리스를 연장한다. 이미 남의 것이면 False 다."""
        raise NotImplementedError

    # -- 수명 -----------------------------------------------------------------

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name=f"{self.task_prefix}-{self.owner}")

    async def stop(self) -> None:
        self._stop.set()
        await cancel_task(self._task)
        self._task = None

    async def _loop(self) -> None:
        await run_periodic(
            self.tick, interval=self.interval, stop=self._stop, log=self.log, label=self.tick_label
        )

    # -- 한 바퀴 ------------------------------------------------------------------

    async def tick(self) -> int:
        """잡을 수 있는 것을 전부 순서대로 처리하고 처리한 수를 돌려준다."""
        n = 0
        while (item := await self.acquire()) is not None:
            await self.process(item)
            n += 1
        return n

    @contextlib.asynccontextmanager
    async def heartbeating(self, item_id: str) -> AsyncIterator[None]:
        """블록이 도는 동안 리스를 갱신한다. 블록을 나가면 하트비트를 거둔다."""
        async with renewing_every(self.lease_seconds, partial(self._heartbeat, item_id)):
            yield

    async def _heartbeat(self, item_id: str) -> bool:
        """한 번 갱신한다. 실패는 로그만 남기고 다음 차례에 다시 한다. 남의 것이면 False 다."""
        try:
            ok = await self.renew(item_id)
        except Exception:  # noqa: BLE001
            self.log.exception("%s %s: lease renew failed", self.item_label, item_id)
            return True
        if not ok:
            self.log.warning("%s %s: lease no longer ours", self.item_label, item_id)
        return ok


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


class QueuedLeaseWorker[T: _Identified](LeaseWorker[T]):
    """틱마다 볼 것을 줄에 세우고(`queue`) 하나마다 리스 `<task_prefix>:<id>` 를 걸어 한
    레플리카만 다루는(`handle`) 워커. 리스는 리스 컬렉션(`store/leases.py` `Lease`)에 둔다. 하나의
    실패는 로그만 남기고 다음 것을 막지 않으며, 끝나면 그 리스를 놓는다. parallel 이 1 보다 크면
    줄의 것을 그 수만큼 함께 다룬다. 일이 대개 런타임의 답을 기다리는 워커가 쓴다."""

    def __init__(self, lease: Lease, *, owner: str | None, interval: float, parallel: int = 1):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease.ttl.total_seconds())
        self.lease = lease
        self.parallel = parallel
        self._queue: list[T] = []

    # -- 하위 클래스가 채운다 --------------------------------------------------------

    async def queue(self, now: datetime) -> list[T]:
        """이 틱에 볼 것. 차례대로 리스를 걸어 본다."""
        raise NotImplementedError

    async def handle(self, item: T, now: datetime) -> None:
        """리스를 쥔 것 하나의 걸음."""
        raise NotImplementedError

    # -- LeaseWorker ----------------------------------------------------------------

    @classmethod
    def _name(cls, item_id: str) -> str:
        return f"{cls.task_prefix}:{item_id}"

    async def tick(self) -> int:
        self._queue = await self.queue(utcnow())
        if self.parallel <= 1:
            return await super().tick()
        gate = asyncio.Semaphore(self.parallel)

        async def one() -> int:
            async with gate:
                item = await self.acquire()
                if item is None:
                    return 0
                await self.process(item)
                return 1

        return sum(await asyncio.gather(*(one() for _ in range(len(self._queue)))))

    async def acquire(self) -> T | None:
        while self._queue:
            item = self._queue.pop(0)
            if await self.lease.acquire(self._name(item.id)):
                return item
        return None

    async def renew(self, item_id: str) -> bool:
        return await self.lease.renew(self._name(item_id))

    async def process(self, item: T) -> None:
        try:
            async with self.heartbeating(item.id):
                await self.handle(item, utcnow())
        except Exception:  # noqa: BLE001 - 하나의 실패가 다른 것을 막지 않는다
            self.log.exception("%s %s: step failed", self.item_label, item.id)
        finally:
            await self.lease.release(self._name(item.id))


__all__ = ["LeaseWorker", "QueuedLeaseWorker", "default_owner"]
