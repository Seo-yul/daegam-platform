"""테스트가 세션 저장소와 장기 기억 저장소를 여는 도우미와, 세션 저장소 셋(메모리·Postgres·Redis)이
같이 지키는 계약이다. redis_store 픽스처가 있어 conftest 가 pytest_plugins 로 싣는다.

- real_store / redis_store / drop_redis_instance: 실제 Postgres·Redis 세션 저장소를 테스트마다 새
  인스턴스 id 로 열고 끝나면 만든 것을 지운다. 주소가 없으면 건너뛴다. redis_store 는 real_store 로
  연 Redis 저장소와 그 인스턴스 id 를 주는 픽스처다(Redis 저장소·색인·카운터 테스트).
- pg_schema / schema_store: 빈 Postgres 가 필요한 테스트에 새 스키마와 그것을 search_path 로 건 DSN
  을 주고 끝나면 그 스키마만 지운다. 컨테이너를 나눠 쓰는 다른 실행의 public 표를 지우지 않는다.
  schema_store 는 그 스키마에 세션 저장소를 연다. 실제 세션 저장소는 이 둘과 real_store 로 연다.
  직접 여는 테스트는 여는 일 자체를 보는 것(동시 setup, 옛 표, 빈 스키마에 생기는 표, Redis 7,
  연결할 수 없는 주소)뿐이다.
- store_for / pod_store / contract_store: 저장소 종류 이름으로 앱에 넘길 세션 저장소를, 앱 여럿이
  나눠 쓰는 파드 안 저장소를, 고정한 sid 를 쓰는 계약 테스트의 저장소를 연다.
- memory_backend / real_memory / open_pg_memory / open_redis_memory: 장기 기억 저장소를 연다.
- session_record·run_record 와 assert_*: 색인·전사 테스트(test_sessions·test_turn_record·
  test_transcript_store·test_redis_index·test_drain_mark)가 저장소마다 거는 같은 단정이다.
  고정한 sid 를 쓰므로 다른 테스트와 나눠 쓰지 않는 저장소에 건다.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from langgraph.store.memory import InMemoryStore
from psycopg.conninfo import make_conninfo
from redis.exceptions import ResponseError

from runtime.config import parse_config
from runtime.dsn import redis_prefix
from runtime.fence import Fence, TurnFenced, fenced
from runtime.long_term import POSTGRES_SCHEMA, LongTermMemory, open_long_term
from runtime.session_index_redis import open_redis_store
from runtime.sessions import (
    RUNS_INCLUDE,
    RUNS_ONLY,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_PENDING,
    DrainMarked,
    RunTaken,
    SessionIndex,
    SessionRecord,
    SessionStore,
    new_turn_token,
    open_postgres_store,
    open_session_store,
    utcnow,
)
from runtime.transcript import TRANSCRIPT_LIMIT, TranscriptStore
from tests.conftest import PG_DSN, REDIS_URL, load_fixture, store_address

# --- 계약 -----------------------------------------------------------------------------------


def session_record(sid: str, user: str, *, age: int = 0, status: str = "active") -> SessionRecord:
    """age 초 전에 만들어져 그때 마지막 턴을 마친 세션이다."""
    when = utcnow() - timedelta(seconds=age)
    return SessionRecord(sid, None, user, when, when, status=status)


def run_record(
    sid: str, user: str, run_id: str, *, age: int = 0, status: str = "active", **fields: Any
) -> SessionRecord:
    """백그라운드 실행의 세션이다(P41 9절). fields 는 그 밖의 칸(untrusted, title 같은)이다."""
    return replace(session_record(sid, user, age=age, status=status), run_id=run_id, **fields)


async def assert_a_task_session_holds_several_runs(index: SessionIndex) -> None:
    """작업 세션은 실행 id 를 여럿 쥔다(runs.py). 다음 실행은 턴의 리스를 쥔 쓰기가 run_id 를
    바꾸고, 앞 실행의 id 로도 그 세션을 찾는다. 어느 세션이든 이미 쥔 id 는 아무것도 쓰지 않고
    RunTaken 이며, 리스를 잃은 쓰기는 거절된다. 색인 행을 지우면 쥔 id 가 모두 풀린다. task 는
    내부값이다. 세 색인이 같은 규칙이다."""
    first, second, other = (uuid.uuid4().hex for _ in range(3))
    await index.create(run_record("t1", "alice", first, task=True))
    await index.create(run_record("r1", "alice", other))
    made = await index.get("t1")
    assert made is not None and (made.task, made.run_id) == (True, first)
    assert "task" not in made.to_dict()
    assert (await index.get("r1")).task is False
    # 리스 없이, 또는 다른 토큰으로는 쓰지 않는다
    with pytest.raises(TurnFenced):
        await index.take_run("t1", new_turn_token(), second)
    assert await index.find_run(second) is None
    token = new_turn_token()
    assert await index.claim_turn("t1", lease_seconds=30, token=token) is True
    # 다른 세션이 쥔 id 와 이 세션이 이미 쥔 id 는 쥔 세션을 알리고 아무것도 쓰지 않는다
    for taken, holder in ((other, "r1"), (first, "t1")):
        with pytest.raises(RunTaken) as info:
            await index.take_run("t1", token, taken)
        assert info.value.session_id == holder
    assert (await index.get("t1")).run_id == first
    moved = await index.take_run("t1", token, second)
    assert (moved.run_id, moved.task, moved.running) == (second, True, True)
    await index.release_turn("t1", token)
    for run_id in (first, second):
        held = await index.find_run(run_id)
        assert held is not None and (held.session_id, held.run_id) == ("t1", second)
    with pytest.raises(RunTaken) as info:
        await index.create(run_record("t2", "alice", second, task=True))
    assert info.value.session_id == "t1" and await index.get("t2") is None
    listed = await index.list(runs=RUNS_ONLY)
    assert {row.session_id for row in listed} == {"t1", "r1"}
    await index.delete("t1")
    assert await index.find_run(first) is None and await index.find_run(second) is None
    await index.create(run_record("t3", "alice", first, task=True))
    assert (await index.find_run(first)).session_id == "t3"


async def assert_holds_one_session_per_run(index: SessionIndex) -> None:
    """실행 id 하나에는 세션 하나뿐이다(P41 9절). 둘째 만들기는 아무것도 쓰지 않고 쥔 세션을
    알리며, 같은 실행 id 로 동시에 만들어도 하나만 이긴다. 닫힌 세션도 실행 id 를 쥐고 찾히며, 색인
    행을 지우면 풀린다. run_id 는 세션 한 줄에 실리고 untrusted 는 내부값이다. 세 색인이 같은
    규칙이다."""
    run_id, raced = uuid.uuid4().hex, uuid.uuid4().hex
    # 웹훅 실행의 세션은 만들 때 바깥 표시도 적는다(turn.py `resolve_session`)
    marked = utcnow().replace(microsecond=0)
    await index.create(run_record("r1", "alice", run_id, untrusted=True, outside_at=marked))
    with pytest.raises(RunTaken) as info:
        await index.create(run_record("r2", "bob", run_id))
    assert info.value.session_id == "r1"
    assert await index.get("r2") is None
    assert await index.list(user_sub="bob", runs=RUNS_INCLUDE) == []
    assert await index.counts() == {"active": 1, "pending": 0, "closed": 0}
    held = await index.find_run(run_id)
    assert held is not None and (held.session_id, held.run_id, held.untrusted) == (
        "r1",
        run_id,
        True,
    )
    assert held.running is False and held.outside_at == marked
    assert held.to_dict()["run_id"] == run_id and "untrusted" not in held.to_dict()
    assert await index.find_run(uuid.uuid4().hex) is None
    # 닫힌 세션도 실행 id 를 쥐고 있다
    await index.update("r1", status=STATUS_CLOSED, last_turn_at=utcnow())
    closed = await index.find_run(run_id)
    assert closed is not None and closed.status == STATUS_CLOSED
    with pytest.raises(RunTaken):
        await index.create(run_record("r3", "alice", run_id))
    # 리스를 잡은 실행 세션은 찾을 때도 running 이다
    token = new_turn_token()
    assert await index.claim_turn("r1", lease_seconds=30, token=token) is True
    assert (await index.find_run(run_id)).running is True
    await index.release_turn("r1", token)
    # 같은 실행 id 로 동시에 만들면 하나만 이긴다
    results = await asyncio.gather(
        index.create(run_record("r4", "alice", raced)),
        index.create(run_record("r5", "alice", raced)),
        return_exceptions=True,
    )
    taken = [r for r in results if isinstance(r, RunTaken)]
    assert len(taken) == 1 and results.count(None) == 1
    winner = await index.find_run(raced)
    assert winner is not None and taken[0].session_id == winner.session_id
    loser = ({"r4", "r5"} - {winner.session_id}).pop()
    assert await index.get(loser) is None
    # 지우면 실행 id 가 풀려 새 세션이 쥘 수 있다
    await index.delete("r1")
    assert await index.find_run(run_id) is None
    await index.create(run_record("r6", "alice", run_id))
    assert (await index.find_run(run_id)).session_id == "r6"
    # 채팅 세션은 실행 id 가 없고 웹훅 실행 표시가 꺼져 있다
    await index.create(session_record("c1", "alice"))
    chat_row = await index.get("c1")
    assert chat_row is not None and chat_row.run_id is None and chat_row.untrusted is False
    assert chat_row.to_dict()["run_id"] is None


async def assert_lists_run_sessions_on_request(index: SessionIndex) -> None:
    """목록은 실행 세션을 기본으로 빼고, include 는 함께, only 는 실행 세션만 준다(P41 9절). 사용자·
    상태·제목·limit 와 겹쳐도 같고 limit 는 거른 뒤의 수다. 세 색인이 같은 규칙이다."""
    await index.create(session_record("chat-a", "alice", age=30))
    await index.create(run_record("run-a", "alice", uuid.uuid4().hex, age=20, title="환불 실행"))
    await index.create(run_record("run-b", "bob", uuid.uuid4().hex, age=10, status=STATUS_PENDING))
    await index.create(replace(session_record("chat-b", "bob"), title="환불 문의"))

    async def listed(**kwargs: Any) -> list[str]:
        return [row.session_id for row in await index.list(**kwargs)]

    assert await listed() == ["chat-b", "chat-a"]
    assert await listed(runs=RUNS_INCLUDE) == ["chat-b", "run-b", "run-a", "chat-a"]
    assert await listed(runs=RUNS_ONLY) == ["run-b", "run-a"]
    assert await listed(runs=RUNS_ONLY, user_sub="alice") == ["run-a"]
    assert await listed(runs=RUNS_ONLY, status=STATUS_PENDING) == ["run-b"]
    assert await listed(status=STATUS_PENDING) == []
    assert await listed(user_sub="bob") == ["chat-b"]
    assert await listed(q="환불") == ["chat-b"]
    assert await listed(q="환불", runs=RUNS_ONLY) == ["run-a"]
    assert await listed(runs=RUNS_INCLUDE, limit=2) == ["chat-b", "run-b"]
    assert await listed(runs=RUNS_ONLY, limit=1) == ["run-b"]
    assert await listed(limit=1) == ["chat-b"]
    with pytest.raises(ValueError):
        await index.list(runs="bogus")  # type: ignore[arg-type]


async def assert_expire_skips_a_leased_session(index: SessionIndex) -> None:
    """TTL sweep 은 리스가 살아 있는 세션을 닫지 않는다(P41 9절). 마감 가까이 재개된 턴이
    체크포인트를 잃지 않게 한다. 리스가 풀리면 다음 sweep 이 닫는다. 세 색인이 같은 규칙이다."""
    await index.create(session_record("busy", "alice", age=4000))
    await index.create(session_record("idle", "alice", age=4000, status=STATUS_PENDING))
    token = new_turn_token()
    assert await index.claim_turn("busy", lease_seconds=30, token=token) is True
    before = utcnow() - timedelta(seconds=3600)
    assert await index.expire(before) == ["idle"]
    assert (await index.get("busy")).status == STATUS_ACTIVE
    await index.release_turn("busy", token)
    assert await index.expire(before) == ["busy"]
    assert (await index.get("busy")).status == STATUS_CLOSED


async def assert_filters_updates_and_counts(index: SessionIndex) -> None:
    """목록은 최근 것부터이고 사용자·상태로 거르며 limit 를 지킨다. 갱신은 없는 세션에 None 이고
    만들 때 정해지는 필드를 받지 않는다. 개수와 합계는 상태별·전체다. 세 색인이 같은 규칙이다."""
    await index.create(session_record("a", "alice", age=20))
    await index.create(session_record("b", "bob", age=10))
    await index.create(session_record("c", "alice", status=STATUS_PENDING))

    async def listed(**kwargs: Any) -> list[str]:
        return [row.session_id for row in await index.list(**kwargs)]

    assert await listed() == ["c", "b", "a"]
    assert await listed(user_sub="alice") == ["c", "a"]
    assert await listed(status=STATUS_PENDING) == ["c"]
    assert await listed(user_sub="alice", status="active") == ["a"]
    assert await listed(user_sub="alice", limit=1) == ["c"]
    assert await listed(status="active", limit=1) == ["b"]
    assert await listed(limit=0) == []
    updated = await index.update("a", turns=3, usage_input=10, usage_output=5)
    assert updated.turns == 3 and (await index.get("a")).usage_input == 10
    assert await index.update("zzz", turns=1) is None
    with pytest.raises(ValueError):
        await index.update("a", user_sub="mallory")
    assert await index.counts() == {"active": 2, "pending": 1, "closed": 0}
    assert await index.totals() == {
        "sessions": 3,
        "turns": 3,
        "input_tokens": 10,
        "output_tokens": 5,
    }


async def assert_purges_only_closed_rows(index: SessionIndex) -> None:
    """closed_before 는 닫힌 행만 준다. 세 색인이 같은 규칙이다(D46).

    TTL 을 보존 기간보다 길게 둔 정의에서는 살아 있는 세션이 보존 기간을 지날 수 있는데, 그때
    대화 중인 사람의 기록을 지우면 안 된다.
    """
    await index.create(session_record("open", "alice", age=4000))
    await index.create(session_record("shut", "alice", age=4000, status=STATUS_CLOSED))
    now = utcnow()
    assert await index.closed_before(now) == ["shut"]
    assert await index.closed_before(now - timedelta(seconds=5000)) == []
    await index.delete("shut")
    assert await index.get("shut") is None
    assert await index.closed_before(now) == []
    # 없는 행을 지워도 탈이 없다. 두 파드가 같은 세션을 집을 수 있다
    await index.delete("shut")


async def assert_cancel_mark_follows_the_lease_token(index: SessionIndex) -> None:
    """세 색인이 같이 지키는 계약이다(D65). 표시는 살아 있는 리스의 토큰이고 중지는 그 토큰을
    돌려준다. 풀기는 제 토큰의 리스와 표시만 지우며, 앞 턴의 표시가 다음 턴을 멈추지 않는다."""
    sid = uuid.uuid4().hex
    await index.create(session_record(sid, "alice"))
    first, second = new_turn_token(), new_turn_token()
    # 진행 중 턴이 없으면 표시를 적지 않는다
    assert await index.request_cancel(sid) is None
    assert await index.claim_turn(sid, lease_seconds=30, token=first) is True
    assert await index.cancel_requested(sid, first) is False
    assert await index.request_cancel(sid) == first
    assert await index.cancel_requested(sid, first) is True
    assert await index.cancel_requested(sid, second) is False
    # 여러 번 불러도 같다
    assert await index.request_cancel(sid) == first
    # 남의 토큰으로는 풀리지 않는다
    await index.release_turn(sid, second)
    assert await index.claim_turn(sid, lease_seconds=30, token=second) is False
    await index.release_turn(sid, first)
    assert await index.cancel_requested(sid, first) is False
    # 다음 턴은 앞 턴의 표시를 보지 않는다
    assert await index.claim_turn(sid, lease_seconds=30, token=second) is True
    assert await index.cancel_requested(sid, second) is False
    assert await index.cancel_requested(sid, first) is False
    await index.release_turn(sid, second)
    assert await index.request_cancel(sid) is None


async def assert_running_and_the_named_stop(index: SessionIndex) -> None:
    """세 색인이 같이 지키는 계약이다(P78 2절). running 은 살아 있는 리스로 읽을 때 정하고 저장하지
    않는다. 진행 중인 한 줄만 turn_id 를 싣는다. 리스를 잡으면 앞 시도의 id 를 지워, 새 시도가 제
    id 를 적기 전에 앞 시도의 이름으로 온 중지가 새 턴을 멈추지 않는다. 이름을 준 중지는 그 시도일
    때만 표시를 적고, cancel_all 은 살아 있는 리스마다 적어 그 수를 돌려준다."""
    sid, other = uuid.uuid4().hex, uuid.uuid4().hex
    user = f"u-{sid}"
    await index.create(replace(session_record(sid, user, age=10), turn_id="t-1"))
    await index.create(replace(session_record(other, user), turn_id="t-9"))
    row = await index.get(sid)
    assert row is not None and row.running is False
    assert row.to_dict()["running"] is False and "turn_id" not in row.to_dict()
    token = new_turn_token()
    assert await index.claim_turn(sid, lease_seconds=30, token=token) is True
    row = await index.get(sid)
    assert row is not None and row.running is True and row.turn_id is None
    # 새 시도가 id 를 적기 전에는 앞 시도의 이름으로 온 중지가 표시를 적지 않는다
    assert await index.request_cancel(sid, turn_id="t-1") is None
    assert await index.cancel_requested(sid, token) is False
    updated = await index.update(sid, turn_id="t-2", last_turn=2)
    assert updated is not None and updated.running is True
    assert updated.to_dict()["turn_id"] == "t-2"
    listed = {r.session_id: r.running for r in await index.list(user_sub=user)}
    assert listed == {sid: True, other: False}
    # 고친 뒤 돌려준 줄도, 다시 읽은 줄도 리스로 정한다
    renamed = await index.update(sid, title="이름")
    assert renamed is not None and renamed.running is True
    assert (await index.get(sid)).running is True
    # 다른 시도의 이름으로는 멈추지 않는다
    assert await index.request_cancel(sid, turn_id="t-1") is None
    assert await index.cancel_requested(sid, token) is False
    assert await index.request_cancel(sid, turn_id="t-2") == token
    assert await index.cancel_requested(sid, token) is True
    await index.release_turn(sid, token)
    assert (await index.get(sid)).running is False
    first, second = new_turn_token(), new_turn_token()
    assert await index.claim_turn(sid, lease_seconds=30, token=first) is True
    assert await index.claim_turn(other, lease_seconds=30, token=second) is True
    assert await index.cancel_all() == 2
    assert await index.cancel_requested(sid, first) is True
    assert await index.cancel_requested(other, second) is True
    await index.release_turn(sid, first)
    await index.release_turn(other, second)
    assert await index.cancel_all() == 0


async def assert_renews_turns_by_token(index: SessionIndex) -> None:
    """세 색인이 같이 지키는 계약이다(P79 1절). 갱신은 제 토큰이 쥔 리스만 늘리고 그 세션을
    돌려준다. 남의 토큰, 풀린 리스, 없는 세션은 돌려주지 않고 리스를 되살리지 않는다."""
    sid, other, gone = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    for s in (sid, other):
        await index.create(session_record(s, "alice"))
    mine, theirs = new_turn_token(), new_turn_token()
    assert await index.renew_turns({}, lease_seconds=60) == set()
    assert await index.claim_turn(sid, lease_seconds=30, token=mine) is True
    assert await index.claim_turn(other, lease_seconds=30, token=theirs) is True
    leases = {sid: mine, other: mine, gone: mine}
    assert await index.renew_turns(leases, lease_seconds=60) == {sid}
    assert await index.renew_turns({sid: mine, other: theirs}, lease_seconds=60) == {sid, other}
    # 풀린 리스는 되살리지 않고, 그 세션은 다른 턴이 잡을 수 있다
    await index.release_turn(sid, mine)
    assert await index.renew_turns({sid: mine}, lease_seconds=60) == set()
    row = await index.get(sid)
    assert row is not None and row.running is False
    assert await index.claim_turn(sid, lease_seconds=30, token=theirs) is True
    # 앞 턴의 토큰은 다음 턴의 리스를 늘리지 않는다
    assert await index.renew_turns({sid: mine}, lease_seconds=60) == set()
    for s in (sid, other):
        await index.release_turn(s, theirs)


async def assert_drain_mark_fences_claims(index: SessionIndex) -> None:
    """세 색인이 같이 지키는 계약이다(P44 7절). 표시를 세우면 그 앞에 잡힌 리스를 센다. 표시가 선
    동안에는 턴의 잡기도 지우기의 잡기도 DrainMarked 로 물러서고 아무것도 쓰지 않으며, 앞에 잡힌
    턴은 그대로 늘리고 푼다. 새 id 로 다시 세우면 id 를 바꾸고 다시 센다. 지우기는 id 가 같은
    표시만 지운다. 앞 재우기의 늦은 지우기는 새 표시를 두고 잡기는 계속 물러선다. 지우면 잡기가 다시
    되고, 없는 표시를 지워도 된다. 짧게 세운 표시는 저절로 풀린다."""
    before, after = uuid.uuid4().hex, uuid.uuid4().hex
    for sid in (before, after):
        await index.create(session_record(sid, "alice"))
    held, token = new_turn_token(), new_turn_token()
    assert await index.drain_marked() is False
    assert await index.claim_turn(before, lease_seconds=30, token=held) is True
    assert await index.mark_drain(mark="sleep-1", seconds=60) == 1
    assert await index.drain_marked() is True
    with pytest.raises(DrainMarked):
        await index.claim_turn(after, lease_seconds=30, token=token)
    with pytest.raises(DrainMarked):
        await index.claim_delete(after, lease_seconds=30, token=token)
    refused = await index.get(after)
    assert refused is not None and (refused.running, refused.orphan) == (False, None)
    assert await index.renew_turns({before: held}, lease_seconds=30) == {before}
    await index.release_turn(before, held)
    assert await index.mark_drain(mark="sleep-2", seconds=60) == 0
    await index.clear_drain_mark("sleep-1")
    assert await index.drain_marked() is True
    with pytest.raises(DrainMarked):
        await index.claim_turn(after, lease_seconds=30, token=token)
    await index.clear_drain_mark("sleep-2")
    assert await index.drain_marked() is False
    await index.clear_drain_mark("sleep-2")
    assert await index.claim_turn(after, lease_seconds=30, token=token) is True
    await index.release_turn(after, token)
    assert await index.mark_drain(mark="sleep-3", seconds=0.05) == 0
    await asyncio.sleep(0.15)
    assert await index.drain_marked() is False
    assert await index.claim_turn(after, lease_seconds=30, token=token) is True
    await index.release_turn(after, token)


async def assert_turn_writes_carry_the_lease_token(store: SessionStore) -> None:
    """세 저장소가 같이 지키는 계약이다(P79 2절). 턴 자신의 색인 쓰기(`update_held`)와 전사
    붙이기(`append_held`)는 리스의 토큰을 건다. 그 토큰이 살아 있는 리스를 쥐고 있을 때만 쓰고,
    아니면 아무것도 쓰지 않고 TurnFenced 를 던지며 지금 문맥의 펜스를 세운다. 상태를 옮기면 앞
    상태의 목록에서 빠지고, None 은 칸을 지우며, 칸이 없는 쓰기는 토큰만 본다. 파드 안 전사는 다른
    파드가 없어 토큰을 보지 않는다. 토큰을 보지 않는 쓰기(`update`, 이름 바꾸기 같은)는 그대로다."""
    index, transcripts = store.index, store.transcripts
    sid = uuid.uuid4().hex
    await index.create(session_record(sid, "alice"))
    mine, theirs = new_turn_token(), new_turn_token()
    assert await index.claim_turn(sid, lease_seconds=30, token=mine) is True
    held = await index.update_held(sid, mine, turn_id="t-1", last_turn=1)
    assert held is not None and held.turn_id == "t-1" and held.running is True

    async def listed(status: str) -> list[str]:
        return [row.session_id for row in await index.list(status=status)]

    now = utcnow()
    moved = await index.update_held(
        sid, mine, status=STATUS_PENDING, last_turn_at=now, pending_since=now
    )
    assert moved is not None and moved.status == STATUS_PENDING and moved.pending_since
    assert sid in await listed(STATUS_PENDING) and sid not in await listed(STATUS_ACTIVE)
    back = await index.update_held(
        sid, mine, status=STATUS_ACTIVE, last_turn_at=utcnow(), pending_since=None
    )
    assert back is not None and back.status == STATUS_ACTIVE and back.pending_since is None
    assert sid in await listed(STATUS_ACTIVE) and sid not in await listed(STATUS_PENDING)
    same = await index.update_held(sid, mine)
    assert same is not None and same.turn_id == "t-1"
    await transcripts.append_held(sid, [{"kind": "user", "text": "a"}], mine)
    fence = Fence(asyncio.get_running_loop().time() + 60)
    with fenced(fence), pytest.raises(TurnFenced):
        await index.update_held(sid, theirs, turn_id="t-2")
    assert fence.lost
    with pytest.raises(TurnFenced):
        await index.update_held(sid, theirs)
    assert (await index.get(sid)).turn_id == "t-1"
    if store.shared:
        with pytest.raises(TurnFenced):
            await transcripts.append_held(sid, [{"kind": "user", "text": "b"}], theirs)
    assert [item["text"] for item in await transcripts.read(sid)] == ["a"]
    await index.release_turn(sid, mine)
    with pytest.raises(TurnFenced):
        await index.update_held(sid, mine, turn_id="t-3")
    if store.shared:
        with pytest.raises(TurnFenced):
            await transcripts.append_held(sid, [{"kind": "user", "text": "c"}], mine)
    renamed = await index.update(sid, title="이름")
    assert renamed is not None and renamed.title == "이름" and renamed.turn_id == "t-1"


TITLED = (("t1", "100% 환불 문의"), ("t2", "100 환불"), ("t3", "a_b 설정"), ("t4", "AXB 설정"))


async def assert_title_search(index: SessionIndex) -> None:
    """검색은 대소문자를 가리지 않고 LIKE 메타문자를 글자 그대로 견준다. 세 색인이 같은 규칙이다."""
    for sid, title in TITLED:
        await index.create(replace(session_record(sid, "alice"), title=title))
    await index.create(session_record("untitled", "alice"))

    async def found(q: str, **kwargs: Any) -> set[str]:
        return {row.session_id for row in await index.list(q=q, **kwargs)}

    assert await found("100%") == {"t1"}
    assert await found("환불", user_sub="alice") == {"t1", "t2"}
    assert await found("a_b") == {"t3"}
    assert await found("axb") == {"t4"}
    assert await found("설정", status="closed") == set()
    assert await found("\u1112\u1161\u11ab") == set()
    assert len(await index.list(q="설정", limit=1)) == 1
    assert len(await index.list()) == 5


def _numbered(n: int) -> dict[str, Any]:
    """차례를 message 에 적은 전사 항목이다."""
    return {"kind": "error", "code": "e", "message": str(n)}


async def _texts(transcripts: TranscriptStore, sid: str) -> list[str]:
    return [item["text"] for item in await transcripts.read(sid)]


async def assert_transcripts_round_trip(transcripts: TranscriptStore) -> None:
    """전사는 세션마다 순서·원문 그대로 왕복하고, 상한을 넘으면 그 세션의 앞부분만 버린다. 세
    저장소가 같은 규칙이다(P18 F1).

    다른 세션의 줄을 먼저 넣어 두므로 상한 정리가 지우는 차례 범위 안에 남의 줄이 든다. 정리가
    세션을 가리지 않으면 Database 를 나눠 쓰는 다른 Agent 의 전사까지 지운다. 그 세션에 줄을 더
    붙인 뒤 이 세션의 상한을 다시 보는 것은 정리 기준을 고를 때 남의 줄까지 세는 결함을 잡는다.
    """
    sid, other = "s1", "s2"
    assert await transcripts.read(sid) == []
    await transcripts.append(other, [{"kind": "user", "text": "남"}])
    long_text = "가" * 5000
    await transcripts.append(sid, [{"kind": "user", "text": "하나"}])
    await transcripts.append(sid, [{"kind": "assistant", "text": long_text, "streaming": False}])
    rows = await transcripts.read(sid)
    assert [r["kind"] for r in rows] == ["user", "assistant"]
    assert rows[1]["text"] == long_text, "긴 본문도 자르지 않는다"
    assert [r["kind"] for r in await transcripts.read(sid, limit=1)] == ["assistant"]
    # 빈 목록은 아무것도 바꾸지 않는다(턴 뒤 finally 가 한 번 더 부른다)
    await transcripts.append(sid, [])
    assert len(await transcripts.read(sid)) == 2

    await transcripts.append(sid, [_numbered(n) for n in range(TRANSCRIPT_LIMIT)])
    rows = await transcripts.read(sid)
    assert len(rows) == TRANSCRIPT_LIMIT, "상한을 넘으면 그만큼만 남는다"
    assert rows[0]["message"] == "0" and rows[-1]["message"] == str(TRANSCRIPT_LIMIT - 1)
    assert await _texts(transcripts, other) == ["남"], "상한 정리가 다른 세션의 줄을 지웠다"

    await transcripts.append(other, [{"kind": "user", "text": "또"}])
    await transcripts.append(sid, [_numbered(TRANSCRIPT_LIMIT)])
    rows = await transcripts.read(sid)
    assert len(rows) == TRANSCRIPT_LIMIT, "다른 세션의 줄까지 세어 정리 기준을 골랐다"
    assert rows[0]["message"] == "1" and rows[-1]["message"] == str(TRANSCRIPT_LIMIT)
    assert await _texts(transcripts, other) == ["남", "또"], "상한 정리가 다른 세션의 줄을 지웠다"

    await transcripts.delete(sid)
    assert await transcripts.read(sid) == []
    assert await _texts(transcripts, other) == ["남", "또"]


# --- 세션 저장소 열기 ------------------------------------------------------------------------


def _fresh_instance(doc: dict[str, Any]) -> str:
    """이 테스트만의 새 인스턴스 id 를 doc 의 instance.id 에 넣고 준다. 색인과 장기 기억이 인스턴스
    id 로 행과 키를 나누므로 둘이 다르면 자기가 만든 것도 못 본다. 같은 컨테이너를 다시 써도 앞선
    실행의 것과 섞이지 않는다."""
    instance_id = f"ai-t{uuid.uuid4().hex[:8]}"
    doc["instance"]["id"] = instance_id
    return instance_id


@asynccontextmanager
async def real_store(kind: str, doc: dict[str, Any]) -> AsyncIterator[SessionStore]:
    """실제 저장소(kind 는 postgres·redis)를 이 테스트만의 새 인스턴스 id 로 연다. doc 의
    instance.id 도 그 값으로 바꾼다. 끝나면 이 테스트가 만든 카운터와 Redis 키·인덱스를 지운다."""
    address = store_address(kind)
    instance_id = _fresh_instance(doc)
    if kind == "postgres":
        store = await open_postgres_store(address, instance_id=instance_id)
        try:
            yield store
        finally:
            await _drop_postgres_counters(store, instance_id)
            await store.close()
        return
    store = await open_redis_store({"url": address}, instance_id=instance_id)
    try:
        yield store
    finally:
        await drop_redis_instance(store._client, instance_id)
        await store.close()


@pytest.fixture
async def redis_store() -> AsyncIterator[tuple[SessionStore, str]]:
    """새 인스턴스 접두어로 연 Redis 저장소와 그 인스턴스 id 다. 끝나면 키·인덱스를
    지운다(real_store)."""
    doc = load_fixture("minimal.json")
    async with real_store("redis", doc) as store:
        yield store, doc["instance"]["id"]


async def _drop_postgres_counters(store: SessionStore, instance_id: str) -> None:
    """Postgres 의 카운터 행은 이 테스트가 만든 것만 지운다."""
    async with store._pool.connection() as conn:
        await conn.execute("DELETE FROM daegam_counters WHERE key LIKE %s", (f"{instance_id}:%",))


async def drop_redis_instance(client: Any, instance_id: str) -> None:
    """인스턴스 접두어 아래의 키와 인덱스(체크포인터 둘, 장기 기억 저장소 하나)를 지운다."""
    prefix = redis_prefix(instance_id)
    keys = [k async for k in client.scan_iter(f"{prefix}*")]
    if keys:
        await client.delete(*keys)
    for name in (f"{prefix}checkpoint", f"{prefix}checkpoint_write", f"{prefix}store"):
        try:
            await client.execute_command("FT.DROPINDEX", name)
        except ResponseError:
            pass


@dataclass(frozen=True)
class PgSchema:
    """pg_schema 가 준 스키마다. dsn 은 search_path 를 이 스키마 하나로 건 접속 문자열이라 그것으로
    연 연결이 만드는 표는 모두 이 스키마에 생긴다."""

    name: str
    dsn: str

    async def tables(self) -> list[str]:
        """이 스키마에 있는 표의 이름이다. 이름 순이다."""
        async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = %s ORDER BY table_name",
                (self.name,),
            )
            return [row[0] for row in await cur.fetchall()]


@asynccontextmanager
async def pg_schema(*, create: bool = True) -> AsyncIterator[PgSchema]:
    """이 테스트만의 Postgres 스키마를 새 이름으로 주고 끝나면 그 스키마를 통째로 지운다.

    빈 Database 가 필요한 테스트(동시 setup, 옛 표)가 public 이나 daegam_store 를 지우는 대신 여기서
    시작한다. create 가 거짓이면 이름만 정하고 만들지 않는다. 스키마가 없을 때를 보는 테스트가 그
    뒤에 만들어도 지우는 것은 여기다. 주소가 없으면 건너뛴다.
    """
    dsn = store_address("postgres")
    name = f"t_{uuid.uuid4().hex[:8]}"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        if create:
            await conn.execute(f"CREATE SCHEMA {name}")
        try:
            yield PgSchema(name=name, dsn=make_conninfo(dsn, options=f"-csearch_path={name}"))
        finally:
            await conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


@asynccontextmanager
async def schema_store(instance_id: str) -> AsyncIterator[SessionStore]:
    """이 테스트만의 스키마(pg_schema)에 Postgres 세션 저장소를 연다. setup 이 그 스키마에 표를
    만든다. 세션 id 는 인스턴스로 나누지 않는 기본 키라 고정한 sid 를 쓰는 테스트가 여기서 돈다."""
    async with pg_schema() as schema:
        store = await open_postgres_store(schema.dsn, instance_id=instance_id)
        try:
            yield store
        finally:
            await store.close()


@asynccontextmanager
async def store_for(kind: str, doc: dict[str, Any]) -> AsyncIterator[SessionStore | None]:
    """memory 면 None 이라 run_app 이 파드 안 저장소를 열고, 아니면 실제 저장소를 이 테스트의
    인스턴스로 연다(real_store)."""
    if kind == "memory":
        yield None
        return
    async with real_store(kind, doc) as store:
        yield store


@asynccontextmanager
async def pod_store(doc: dict[str, Any]) -> AsyncIterator[SessionStore]:
    """doc 으로 파드가 여는 세션 저장소를 열고 나갈 때 닫는다. memory 노드가 없는 문서면 메모리
    저장소다. 앱 여럿에 같이 넘기면 저장소를 나눠 쓰는 파드들이 된다."""
    store = await open_session_store(parse_config(doc))
    try:
        yield store
    finally:
        await store.close()


@asynccontextmanager
async def contract_store(kind: str) -> AsyncIterator[SessionStore]:
    """계약 테스트가 kind 의 세션 저장소를 이 테스트만의 자리에 연다. 계약은 고정한 sid 를 쓰고
    Postgres 의 전사·첨부 표는 인스턴스로 나누지 않으므로 Postgres 는 새 스키마(schema_store)이고
    Redis 는 새 인스턴스 접두어(real_store)다. memory 는 파드 안 저장소(pod_store)다."""
    if kind == "memory":
        opened = pod_store(load_fixture("minimal.json"))
    elif kind == "postgres":
        opened = schema_store("inst-1")
    else:
        opened = real_store(kind, load_fixture("minimal.json"))
    async with opened as store:
        yield store


# --- 장기 기억 저장소 열기 ------------------------------------------------------------------


def memory_backend(store: InMemoryStore | None = None) -> LongTermMemory:
    """InMemoryStore 위의 장기 기억이다. 테넌트는 tenant-a, 인스턴스는 inst-1 이다."""
    return LongTermMemory(
        store=store if store is not None else InMemoryStore(),
        tenant="tenant-a",
        instance_id="inst-1",
    )


@asynccontextmanager
async def real_memory(kind: str, doc: dict[str, Any]) -> AsyncIterator[LongTermMemory]:
    """실제 장기 기억 저장소(kind 는 postgres·redis)를 이 테스트만의 새 인스턴스 id 로 연다. doc 의
    instance.id 도 그 값으로 바꾸고(real_store 와 같다), 끝나면 그 인스턴스의 것만 지운다. Postgres
    는 전용 스키마의 store 표를 다른 실행과 나눠 쓰므로 그 인스턴스의 행만 지운다."""
    address = store_address(kind)
    _fresh_instance(doc)
    if kind == "postgres":
        # 런타임은 전용 스키마를 만들지 않는다. 기동 전에 있어야 한다
        async with await psycopg.AsyncConnection.connect(address, autocommit=True) as conn:
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {POSTGRES_SCHEMA}")
        memory = await open_pg_memory(doc)
    else:
        memory = await open_redis_memory(doc)
    try:
        yield memory
    finally:
        if kind == "postgres":
            async with memory._pool.connection() as conn:
                prefix = f"{memory.tenant}.{memory.instance_id}.%"
                await conn.execute("DELETE FROM store WHERE prefix LIKE %s", (prefix,))
        else:
            await drop_redis_instance(memory._client, memory.instance_id)
        await memory.close()


async def open_pg_memory(doc: dict[str, Any], dsn: str | None = None) -> LongTermMemory:
    """dsn(없으면 PG_DSN)이 가리키는 Postgres 에 장기 기억을 연다."""
    return await _open_with_env(doc, {"DAEGAM_DB_0_URL": dsn or PG_DSN})


async def open_redis_memory(doc: dict[str, Any]) -> LongTermMemory:
    doc = dict(doc)
    doc["memory"] = {
        "checkpointer": {"kind": "redis", "dsn_env": "DAEGAM_DB_0_URL"},
        "long_term": doc["memory"]["long_term"],
    }
    return await _open_with_env(doc, {"DAEGAM_DB_0_URL": REDIS_URL})


async def _open_with_env(doc: dict[str, Any], env: dict[str, Any]) -> LongTermMemory:
    old = {key: os.environ.get(key) for key in env}
    os.environ.update({key: str(value) for key, value in env.items()})
    try:
        memory = await open_long_term(parse_config(doc), tenant="tenant-a")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert memory is not None
    return memory
