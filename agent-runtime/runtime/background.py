"""파드의 배경 태스크를 멈추는 도우미다. 기동(boot.py)의 작업과 턴 리스 관리자(lease_keeper.py)가
같이 쓴다."""

from __future__ import annotations

import asyncio
from typing import Any


async def stop_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """백그라운드 작업을 멈추고 끝날 때까지 기다린다. 닫힌 자원을 쓰지 않게 가장 먼저 부른다."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 종료 중 예외는 삼킨다
            pass
