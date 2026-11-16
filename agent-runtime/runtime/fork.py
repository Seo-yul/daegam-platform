"""다시 생성이 낸 갈래다(P36 5절). 갈래를 정하는 것은 regenerate.py 이고, 여기의 `Fork` 는
turn.py 의 턴 실행이 들고 돌며 턴을 마감할 때 갈래가 생겼는지 본다.

체크포인트는 루트 네임스페이스(`ROOT_NAMESPACE`)의 것만 본다. 서브에이전트는 `tools:<id>` 에 부모
없는 체크포인트를 남기므로 섞이면 갈래의 부모와 head 를 잘못 읽는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from runtime import stream
from runtime.transcript import TurnTranscript

log = logging.getLogger("daegam.agent-runtime.fork")

ROOT_NAMESPACE = ""
"""루트 그래프의 체크포인트 네임스페이스다. 서브에이전트는 `tools:<id>` 에 남긴다."""


async def root_head(saver: BaseCheckpointSaver, session_id: str) -> CheckpointTuple | None:
    """루트 네임스페이스의 가장 최근 체크포인트(head)다. 서브에이전트 체크포인트는 섞이지 않는다."""
    return await saver.aget_tuple(stream.thread_config(session_id, checkpoint_ns=ROOT_NAMESPACE))


def checkpoint_id(config: dict[str, Any] | None) -> str | None:
    """체크포인트 설정이 가리키는 id 다. 설정이 없으면(부모 없는 체크포인트) None 이다."""
    if config is None:
        return None
    return config["configurable"].get("checkpoint_id")


@dataclass(frozen=True)
class Fork:
    """다시 생성 한 번의 갈래다. turn.py 의 턴 실행이 이것을 들고 돈다."""

    session_id: str
    parent_id: str | None
    """갈래를 낼 부모 체크포인트 id 다. None 이면 첫 턴이라 스레드를 지우고 새로 돈다."""
    head_id: str | None
    """리스를 잡은 뒤 읽은 루트 head 의 id 다."""
    replaced_turn_id: str | None
    """버리는 턴의 id 다. 입력 체크포인트에 든 질문의 id(`u-<turn_id>`)에서 읽는다. 옛 세션이면
    None 이다."""
    edited: bool

    def start_fields(self) -> dict[str, Any]:
        """turn_start 에 붙는 칸이다. 값이 None 인 칸은 이벤트에 실리지 않는다."""
        return {"regenerated": True, "edited": True if self.edited else None}

    async def settle(
        self,
        saver: BaseCheckpointSaver,
        turn: stream.TurnState,
        transcript: TurnTranscript,
        *,
        branched: bool = False,
    ) -> str | None:
        """턴을 마감할 때 부른다. 갈래가 생겼으면 turn_end 의 replaced_turn_id 를 돌려준다. 생기기
        전에 실패했으면 전사에서 표식과 이번 턴 항목을 버려 오류 항목만 붙게 하고 None 이다.

        끝까지 돈 턴(done·pending)은 언제나 갈래를 냈다. 오류로 끝난 턴만 루트 head 를 다시 읽어
        시작 때와 견준다. 조각마다의 붙이기가 이미 옮겨 간 것을 봤으면(branched) 읽지 않는다. 그
        항목은 이미 붙었으므로 버릴 수도 없다. 읽지 못하면 갈래가 없는 것으로 본다. 앞 턴이 화면에
        남는 쪽이 체크포인트에 없는 대화를 보이는 쪽보다 낫기 때문이다.
        """
        if turn.error is None or branched or await self.head_moved(saver):
            return self.replaced_turn_id
        transcript.discard()
        return None

    async def head_moved(self, saver: BaseCheckpointSaver) -> bool:
        """루트 head 가 시작 때와 다른가. 갈래가 생겼다는 뜻이다. 첫 턴의 다시 생성은 스레드를
        지우고 시작하므로 처음부터 참이다. 턴이 전사를 붙이기 시작하는 때도 이것으로 정한다
        (turn.py)."""
        try:
            head = await root_head(saver, self.session_id)
        except Exception as e:  # noqa: BLE001  체크포인트 장애가 사용량·turn_end 마감을 막지는 않는다
            log.warning("세션 %s 의 가장 최근 체크포인트를 읽지 못했다: %s", self.session_id, e)
            return False
        return checkpoint_id(head.config if head else None) != self.head_id
