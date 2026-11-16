"""끊긴 턴의 되살리기 테스트(test_turn_repair.py)가 쓰는 도우미다(P79 3절).

- FakeGraph: 되살리기가 읽는 체크포인트다. 인터럽트, 마지막 사람 메시지, 오류를 정하고 읽은 수를
  센다. hook 은 읽는 도중에 다른 파드의 일을 끼워 넣는다.
- pod: 실제 저장소를 나눠 쓰는 파드 하나의 상태다. 되살리기 자리, 이벤트 기록, 리스 관리자를 든다.
- dead_turn: 리스를 풀지 못하고 끝난 턴을 저장소에 만든다. 잡기, 시도 기록(실행 표식), 저장한 전사,
  마감을 차례로 적은 뒤 리스를 끝낸다(`lapse`).
- 전사 항목(user·tool card·interrupt·done·error)과 되살린 줄의 모양, turn_end 읽기, Redis 의
  `turns:open` 읽기다.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import HumanMessage

from runtime.cancel import LEASE_LOST
from runtime.config import AgentConfig, parse_config
from runtime.dsn import turn_lease_key
from runtime.events import EventLog
from runtime.lease_keeper import IndexRenewer, LeaseKeeper
from runtime.sessions import PostgresSessionIndex, SessionStore, new_turn_token, utcnow
from runtime.transcript import error_item, user_item
from runtime.turn_record import RunMarker, human_message_id, new_turn_id
from runtime.turn_repair import REPAIR_BACKOFF_SECONDS, REPAIR_SLOTS, TurnRepairs
from tests.conftest import load_fixture
from tests.store_fixtures import session_record

QUESTION = "다시 물을 질문"
"""체크포인트의 마지막 사람 메시지의 글이다. 다시 세운 사용자 항목이 이 글을 든다."""


@dataclass
class FakeGraph:
    """체크포인트 읽기(`aget_state`)만 하는 그래프다. human_id 가 있으면 그 id 의 사람 메시지가
    마지막이고, error 가 있으면 그것을 던진다. calls 는 읽은 수다."""

    interrupted: bool = False
    human_id: str | None = None
    error: BaseException | None = None
    hook: Callable[[], Awaitable[None]] | None = None
    delay: float = 0.0
    calls: int = 0
    active: int = 0
    peak: int = 0

    async def aget_state(self, config: dict[str, Any]) -> Any:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.hook is not None:
                await self.hook()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
        finally:
            self.active -= 1
        messages = [HumanMessage(QUESTION, id=self.human_id)] if self.human_id else []
        interrupts = (SimpleNamespace(value={}),) if self.interrupted else ()
        return SimpleNamespace(interrupts=interrupts, values={"messages": messages})


@dataclass
class Pod:
    """저장소를 나눠 쓰는 파드 하나다. 되살리기와 지우기 라우트가 읽는 RuntimeState 의 칸만 든다.
    config 는 세션 읽기가 세션 TTL 을 보는 문서다(P44 11절)."""

    config: AgentConfig
    store: SessionStore
    agent: Any
    events: EventLog
    repairs: TurnRepairs
    leases: LeaseKeeper
    out: io.StringIO = field(default_factory=io.StringIO)

    def ended(self) -> list[dict[str, Any]]:
        """이 파드가 낸 turn_end 다."""
        lines = [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]
        return [line for line in lines if line["daegam_event"] == "turn_end"]


def pod(
    store: SessionStore,
    graph: FakeGraph,
    *,
    slots: int = REPAIR_SLOTS,
    backoff: float = REPAIR_BACKOFF_SECONDS,
) -> Pod:
    out = io.StringIO()
    config = parse_config(load_fixture("minimal.json"))
    events = EventLog(config, out=out)
    return Pod(
        config=config,
        store=store,
        agent=SimpleNamespace(graph=graph),
        events=events,
        repairs=TurnRepairs(slots=slots, backoff=backoff),
        leases=LeaseKeeper(IndexRenewer(store.index)),
        out=out,
    )


@dataclass(frozen=True)
class Dead:
    """dead_turn 이 만든 끊긴 턴이다."""

    sid: str
    token: str
    turn_id: str
    marker: RunMarker | None


async def lapse(store: SessionStore, sid: str) -> None:
    """세션의 리스를 끝낸다. 파드가 죽은 뒤 1분이 지난 것과 같다. 열린 표시는 남는다."""
    index = store.index
    if isinstance(index, PostgresSessionIndex):
        async with store._pool.connection() as conn:
            await conn.execute(
                "UPDATE daegam_sessions SET turn_lease = now() - interval '1 second' "
                "WHERE session_id = %s",
                [sid],
            )
        return
    prefix = index._prefix
    await store._client.delete(turn_lease_key(prefix, sid), f"{prefix}turn_cancel:{sid}")


def marker_for(token: str, turn_id: str, kind: str) -> RunMarker:
    return RunMarker(
        token=token,
        turn_id=turn_id,
        turn=1,
        kind=kind,
        trace_id=uuid.uuid4().hex,
        span_id=uuid.uuid4().hex[:16],
        start=utcnow().replace(microsecond=0),
    )


async def dead_turn(
    store: SessionStore,
    *,
    status: str = "active",
    kind: str | None = "chat",
    saved: Callable[[str, str | None], list[dict[str, Any]]] = lambda turn_id, trace: [],
    settled: bool = False,
    deleting: bool = False,
    closed_after: bool = False,
    sid: str | None = None,
) -> Dead:
    """리스를 풀지 못하고 끝난 턴을 만든다. kind 가 None 이면 잡은 뒤 시도 기록 전에 죽은 턴이고,
    deleting 이면 지우기의 잡기 뒤에 죽은 것이다. saved 는 turn_id 와 실행의 trace_id 로 저장한 전사
    항목을 만든다. settled 면 마감의 색인 쓰기(실행 표식을 지운다)까지 갔다. closed_after 면 그 뒤에
    sweep 이 세션을 닫았다."""
    index = store.index
    sid = sid or uuid.uuid4().hex
    await index.create(session_record(sid, "alice", status=status, age=10))
    token = new_turn_token()
    if deleting:
        claimed, _ = await index.claim_delete(sid, lease_seconds=30, token=token)
    else:
        claimed = await index.claim_turn(sid, lease_seconds=30, token=token)
    assert claimed
    turn_id = new_turn_id()
    marker = None
    if kind is not None and not deleting:
        marker = marker_for(token, turn_id, kind)
        await index.update_held(sid, token, run=marker, turn_id=turn_id, last_turn=1)
    items = saved(turn_id, marker.trace_id if marker is not None else None)
    if items:
        await store.transcripts.append_held(sid, items, token)
    if settled:
        await index.update_held(
            sid, token, settle=True, status=status, last_turn_at=utcnow(), pending_since=None
        )
    await lapse(store, sid)
    if closed_after:
        # sweep 의 expire 처럼 상태만 옮기고 last_turn_at 은 둔다
        record = await index.get(sid)
        assert record is not None
        await index.update(sid, status="closed", last_turn_at=record.last_turn_at)
    return Dead(sid=sid, token=token, turn_id=turn_id, marker=marker)


# --- 전사 항목 -------------------------------------------------------------------------------


def user(turn_id: str | None = None) -> dict[str, Any]:
    """저장된 사용자 항목이다. turn_id 가 없으면 P79 앞의 것이다."""
    return user_item("질문", turn_id=turn_id)


TOOL_CARD = {
    "kind": "tool_call",
    "call": {"id": "c1", "name": "Add", "args": {"a": 1, "b": 2}},
    "result": {"id": "c1", "name": "Add", "status": "success", "content": "3"},
}
INTERRUPT_CARD = {"kind": "interrupt", "pending": {"action_requests": []}, "resolved": False}
REWIND = {"kind": "rewind"}
EARLIER = [
    user_item("앞 질문", turn_id="earlier"),
    {"kind": "done", "usage": {}, "turn": 1, "turn_id": "earlier", "trace_id": "earlier-trace"},
]
"""앞 턴의 사용자 항목과 끝이다."""


def done(turn_id: str, trace_id: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "done", "usage": {}, "turn": 1, "turn_id": turn_id}
    if trace_id is not None:
        item["trace_id"] = trace_id
    return item


def failed(turn_id: str, trace_id: str) -> dict[str, Any]:
    """같은 턴의 앞 재개가 오류로 끝난 항목이다."""
    return error_item(
        {"turn_id": turn_id, "trace_id": trace_id, "code": "turn_failed", "message": "x"}
    )


def aborted_line(turn_id: str, trace_id: str | None) -> dict[str, Any]:
    """되살리기가 저장하는 줄이다."""
    return error_item(
        {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "code": LEASE_LOST.code,
            "message": LEASE_LOST.detail,
        }
    )


def rebuilt_user(turn_id: str) -> dict[str, Any]:
    """체크포인트의 사람 메시지로 다시 세운 사용자 항목이다."""
    return user_item(QUESTION, turn_id=turn_id)


def human_of(dead: Dead) -> str:
    return human_message_id(dead.turn_id)


async def open_members(store: SessionStore) -> set[str]:
    """Redis 의 turns:open 이다."""
    return set(await store._client.smembers(store.index._open_key()))
