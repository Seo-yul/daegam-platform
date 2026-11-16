"""끊긴 턴의 되살리기다(P79 3절). 실제 Postgres·Redis 에 리스를 풀지 못하고 끝난 턴을 만들고, 세션
읽기(`load_session`), sweep(`TurnRepairs.sweep`), 지우기(`DELETE /sessions/{sid}` 의 라우트 함수)가
3절 표의 줄마다 같은 결과를 내는지 본다. 두 파드가 함께 되살리는 경주, 읽기와 되살리기 사이의 잡기,
지우기와의 경주, 체크포인트 읽기의 저장소 오류, 파드 하나의 되살리기 자리, Redis 의 옛 파드 섞임,
turns:open 정리, 옛 행과 해시, 준비해 둔 문장과 이 단계의 DDL 도 여기서 본다. 앱을 띄워 보는 것은
test_turn_repair_app.py 에 있다."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from redis import exceptions as redis_errors

from runtime import sessions_api, turn_repair
from runtime.auth import Caller
from runtime.cancel import LEASE_LOST
from runtime.dsn import turn_lease_key
from runtime.fence import TurnFenced
from runtime.lease_keeper import LeaseKeeper, PostgresRenewer
from runtime.session_index_redis import RedisSessionIndex, make_redis_client
from runtime.sessions import (
    LIVE_LEASE,
    SELECT_ROW,
    SESSIONS_DDL,
    PostgresSessionIndex,
    SessionStore,
    ddl_statements,
    new_turn_token,
    open_postgres_store,
    utcnow,
)
from runtime.turn_gate import load_session
from runtime.turn_record import RUN_CHAT, RUN_REGENERATE, RUN_RESUME, RunMarker
from runtime.turn_repair import REPAIR_FALLBACK_AFTER, CheckpointView, emit_repaired_end
from tests.conftest import REDIS_URL
from tests.repair_fixtures import (
    EARLIER,
    INTERRUPT_CARD,
    REWIND,
    TOOL_CARD,
    Dead,
    FakeGraph,
    Pod,
    aborted_line,
    dead_turn,
    done,
    failed,
    human_of,
    lapse,
    marker_for,
    open_members,
    pod,
    rebuilt_user,
    user,
)
from tests.store_fixtures import contract_store, pg_schema, schema_store, session_record

STORES = pytest.mark.parametrize("kind", ["postgres", "redis"])
OWNER = Caller(sub="alice", external=False, approver=False)


# --- 3절 표의 줄 ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """3절 표의 한 줄이다. 앞의 칸은 죽기 전 상태이고(dead_turn 의 인자와 체크포인트), 뒤의 칸은
    되살린 뒤 기대하는 것이다. added 는 전사 끝에 붙는 항목의 차례이고 "user" 는 다시 세운 사용자
    항목, "line" 은 turn_aborted 줄이다. trace 는 줄이 실행의 trace_id 를 드는가다."""

    status: str
    kind: str | None
    interrupted: bool = False
    human: bool = False
    # 체크포인트의 마지막 사람 메시지가 앞 턴의 것이다
    other_human: bool = False
    saved: Callable[[str, str | None], list[dict[str, Any]]] = lambda turn_id, trace: []
    settled: bool = False
    deleting: bool = False
    closed_after: bool = False
    unreadable: bool = False
    after: str = "active"
    pending_since: bool = False
    added: tuple[str, ...] = ()
    trace: bool = True
    turn_end: bool = True


ROWS: dict[str, Row] = {
    # 새 말이나 다시 생성의 도중. 줄이 붙고 다음 말이 된다
    "chat": Row(
        "active", RUN_CHAT, human=True, saved=lambda t, _: [user(t), TOOL_CARD], added=("line",)
    ),
    "regenerate": Row(
        "active",
        RUN_REGENERATE,
        human=True,
        saved=lambda t, _: [REWIND, user(t), TOOL_CARD],
        added=("line",),
    ),
    # 첫 조각 끝 전의 새 말. 사용자 항목을 체크포인트의 사람 메시지로 다시 세운다
    "chat-before-first-chunk": Row(
        "active", RUN_CHAT, human=True, saved=lambda t, _: list(EARLIER), added=("user", "line")
    ),
    # 같은 새 말이지만 체크포인트의 마지막 사람 메시지가 그 시도의 것(u-<turn_id>)이 아니다. 그
    # 시도의 입력이 체크포인트에 닿기 전에 죽었으니 사용자 항목을 다시 세우지 않는다
    "chat-before-its-input": Row(
        "active", RUN_CHAT, other_human=True, saved=lambda t, _: list(EARLIER), added=("line",)
    ),
    # 첫 조각 끝 전의 재개와 다시 생성. 사용자 항목을 다시 세우지 않고 마지막 저장 항목 뒤에 줄이
    # 붙는다. 재개의 사용자 항목은 P79 앞의 것(turn_id 없음)이다
    "resume-before-first-chunk": Row(
        "pending",
        RUN_RESUME,
        human=True,
        saved=lambda t, _: [user(), INTERRUPT_CARD],
        added=("line",),
    ),
    "regenerate-before-first-chunk": Row(
        "active", RUN_REGENERATE, human=True, saved=lambda t, _: list(EARLIER), added=("line",)
    ),
    # 인터럽트를 소비한 뒤의 재개. active 가 되어 다음 말이 된다(409 session_pending 이 아니다)
    "resume-after-consumed": Row(
        "pending",
        RUN_RESUME,
        saved=lambda t, _: [user(t), INTERRUPT_CARD, TOOL_CARD],
        added=("line",),
    ),
    # 앞 재개가 오류로 끝난 턴의 둘째 재개. 같은 turn_id 의 오류가 있어도 trace_id 로 가른다
    "second-resume": Row(
        "pending",
        RUN_RESUME,
        saved=lambda t, _: [user(t), INTERRUPT_CARD, failed(t, "first-resume")],
        added=("line",),
    ),
    # 인터럽트를 소비하기 전의 재개. 보류 그대로라 승인 카드가 다시 열린다
    "resume-before-consumed": Row(
        "pending", RUN_RESUME, interrupted=True, saved=lambda t, _: [user(t)], after="pending"
    ),
    # interrupt 를 내던 턴. pending 이 되어 다음 결정이 된다(409 not_pending 이 아니다)
    "emitting-interrupt": Row(
        "active",
        RUN_CHAT,
        interrupted=True,
        human=True,
        saved=lambda t, _: [user(t)],
        after="pending",
        pending_since=True,
    ),
    # 잡기와 시도 기록 사이. 시도의 id 도 표식도 없어 줄도 turn_end 도 없다
    "between-claim-and-record": Row("active", None, turn_end=False),
    # 색인을 적은 마감이 마지막 전사 전에 죽었다. 답이 전사에 없어 줄이 붙고, 표식은 마감이 지웠다
    "settled-before-final-save": Row(
        "active",
        RUN_CHAT,
        saved=lambda t, _: [user(t), TOOL_CARD],
        settled=True,
        added=("line",),
        trace=False,
        turn_end=False,
    ),
    # 마감을 마치고 풀기만 실패했다. 끝이 전사에 있어 줄이 없고 turn_end 도 다시 내지 않는다
    "settled-release-failed": Row(
        "active",
        RUN_CHAT,
        saved=lambda t, trace: [user(t), done(t, trace)],
        settled=True,
        turn_end=False,
    ),
    # sweep 이 닫은 세션. 닫힌 채로 두고 체크포인트를 읽지 않는다
    "closed": Row(
        "active",
        RUN_CHAT,
        saved=lambda t, _: [user(t)],
        closed_after=True,
        after="closed",
        added=("line",),
    ),
    # 저장소는 답했지만 그래프가 만들지 못한 체크포인트. 상태를 두고 줄은 전사로 정한다
    "unreadable-checkpoint": Row(
        "pending",
        RUN_CHAT,
        saved=lambda t, _: [user(t)],
        unreadable=True,
        after="pending",
        added=("line",),
    ),
    # 지우기의 잡기 뒤에 죽었다. 상태는 체크포인트에서 오고 줄도 turn_end 도 없다
    "delete": Row("pending", None, deleting=True, turn_end=False),
}


def _graph(row: Row) -> FakeGraph:
    if row.unreadable:
        return FakeGraph(error=ValueError("serde refused a type"))
    return FakeGraph(interrupted=row.interrupted)


async def _dead(store: SessionStore, row: Row) -> tuple[Dead, FakeGraph]:
    dead = await dead_turn(
        store,
        status=row.status,
        kind=row.kind,
        saved=row.saved,
        settled=row.settled,
        deleting=row.deleting,
        closed_after=row.closed_after,
    )
    graph = _graph(row)
    if row.human:
        graph.human_id = human_of(dead)
    if row.other_human:
        graph.human_id = "u-earlier"
    return dead, graph


async def _assert_repaired(
    store: SessionStore, row: Row, dead: Dead, before: list[dict[str, Any]], pods: list[Pod]
) -> None:
    """3절 표의 뒤 칸이다. 표시가 지워져 다음 잡기가 되고, 상태와 전사, turn_end 가 그 줄과 같다."""
    record = await store.index.get(dead.sid)
    assert record is not None and record.orphan is None and record.running is False
    assert record.status == row.after
    if row.after == "pending" and row.pending_since:
        assert record.pending_since is not None
    if row.after == "active":
        assert record.pending_since is None
    items = await store.transcripts.read(dead.sid)
    assert items[: len(before)] == before
    expected: list[dict[str, Any]] = []
    for added in row.added:
        if added == "user":
            expected.append(rebuilt_user(dead.turn_id))
        else:
            trace = dead.marker.trace_id if row.trace and dead.marker is not None else None
            expected.append(aborted_line(dead.turn_id, trace))
    assert items[len(before) :] == expected
    ended = [event for p in pods for event in p.ended()]
    assert len(ended) == (1 if row.turn_end else 0)
    if ended:
        (end,) = ended
        marker = dead.marker
        assert marker is not None
        assert {k: end[k] for k in ("status", "code", "repaired", "turn_id", "turn")} == {
            "status": LEASE_LOST.code,
            "code": LEASE_LOST.code,
            "repaired": True,
            "turn_id": marker.turn_id,
            "turn": marker.turn,
        }
        assert (end["trace_id"], end["span_id"]) == (marker.trace_id, marker.span_id)
        assert end["session_id"] == dead.sid and end["user_sub"] == "alice"
        assert "duration_ms" not in end and "latency_ms" not in end
    if isinstance(store.index, RedisSessionIndex):
        assert dead.sid not in await open_members(store)
    # 표시가 지워졌으므로 다음 턴이 잡는다
    token = new_turn_token()
    assert await store.index.claim_turn(dead.sid, lease_seconds=30, token=token) is True
    await store.index.release_turn(dead.sid, token)


@STORES
@pytest.mark.parametrize("name", list(ROWS))
async def test_a_session_read_repairs_each_row_of_the_table(kind: str, name: str) -> None:
    row = ROWS[name]
    async with contract_store(kind) as store:
        dead, graph = await _dead(store, row)
        before = await store.transcripts.read(dead.sid)
        state = pod(store, graph)
        record = await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert record.orphan is None and record.status == row.after
        assert graph.calls == (0 if row.closed_after else 1), "닫힌 세션은 체크포인트를 읽지 않는다"
        await _assert_repaired(store, row, dead, before, [state])


@STORES
@pytest.mark.parametrize("name", list(ROWS))
async def test_the_sweep_repairs_each_row_of_the_table(kind: str, name: str) -> None:
    row = ROWS[name]
    async with contract_store(kind) as store:
        dead, graph = await _dead(store, row)
        before = await store.transcripts.read(dead.sid)
        state = pod(store, graph)
        assert await store.index.orphans() == [dead.sid]
        assert await state.repairs.sweep(state) == 1  # type: ignore[arg-type]
        assert await store.index.orphans() == []
        await _assert_repaired(store, row, dead, before, [state])


@STORES
@pytest.mark.parametrize("name", list(ROWS))
async def test_a_delete_claims_over_each_row_and_ends_the_run_once(kind: str, name: str) -> None:
    """지우기는 끊긴 표시 위에서 되살리기를 기다리지 않고 잡는다. 읽기가 되살리지 못하게 자리를
    비워 둔다. 표식이 있는 줄만 turn_end 를 내고, 세션과 그 표식, turns:open 의 줄이 사라진다."""
    row = ROWS[name]
    async with contract_store(kind) as store:
        dead, graph = await _dead(store, row)
        state = pod(store, graph, slots=0)
        answer = await sessions_api.delete_session(dead.sid, OWNER, state)  # type: ignore[arg-type]
        assert answer == {"session_id": dead.sid, "deleted": True}
        assert await store.index.get(dead.sid) is None
        assert await store.transcripts.read(dead.sid) == []
        assert graph.calls == 0
        ended = state.ended()
        assert len(ended) == (1 if row.turn_end else 0)
        if ended:
            assert ended[0]["repaired"] is True and ended[0]["turn_id"] == dead.turn_id
        if kind == "redis":
            assert await open_members(store) == set()
        else:
            assert await _markers(store, dead.sid) == []


async def _markers(store: SessionStore, sid: str) -> list[str]:
    """Postgres 의 그 세션 실행 표식 토큰이다."""
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT token FROM daegam_turn_runs WHERE session_id = %s", [sid])
        return [row["token"] for row in await cur.fetchall()]


async def _redis_run(store: SessionStore, sid: str) -> str | None:
    raw = await store._client.hget(store.index._session_key(sid), "turn_run")
    return RunMarker.from_json(raw).token if raw else None


async def _marker_tokens(store: SessionStore, sid: str) -> list[str]:
    if isinstance(store.index, PostgresSessionIndex):
        return await _markers(store, sid)
    token = await _redis_run(store, sid)
    return [token] if token else []


# --- 시도 기록과 마감의 실행 표식 -------------------------------------------------------------


@STORES
async def test_the_attempt_record_writes_the_marker_and_the_settlement_deletes_it(
    kind: str,
) -> None:
    """시도 기록의 색인 쓰기가 같은 문장에서 실행 표식을 적고 마감이 지운다. 마감이 거절되면(남의
    토큰) 표식은 남는다. 그래서 표식이 남아 있으면 그 시도는 마감하지 못한 것이다."""
    async with contract_store(kind) as store:
        index = store.index
        await index.create(session_record("s1", "alice"))
        token = new_turn_token()
        assert await index.claim_turn("s1", lease_seconds=30, token=token) is True
        marker = marker_for(token, "t-1", RUN_CHAT)
        row = await index.update_held("s1", token, run=marker, turn_id="t-1", last_turn=1)
        assert row is not None and row.turn_id == "t-1"
        assert await _marker_tokens(store, "s1") == [token]
        # 칸이 같은 재개도 표식을 적는다
        again = replace(marker, kind=RUN_RESUME, trace_id="second")
        await index.update_held("s1", token, run=again)
        assert await _marker_tokens(store, "s1") == [token]
        with pytest.raises(TurnFenced):
            await index.update_held("s1", "someone-else", settle=True, last_turn_at=utcnow())
        assert await _marker_tokens(store, "s1") == [token]
        await index.update_held("s1", token, settle=True, last_turn_at=utcnow())
        assert await _marker_tokens(store, "s1") == []
        await index.release_turn("s1", token)


# --- 경주와 실패 ---------------------------------------------------------------------------


class _Rendezvous:
    """두 파드의 체크포인트 읽기가 함께 지나도록 둘 다 올 때까지 기다린다."""

    def __init__(self, parties: int = 2) -> None:
        self._parties = parties
        self._arrived = 0
        self._all = asyncio.Event()

    async def __call__(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._all.set()
        await asyncio.wait_for(self._all.wait(), 5)


@STORES
async def test_two_pods_repairing_at_once_store_one_line_and_one_turn_end(kind: str) -> None:
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        meet = _Rendezvous()
        a, b = pod(store, FakeGraph(hook=meet)), pod(store, FakeGraph(hook=meet))
        first, second = await asyncio.gather(
            load_session(a, dead.sid),  # type: ignore[arg-type]
            load_session(b, dead.sid),  # type: ignore[arg-type]
        )
        assert first.orphan is None and second.orphan is None
        items = await store.transcripts.read(dead.sid)
        assert [item["kind"] for item in items] == ["user", "error"]
        assert len(a.ended()) + len(b.ended()) == 1


@STORES
async def test_a_claim_between_the_read_and_the_repair_is_left_alone(kind: str) -> None:
    """읽은 뒤 다른 파드가 먼저 되살리고 새 턴이 잡았다. 이 파드의 되살리기는 아무것도 쓰지 않고
    다시 읽은 한 줄(새 턴이 도는 중)을 돌려준다. 새 턴의 리스와 표식은 그대로다."""
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        other = pod(store, FakeGraph())
        fresh = new_turn_token()

        async def meanwhile() -> None:
            await load_session(other, dead.sid)  # type: ignore[arg-type]
            assert await store.index.claim_turn(dead.sid, lease_seconds=30, token=fresh)
            await store.index.update_held(
                dead.sid, fresh, run=marker_for(fresh, "t-new", RUN_CHAT), turn_id="t-new"
            )

        late = pod(store, FakeGraph(hook=meanwhile))
        record = await load_session(late, dead.sid)  # type: ignore[arg-type]
        assert record.running is True and record.orphan is None and record.turn_id == "t-new"
        assert await _marker_tokens(store, dead.sid) == [fresh]
        assert [i["kind"] for i in await store.transcripts.read(dead.sid)] == ["user", "error"]
        assert len(other.ended()) == 1 and late.ended() == []
        await store.index.release_turn(dead.sid, fresh)


@STORES
@pytest.mark.parametrize("delete_first", [True, False], ids=["delete-first", "repair-first"])
async def test_a_delete_racing_a_repair_emits_one_turn_end(kind: str, delete_first: bool) -> None:
    """지우기의 잡기와 되살리기는 같은 토큰을 겨룬다. 한쪽만 실행 표식을 얻어 turn_end 는 하나다."""
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        deleting = pod(store, FakeGraph())
        record = await store.index.get(dead.sid)
        assert record is not None

        async def delete_claim() -> None:
            token = new_turn_token()
            claimed, marker = await store.index.claim_delete(
                dead.sid, lease_seconds=30, token=token
            )
            assert claimed
            if marker is not None:
                emit_repaired_end(deleting.events, record, marker)

        repairing = pod(store, FakeGraph(hook=delete_claim if delete_first else None))
        await load_session(repairing, dead.sid)  # type: ignore[arg-type]
        if not delete_first:
            await delete_claim()
        assert len(deleting.ended()) + len(repairing.ended()) == 1
        assert len(deleting.ended()) == (1 if delete_first else 0)


@STORES
async def test_a_store_error_in_the_checkpoint_read_leaves_the_record_and_the_mark(
    kind: str,
) -> None:
    """체크포인트 읽기가 저장소 오류면 되살리기를 그만두고 표시와 표식을 다음에 남긴다. 그 파드의
    읽기는 쉬는 동안 다시 건드리지 않고, sweep 은 쉬는 시간을 보지 않고 다시 한다."""
    error = (
        psycopg.OperationalError("down") if kind == "postgres" else redis_errors.ConnectionError()
    )
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        graph = FakeGraph(error=error)
        state = pod(store, graph, backoff=60)
        record = await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert record.orphan == dead.token
        assert await _marker_tokens(store, dead.sid) == [dead.token]
        assert [i["kind"] for i in await store.transcripts.read(dead.sid)] == ["user"]
        assert state.ended() == []
        await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert graph.calls == 1, "쉬는 동안의 읽기는 되살리지 않는다"
        graph.error = None
        assert await state.repairs.sweep(state) == 1  # type: ignore[arg-type]
        assert graph.calls == 2
        assert (await store.index.get(dead.sid)).orphan is None
        assert len(state.ended()) == 1


@STORES
async def test_a_read_with_both_slots_taken_returns_the_unrepaired_record(kind: str) -> None:
    async with contract_store(kind) as store:
        dead = await dead_turn(store)
        state = pod(store, FakeGraph(), slots=0)
        record = await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert record.orphan == dead.token and record.running is False


@STORES
async def test_a_failed_claim_reads_again_and_repairs(kind: str) -> None:
    """잡기는 끊긴 열린 표시를 받지 않는다. 잡지 못한 요청은 다시 읽어 되살리고 409 이며, 두 번째로
    잡지 않는다. 다음 요청은 잡는다."""
    from fastapi import HTTPException

    from runtime.turn_gate import held_lease

    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        assert await store.index.claim_turn(dead.sid, lease_seconds=30, token="x") is False
        state = pod(store, FakeGraph())
        with pytest.raises(HTTPException) as refused:
            async with held_lease(state, dead.sid):  # type: ignore[arg-type]
                raise AssertionError("잡지 않아야 한다")
        assert refused.value.status_code == 409
        assert refused.value.detail["code"] == "turn_in_progress"
        assert (await store.index.get(dead.sid)).orphan is None
        assert len(state.ended()) == 1
        async with held_lease(state, dead.sid) as lease:  # type: ignore[arg-type]
            assert lease.session_id == dead.sid


async def test_twenty_orphans_on_one_pod_use_two_slots_and_the_keeper_keeps_renewing() -> None:
    """풀이 8 인 Postgres 에서 끊긴 세션 스물을 한꺼번에 읽는다. 체크포인트 읽기는 실제처럼 풀의
    연결 하나를 쓰고, 한 세션의 행은 다른 연결이 4초 동안 잠가 둔다. 되살리기는 둘까지만 돌고(풀을
    다 쓰지 않는다), 잠긴 세션의 되살리기는 lock_timeout(3초)에 실패해 그 연결을 놓으며, 다른
    되살리기는 그 3초 남짓 안에 끝나고, 뒤따르는 읽기가 모두 되살린다. 리스 관리자의 바퀴는 풀 밖의
    제 연결로 그동안에도 확인된다."""
    async with schema_store("inst-1") as store:
        index = store.index
        dead = [await dead_turn(store, saved=lambda t, _: [user(t)]) for _ in range(20)]

        async def read_through_the_pool() -> None:
            async with store._pool.connection() as conn:
                await conn.execute("SELECT pg_sleep(0.05)")

        graph = FakeGraph(hook=read_through_the_pool)
        state = pod(store, graph, backoff=0.1)
        loop = asyncio.get_running_loop()
        took: list[tuple[float, BaseException | None]] = []
        repair = index.repair_turn

        async def timed(*args: Any, **kwargs: Any) -> Any:
            started = loop.time()
            error: BaseException | None = None
            try:
                return await repair(*args, **kwargs)
            except BaseException as e:
                error = e
                raise
            finally:
                took.append((loop.time() - started, error))

        index.repair_turn = timed  # type: ignore[method-assign]
        await index.create(session_record("busy", "alice"))
        keeper = LeaseKeeper(
            PostgresRenewer(index), interval=0.05, retry=0.05, fence_seconds=2, lease_seconds=5
        )
        token = new_turn_token()
        assert await index.claim_turn("busy", lease_seconds=5, token=token)
        held = keeper.hold("busy", token, sent_at=loop.time())
        first_fence = held.fence.deadline
        locker = await psycopg.AsyncConnection.connect(store._pool.conninfo)
        await locker.execute(
            "SELECT 1 FROM daegam_sessions WHERE session_id = %s FOR UPDATE", [dead[0].sid]
        )

        async def hold_then_release() -> None:
            await asyncio.sleep(4)
            await locker.rollback()

        release_lock = asyncio.create_task(hold_then_release())
        keeper.start()
        try:
            deadline = loop.time() + 40
            while True:
                records = await asyncio.gather(*(load_session(state, d.sid) for d in dead))
                if all(record.orphan is None for record in records):
                    break
                assert loop.time() < deadline, "다 되살리지 못했다"
                await asyncio.sleep(0.05)
            assert graph.peak <= 2
            failed = [error for _, error in took if error is not None]
            assert len(failed) == 1 and isinstance(failed[0], psycopg.errors.LockNotAvailable)
            assert all(seconds < 3.5 for seconds, _ in took)
            assert [seconds >= 2.9 for seconds, _ in took].count(True) == 1
            assert len([1 for _, error in took if error is None]) == 20
            assert len(state.ended()) == 20
            assert held.fence.deadline > first_fence and not held.fence.standing
        finally:
            await keeper.stop()
            await release_lock
            await locker.close()
            await index.release_turn("busy", token)


# --- Redis 의 turns:open 과 옛 파드 ---------------------------------------------------------


async def test_turns_open_is_empty_after_a_delete_a_purge_and_an_old_pods_delete(
    redis_store,
) -> None:
    store, _ = redis_store
    index = store.index
    await index.create(session_record("deleted", "alice"))
    assert await index.claim_turn("deleted", lease_seconds=30, token="t1")
    assert await open_members(store) == {"deleted"}
    await store.delete_session("deleted")
    assert await open_members(store) == set()
    # 보존 기간이 지난 닫힌 세션의 끊긴 표시도 기록과 함께 빠진다
    await index.create(session_record("purged", "alice", age=4000, status="closed"))
    assert await index.claim_turn("purged", lease_seconds=30, token="t2")
    await lapse(store, "purged")
    assert await store.purge_records(utcnow()) == ["purged"]
    assert await open_members(store) == set()
    # 옛 파드가 해시를 지우면 sweep 의 후보 찾기가 그 줄을 뺀다
    await index.create(session_record("old-delete", "alice"))
    assert await index.claim_turn("old-delete", lease_seconds=30, token="t3")
    await lapse(store, "old-delete")
    await store._client.delete(index._session_key("old-delete"))
    assert await index.orphans() == []
    assert await open_members(store) == set()
    # 리스가 살아 있는 줄은 후보가 아니다. 풀면 빠진다
    await index.create(session_record("live", "alice"))
    assert await index.claim_turn("live", lease_seconds=30, token="t4")
    assert await index.orphans() == []
    await index.release_turn("live", "t4")
    assert await open_members(store) == set()


async def _old_claim(store: SessionStore, sid: str, token: str) -> None:
    """P79 앞 런타임의 잡기 스크립트다. 열린 표시를 모르고 SET NX PX 와 turn_id 지우기뿐이다."""
    prefix = store.index._prefix
    assert await store._client.set(turn_lease_key(prefix, sid), token, nx=True, px=30_000)
    await store._client.hdel(store.index._session_key(sid), "turn_id")


async def _old_release(store: SessionStore, sid: str, token: str) -> None:
    prefix = store.index._prefix
    key = turn_lease_key(prefix, sid)
    if await store._client.get(key) == token:
        await store._client.delete(key, f"{prefix}turn_cancel:{sid}")


async def test_an_old_pods_claim_and_release_over_a_dead_open_mark(redis_store) -> None:
    """옛 파드가 새 파드의 끊긴 턴 위에서 잡고 마친 뒤 풀었다. 열린 표시는 죽은 토큰 그대로다.
    되살리기는 옛 파드의 뒤 턴이 끝난 것을 보고 줄을 붙이지 않으며, 표시를 지우고 표식이 아직 맞는
    죽은 실행의 turn_end 를 낸다(5절)."""
    store, _ = redis_store
    dead = await dead_turn(store, saved=lambda t, _: [user(t)])
    await _old_claim(store, dead.sid, "old-pod")
    await store._client.hset(store.index._session_key(dead.sid), "turn_id", "t-old")
    await store.transcripts.append(dead.sid, [user("t-old"), done("t-old", "old-trace")])
    await _old_release(store, dead.sid, "old-pod")
    record = await store.index.get(dead.sid)
    assert record is not None and record.orphan == dead.token
    state = pod(store, FakeGraph())
    repaired = await load_session(state, dead.sid)  # type: ignore[arg-type]
    assert repaired.orphan is None
    items = await store.transcripts.read(dead.sid)
    assert [i["kind"] for i in items] == ["user", "user", "done"]
    (end,) = state.ended()
    assert end["turn_id"] == dead.turn_id and end["repaired"] is True


async def test_an_old_pods_live_redis_turn_is_never_repaired(redis_store) -> None:
    """옛 파드는 열린 표시를 적지 않으므로 새 파드는 그 턴을 끊긴 것으로 보지 않는다. 옛 해시도
    예전처럼 읽힌다."""
    store, _ = redis_store
    index = store.index
    await store._client.hset(
        index._session_key("old"),
        mapping={
            "session_id": "old",
            "user_sub": "alice",
            "created_at": utcnow().isoformat(),
            "last_turn_at": utcnow().isoformat(),
            "status": "active",
        },
    )
    record = await index.get("old")
    assert record is not None and (record.running, record.orphan, record.turns) == (False, None, 0)
    await _old_claim(store, "old", "old-pod")
    assert (await index.get("old")).running is True
    await _old_release(store, "old", "old-pod")
    record = await index.get("old")
    assert record is not None and (record.running, record.orphan) == (False, None)
    assert await index.orphans() == []
    assert await index.claim_turn("old", lease_seconds=30, token="new") is True


# --- Postgres 의 옛 행, 이 단계의 DDL, 도는 붙이기 --------------------------------------------


async def test_old_postgres_rows_read_as_before_and_an_old_pods_dead_turn_is_repaired() -> None:
    """옛 런타임이 넣은 행은 예전처럼 읽힌다. 옛 파드의 끊긴 턴(긴 리스가 끝나고 토큰이 남은 행)은
    표식이 없어 turn_end 없이 되살리고, 줄은 색인의 turn_id 로 정한다."""
    async with schema_store("inst-1") as store:
        now = utcnow()
        async with store._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO daegam_sessions (session_id, instance_id, user_sub, created_at, "
                "last_turn_at, status, turn_id, turn_token, turn_lease) "
                "VALUES ('old', 'inst-1', 'alice', %s, %s, 'active', 't-old', 'old-pod', "
                "now() - interval '1 second')",
                [now, now],
            )
            await conn.execute(
                "INSERT INTO daegam_sessions (session_id, instance_id, user_sub, created_at, "
                "last_turn_at, status) VALUES ('plain', 'inst-1', 'alice', %s, %s, 'active')",
                [now, now],
            )
        plain = await store.index.get("plain")
        assert plain is not None and (plain.orphan, plain.running, plain.turns) == (None, False, 0)
        state = pod(store, FakeGraph())
        record = await load_session(state, "old")  # type: ignore[arg-type]
        assert record.orphan is None
        assert await store.transcripts.read("old") == [aborted_line("t-old", None)]
        assert state.ended() == []


OLD_SESSION_READ = f"SELECT *, ({LIVE_LEASE}) AS running FROM daegam_sessions WHERE session_id = %s"
"""P79 앞 런타임이 준비해 둔 세션 읽기다."""


async def test_a_prepared_old_session_read_survives_this_phases_ddl() -> None:
    """옛 파드가 준비해 둔 `SELECT *` 세션 읽기는 다른 파드가 이 단계의 DDL(새 표와 부분 인덱스)을
    돌린 뒤에도 같은 연결에서 돈다. `daegam_sessions` 에 열을 더하면 그 문장은 깨지고, 열 이름으로
    읽는 새 문장은 그래도 돈다. 그래서 실행 표식을 제 표에 둔다. DDL 은 다른 연결에서 돌린다. 같은
    연결의 DDL 은 psycopg 가 준비해 둔 문장을 스스로 버려 깨짐을 가린다."""
    statements = ddl_statements(SESSIONS_DDL)
    phase = [s for s in statements if "daegam_turn_runs" in s or "daegam_sessions_open" in s]
    assert len(phase) == 2
    async with pg_schema() as schema:
        connect = psycopg.AsyncConnection.connect
        async with (
            await connect(schema.dsn, autocommit=True) as other,
            await connect(schema.dsn, autocommit=True, prepare_threshold=0) as old_pod,
        ):
            for statement in statements:
                if statement not in phase:
                    await other.execute(statement)
            now = utcnow()
            await other.execute(
                "INSERT INTO daegam_sessions (session_id, user_sub, created_at, last_turn_at, "
                "status) VALUES ('s1', 'alice', %s, %s, 'active')",
                [now, now],
            )
            new_read = f"SELECT {SELECT_ROW} FROM daegam_sessions WHERE session_id = %s"

            async def read(query: str) -> Any:
                return await (await old_pod.execute(query, ["s1"])).fetchone()

            assert await read(OLD_SESSION_READ) is not None
            assert await read(new_read) is not None
            for statement in phase:
                await other.execute(statement)
            assert await read(OLD_SESSION_READ) is not None
            await other.execute("ALTER TABLE daegam_sessions ADD COLUMN later text")
            # "cached plan must not change result type" 다. 서버의 언어와 상관없이 SQLSTATE 로 본다
            with pytest.raises(psycopg.errors.FeatureNotSupported):
                await read(OLD_SESSION_READ)
            assert await read(new_read) is not None


async def test_an_append_stalled_across_the_repair_lands_before_its_read_or_inserts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """도는 턴의 붙이기는 세션 행을 FOR KEY SHARE 로 잡으므로 되살리기의 FOR UPDATE 와 부딪힌다.
    붙이기가 먼저 잠그면 되살리기는 그 커밋을 기다렸다가 붙인 항목까지 읽고(여기서는 done 이라 줄이
    없다), 되살리기가 먼저 잠그면 붙이기는 기다렸다가 조건을 다시 보고 아무것도 넣지 않는다."""
    from runtime import sessions
    from runtime.transcript import HELD_APPEND_SQL, dumps

    async with schema_store("inst-1") as store:
        index = store.index
        # 붙이기가 먼저 잠근다
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        assert dead.marker is not None
        async with store._pool.connection() as conn:
            await conn.execute(
                "UPDATE daegam_sessions SET turn_lease = now() + interval '30 seconds' "
                "WHERE session_id = %s",
                [dead.sid],
            )
        appending = await psycopg.AsyncConnection.connect(store._pool.conninfo, autocommit=True)
        try:
            async with appending.transaction():
                cur = await appending.execute(
                    HELD_APPEND_SQL,
                    (dead.sid, dead.token, [dumps(done(dead.turn_id, dead.marker.trace_id))]),
                )
                assert cur.rowcount == 1
                await lapse(store, dead.sid)
                state = pod(store, FakeGraph())
                repairing = asyncio.create_task(load_session(state, dead.sid))  # type: ignore[arg-type]
                await asyncio.sleep(0.3)
                assert not repairing.done(), "되살리기가 붙이기의 잠금을 기다려야 한다"
            record = await repairing
        finally:
            await appending.close()
        assert record.orphan is None
        assert [i["kind"] for i in await store.transcripts.read(dead.sid)] == ["user", "done"]

        # 되살리기가 먼저 잠근다. 전사 읽기를 늦춰 그 트랜잭션이 잠금을 쥔 동안 붙이기가 온다
        late = await dead_turn(store, saved=lambda t, _: [user(t)])
        appending = await psycopg.AsyncConnection.connect(store._pool.conninfo, autocommit=True)
        try:
            async with appending.transaction():
                # 붙이기의 now() 는 이 트랜잭션의 시작이라 그때 리스는 살아 있었다
                await appending.execute("SELECT now()")
                async with store._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE daegam_sessions SET turn_lease = now() + interval '0.2 seconds' "
                        "WHERE session_id = %s",
                        [late.sid],
                    )
                await asyncio.sleep(0.3)
                monkeypatch.setattr(
                    sessions,
                    "READ_SQL",
                    "SELECT pg_sleep(0.5), item " + sessions.READ_SQL[12:],
                )
                state = pod(store, FakeGraph())
                repairing = asyncio.create_task(load_session(state, late.sid))  # type: ignore[arg-type]
                await asyncio.sleep(0.2)
                cur = await appending.execute(
                    HELD_APPEND_SQL, (late.sid, late.token, [dumps(user("stalled"))])
                )
                assert cur.rowcount == 0, "되살린 뒤 조건을 다시 보아 넣지 않아야 한다"
            record = await repairing
        finally:
            await appending.close()
        assert record.orphan is None
        items = await store.transcripts.read(late.sid)
        assert [i["kind"] for i in items] == ["user", "error"]
        assert index is store.index


async def test_a_fresh_postgres_store_creates_the_marker_table_and_the_partial_index() -> None:
    async with pg_schema() as schema:
        store = await open_postgres_store(schema.dsn, instance_id="inst-1")
        try:
            assert "daegam_turn_runs" in await schema.tables()
            async with store._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'daegam_sessions_open' "
                    "AND schemaname = %s",
                    [schema.name],
                )
                (row,) = await cur.fetchall()
            assert row["indexdef"].endswith("WHERE (turn_token IS NOT NULL)")
        finally:
            await store.close()


async def test_the_orphans_query_keeps_to_its_instance() -> None:
    """sweep 의 후보는 이 인스턴스의 행뿐이다. 같은 Database 를 나눠 쓰는 다른 Agent 의 끊긴 턴은
    건드리지 않는다."""
    async with schema_store("inst-a") as store:
        dead = await dead_turn(store)
        theirs = PostgresSessionIndex(store._pool, "inst-b")
        assert await theirs.orphans() == []
        assert await store.index.orphans() == [dead.sid]


# --- 자리, 최소 되살리기, 표식을 모르는 파드의 턴, 행 잠금 아래의 지우기 ---------------------


@STORES
async def test_a_session_being_repaired_here_takes_no_second_slot(kind: str) -> None:
    """한 세션의 되살리기가 멈춰 있는 동안 그 세션의 다른 읽기는 둘째 자리를 잡지 않고 고치지 않은
    한 줄을 돌려준다. 둘째 자리는 다른 세션이 쓰고, sweep 도 그 세션을 건너뛴다."""
    async with contract_store(kind) as store:
        stuck = await dead_turn(store, saved=lambda t, _: [user(t)])
        other = await dead_turn(store, saved=lambda t, _: [user(t)])
        entered, gate = asyncio.Event(), asyncio.Event()

        async def hold_the_first() -> None:
            if graph.calls == 1:
                entered.set()
                await gate.wait()

        graph = FakeGraph(hook=hold_the_first)
        state = pod(store, graph)
        first = asyncio.create_task(load_session(state, stuck.sid))  # type: ignore[arg-type]
        await asyncio.wait_for(entered.wait(), 5)
        again = await load_session(state, stuck.sid)  # type: ignore[arg-type]
        assert again.orphan == stuck.token and graph.calls == 1
        repaired = await load_session(state, other.sid)  # type: ignore[arg-type]
        assert repaired.orphan is None and graph.calls == 2
        assert await state.repairs.sweep(state) == 0  # type: ignore[arg-type]
        assert graph.calls == 2
        gate.set()
        assert (await first).orphan is None
        assert len(state.ended()) == 2


def _failing_plan(*args: Any) -> list[dict[str, Any]]:
    raise ValueError("a defect in the line decision")


@STORES
async def test_a_failure_that_is_not_a_store_error_falls_back_to_a_minimal_repair(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """줄을 정하다 저장소 오류가 아닌 것으로 실패하면 곧바로 최소 되살리기다. 표시를 지우고 상태를
    두며(체크포인트로는 active 였을 보류) 줄은 붙이지 않고, 실행 표식으로 turn_end 를 낸다. 세션은
    읽을 수 없는 것 때문에 막히지 않는다."""
    monkeypatch.setattr(turn_repair, "repair_items", _failing_plan)
    async with contract_store(kind) as store:
        dead = await dead_turn(
            store, status="pending", kind=RUN_RESUME, saved=lambda t, _: [user(t)]
        )
        before = await store.transcripts.read(dead.sid)
        state = pod(store, FakeGraph())
        record = await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert record.orphan is None and record.status == "pending"
        assert await store.transcripts.read(dead.sid) == before
        (end,) = state.ended()
        assert end["repaired"] is True and end["turn_id"] == dead.turn_id
        assert await _marker_tokens(store, dead.sid) == []
        if kind == "redis":
            assert dead.sid not in await open_members(store)
        assert await store.index.claim_turn(dead.sid, lease_seconds=30, token="next") is True


async def test_an_unreadable_redis_transcript_entry_falls_back_to_a_minimal_repair(
    redis_store,
) -> None:
    """Redis 전사의 항목 하나를 JSON 으로 읽을 수 없으면 최소 되살리기다. 최소 되살리기는 전사를
    읽지 않는다."""
    store, _ = redis_store
    dead = await dead_turn(store, saved=lambda t, _: [user(t)])
    key = f"{store.index._prefix}transcript:{dead.sid}"
    await store._client.rpush(key, "not json")
    state = pod(store, FakeGraph())
    record = await load_session(state, dead.sid)  # type: ignore[arg-type]
    assert record.orphan is None
    assert (await store._client.lrange(key, -1, -1)) == ["not json"]
    assert len(state.ended()) == 1


@STORES
async def test_repeated_store_errors_fall_back_to_a_minimal_repair(kind: str) -> None:
    """체크포인트 읽기가 저장소 오류로 연이어 실패하면 REPAIR_FALLBACK_AFTER 번째에 체크포인트 없이
    최소로 되살린다."""
    error: Exception = (
        psycopg.OperationalError("down") if kind == "postgres" else redis_errors.ConnectionError()
    )
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        graph = FakeGraph(error=error)
        state = pod(store, graph, backoff=0)
        for _ in range(REPAIR_FALLBACK_AFTER - 1):
            assert (await load_session(state, dead.sid)).orphan == dead.token  # type: ignore[arg-type]
        assert state.ended() == []
        record = await load_session(state, dead.sid)  # type: ignore[arg-type]
        assert record.orphan is None and graph.calls == REPAIR_FALLBACK_AFTER
        assert [i["kind"] for i in await store.transcripts.read(dead.sid)] == ["user"]
        assert len(state.ended()) == 1


def test_a_marker_older_than_the_index_does_not_key_the_line() -> None:
    """표식 뒤에 마감이 last_turn_at 을 옮겼으면(표식을 모르는 파드의 같은 턴 재개) 줄은 그 실행의
    trace_id 가 아니라 색인의 turn_id 로 정한다. 그 턴이 끝났으면 줄이 없다."""
    marker = marker_for("t", "turn-1", RUN_RESUME)
    record = replace(session_record("s", "alice"), turn_id="turn-1")
    later = replace(record, last_turn_at=marker.start + timedelta(seconds=5))
    earlier = replace(record, last_turn_at=marker.start - timedelta(seconds=5))
    settled_by_another_pod = [user("turn-1"), done("turn-1", "another-run")]
    view = CheckpointView(status="active")
    assert turn_repair.repair_items(view, later, marker, settled_by_another_pod) == []
    lines = turn_repair.repair_items(view, earlier, marker, settled_by_another_pod)
    assert lines == [aborted_line("turn-1", marker.trace_id)]


async def test_the_redis_repair_refuses_when_last_turn_at_moved_after_its_read(
    redis_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """되살리기가 읽은 뒤 표시를 모르는 파드가 그 세션에서 턴을 마감했다(배포 도중). 스크립트는
    last_turn_at 이 읽은 값과 다르면 쓰지 않고, 다음 읽기가 새 상태로 다시 정한다. 그 턴이 같은 턴의
    재개로 끝났으면 줄 없이 표시만 지우고 죽은 실행의 turn_end 는 낸다."""
    store, _ = redis_store
    index = store.index
    dead = await dead_turn(store, status="pending", kind=RUN_RESUME, saved=lambda t, _: [user(t)])
    script = index._repair_script
    moved: list[str] = []

    async def another_pod_settles_first(*args: Any, **kwargs: Any) -> Any:
        if not moved:
            moved.append(dead.sid)
            await _old_claim(store, dead.sid, "old-pod")
            await store._client.hset(
                index._session_key(dead.sid),
                mapping={"turn_id": dead.turn_id, "last_turn_at": utcnow().isoformat()},
            )
            await store.transcripts.append(dead.sid, [done(dead.turn_id, "old-pod-run")])
            await _old_release(store, dead.sid, "old-pod")
        return await script(*args, **kwargs)

    monkeypatch.setattr(index, "_repair_script", another_pod_settles_first)
    state = pod(store, FakeGraph())
    record = await load_session(state, dead.sid)  # type: ignore[arg-type]
    assert record.orphan == dead.token, "읽은 뒤 바뀐 세션에는 쓰지 않는다"
    assert state.ended() == []
    record = await load_session(state, dead.sid)  # type: ignore[arg-type]
    assert record.orphan is None
    assert [i["kind"] for i in await store.transcripts.read(dead.sid)] == ["user", "done"]
    (end,) = state.ended()
    assert end["turn_id"] == dead.turn_id


def _claim_from_another_pod(store: SessionStore, sid: str, out: list[Any]) -> None:
    """다른 파드의 지우기 잡기를 제 이벤트 루프와 제 연결로 돌린다. 되살리기가 plan 안에서 행 잠금을
    쥔 채 이것을 기다리게 하려고 스레드에서 돈다."""

    async def run() -> None:
        if isinstance(store.index, PostgresSessionIndex):
            from psycopg_pool import AsyncConnectionPool

            from runtime.sessions import postgres_pool_kwargs

            pool = AsyncConnectionPool(
                store._pool.conninfo,
                open=False,
                min_size=1,
                max_size=1,
                kwargs=postgres_pool_kwargs(fenced=False),
            )
            await pool.open()
            try:
                index = PostgresSessionIndex(pool, "inst-1")
                out.append(await index.claim_delete(sid, lease_seconds=30, token="deleting"))
            finally:
                await pool.close()
            return
        client = make_redis_client({"url": REDIS_URL})
        try:
            index = RedisSessionIndex(client, store.index._prefix)
            out.append(await index.claim_delete(sid, lease_seconds=30, token="deleting"))
        finally:
            await client.aclose()

    asyncio.run(run())


@STORES
async def test_a_delete_claim_during_the_repairs_line_decision_ends_the_run_once(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """되살리기가 줄을 정하는 동안(Postgres 는 그 행을 FOR UPDATE 로 쥐고 있다) 다른 파드의 지우기가
    잡는다. Postgres 의 잡기는 그 잠금을 기다렸다가 되살린 행을 잡아 표식을 얻지 못하고, Redis 의
    잡기는 먼저 끝나 표식을 얻으며 되살리기 스크립트가 쓰지 않는다. 어느 쪽이든 turn_end 는
    하나다."""
    async with contract_store(kind) as store:
        dead = await dead_turn(store, saved=lambda t, _: [user(t)])
        record = await store.index.get(dead.sid)
        assert record is not None
        claimed: list[Any] = []
        blocked: list[bool] = []
        decide = turn_repair.repair_items

        def decide_while_a_delete_claims(*args: Any) -> list[dict[str, Any]]:
            thread = threading.Thread(
                target=_claim_from_another_pod, args=(store, dead.sid, claimed)
            )
            thread.start()
            thread.join(1.0)
            blocked.append(thread.is_alive())
            threads.append(thread)
            return decide(*args)

        threads: list[threading.Thread] = []
        monkeypatch.setattr(turn_repair, "repair_items", decide_while_a_delete_claims)
        repairing, deleting = pod(store, FakeGraph()), pod(store, FakeGraph())
        await load_session(repairing, dead.sid)  # type: ignore[arg-type]
        for thread in threads:
            await asyncio.to_thread(thread.join, 10)
        ((ok, marker),) = claimed
        assert ok is True
        if marker is not None:
            emit_repaired_end(deleting.events, record, marker)
        if kind == "postgres":
            assert blocked == [True], "지우기의 잡기가 되살리기의 행 잠금을 기다려야 한다"
            assert marker is None and len(repairing.ended()) == 1
        else:
            assert blocked == [False] and marker is not None and repairing.ended() == []
        assert len(repairing.ended()) + len(deleting.ended()) == 1
