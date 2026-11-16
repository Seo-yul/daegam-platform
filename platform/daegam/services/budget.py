"""리컨실 틱 안 단계들이 함께 쓰는 시간 몫과 이어 돌 자리 (P67 2절).

몫은 일을 고르게 나눌 뿐 리스를 살리지 않는다. 리스는 틱이 항목마다 늘린다(`claims.TickHold`).
몫을 넘긴 단계는 끝낸 마지막 항목의 자리를 두고 다음 틱이 그 뒤부터 잇는다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, Sequence
from typing import Any


class Budget:
    """단계 하나가 한 틱에 쓰는 시간. 단조 시계로 잰다."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds

    def restart(self) -> None:
        """지금부터 다시 잰다. 몫 밖의 준비를 마친 뒤 시계를 거는 단계가 쓴다."""
        self.deadline = time.monotonic() + self.seconds

    def out_of_time(self) -> bool:
        return time.monotonic() >= self.deadline


def resumed[T](
    items: Sequence[T], after: Hashable | None, key: Callable[[T], Any], *, wrap: bool
) -> list[T]:
    """지난 틱이 몫을 넘겨 멈춘 자리(after) 다음부터의 차례. key 의 순서로 줄을 세운다.

    wrap 이면 그 앞의 항목을 뒤에 붙여 한 바퀴를 돈다. 틱마다 모두를 보는 단계(공유 복제본)다.
    wrap 이 아니면 그 뒤의 항목만이다. 여러 틱에 걸쳐 한 번 끝까지 도는 단계(서비스 토큰 스윕, 한
    시간에 한 번)다. 앞의 항목은 이미 이번 시간에 보았다."""
    ordered = sorted(items, key=key)
    if after is None:
        return ordered
    later = [i for i in ordered if key(i) > after]
    return later + [i for i in ordered if key(i) <= after] if wrap else later


__all__ = ["Budget", "resumed"]
