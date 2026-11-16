"""끊긴 턴의 되살리기다(P79 3절, D98).

턴을 돌던 파드가 죽거나 리스를 잃은 턴은 리스를 풀지 못하고 끝나 세션에 열린 표시가 남는다
(Postgres 는 `turn_token`, Redis 는 해시 칸 `turn_open`, sessions.py). 리스가 살아 있지 않은데 그
표시가 남은 세션 한 줄은 그 토큰을 `orphan` 으로 들고, 그 세션은 새 턴을 잡지 못한다. 되살리기만
그 표시를 지운다.

- **언제.** 세션을 읽는 모든 라우트가 검사 전에 부르는 `turn_gate.load_session`, 파드마다 도는
  sweep(sessions.py `sweep_loop`), 지우기의 잡기(`claim_delete`)다. 잡기가 실패한 요청도 세션을 다시
  읽어 되살린 뒤 409 를 낸다(turn_gate.py `held_lease`).
- **얼마나.** 파드 하나가 한 번에 둘까지만 되살린다(`REPAIR_SLOTS`). 두 자리가 다 찼거나 그 세션을
  이 파드가 이미 되살리는 중인 읽기는 고치지 않은 한 줄을 그대로 돌려주고(running 은 이미
  거짓이다) 다음 읽기가 되살린다. 실패한 세션은 그 파드에서 10초(`REPAIR_BACKOFF_SECONDS`) 동안
  읽기가 건드리지 않는다. 화면이 폴링하는 실패하는 세션 하나가 자리를 붙잡지 않게 하려는 것이다.
  sweep 은 자리를 기다리고 그 시간을 보지 않는다.
- **읽지 못할 때.** 저장소 오류가 아닌 실패(읽을 수 없는 전사 항목, 첨부 읽기, 결정의 결함)나 저장소
  오류의 연이은 `REPAIR_FALLBACK_AFTER` 번째 실패 뒤에는 체크포인트도 전사도 읽지 않는 최소
  되살리기를 한다. 표시를 지우고 상태를 두며 줄은 붙이지 않고, 실행 표식이 있으면 turn_end 는 낸다.
  세션이 읽을 수 없는 것 때문에 막히지 않게 하려는 것이다.
- **차례.** 체크포인트를 먼저 잠금 없이 읽는다(`read_checkpoint`). 닫힌 세션은 읽지 않고 상태를
  둔다.
  저장소 오류(연결, 시간 초과)면 그만두고 표시를 다음 읽기에 남긴다. 저장소가 답했는데 상태를
  만들지 못하면 상태를 그대로 둔다. 그 뒤 색인이 Postgres 는 행 잠금 아래 한 트랜잭션, Redis 는
  스크립트 하나로 고친다(`SessionIndex.repair_turn`). 붙일 항목은 그 안에서 읽은 전사로 정한다
  (`repair_items`). 쓴 쪽만, 잠금 아래 읽은 실행 표식이 있을 때만 `turn_end{status: turn_aborted,
  repaired: true}` 를 낸다(`emit_repaired_end`). 두 파드가 함께 되살려도 줄은 하나, turn_end 도
  하나다.

P79 앞에 막힌 세션(열린 표시 없이 색인 상태가 체크포인트와 어긋난 세션)은 체크포인트를 이미 읽는
라우트(전체 읽기, 보류 읽기, 결정)가 `align_with_checkpoint` 로 상태를 맞춘다. 닫힌 세션에는 쓰지
않는다.

되살리기와 상태 맞추기는 어느 턴의 것도 아니라 펜스 문맥 밖에서 돈다(fence.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import psycopg
from langchain_core.messages import HumanMessage
from redis import exceptions as redis_errors

from runtime import events as ev
from runtime.cancel import LEASE_LOST
from runtime.fence import fenced
from runtime.sessions import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PENDING, SessionRecord, utcnow
from runtime.stream import snapshot_messages, thread_config
from runtime.transcript import KIND_DONE, KIND_ERROR, error_item, user_item
from runtime.turn_record import RUN_CHAT, RunMarker, human_message_id
from runtime.uploads import TurnUploads, message_refs

if TYPE_CHECKING:
    from runtime.events import EventLog
    from runtime.state import RuntimeState

log = logging.getLogger("daegam.agent-runtime.repair")

REPAIR_SLOTS = 2
"""파드 하나가 한 번에 돌리는 되살리기 수다. 풀의 연결 8 개 가운데 둘까지만 쓴다."""
REPAIR_BACKOFF_SECONDS = 10.0
"""되살리기가 실패한 세션을 그 파드의 읽기가 다시 건드리지 않는 시간이다."""
REPAIR_FALLBACK_AFTER = 3
"""저장소 오류로 연이어 실패한 이 횟수째에 최소 되살리기로 넘어간다. 저장소 오류가 아닌 실패는
처음부터 넘어간다."""

# 저장소에 닿지 못한 오류다. 체크포인트 읽기가 이것이면 되살리기를 그만두고 다음 읽기에 맡긴다.
# psycopg 의 OperationalError 는 끊긴 연결, 풀의 시간 초과, 잠금 기다림 초과를 함께 든다
STORE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    psycopg.OperationalError,
    redis_errors.ConnectionError,
    redis_errors.TimeoutError,
)


def store_failure(error: BaseException) -> bool:
    """예외나 그 원인 사슬에 저장소에 닿지 못한 오류가 있는가. 체크포인터가 감싼 오류도 본다."""
    seen: set[int] = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, STORE_ERRORS):
            return True
        stack.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False


@dataclass(frozen=True)
class CheckpointView:
    """되살리기가 체크포인트에서 읽은 것이다. status 가 None 이면 상태를 그대로 둔다(닫힌 세션,
    읽을 수 없는 체크포인트). 마지막 사람 메시지의 id, 친 글과 첨부 요약은 새 말의 사용자 항목을
    다시 세울 때 쓴다."""

    status: str | None = None
    interrupted: bool = False
    human_id: str | None = None
    text: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


KEEP = CheckpointView()


async def read_checkpoint(state: RuntimeState, record: SessionRecord) -> CheckpointView:
    """되살리기의 1단계다. 체크포인터의 잠금과 제 풀 연결로 읽고 행을 잠그지 않는다. 리스가 끝난
    뒤에는 아무도 그 스레드에 쓰지 않으므로 잠금 없이 정해도 된다. 저장소 오류는 올린다."""
    if record.status == STATUS_CLOSED:
        return KEEP
    try:
        snapshot = await state.agent.graph.aget_state(thread_config(record.session_id))
    except Exception as e:
        if store_failure(e):
            raise
        log.warning("세션 %s 체크포인트를 만들지 못해 상태를 그대로 둔다: %s", record.session_id, e)
        return KEEP
    interrupted = bool(snapshot.interrupts)
    humans = [m for m in snapshot_messages(snapshot) if isinstance(m, HumanMessage)]
    view = CheckpointView(
        status=STATUS_PENDING if interrupted else STATUS_ACTIVE, interrupted=interrupted
    )
    if not humans:
        return view
    last = humans[-1]
    return CheckpointView(
        status=view.status,
        interrupted=interrupted,
        human_id=last.id,
        text=_typed_text(last),
        attachments=await _attachment_summaries(state, record, last),
    )


def _typed_text(message: HumanMessage) -> str:
    """사용자가 친 글이다(attachments.py `typed_text`). attachments 가 state 와 turn_gate 를
    가져오고 turn_gate 가 이 모듈을 가져오므로 여기서 가져온다."""
    from runtime.attachments import typed_text

    return typed_text(message)


async def _attachment_summaries(
    state: RuntimeState, record: SessionRecord, message: HumanMessage
) -> list[dict[str, Any]]:
    """사람 메시지가 단 첨부의 요약이다. 턴이 사용자 항목에 싣는 것과 같다. 첨부를 저장한 적 없는
    세션은 저장소를 읽지 않는다."""
    refs = message_refs(message)
    if not refs or not record.upload_bytes:
        return []
    files = await state.store.uploads.read(record.session_id)
    return [upload.summary() for upload in TurnUploads(files=files).select(refs)]


def repair_items(
    view: CheckpointView,
    record: SessionRecord,
    marker: RunMarker | None,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """되살리기의 3단계다. 잠근 한 줄(record), 그 토큰의 실행 표식, 저장된 전사로 붙일 항목을
    정한다. 정하는 시도는 색인의 turn_id 가 가리키는 가장 최근 시도다.

    시도는 전사에 그 실행의 trace_id(표식이 없으면 색인의 turn_id)를 단 done 이나 error 가 있으면
    끝난 것이다. 한 턴의 재개 여럿은 turn_id 를 나눠 쓰므로 trace_id 로 가른다. 표식의 turn_id 가
    색인의 것과 다르면 표식 뒤에 옛 파드가 표식 없이 시도를 적은 것이라(5절) 그 시도를 turn_id 로
    본다. 끝나지 않았고, 체크포인트에 인터럽트가 없고, 색인에 turn_id 가 있으면 `turn_aborted` 줄을
    붙인다. 새 말의 시도인데 그 turn_id 를 단 항목이 하나도 없고(첫 조각 끝 전에 죽어 사용자 항목도
    저장되지 않았다) 체크포인트의 마지막 사람 메시지가 그 시도의 것(`u-<turn_id>`)이면 그 메시지로
    사용자 항목을 다시 세워 줄 앞에 둔다. 재개와 다시 생성은 다시 세우지 않는다. 재개의 사용자
    항목은 P79 앞의 것이라 turn_id 가 없을 수 있어 질문이 두 번 보일 수 있고, 다시 생성은 되감기
    표식 없이 세우면 앞 답과 질문이 다시 보인다."""
    turn_id = record.turn_id
    if turn_id is None or view.interrupted:
        return []
    run = marker if marker is not None and _current(marker, record) else None
    key, value = ("trace_id", run.trace_id) if run is not None else ("turn_id", turn_id)
    if any(
        item.get("kind") in (KIND_DONE, KIND_ERROR) and item.get(key) == value for item in items
    ):
        return []
    added: list[dict[str, Any]] = []
    if (
        run is not None
        and run.kind == RUN_CHAT
        and view.text is not None
        and view.human_id == human_message_id(turn_id)
        and not any(item.get("turn_id") == turn_id for item in items)
    ):
        added.append(user_item(view.text, view.attachments, turn_id=turn_id))
    added.append(
        error_item(
            {
                "turn_id": turn_id,
                "trace_id": run.trace_id if run is not None else None,
                "code": LEASE_LOST.code,
                "message": LEASE_LOST.detail,
            }
        )
    )
    return added


def _current(marker: RunMarker, record: SessionRecord) -> bool:
    """표식이 색인의 가장 최근 시도의 것인가. turn_id 가 다르거나 표식 뒤에 마감이 last_turn_at 을
    옮겼으면 옛 파드가 표식 없이 그 뒤의 시도를 적은 것이다(같은 턴의 재개는 turn_id 가 같다)."""
    return marker.turn_id == record.turn_id and record.last_turn_at <= marker.start


def emit_repaired_end(events: EventLog, record: SessionRecord, marker: RunMarker) -> None:
    """되살린 실행의 turn_end 다(P79 3절 5단계). 표식의 시도, 트레이스 칸, 시작 시각을 들고 길이는
    싣지 않아 지연 표본에 들지 않는다. 본문도 없다. 프로필 판은 되살린 파드의 것이다."""
    events.emit(
        ev.TURN_END,
        session_id=record.session_id,
        user_sub=record.user_sub,
        run_id=record.run_id,
        **marker.end_fields(),
        status=LEASE_LOST.code,
        code=LEASE_LOST.code,
        error=LEASE_LOST.detail,
        repaired=True,
    )


class TurnRepairs:
    """파드의 되살리기 자리와 실패한 세션의 쉬는 시간이다. 시간 값은 시험이 줄여 쓴다."""

    def __init__(
        self, *, slots: int = REPAIR_SLOTS, backoff: float = REPAIR_BACKOFF_SECONDS
    ) -> None:
        self._slots = asyncio.Semaphore(slots)
        self._backoff = backoff
        # 세션 id 와 읽기가 다시 건드려도 되는 때(time.monotonic)다
        self._resting: dict[str, float] = {}
        # 이 파드가 지금 되살리는 세션이다. 한 세션이 두 자리를 다 붙잡지 않게 한다
        self._repairing: set[str] = set()
        # 세션 id 와 연이은 실패 수다
        self._failures: dict[str, int] = {}

    async def on_read(self, state: RuntimeState, record: SessionRecord) -> SessionRecord | None:
        """세션 한 줄을 읽은 쪽이 부른다. 끊긴 표시가 없거나, 쉬는 중이거나, 자리가 다 찼으면 그
        한 줄을 그대로 돌려준다. 되살렸으면 고친 한 줄이고, 그 사이에 세션이 지워졌으면
        None 이다."""
        if record.orphan is None or record.session_id in self._repairing:
            return record
        resting = self._resting.get(record.session_id)
        if resting is not None and time.monotonic() < resting:
            return record
        if self._slots.locked():
            return record
        async with self._slots:
            return await self._repair(state, record)

    async def sweep(self, state: RuntimeState) -> int:
        """이 인스턴스의 끊긴 표시를 모두 한 번씩 되살린다. 자리를 기다리고 쉬는 시간을 보지 않는다.
        sweep 마다 파드마다 돌므로 아무도 다시 열지 않는 세션도 줄과 turn_end 를 얻는다. 되살린
        수다."""
        index = state.store.index
        repaired = 0
        for session_id in await index.orphans():
            record = await index.get(session_id)
            if record is None or record.orphan is None or session_id in self._repairing:
                continue
            async with self._slots:
                after = await self._repair(state, record)
            repaired += after is not None and after.orphan is None
        return repaired

    async def _repair(self, state: RuntimeState, record: SessionRecord) -> SessionRecord | None:
        """하나를 되살린다. 그동안 이 파드의 읽기는 이 세션을 건드리지 않는다. 실패하면 최소
        되살리기로 넘어가거나(`_after_failure`) 남기고 고치지 않은 한 줄을 돌려준다."""
        sid = record.session_id
        self._repairing.add(sid)
        try:
            with fenced(None):
                repaired = await _repair_one(state, record)
        except Exception as e:  # noqa: BLE001  실패한 되살리기는 다음 읽기나 sweep 이 다시 한다
            return await self._after_failure(state, record, e)
        finally:
            self._repairing.discard(sid)
        self._settled(sid)
        return repaired

    async def _after_failure(
        self, state: RuntimeState, record: SessionRecord, error: Exception
    ) -> SessionRecord | None:
        sid = record.session_id
        failures = self._failures.get(sid, 0) + 1
        self._failures[sid] = failures
        log.warning("세션 %s 끊긴 턴을 되살리지 못했다(%d번째): %s", sid, failures, error)
        if not store_failure(error) or failures >= REPAIR_FALLBACK_AFTER:
            try:
                with fenced(None):
                    repaired = await _repair_one(state, record, minimal=True)
            except Exception as e:  # noqa: BLE001  최소 되살리기도 다음 읽기나 sweep 이 다시 한다
                log.warning("세션 %s 최소 되살리기도 하지 못했다: %s", sid, e)
            else:
                log.warning("세션 %s 를 줄 없이 최소로 되살렸다", sid)
                self._settled(sid)
                return repaired
        self._resting[sid] = time.monotonic() + self._backoff
        return record

    def _settled(self, session_id: str) -> None:
        self._resting.pop(session_id, None)
        self._failures.pop(session_id, None)


async def _repair_one(
    state: RuntimeState, record: SessionRecord, *, minimal: bool = False
) -> SessionRecord | None:
    """되살리기 하나다. minimal 이면 체크포인트도 전사도 읽지 않고 상태를 둔 채 표시만 지운다."""
    assert record.orphan is not None
    view = KEEP if minimal else await read_checkpoint(state, record)
    index = state.store.index
    repaired = await index.repair_turn(
        record.session_id,
        record.orphan,
        status=view.status,
        pending_since=utcnow(),
        plan=None if minimal else partial(repair_items, view),
    )
    if repaired is None:
        # 다른 파드가 먼저 고쳤거나 그 사이에 잡았다. 체크포인트로 정한 것은 버리고 다시 읽는다
        return await index.get(record.session_id)
    row, marker = repaired
    if marker is not None:
        emit_repaired_end(state.events, row, marker)
    return row


async def align_with_checkpoint(
    state: RuntimeState, record: SessionRecord, *, interrupted: bool
) -> SessionRecord:
    """P79 앞에 막힌 세션의 상태를 체크포인트에 맞춘다(P79 3절). 체크포인트를 이미 읽은 라우트가
    부른다. 인터럽트 위의 active 는 pending 으로, 인터럽트 없는 pending 은 active 로 옮긴다. 리스도
    열린 표시도 없고 last_turn_at 이 읽은 뒤 그대로일 때만 쓰고, 닫힌 세션에는 쓰지 않는다. 쓰지
    못하면 읽은 한 줄 그대로다."""
    if record.status == STATUS_CLOSED or record.running or record.orphan is not None:
        return record
    if interrupted and record.status == STATUS_ACTIVE:
        status, since = STATUS_PENDING, record.pending_since or utcnow()
    elif not interrupted and record.status == STATUS_PENDING:
        status, since = STATUS_ACTIVE, None
    else:
        return record
    try:
        with fenced(None):
            aligned = await state.store.index.align_status(
                record.session_id, status=status, pending_since=since, seen=record.last_turn_at
            )
    except Exception as e:  # noqa: BLE001  맞추지 못해도 읽은 한 줄로 답한다
        log.warning("세션 %s 상태를 체크포인트에 맞추지 못했다: %s", record.session_id, e)
        return record
    return aligned or record


__all__ = [
    "REPAIR_BACKOFF_SECONDS",
    "REPAIR_FALLBACK_AFTER",
    "REPAIR_SLOTS",
    "CheckpointView",
    "TurnRepairs",
    "align_with_checkpoint",
    "emit_repaired_end",
    "read_checkpoint",
    "repair_items",
    "store_failure",
]
