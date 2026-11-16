"""파드 종료의 기다림이다(P78 5절, D92).

턴은 요청과 떨어진 태스크라(turn_task.py) 파드가 내려갈 때 도는 턴을 어떻게 끝낼지 정해야 한다.

- **시계는 SIGTERM 에서 시작한다.** `DrainServer.handle_exit` 가 루프에 `Drain.begin` 을 한 번만
  건다. uvicorn 은 곧바로 소켓을 닫으므로 새 턴은 다른 파드로 간다. `timeout_graceful_shutdown` 은
  drain 에 마감 몫(`SETTLE_SECONDS`)을 더한 것이라 읽기가 열린 동안 uvicorn 이 기다리고, 그래서
  지켜보는 사람은 턴의 done 을 받는다.
- **drain 끝의 타이머**가 남은 턴을 모두 함께 멈춘다(turn_aborted). 그래서 아직 붙어 있는 읽기는
  uvicorn 이 닫기 전에 마지막 이벤트를 받는다. lifespan 본문은 yield 뒤에 그 마감들을 기다린
  다음에야 exit stack(이벤트 보내기, 도구 소스, 위임, 저장소)을 닫는다(`Drain.settle`).
- **env 가 없으면 drain 도 없다.** `DAEGAM_TURN_DRAIN_SECONDS` 는 플랫폼이 provision 마다 렌더한다.
  없으면(이 판 아래로 플랫폼을 되돌린 뒤) SIGTERM 에 모든 턴을 곧바로 멈춰 마감하므로 30초 grace 에
  든다.
- **공유하지 않는 저장소**(memory 노드가 없다)에서는 읽는 사람이 남지 않은 턴을 곧바로 멈춘다.
  결과가 파드와 함께 사라지기 때문이다. SIGTERM 때 이미 떠났으면 그때, 기다리는 중에 떠나면 그때다.
- **인스턴스 토큰이 거절되면**(배포 해제·삭제가 회수했다) 기다리는 턴을 멈춘다. 위임 갱신의 401 과
  이벤트 보내기의 401 이 그 신호다. 둘 다 꺼져 있으면 drain 끝까지 간다.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from types import FrameType
from typing import TYPE_CHECKING, Any

import uvicorn

from runtime.cancel import SHUTDOWN, CancelWatch, notify_all

if TYPE_CHECKING:
    from runtime.state import RuntimeState

log = logging.getLogger("daegam.agent-runtime.drain")

DRAIN_ENV = "DAEGAM_TURN_DRAIN_SECONDS"
# drain 끝에 멈춘 턴이 마감하는 몫이다. 그래프 닫기(stream.GRAPH_CLOSE_SECONDS·감시 정리)와 막이
# 안의 마감 셋(색인, 위임 끝, 전사·리스)이 든다. 플랫폼의 grace 도 이 값을 더한다
SETTLE_SECONDS = 45.0


def drain_seconds(raw: str | None) -> float | None:
    """env 값을 drain 초로 읽는다. 없거나, 숫자가 아니거나, 음수면 None(기다리지 않음)이다."""
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        log.warning("%s=%r 가 숫자가 아니라 기다리지 않는다", DRAIN_ENV, raw)
        return None
    return seconds if seconds >= 0 else None


class Drain:
    """이 파드의 종료 기다림이다. 신호 처리와 lifespan 이 같은 것을 든다."""

    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds
        self.started = False
        self.deadline: float | None = None
        self._state: RuntimeState | None = None

    @property
    def graceful_timeout(self) -> int:
        """uvicorn 이 열린 읽기를 기다리는 상한이다. drain 에 마감 몫을 더한다."""
        return int((self.seconds or 0) + SETTLE_SECONDS)

    def begin(self, state: RuntimeState | None) -> None:
        """종료가 시작됐다. 루프에서 한 번만 돈다. 아직 기동하지 못했으면(저장소를 열지 못했다)
        멈출 턴이 없다."""
        if self.started:
            return
        self.started = True
        loop = asyncio.get_running_loop()
        self.deadline = loop.time() + (self.seconds or 0)
        if state is None:
            return
        self._state = state
        state.draining = True
        state.drain = self
        if not self.seconds:
            log.info("종료: drain 이 없어 진행 중 턴 %d 개를 멈춘다", len(state.cancels))
            self.stop_all()
            return
        log.info("종료: 진행 중 턴 %d 개를 %g초까지 기다린다", len(state.cancels), self.seconds)
        loop.call_later(self.seconds, self.stop_all)
        if state.events.shipper is not None:
            state.events.shipper.on_unauthorized = self.stop_all
        for watch in list(state.cancels.values()):
            self.watch_reader(watch)

    def admit(self, watch: CancelWatch) -> None:
        """종료가 시작된 뒤에 감시를 건 턴이다(SIGTERM 때 아직 입구에 있던 요청). drain 이 없거나
        drain 끝이 이미 지났으면 곧바로 멈추고, 아니면 읽는 사람을 지켜본다. drain 끝의 타이머는
        그때 남은 턴을 모두 멈추므로 이 턴도 든다."""
        if not self.seconds or self._past_deadline():
            watch.notify(SHUTDOWN)
            return
        self.watch_reader(watch)

    def _past_deadline(self) -> bool:
        """drain 끝의 타이머가 이미 돌았는가."""
        return self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline

    def watch_reader(self, watch: CancelWatch) -> None:
        """공유하지 않는 저장소면 읽는 사람이 남지 않은 턴을 멈춘다. 이미 떠났으면 지금, 아니면 떠날
        때다."""
        state = self._state
        reader = watch.reader
        if state is None or state.store.shared or reader is None:
            return
        if reader.closed:
            watch.notify(SHUTDOWN)
            return
        reader.on_close = partial(watch.notify, SHUTDOWN)

    def stop_all(self) -> None:
        """남은 턴을 모두 멈춘다. drain 끝의 타이머와 거절된 인스턴스 토큰이 부른다."""
        if self._state is None:
            return
        if stopped := notify_all(self._state.cancels, SHUTDOWN):
            log.info("종료: 남은 턴 %d 개를 멈췄다", stopped)

    async def settle(self, state: RuntimeState) -> None:
        """lifespan 이 yield 뒤에 부른다. 남은 턴의 마감을 drain 끝과 마감 몫까지 기다린다. 신호
        없이 닫히는 앱(테스트, 직접 종료)도 여기서 종료를 시작한다."""
        pending = [task for task in state.turns if not task.done()]
        if not pending:
            return
        self.begin(state)
        loop = asyncio.get_running_loop()
        assert self.deadline is not None  # begin 이 정했다
        remaining = max(0.0, self.deadline - loop.time()) + SETTLE_SECONDS
        _, left = await asyncio.wait(pending, timeout=remaining)
        if left:
            log.warning("종료: 턴 %d 개가 %g초 안에 마감하지 못했다", len(left), remaining)


class DrainServer(uvicorn.Server):
    """SIGTERM 에서 drain 시계를 거는 uvicorn 서버다. uvicorn 의 `capture_signals` 가 이
    `handle_exit` 를 신호 처리기로 건다."""

    def __init__(self, config: uvicorn.Config, *, drain: Drain, app: Any) -> None:
        super().__init__(config)
        self._drain = drain
        self._app = app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._armed = False

    async def serve(self, sockets: Any = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if not self._armed and self._loop is not None:
            self._armed = True
            self._loop.call_soon_threadsafe(self._begin)
        super().handle_exit(sig, frame)

    def _begin(self) -> None:
        self._drain.begin(getattr(self._app.state, "runtime", None))


__all__ = ["DRAIN_ENV", "SETTLE_SECONDS", "Drain", "DrainServer", "drain_seconds"]
