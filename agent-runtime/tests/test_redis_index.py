"""Redis 세션 색인(D33 F2)을 실제 Redis 8 에서 본다. `DAEGAM_TEST_REDIS_URL` 이 없으면 건너뛴다.
색인의 계약은 메모리·Postgres 와 같은 것(store_fixtures.py)을 걸고, 그 밖에 Redis 에만 있는 것을
본다. 리스와 중지 표시의 만료, sweep 과 턴 끝의 경주, 해시 왕복, 정렬 집합, 실행 키, 페이지 단위의
제목 검색이다."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from runtime import session_index_redis
from runtime.dsn import redis_prefix
from runtime.session_index_redis import EXPIRE_LUA
from runtime.sessions import SessionIndex, SessionRecord, utcnow
from tests.conftest import needs_redis
from tests.store_fixtures import (
    assert_a_task_session_holds_several_runs,
    assert_cancel_mark_follows_the_lease_token,
    assert_expire_skips_a_leased_session,
    assert_filters_updates_and_counts,
    assert_holds_one_session_per_run,
    assert_lists_run_sessions_on_request,
    assert_purges_only_closed_rows,
    assert_renews_turns_by_token,
    assert_running_and_the_named_stop,
    assert_title_search,
    run_record,
    session_record,
)

T1 = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 9, 12, 5, 30, tzinfo=UTC)


@needs_redis
async def test_real_redis_reports_running_and_the_named_stop(redis_store) -> None:
    """실제 Redis 에서 running 은 리스 키의 EXISTS 이고, 이름을 준 중지는 CANCEL_LUA 가 세션 해시의
    turn_id 를 읽으며, cancel_all 은 인스턴스 접두어 아래 리스 키를 SCAN 한다. 해시에 running 은
    쓰지 않는다."""
    store, _ = redis_store
    await assert_running_and_the_named_stop(store.index)
    index = store.index
    await index.create(session_record("s-hash", "alice"))
    assert "running" not in await store._client.hgetall(f"{index._prefix}session:s-hash")


@needs_redis
async def test_real_cancel_mark_follows_the_lease_token(redis_store) -> None:
    """실제 Redis 에서 Lua 둘이 돈다. 표시는 리스의 남은 만료를 물려받는다."""
    store, _ = redis_store
    await assert_cancel_mark_follows_the_lease_token(store.index)
    index = store.index
    await index.create(session_record("s-ttl", "alice"))
    assert await index.claim_turn("s-ttl", lease_seconds=30, token="t1") is True
    assert await index.request_cancel("s-ttl") == "t1"
    mark_ttl = await store._client.pttl(f"{index._prefix}turn_cancel:s-ttl")
    assert 0 < mark_ttl <= 30_000
    await index.release_turn("s-ttl", "t1")
    assert await store._client.exists(f"{index._prefix}turn_cancel:s-ttl") == 0


@needs_redis
async def test_real_redis_renews_the_lease_and_its_stop_mark_by_token(redis_store) -> None:
    """실제 Redis 에서 갱신은 리스마다 RENEW_LUA 를 파이프라인 하나에 싣는다. 리스와 같은 토큰의
    중지 표시를 함께 늘리고, 만료된 리스나 지운 세션 해시에는 아무 키도 만들지 않는다."""
    store, _ = redis_store
    index = store.index
    client = store._client
    await assert_renews_turns_by_token(index)
    await index.create(session_record("s-renew", "alice"))
    assert await index.claim_turn("s-renew", lease_seconds=1, token="t1") is True
    assert await index.request_cancel("s-renew") == "t1"
    assert await index.renew_turns({"s-renew": "t1"}, lease_seconds=60) == {"s-renew"}
    for key in ("turn_lease:s-renew", "turn_cancel:s-renew"):
        assert 30_000 < await client.pttl(f"{index._prefix}{key}") <= 60_000
    # 만료된 리스는 되살리지 않는다
    await index.create(session_record("s-lapsed", "alice"))
    assert await index.claim_turn("s-lapsed", lease_seconds=0.05, token="t2") is True
    await asyncio.sleep(0.1)
    assert await index.renew_turns({"s-lapsed": "t2"}, lease_seconds=60) == set()
    assert await client.exists(f"{index._prefix}turn_lease:s-lapsed") == 0
    # 세션 해시가 지워진 리스는 늘리지 않는다
    await client.delete(f"{index._prefix}session:s-renew")
    assert await index.renew_turns({"s-renew": "t1"}, lease_seconds=60) == set()
    assert await client.exists(f"{index._prefix}session:s-renew") == 0


@needs_redis
async def test_real_expire_race_keeps_session_that_just_finished_a_turn(redis_store) -> None:
    store, instance_id = redis_store
    index = store.index
    client = store._client
    prefix = redis_prefix(instance_id)
    old = utcnow() - timedelta(seconds=4000)
    before = utcnow() - timedelta(seconds=3600)
    await index.create(SessionRecord("s1", None, "alice", old, old))
    active = f"{prefix}sessions:status:active"
    # sweep 이 후보를 읽는다
    candidates = await client.zrangebyscore(active, "-inf", f"({before.timestamp()}")
    assert candidates == ["s1"]
    # 그 사이 다른 파드가 턴을 끝낸다
    now = utcnow()
    await index.update(
        "s1",
        last_turn_at=now,
        status="active",
        turns=1,
        usage_input=1,
        usage_output=1,
        pending_since=None,
    )
    # 점수 조건부 스크립트는 닫지 않는다
    script = client.register_script(EXPIRE_LUA)
    result = await script(
        keys=[
            active,
            f"{prefix}sessions:status:closed",
            f"{prefix}session:s1",
            f"{prefix}turn_lease:s1",
        ],
        args=["s1", before.timestamp()],
    )
    assert int(result) == 0
    assert (await index.get("s1")).status == "active"
    assert await index.expire(before) == []
    # 점수가 아직 옛 것인 세션은 닫힌다
    await index.create(SessionRecord("s2", None, "bob", old, old, status="pending"))
    assert await index.expire(before) == ["s2"]
    assert (await index.get("s2")).status == "closed"
    assert await index.counts() == {"active": 1, "pending": 0, "closed": 1}
    assert await client.zscore(f"{prefix}sessions:status:closed", "s2") == pytest.approx(
        old.timestamp()
    )


@needs_redis
async def test_real_index_round_trip(redis_store) -> None:
    store, _ = redis_store
    index = store.index
    old = utcnow() - timedelta(seconds=60)
    await index.create(SessionRecord("s1", None, "alice", old, old))
    await index.create(SessionRecord("s2", None, "bob", old, old))
    now = utcnow()
    updated = await index.update(
        "s1",
        last_turn_at=now,
        turns=1,
        usage_input=10,
        usage_output=5,
        status="pending",
        pending_since=now,
    )
    assert updated is not None and updated.turns == 1 and updated.pending_since == now
    assert await index.update("nope", last_turn_at=now, turns=1) is None
    assert [r.session_id for r in await index.list()] == ["s1", "s2"]
    assert [r.session_id for r in await index.list(user_sub="alice")] == ["s1"]
    assert [r.session_id for r in await index.list(status="pending")] == ["s1"]
    assert [r.session_id for r in await index.list(status="active")] == ["s2"]
    assert await index.counts() == {"active": 1, "pending": 1, "closed": 0}
    assert await index.totals() == {
        "sessions": 2,
        "turns": 1,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    assert await index.claim_turn("s1", lease_seconds=30, token="a") is True
    assert await index.claim_turn("s1", lease_seconds=30, token="b") is False
    assert await index.claim_turn("s2", lease_seconds=30, token="c") is True
    await index.release_turn("s1", "a")
    assert await index.claim_turn("s1", lease_seconds=30, token="d") is True
    # 세션 해시가 없으면 잡지 않는다(P79 1절)
    assert await index.claim_turn("s3", lease_seconds=30, token="e") is False
    # 임차는 만료가 있다. 풀지 못하고 만료된 리스는 열린 표시를 남겨 되살리기 전에는 다시 잡히지
    # 않는다(turn_repair.py)
    await index.create(SessionRecord("s3", None, "alice", old, old))
    assert await index.claim_turn("s3", lease_seconds=0.05, token="e") is True
    await asyncio.sleep(0.1)
    assert await index.claim_turn("s3", lease_seconds=30, token="f") is False
    lapsed = await index.get("s3")
    assert lapsed is not None and (lapsed.running, lapsed.orphan) == (False, "e")


@needs_redis
@pytest.mark.parametrize(
    "contract",
    [
        assert_filters_updates_and_counts,
        assert_purges_only_closed_rows,
        assert_title_search,
        assert_holds_one_session_per_run,
        assert_a_task_session_holds_several_runs,
        assert_lists_run_sessions_on_request,
        assert_expire_skips_a_leased_session,
    ],
    ids=[
        "filters",
        "purge",
        "titles",
        "one-session-per-run",
        "task-runs",
        "runs-filter",
        "expire-skips-leased",
    ],
)
async def test_real_index_keeps_the_shared_contract(
    redis_store, contract: Callable[[SessionIndex], Awaitable[None]]
) -> None:
    """메모리·Postgres 색인과 같은 계약을 실제 Redis 에서 본다."""
    store, _ = redis_store
    await contract(store.index)


@needs_redis
async def test_real_record_round_trip(redis_store) -> None:
    """세션 한 줄이 해시에서 그대로 돌아온다. 테넌트·보류 시각·턴 기록 칸이 실리고 비운 칸은 None
    으로 돌아오며, 인스턴스를 주지 않은 줄은 색인의 인스턴스를 받는다. None 으로 갱신한 칸은
    지워지고 필드 없이 부른 갱신은 읽기다."""
    store, instance_id = redis_store
    index = store.index
    full = SessionRecord(
        "s1",
        "tenant-a",
        "bob",
        T1,
        T2,
        turns=2,
        usage_input=3,
        usage_output=4,
        status="pending",
        pending_since=T2,
        instance_id="x",
        title="환불 문의",
        turn_id="t" * 32,
        last_turn=2,
        upload_bytes=10,
        cite_seq=3,
        run_id="run-1",
        untrusted=True,
    )
    await index.create(full)
    raw = await store._client.hgetall(f"{index._prefix}session:s1")
    assert (raw["run_id"], raw["untrusted"]) == ("run-1", "1")
    assert await index.get("s1") == full
    assert await index.update("s1") == full
    bare = SessionRecord("s2", None, "alice", T1, T1)
    await index.create(bare)
    assert await index.get("s2") == replace(bare, instance_id=instance_id)
    raw = await store._client.hgetall(f"{index._prefix}session:s2")
    assert "run_id" not in raw and raw["untrusted"] == "0"
    assert await index.get("missing") is None
    resumed = await index.update("s1", last_turn_at=T2, status="active", pending_since=None)
    assert resumed == replace(full, status="active", pending_since=None)
    assert await index.get("s1") == resumed


@needs_redis
async def test_real_update_refuses_before_writing(redis_store) -> None:
    """상태를 옮기려면 정렬 집합의 점수가 될 last_turn_at 이 함께 와야 하고, 모르는 상태와 만들 때
    정해지는 필드는 받지 않는다. 거절은 아무것도 쓰기 전이다."""
    store, instance_id = redis_store
    index = store.index
    record = SessionRecord("s1", None, "alice", T1, T1, instance_id=instance_id)
    await index.create(record)
    for fields in (
        {"status": "closed"},
        {"last_turn_at": T2, "status": "bogus"},
        {"last_turn_at": T2, "user_sub": "mallory"},
        {"last_turn_at": None},
    ):
        with pytest.raises(ValueError):
            await index.update("s1", **fields)
    assert await index.get("s1") == record
    assert await index.counts() == {"active": 1, "pending": 0, "closed": 0}


@needs_redis
async def test_real_update_without_status_moves_only_its_own_status_set(redis_store) -> None:
    """턴 오류 경로는 지금 상태를 모른 채 last_turn_at·usage 만 고친다. 세션은 제 상태 집합에만 남고
    그 점수도 새 시각이라 방금 턴을 끝낸 세션을 sweep 이 닫지 않는다."""
    store, _ = redis_store
    index = store.index
    old = utcnow() - timedelta(seconds=4000)
    await index.create(SessionRecord("s1", None, "alice", old, old, status="pending"))
    await index.update("s1", last_turn_at=utcnow(), usage_input=1, usage_output=2)
    assert await index.counts() == {"active": 0, "pending": 1, "closed": 0}
    assert await index.expire(utcnow() - timedelta(seconds=3600)) == []


@needs_redis
async def test_real_delete_removes_the_session_from_every_key(redis_store) -> None:
    """색인 행을 지우면 해시·리스·중지 표시와 정렬 집합에서 함께 빠진다. 한 곳이라도 남으면 목록이
    없는 세션을 보이거나 sweep 이 같은 후보를 영영 다시 집는다. 없는 세션을 지워도 탈이 없다."""
    store, instance_id = redis_store
    index = store.index
    await index.create(SessionRecord("s1", None, "alice", T1, T1))
    await index.create(run_record("s2", "alice", "run-1"))
    assert await index.claim_turn("s1", lease_seconds=30, token="t1") is True
    assert await index.request_cancel("s1") == "t1"
    assert await store._client.get(f"{index._prefix}run:run-1") == "s2"
    await index.delete("s1")
    await index.delete("s2")
    assert [key async for key in store._client.scan_iter(f"{redis_prefix(instance_id)}*")] == []
    await index.delete("s1")


@needs_redis
async def test_real_run_sessions_keep_their_own_sorted_sets(redis_store) -> None:
    """실행 세션은 최근 순·사용자별 정렬 집합을 따로 두고, 채팅 세션은 그 집합에 들지 않는다(P41
    9절). 상태 집합은 둘 다 든다. 만들기, 쓰기, 지우기가 세션이 속한 쪽의 집합을 따르고 합계는
    채팅과 실행을 다 센다."""
    store, _ = redis_store
    index, client, prefix = store.index, store._client, store.index._prefix

    async def members(suffix: str) -> list[str]:
        return list(await client.zrange(f"{prefix}{suffix}", 0, -1))

    await index.create(session_record("chat", "alice", age=20))
    await index.create(run_record("run", "alice", "run-1", age=10))
    now = utcnow()
    await index.update("run", last_turn_at=now, status="pending", pending_since=now, turns=2)
    await index.update("chat", last_turn_at=now, turns=1)
    assert await members("sessions:by_turn") == ["chat"]
    assert await members("sessions:user:alice") == ["chat"]
    assert await members("sessions:runs:by_turn") == ["run"]
    assert await members("sessions:runs:user:alice") == ["run"]
    assert await members("sessions:status:pending") == ["run"]
    assert await members("sessions:status:active") == ["chat"]
    assert await index.totals() == {
        "sessions": 2,
        "turns": 3,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    await index.delete("run")
    assert await members("sessions:runs:by_turn") == []
    assert await members("sessions:runs:user:alice") == []
    assert await members("sessions:by_turn") == ["chat"]


@needs_redis
async def test_real_include_merges_chat_and_run_sets_by_recency(redis_store) -> None:
    """include 는 채팅과 실행의 집합을 읽어 최근 순으로 합치고 limit 를 지킨다. 전체 목록은 집합마다
    앞 limit 줄만 읽어 합치고, 사용자 목록은 채팅 집합을 모두, 실행 집합을 limit 까지 읽어
    합친다(P41 9절)."""
    store, _ = redis_store
    index = store.index
    for n in range(6):
        age = 60 - n * 10
        if n % 2:
            await index.create(run_record(f"r{n}", "alice", f"run-{n}", age=age))
        else:
            await index.create(session_record(f"c{n}", "alice", age=age))

    async def listed(**kwargs: Any) -> list[str]:
        return [row.session_id for row in await index.list(**kwargs)]

    assert await listed(runs="include") == ["r5", "c4", "r3", "c2", "r1", "c0"]
    assert await listed(runs="include", limit=3) == ["r5", "c4", "r3"]
    assert await listed(runs="include", user_sub="alice", limit=4) == ["r5", "c4", "r3", "c2"]
    assert await listed() == ["c4", "c2", "c0"]
    assert await listed(runs="only", limit=2) == ["r5", "r3"]
    assert await listed(runs="include", status="active", limit=2) == ["r5", "c4"]


@needs_redis
async def test_real_title_search_pages_the_recent_set_until_the_limit(
    redis_store, monkeypatch
) -> None:
    """승인자의 전체 목록에서 제목을 검색하면 최근 순 집합을 페이지 단위로 읽어 limit 개를 채우거나
    훑는 상한에서 멈춘다. 사용자를 주면 그 사람의 세션을 모두 읽고 거른다(P36 4절)."""
    monkeypatch.setattr(session_index_redis, "REDIS_SEARCH_PAGE", 2)
    monkeypatch.setattr(session_index_redis, "REDIS_SEARCH_SCAN_LIMIT", 6)
    store, _ = redis_store
    index = store.index
    titles = {"s1": "환불 문의", "s4": "환불 기한", "s5": "로그인", "s8": "환불 영수증"}
    now = utcnow()
    # s0 이 가장 최근이다
    for n in range(9):
        sid, when = f"s{n}", now - timedelta(minutes=n)
        await index.create(SessionRecord(sid, None, "alice", when, when, title=titles.get(sid)))

    async def found(**kwargs: Any) -> list[str]:
        return [row.session_id for row in await index.list(q="환불", **kwargs)]

    assert await found(limit=2) == ["s1", "s4"]
    # 상한(6줄)까지만 훑으므로 s8 은 보지 못한다
    assert await found(limit=10) == ["s1", "s4"]
    assert await found(user_sub="alice") == ["s1", "s4", "s8"]


async def test_redis_held_writes_refuse_an_empty_token() -> None:
    # 빈 토큰은 스크립트가 검사 없는 쓰기로 읽는다. 저장소에 닿기 전에 막는다
    from runtime.session_index_redis import RedisSessionIndex
    from runtime.transcript import RedisTranscripts

    index = RedisSessionIndex.__new__(RedisSessionIndex)
    transcripts = RedisTranscripts.__new__(RedisTranscripts)
    with pytest.raises(ValueError):
        await index.update_held("s1", "", turns=1)
    with pytest.raises(ValueError):
        await transcripts.append_held("s1", [{"role": "user"}], "")
