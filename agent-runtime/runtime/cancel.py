"""진행 중 턴을 멈춘다(D65, D92, P35 4절, P78 1절).

중지는 공유 색인의 표시로 전한다. 턴을 도는 파드가 요청을 받은 파드와 다를 수 있기 때문이다. 턴은
리스를 잡을 때 무작위 턴 토큰을 함께 적고(`sessions.new_turn_token`), 중지는 살아 있는 리스의 토큰을
표시에 옮겨 적으며(`SessionIndex.request_cancel`), 턴은 제 토큰과 같은 표시만 따른다. 그래서 늦게 온
중지가 다음 턴을 멈추지 않고 잡기와 지우기 사이의 경주가 없다.

턴은 `CancelWatch` 를 들고 돈다. 같은 파드로 들어온 요청은 `notify` 로 바로 알리고, 아니면
`Settings.cancel_poll_interval`(기본 CANCEL_POLL_SECONDS)마다 표시를 읽는다. 알림이 오면
`stream_turn` 의 턴 제한 시간(`asyncio.timeout`)을 지금으로 당겨 그래프 실행만 멈추고, `stream_turn`
은 `reason` 을 보고 제한 시간 초과가 아니라 그 까닭의 코드로 옮긴다. 제한 시간이 이미 지난
뒤(그래프를 닫는 중)에 온 중지는 당기지 않고 까닭도 적지 않는다. 그 턴은 제한 시간 초과로 끝난다.

멈추는 까닭은 넷이다. 중지 요청(turn_cancelled, 화면의 중단), 갱신이 거절된
사람(turn_access_lost, turn_grants.py), 파드 종료(turn_aborted, drain.py), 잃은 턴
리스(turn_aborted, lease_keeper.py). 연결이 끊긴 것은 까닭이 아니다(D92). 마감은 오류로 끝난 턴과
같은 길이고, 리스를 잃은 턴만 세션에 더 쓰지 않는다(turn.py).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from runtime.sessions import SessionIndex

if TYPE_CHECKING:
    from runtime.turn_task import TurnEvents

log = logging.getLogger("daegam.agent-runtime.cancel")

CANCEL_POLL_SECONDS = 1.0
"""다른 파드가 적은 표시를 읽는 기본 간격이다(`Settings.cancel_poll_interval`). Postgres 면 도는
턴마다 초당 쿼리 하나다."""
POLL_CLOSE_SECONDS = 5.0
"""그래프가 끝난 뒤 폴링 태스크가 멈추기를 기다리는 상한이다. 색인 질의 하나가 끝날 시간이다."""
# 멈춘 턴의 오류 코드다. error 이벤트의 code 이자 turn_end 의 status 다(stream.py 가 다시 낸다).
# turn_cancelled 와 turn_access_lost 는 turn_end 에서 cancelled 로 옮겨 적어 오류율에 들지 않고,
# turn_aborted 는 그대로 적어 오류율에 든다(turn.py)
TURN_CANCELLED = "turn_cancelled"
TURN_ACCESS_LOST = "turn_access_lost"
TURN_ABORTED = "turn_aborted"
REQUESTED_DETAIL = "요청으로 턴을 멈췄다"
"""요청으로 멈춘 턴의 error 이벤트 문장이다."""


@dataclass(frozen=True)
class StopReason:
    """턴을 멈춘 까닭이다. code 와 detail 이 멈춘 턴의 error 이벤트가 된다."""

    code: str
    detail: str


STOP_REQUESTED = StopReason(TURN_CANCELLED, REQUESTED_DETAIL)
ACCESS_LOST = StopReason(
    TURN_ACCESS_LOST, "대화를 시작한 사람의 접근 권한을 다시 확인하지 못해 턴을 멈췄다"
)
SHUTDOWN = StopReason(TURN_ABORTED, "런타임이 내려가 턴을 끝까지 잇지 못했다")
# 리스를 잃은 턴이다(P79 2절, lease_keeper.py). 코드와 글은 파드 종료와 같다
LEASE_LOST = StopReason(TURN_ABORTED, SHUTDOWN.detail)


class CancelWatch:
    """한 턴의 중지 감시다. `_start_turn` 이 리스를 잡은 뒤 만들고 `stream_turn` 이 든다."""

    def __init__(
        self,
        index: SessionIndex,
        session_id: str,
        token: str,
        *,
        poll_seconds: float,
    ) -> None:
        self.session_id = session_id
        self.token = token
        self._index = index
        self._poll_seconds = poll_seconds
        self._reason: StopReason | None = None
        # 이 턴의 제한 시간이다. 당기는 것은 그래프가 도는 동안(_armed)뿐이고, 만료를 보려고
        # 그 뒤에도 든다
        self._deadline: asyncio.Timeout | None = None
        self._armed = False
        # 이 턴의 이벤트를 읽는 응답이다(turn_task.py). 파드 종료가 읽는 사람이 남았는지 본다
        self.reader: TurnEvents | None = None

    @property
    def requested(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> StopReason | None:
        """턴을 멈춘 까닭이다. 아직 멈추라는 말이 없었으면 None 이다."""
        return self._reason

    def notify(self, reason: StopReason = STOP_REQUESTED) -> None:
        """멈추라는 말이 왔다. 같은 파드의 중지 요청과 폴링, 위임 갱신의 거절, 파드 종료가 부른다.
        그래프가 돌고 있으면 제한 시간을 지금으로 당긴다. 제한 시간이 이미 지났으면 아무것도 하지
        않는다. 만료 중인 제한 시간은 당길 수 없고 그 턴은 멈춘 것이 아니다. 먼저 온 까닭이
        남는다."""
        if self._deadline is not None and self._deadline.expired():
            return
        if self._reason is None:
            self._reason = reason
        self._trip()

    def _trip(self) -> None:
        if self._armed and self._deadline is not None:
            self._deadline.reschedule(asyncio.get_running_loop().time())

    async def _poll(self) -> None:
        while not self.requested:
            await asyncio.sleep(self._poll_seconds)
            try:
                flagged = await self._index.cancel_requested(self.session_id, self.token)
            except Exception as e:  # noqa: BLE001  색인 장애가 턴을 멈추지는 않는다. 다음 주기에 다시 읽는다
                log.warning("세션 %s 중지 표시 읽기 실패: %s", self.session_id, e)
                continue
            if flagged:
                self.notify()

    @asynccontextmanager
    async def armed(self, deadline: asyncio.Timeout) -> AsyncIterator[None]:
        """그래프가 도는 동안 표시를 지켜본다. `deadline` 은 들어와 있는 `stream_turn` 의 턴 제한
        시간이다. 그래프가 뜨기 전에 온 중지는 여기서 곧바로 당긴다."""
        self._deadline, self._armed = deadline, True
        if self.requested:
            self._trip()
        task = asyncio.create_task(self._poll())
        try:
            yield
        finally:
            self._armed = False
            task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task}, timeout=POLL_CLOSE_SECONDS)


def notify_all(cancels: Mapping[str, CancelWatch], reason: StopReason = STOP_REQUESTED) -> int:
    """이 파드가 돌리는 턴을 모두 멈추라고 알리고 그 수를 돌려준다. 인스턴스의 턴을 모두 멈추는
    요청과 파드 종료(drain.py)가 부른다."""
    watches = list(cancels.values())
    for watch in watches:
        watch.notify(reason)
    return len(watches)


def arm(watch: CancelWatch | None, deadline: asyncio.Timeout) -> AbstractAsyncContextManager[None]:
    """`stream_turn` 이 그래프를 돌리는 동안 감시를 건다. 감시가 없으면(직접 부른 `stream_turn`)
    아무것도 지켜보지 않는다."""
    return watch.armed(deadline) if watch is not None else nullcontext()
