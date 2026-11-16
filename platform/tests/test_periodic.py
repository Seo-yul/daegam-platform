"""백그라운드 워커의 공통 수명 (services/periodic.py)."""

from __future__ import annotations

import asyncio
import logging

from daegam.services.periodic import cancel_task, run_periodic

log = logging.getLogger("daegam.test.periodic")


def test_a_failing_tick_is_logged_and_the_loop_goes_on(caplog):
    calls = 0

    async def main() -> None:
        stop = asyncio.Event()

        async def tick() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            stop.set()

        await asyncio.wait_for(
            run_periodic(tick, interval=0.01, stop=stop, log=log, label="demo tick"), timeout=5
        )

    with caplog.at_level(logging.ERROR, logger=log.name):
        asyncio.run(main())
    assert calls == 2
    # 예외의 글이 아니라 종류만 남는다
    assert any(r.getMessage() == "demo tick failed: RuntimeError" for r in caplog.records)


def test_stop_ends_the_wait_without_sleeping_out_the_interval():
    async def main() -> float:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def tick() -> None:
            return None

        task = asyncio.create_task(
            run_periodic(tick, interval=60, stop=stop, log=log, label="idle")
        )
        await asyncio.sleep(0.01)
        started = loop.time()
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return loop.time() - started

    assert asyncio.run(main()) < 5


def test_cancel_task_swallows_cancellation_and_task_errors():
    async def main() -> None:
        await cancel_task(None)
        sleeper = asyncio.create_task(asyncio.sleep(60))
        await cancel_task(sleeper)
        assert sleeper.cancelled()

        async def broken() -> None:
            raise ValueError("already dead")

        dead = asyncio.create_task(broken())
        await asyncio.sleep(0)
        await cancel_task(dead)

    asyncio.run(main())
