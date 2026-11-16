"""백그라운드 워커의 공통 수명. 한 바퀴를 부르고 기다리는 루프와 태스크 정리다.

reconcile·감사 수집기·Spec Generator 워커·Knowledge 인덱서가 같은 루프와 정리 코드를 각자 적고
있었다. 앞의 둘은 sleep 도중 취소되는 것에 기댔고 뒤의 둘은 stop 이벤트로 기다렸다. 여기서는 stop
이벤트 하나로 기다린다. start 의 동기·비동기 여부와 태스크 이름, 리스 반납은 워커마다 다르므로 각자
둔다. 블록이 도는 동안 리스를 늘리는 하트비트(`renewing_every`)도 여기 둔다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from daegam.store.base import error_kind

# 하트비트가 리스를 늘리는 간격. 리스 수명에 이 비율을 곱한다
RENEW_EVERY_SHARE = 1 / 3
# 하트비트 간격의 아래 한도(초). 아주 짧은 리스에서도 쉬지 않고 돌지 않게 한다
MIN_RENEW_SECONDS = 0.05


async def run_periodic(
    tick: Callable[[], Awaitable[Any]],
    *,
    interval: float,
    stop: asyncio.Event,
    log: logging.Logger,
    label: str,
    delay_first: bool = False,
) -> None:
    """stop 이 켜질 때까지 tick 을 부르고 interval 초 기다린다. tick 의 예외는 그 종류만 로그로
    남기고(예외의 글에는 문서의 값이 들 수 있다) 루프는 죽지 않는다. 취소는 그대로 올린다.
    delay_first 면 첫 tick 도 interval 뒤다."""
    if delay_first:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    while not stop.is_set():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 루프는 죽지 않는다
            log.error("%s failed: %s", label, error_kind(e))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    """태스크를 취소하고 끝나기를 기다린다. 취소와 태스크 안에서 난 예외는 삼킨다."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@contextlib.asynccontextmanager
async def renewing_every(
    lease_seconds: float, renew: Callable[[], Awaitable[bool]], *, limit: float | None = None
) -> AsyncIterator[None]:
    """블록이 도는 동안 리스 수명의 `RENEW_EVERY_SHARE` 마다 `renew()` 를 부른다. renew 가 False 면
    (리스가 남의 것이 됐다) 멈춘다. limit 초가 지나면 더 늘리지 않는다. 블록의 일이 멈춰도 리스를
    끝없이 쥐지 않게 하려는 것이다(D79). 블록을 나가면 거둔다. renew 는 예외를 올리지 않는다."""
    task = asyncio.create_task(_renew_loop(lease_seconds, renew, limit))
    try:
        yield
    finally:
        await cancel_task(task)


async def _renew_loop(
    lease_seconds: float, renew: Callable[[], Awaitable[bool]], limit: float | None
) -> None:
    period = max(lease_seconds * RENEW_EVERY_SHARE, MIN_RENEW_SECONDS)
    deadline = None if limit is None else time.monotonic() + limit
    while True:
        await asyncio.sleep(period)
        if deadline is not None and time.monotonic() >= deadline:
            return
        if not await renew():
            return
