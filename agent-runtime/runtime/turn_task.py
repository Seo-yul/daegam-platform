"""요청과 떼어 낸 턴 태스크와 그 이벤트 큐다(P78 1절, D92).

턴은 `open_turn` 이 띄운 태스크 하나가 끝까지 돌리고, HTTP 응답은 그 태스크의 이벤트 큐를 읽기만
한다. 응답이 어떤 길로 끝나든(클라이언트가 떠남, 프록시의 유휴 시간, 플랫폼의 읽기 시간) 읽기만
멈추고 큐가 닫히며, 태스크는 그대로 가서 그 뒤의 이벤트를 버린다. 턴을 멈추는 길은 중지
요청(cancel.py), 접근을 잃은 사람(turn_grants.py), 파드 종료(drain.py)뿐이다.

큐는 크기 제한이 없다. 닫힌 것을 알아채지 못한 읽기(k3d 실측)가 턴을 붙잡지 못하게 하려는 것이고,
비서의 `EventQueue` 와 같은 꼴이다. 큐가 조용하면 읽기는 15초마다 `: keepalive` 주석을 내어
Traefik·앞단 프록시·플랫폼의 읽기 시간이 오래 말 없는 모델 호출 도중에 연결을 닫지 않게 한다.

태스크는 `RuntimeState.turns` 가 들어 GC 되지 않게 하고, 끝나면 뺀다. 예상하지 못한 예외는 끝날 때
로그로 남긴다. 마감(색인·전사·위임·리스)은 `_run_turn` 이 스스로 하므로 여기서는 하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger("daegam.agent-runtime.turn_task")

# 큐가 이만큼 조용하면 읽기가 SSE 주석을 낸다
KEEPALIVE_SECONDS = 15.0
KEEPALIVE = ": keepalive\n\n"


class TurnEvents:
    """턴 태스크 → SSE 응답. 응답이 끝나면 닫히고 그 뒤의 이벤트는 버린다. None 이 끝 표시다.

    `on_close` 는 읽기가 떠날 때 한 번 불린다. 공유하지 않는 저장소에서 종료 중이면 아무도 읽지
    않는 턴을 곧바로 멈추는 데 쓴다(drain.py)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.on_close: Callable[[], None] | None = None

    def put(self, chunk: str | None) -> None:
        if not self.closed:
            self._queue.put_nowait(chunk)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.on_close is not None:
            self.on_close()

    def __aiter__(self) -> TurnEvents:
        """응답의 본문이다. 끝 표시를 받거나 응답이 닫히면(`aclose`) 큐를 닫는다. 생성기가 아니라
        한 번도 읽기 전에 닫혀도 닫힌 것을 안다."""
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        try:
            chunk = await asyncio.wait_for(self._queue.get(), timeout=KEEPALIVE_SECONDS)
        except TimeoutError:
            return KEEPALIVE
        if chunk is None:
            self.close()
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        self.close()


def start_turn_task(
    turns: set[asyncio.Task[Any]], body: AsyncIterator[str], *, session_id: str
) -> TurnEvents:
    """body(`_run_turn` 의 SSE 생성기)를 태스크로 돌리고 그 이벤트 큐를 돌려준다. 태스크는 turns 에
    들었다가 끝나면 빠진다."""
    events = TurnEvents()

    async def run() -> None:
        try:
            async for chunk in body:
                events.put(chunk)
        finally:
            events.put(None)

    task = asyncio.get_running_loop().create_task(run(), name=f"turn:{session_id}")
    turns.add(task)
    task.add_done_callback(lambda done: _forget(turns, done))
    return events


def _forget(turns: set[asyncio.Task[Any]], task: asyncio.Task[Any]) -> None:
    turns.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error("턴 태스크 %s 가 예외로 끝났다", task.get_name(), exc_info=error)


__all__ = ["KEEPALIVE", "KEEPALIVE_SECONDS", "TurnEvents", "start_turn_task"]
