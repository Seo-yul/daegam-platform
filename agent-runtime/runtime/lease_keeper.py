"""턴 리스의 관리자다(P79 1·2절, D98).

턴, 다시 생성, 재개, 세션 지우기는 리스를 60초(`LEASE_SECONDS`) 잡고, 파드마다 하나인 관리자가
잡은 리스를 모두 10초(`RENEW_SECONDS`)마다 한 바퀴에 토큰으로 늘린다(`SessionIndex.renew_turns`).
그래서 파드가 죽으면 세션은 턴 제한 시간과 상관없이 1분 안에 풀린다.

- **한 번에 한 바퀴.** 바퀴는 제 태스크로 돌고, 그 바퀴에 든 리스의 가장 이른 펜스와 10초 가운데
  이른 때까지만 기다린다. 그때까지 답이 없는 바퀴는 버리고 그 연결을 닫는다(Postgres 는 닫아야
  psycopg 의 기다림이 끝난다). 버린 바퀴는 아무것도 확인하지 않으므로 펜스를 옮기지 않는다. 저장소가
  나중에 적용해도 리스가 길어질 뿐이다. 오류나 버림 뒤에만 5초(`RETRY_SECONDS`) 뒤에 다시 돈다.
- **펜스.** 리스마다 펜스(fence.py)를 두고, 마감은 그 리스를 확인한 마지막 바퀴(처음에는 잡기)를
  보낸 시각에 45초(`FENCE_SECONDS`)를 더한 때다. 저장소는 갱신을 보낸 뒤에야 적용하므로 저장소의
  만료는 펜스보다 적어도 15초 늦다. 펜스 마감이 확인 없이 지나거나, 바퀴가 그 리스를 잃었다고 답하면
  관리자는 펜스를 세우고 다시 늘리지 않으며 턴을 멈추게 한다(`HeldLease.on_lost`).
- **연결.** Postgres 는 풀 밖의 연결 하나로 늘린다. 풀의 8 개를 두고 턴과 다투지 않고 새 primary 에
  곧바로 닿는다. 그 연결은 풀과 같은 전송 시간 제한, 짧은 접속 제한, 세션의 `lock_timeout` 3초를
  갖는다. 잠금이나 죽은 연결에 걸린 바퀴가 일찍 실패해 5초 뒤의 다시 시도가 펜스 전에 닿는다. 오류나
  버림 뒤에는 새로 연다. Redis 와 메모리 색인은 세션 색인 그대로 늘린다(Redis 의 풀은 버린 바퀴의
  연결을 버린다).

관리자는 저장소를 연 뒤에 뜨고 종료 기다림(drain.py)의 마감이 끝난 뒤에 멈춘다. SIGTERM 은 그것을
멈추지 않는다. 펜스를 옮기거나 세우는 것(`Fence.move`·`Fence.lose`)은 이벤트 루프 위에서만 한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from runtime.background import stop_tasks
from runtime.fence import Fence
from runtime.sessions import (
    PostgresSessionIndex,
    SessionIndex,
    SessionStore,
    postgres_pool_kwargs,
)

log = logging.getLogger("daegam.agent-runtime.leases")

LEASE_SECONDS = 60.0
"""잡기와 갱신이 주는 리스의 길이다."""
RENEW_SECONDS = 10.0
"""갱신 바퀴의 간격이다."""
RETRY_SECONDS = 5.0
"""오류로 끝나거나 버린 바퀴 뒤 다음 바퀴까지의 시간이다."""
FENCE_SECONDS = 45.0
"""확인된 마지막 갱신(처음에는 잡기)을 보낸 시각에서 펜스 마감까지다."""
LOCK_TIMEOUT = "3s"
"""관리자 연결의 lock_timeout 이다. 행 잠금에 걸린 바퀴가 다시 시도 전에 실패한다."""
CONNECT_TIMEOUT_SECONDS = 3
"""관리자 연결의 접속 제한이다. tcp_user_timeout 은 접속하는 동안에는 걸리지 않는다."""


class Renewer(Protocol):
    """리스를 늘리는 쪽이다. 저장소 종류마다 하나다(`renewer_for`)."""

    async def renew(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]: ...

    async def abandon(self) -> None:
        """답이 없는 바퀴를 버렸다. 그 연결을 닫는다."""
        ...

    async def close(self) -> None: ...


class IndexRenewer:
    """세션 색인 그대로 늘린다. Redis 와 메모리 색인이다."""

    def __init__(self, index: SessionIndex) -> None:
        self._index = index

    async def renew(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]:
        return await self._index.renew_turns(leases, lease_seconds=lease_seconds)

    async def abandon(self) -> None:
        pass

    async def close(self) -> None:
        pass


class PostgresRenewer:
    """풀 밖의 연결 하나로 늘린다. 오류나 버림 뒤에는 다음 바퀴가 새로 연다."""

    def __init__(self, index: PostgresSessionIndex) -> None:
        self._index = index
        self._conn: Any = None

    async def _connection(self) -> Any:
        """늘릴 연결이다. 연결과 lock_timeout 이 둘 다 된 뒤에야 쥔다."""
        if self._conn is not None and not self._conn.closed:
            return self._conn
        import psycopg

        conn = await psycopg.AsyncConnection.connect(
            self._index.conninfo,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            **postgres_pool_kwargs(fenced=False),
        )
        try:
            await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        return conn

    async def renew(self, leases: Mapping[str, str], *, lease_seconds: float) -> set[str]:
        conn = await self._connection()
        try:
            return await self._index.renew_turns(leases, lease_seconds=lease_seconds, conn=conn)
        except Exception:
            await self.close()
            raise

    async def abandon(self) -> None:
        await self.close()

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception as e:  # noqa: BLE001 - 닫다 실패해도 다음 바퀴가 새로 연다
                log.warning("lease keeper: closing its connection failed: %s", e)


def renewer_for(store: SessionStore) -> Renewer:
    """저장소의 갱신 쪽이다. Postgres 는 풀의 접속 정보로 따로 연결하고 나머지는 색인 그대로다."""
    if isinstance(store.index, PostgresSessionIndex):
        return PostgresRenewer(store.index)
    return IndexRenewer(store.index)


@dataclass(eq=False)
class HeldLease:
    """이 파드가 쥔 리스 하나다. 잡은 곳(turn_gate.py `held_lease`)이 받아 턴이나 지우기에
    넘긴다."""

    session_id: str
    token: str
    fence: Fence
    # 리스를 잃었을 때 부른다. 턴은 제 중지 감시를 멈추게 한다(turn.py)
    on_lost: Callable[[], None] | None = None
    # 턴 태스크가 넘겨받았다. 그러면 잡은 블록이 끝나도 관리자가 계속 늘리고, 턴의 마감이 놓는다
    kept: bool = field(default=False)


class LeaseKeeper:
    """파드의 리스 관리자다. 시간 값은 시험이 줄여 쓴다."""

    def __init__(
        self,
        renewer: Renewer,
        *,
        lease_seconds: float = LEASE_SECONDS,
        interval: float = RENEW_SECONDS,
        retry: float = RETRY_SECONDS,
        fence_seconds: float = FENCE_SECONDS,
    ) -> None:
        self.lease_seconds = lease_seconds
        self._renewer = renewer
        self._interval = interval
        self._retry = retry
        self._fence_seconds = fence_seconds
        # 세션 id 로 찾는 이 파드의 리스다. 한 세션의 리스는 하나다
        self._leases: dict[str, HeldLease] = {}
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def hold(self, session_id: str, token: str, *, sent_at: float) -> HeldLease:
        """잡은 리스를 맡긴다. sent_at 은 잡기를 보낸 루프 시각이고 첫 펜스 마감은 그로부터 잰다.
        같은 세션의 앞 리스가 남아 있으면 저장소가 이미 다른 토큰에 넘긴 것이라 잃은 것이다."""
        previous = self._leases.get(session_id)
        if previous is not None:
            self.lose(previous)
        # 펜스를 처음 잃는 곳이 관리자든 거절된 쓰기(fence.refused)든 같은 마무리를 한다
        fence = Fence(sent_at + self._fence_seconds, on_lose=lambda: self._forget(lease))
        lease = HeldLease(session_id, token, fence)
        self._leases[session_id] = lease
        self._changed.set()
        return lease

    def drop(self, lease: HeldLease) -> None:
        """리스를 더 늘리지 않는다. 풀었거나 지운 세션의 것이다. 여러 번 불러도 같다."""
        if self._leases.get(lease.session_id) is lease:
            del self._leases[lease.session_id]

    def lose(self, lease: HeldLease) -> None:
        """리스를 잃었다. 펜스를 세우면 그 펜스가 `_forget` 을 부른다. 두 번째부터는 아무것도 하지
        않는다."""
        self.drop(lease)
        lease.fence.lose()

    def _forget(self, lease: HeldLease) -> None:
        """펜스를 처음 잃었을 때다. 다시 늘리지 않고 턴을 멈추게 한다."""
        self.drop(lease)
        if lease.on_lost is not None:
            try:
                lease.on_lost()
            except Exception:  # noqa: BLE001 - 턴 쪽 실패가 관리자를 멈추지 않는다
                log.exception(
                    "lease keeper: stopping the turn of session %s failed", lease.session_id
                )

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="lease-keeper")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            await stop_tasks([task])
        await self._renewer.close()

    async def _run(self) -> None:
        """관리자가 멈추면 모든 턴이 잡은 지 45초에 펜스에 막히므로, 한 걸음이 실패해도 남기고
        이어 간다."""
        loop = asyncio.get_running_loop()
        next_round = loop.time() + self._interval
        while True:
            try:
                next_round = await self._step(next_round)
            except Exception:  # noqa: BLE001 - 다음 걸음이 다시 늘린다
                log.exception("lease keeper: a step failed")
                next_round = loop.time() + self._retry
                # 기다리기 전에 실패하는 걸음이 루프를 붙잡지 않게 한 번 양보한다
                await asyncio.sleep(0)

    async def _step(self, next_round: float) -> float:
        """다음 바퀴나 가장 이른 펜스 마감까지 기다렸다가, 바퀴 때가 되었으면 돈다. 다음 바퀴의
        시각을 돌려준다."""
        loop = asyncio.get_running_loop()
        self._lose_past_fences()
        wake_at = min([next_round, *(lease.fence.deadline for lease in self._leases.values())])
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), max(0.0, wake_at - loop.time()))
            return next_round  # 새 리스가 들어왔다. 깰 때를 다시 정한다
        except TimeoutError:
            pass
        if loop.time() < next_round:
            return next_round  # 펜스 마감이 먼저 왔다. 다음 걸음이 그 리스를 잃는다
        started = loop.time()
        ok = await self.round()
        return started + self._interval if ok else loop.time() + self._retry

    def _lose_past_fences(self) -> None:
        for lease in list(self._leases.values()):
            if lease.fence.standing:
                log.warning("lease keeper: session %s lease passed its fence", lease.session_id)
                self.lose(lease)

    async def round(self) -> bool:
        """맡은 리스를 한 바퀴에 늘린다. 답했으면(잃은 리스가 있어도) 참, 오류나 버림이면 거짓이다.
        확인된 리스의 펜스는 이 바퀴를 보낸 시각부터 다시 잰다. 이미 펜스가 선 리스는 늘리지 않고
        잃는다."""
        self._lose_past_fences()
        leases = list(self._leases.values())
        if not leases:
            return True
        loop = asyncio.get_running_loop()
        sent = loop.time()
        bound = min(min(lease.fence.deadline for lease in leases), sent + self._interval)
        task = asyncio.create_task(
            self._renewer.renew(
                {lease.session_id: lease.token for lease in leases},
                lease_seconds=self.lease_seconds,
            )
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.0, bound - sent))
        finally:
            # 관리자가 멈추는 중이면 여기서 끊긴다. 남은 갱신은 취소하고 그 끝을 받아 둔다
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume)
        if not done:
            log.warning("lease keeper: a round of %d leases got no answer in time", len(leases))
            await self._renewer.abandon()
            return False
        if task.cancelled():
            log.warning("lease keeper: a round of %d leases was cancelled", len(leases))
            return False
        try:
            held = task.result()
        except Exception as e:  # noqa: BLE001 - 다음 바퀴가 다시 늘린다. 펜스가 마지막 그물이다
            log.warning("lease keeper: a round of %d leases failed: %s", len(leases), e)
            return False
        for lease in leases:
            if self._leases.get(lease.session_id) is not lease:
                continue  # 그 사이에 놓았다
            if lease.session_id in held:
                lease.fence.move(sent + self._fence_seconds)
            else:
                log.warning("lease keeper: session %s lease is gone", lease.session_id)
                self.lose(lease)
        return True


def _consume(task: asyncio.Task[Any]) -> None:
    """버린 바퀴의 끝을 받아 둔다. 꺼내지 않은 예외가 로그를 어지럽히지 않게 한다."""
    if not task.cancelled():
        task.exception()


__all__ = [
    "FENCE_SECONDS",
    "LEASE_SECONDS",
    "RENEW_SECONDS",
    "RETRY_SECONDS",
    "HeldLease",
    "IndexRenewer",
    "LeaseKeeper",
    "PostgresRenewer",
    "Renewer",
    "renewer_for",
]
