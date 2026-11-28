"""세션 색인(인메모리)과 TTL sweep, 저장소 종류 분기, 턴 리스의 토큰과 중지 표시(D65)를 본다. 색인
셋이 같이 지키는 계약은 store_fixtures.py 에 있다. Postgres 색인은 같은 계약을 실제 표에서 보고
`DAEGAM_TEST_PG_DSN` 이 없으면 건너뛴다. Redis 색인은 test_redis_index.py 가, DSN·Redis 인자 조립은
test_dsn.py 가, 턴 기록의 색인 칸과 제목·검색(P36 3절)은 test_turn_record.py 가 본다."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from runtime.compile import compile_agent
from runtime.config import ConfigError, parse_config
from runtime.counters import COUNTERS_DDL
from runtime.fence import TurnFenced
from runtime.sessions import (
    DEFAULT_TRANSCRIPT_RETENTION_DAYS,
    NO_SHARED_STATE,
    RUN_RECORD_HOLD,
    SESSIONS_DDL,
    STATUS_CLOSED,
    MemorySessionIndex,
    PostgresSessionIndex,
    SessionIndex,
    ddl_statements,
    memory_kind,
    open_session_store,
    transcript_retention_days,
    utcnow,
)
from runtime.state import Settings
from runtime.transcript import TRANSCRIPTS_DDL
from tests.app_fixtures import sid_of
from tests.attachment_fixtures import stored
from tests.conftest import chat, load_fixture, needs_pg, offline_doc, run_app
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
    run_record,
    schema_store,
    session_record,
)


async def test_open_session_store_redis_kind_opens_redis_store(monkeypatch) -> None:
    doc = load_fixture("redis-memory.json")
    config = parse_config(doc)
    assert memory_kind(config) == "redis"
    calls: list[tuple[dict[str, Any], str]] = []

    async def fake_open_redis_store(conn: dict[str, Any], *, instance_id: str) -> str:
        calls.append((conn, instance_id))
        return "store"

    monkeypatch.setattr("runtime.session_index_redis.open_redis_store", fake_open_redis_store)
    monkeypatch.delenv("DAEGAM_DB_0_HOST", raising=False)
    monkeypatch.setenv("DAEGAM_DB_0_URL", "redis://redis.svc:6379/0")
    assert await open_session_store(config) == "store"
    assert calls == [({"url": "redis://redis.svc:6379/0"}, "ai-support-0001")]
    # 개별 env 가 URL 보다 앞선다
    monkeypatch.setenv("DAEGAM_DB_0_HOST", "redis.svc")
    monkeypatch.setenv("DAEGAM_DB_0_DB", "3")
    await open_session_store(config)
    assert calls[-1] == ({"host": "redis.svc", "port": 6379, "db": 3}, "ai-support-0001")
    # 둘 다 없으면 postgres 와 같은 코드다
    for name in ("DAEGAM_DB_0_URL", "DAEGAM_DB_0_HOST", "DAEGAM_DB_0_DB"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as info:
        await open_session_store(config)
    assert info.value.code == "memory_dsn_missing"


def test_memory_kind_names_postgres_and_none() -> None:
    doc = load_fixture("support-agent.json")
    assert memory_kind(parse_config(doc)) == "postgres"
    doc["memory"] = None
    assert memory_kind(parse_config(doc)) == "none"


def test_ddl_splits_into_single_statements() -> None:
    """prepare_threshold 0 인 풀은 문장 하나짜리 준비문만 받으므로 DDL 은 문장 단위여야 한다."""
    sessions = ddl_statements(SESSIONS_DDL)
    assert len(sessions) == 8 and all(";" not in s for s in sessions)
    assert sessions[0].startswith("CREATE TABLE IF NOT EXISTS daegam_sessions")
    assert [s.split(" ON ")[0].split()[-1] for s in sessions[1:5]] == [
        "daegam_sessions_user",
        "daegam_sessions_status",
        "daegam_sessions_instance",
        "daegam_sessions_run",
    ]
    # 실행 id 하나에 세션 하나다. 채팅 세션(실행 id 없음)은 막지 않는다(P41 9절)
    assert sessions[4].startswith("CREATE UNIQUE INDEX IF NOT EXISTS daegam_sessions_run")
    assert sessions[4].endswith("WHERE run_id IS NOT NULL")
    # 실행 표식은 제 표에 두고 색인 표의 열은 늘리지 않는다. sweep 의 후보는 부분 인덱스로 찾는다
    # (P79 3절)
    assert sessions[5].startswith("CREATE TABLE IF NOT EXISTS daegam_turn_runs")
    assert sessions[6].startswith("CREATE INDEX IF NOT EXISTS daegam_sessions_open")
    assert sessions[6].endswith("WHERE turn_token IS NOT NULL")
    # 인스턴스의 drain 표시도 제 표에 둔다(P44 7절)
    assert sessions[7].startswith("CREATE TABLE IF NOT EXISTS daegam_drains")
    counters = ddl_statements(COUNTERS_DDL)
    assert len(counters) == 1
    assert counters[0].startswith("CREATE TABLE IF NOT EXISTS daegam_counters")
    # 전사 표도 색인 setup 이 나눠 실행한다
    transcripts = ddl_statements(TRANSCRIPTS_DDL)
    assert len(transcripts) == 2 and all(";" not in s for s in transcripts)
    assert transcripts[0].startswith("CREATE TABLE IF NOT EXISTS daegam_transcripts")
    assert transcripts[1].startswith("CREATE INDEX IF NOT EXISTS daegam_transcripts_session")


async def test_new_session_carries_instance_id(support_doc) -> None:
    """새 세션의 행은 문서의 인스턴스 id 와 설정(DAEGAM_TENANT)의 테넌트를 싣는다."""
    async with run_app(support_doc, settings=Settings(tenant="tenant-a")) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        row = (await running.client.get(f"/sessions/{sid}")).json()["session"]
        assert row["instance_id"] == support_doc["instance"]["id"]
        assert row["tenant"] == "tenant-a"


async def test_open_session_store_without_dsn_is_config_error(monkeypatch) -> None:
    doc = load_fixture("support-agent.json")
    for name in ("DAEGAM_DB_0_URL", "DAEGAM_DB_0_HOST"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as info:
        await open_session_store(parse_config(doc))
    assert info.value.code == "memory_dsn_missing"
    assert "DAEGAM_DB_0_HOST" in info.value.message


async def test_memory_index_filters_updates_and_counts() -> None:
    await assert_filters_updates_and_counts(MemorySessionIndex())


async def test_memory_index_purges_only_closed_rows() -> None:
    await assert_purges_only_closed_rows(MemorySessionIndex())


@pytest.mark.parametrize(
    "contract",
    [
        assert_holds_one_session_per_run,
        assert_a_task_session_holds_several_runs,
        assert_lists_run_sessions_on_request,
        assert_expire_skips_a_leased_session,
    ],
    ids=["one-session-per-run", "task-runs", "runs-filter", "expire-skips-leased"],
)
async def test_memory_index_keeps_the_run_contract(
    contract: Callable[[SessionIndex], Awaitable[None]],
) -> None:
    await contract(MemorySessionIndex())


async def _memory_store_with_two_sessions():
    """세션 둘(오래된 것·방금 것)에 체크포인트, 전사, 첨부를 남긴 인메모리 저장소다."""
    doc = offline_doc()
    config = parse_config(doc)
    store = await open_session_store(config)
    assert store.shared is False and store.warnings == [NO_SHARED_STATE]

    agent = compile_agent(config, tools_by_ref={}, checkpointer=store.checkpointer)
    for sid, age in (("old", 4000), ("fresh", 10)):
        await store.index.create(session_record(sid, "alice", age=age))
        await store.transcripts.append(sid, [{"kind": "user", "text": sid}])
        await store.uploads.add(sid, [stored(f"{sid}-upload")])
        await agent.graph.ainvoke(
            {"messages": [HumanMessage("hi")]}, config={"configurable": {"thread_id": sid}}
        )
    return config, store, agent


async def test_sweep_closes_expired_and_keeps_the_record() -> None:
    """세션 TTL 이 지나면 닫고 체크포인트와 첨부를 지운다. 전사와 색인 행은 그대로다(D46)."""
    config, store, agent = await _memory_store_with_two_sessions()
    ttl = config.entry["session_ttl_seconds"]
    assert await store.sweep(ttl) == ["old"]
    assert (await store.index.get("old")).status == STATUS_CLOSED
    assert (await store.index.get("fresh")).status == "active"
    old_state = await agent.graph.aget_state({"configurable": {"thread_id": "old"}})
    assert not old_state.values
    fresh_state = await agent.graph.aget_state({"configurable": {"thread_id": "fresh"}})
    assert fresh_state.values["messages"]
    assert await store.uploads.read("old") == []
    assert [u.upload_id for u in await store.uploads.read("fresh")] == ["fresh-upload"]
    # 닫혀도 기록은 남아 목록에서 열 수 있다
    assert await store.transcripts.read("old") == [{"kind": "user", "text": "old"}]
    # 이미 닫힌 세션은 다시 나오지 않는다
    assert await store.sweep(ttl) == []
    assert (await store.index.get("old")) is not None
    await store.close()


async def test_sweep_deletes_the_record_past_retention() -> None:
    """보존 기간이 지나야 전사와 색인 행이 함께 사라진다. 살아 있는 세션은 건드리지 않는다."""
    config, store, _ = await _memory_store_with_two_sessions()
    ttl = config.entry["session_ttl_seconds"]
    assert await store.sweep(ttl, retention_days=30) == ["old"]
    assert (await store.index.get("old")).status == STATUS_CLOSED
    assert await store.transcripts.read("old")

    # 보존 기간이 0 이면 닫힌 세션의 기록을 그 자리에서 지운다
    assert await store.sweep(ttl, retention_days=0) == []
    assert await store.index.get("old") is None
    assert await store.transcripts.read("old") == []
    assert await store.index.get("fresh") is not None
    assert await store.transcripts.read("fresh")
    await store.close()


async def test_drop_state_deletes_checkpoints_and_uploads_and_keeps_the_record() -> None:
    """턴이 대기 없이 끝난 실행 세션은 TTL 로 닫힌 세션과 같은 차례로 상태를 지운다(P41 9절).
    체크포인트와 첨부만 지우고 전사와 색인 행은 남는다. 하나를 지우지 못해도 다른 것은 지우고
    올리지 않는다."""
    _, store, agent = await _memory_store_with_two_sessions()
    await store.drop_state("fresh")
    state = await agent.graph.aget_state({"configurable": {"thread_id": "fresh"}})
    assert not state.values
    assert await store.uploads.read("fresh") == []
    assert await store.transcripts.read("fresh") == [{"kind": "user", "text": "fresh"}]
    assert await store.index.get("fresh") is not None

    async def broken(session_id: str) -> None:
        raise RuntimeError("checkpointer down")

    store.delete_thread = broken
    await store.drop_state("old")
    assert await store.uploads.read("old") == []
    await store.close()


async def test_sweep_with_zero_retention_closes_and_deletes_at_once() -> None:
    """보존 기간 0 은 D46 앞의 동작이다. 닫는 그 sweep 이 기록까지 지운다."""
    config, store, _ = await _memory_store_with_two_sessions()
    assert await store.sweep(config.entry["session_ttl_seconds"], retention_days=0) == ["old"]
    assert await store.index.get("old") is None
    assert await store.transcripts.read("old") == []
    await store.close()


async def test_zero_retention_keeps_a_closed_run_session_for_the_hold() -> None:
    """보존 기간이 0 이어도 닫힌 실행 세션의 기록은 RUN_RECORD_HOLD 동안 남아 run_id 를 쥔다(P41
    9절). 그보다 오래 닫힌 실행 세션은 지우고, 기록 지우기는 닫을 때 남은 체크포인트까지 지운다."""
    config, store, agent = await _memory_store_with_two_sessions()
    stale = int(RUN_RECORD_HOLD.total_seconds()) + 60
    for sid, age in (("run", 0), ("stale-run", stale)):
        await store.index.create(
            run_record(sid, "alice", f"{sid}-id", age=age, status=STATUS_CLOSED)
        )
        await agent.graph.ainvoke(
            {"messages": [HumanMessage("hi")]}, config={"configurable": {"thread_id": sid}}
        )
    await store.sweep(config.entry["session_ttl_seconds"], retention_days=0)
    assert (await store.index.find_run("run-id")).session_id == "run"
    assert await store.index.find_run("stale-run-id") is None
    assert await store.index.get("old") is None
    state = await agent.graph.aget_state({"configurable": {"thread_id": "stale-run"}})
    assert not state.values
    await store.close()


def test_transcript_retention_days_reads_the_env() -> None:
    """env 가 비었거나 정수가 아니거나 음수면 기본값이다. 0 은 뜻이 있는 값이라 그대로 쓴다."""
    assert transcript_retention_days("7") == 7
    assert transcript_retention_days("0") == 0
    assert transcript_retention_days(None) == DEFAULT_TRANSCRIPT_RETENTION_DAYS
    assert transcript_retention_days("") == DEFAULT_TRANSCRIPT_RETENTION_DAYS
    assert transcript_retention_days("서른") == DEFAULT_TRANSCRIPT_RETENTION_DAYS
    assert transcript_retention_days("-1") == DEFAULT_TRANSCRIPT_RETENTION_DAYS


async def test_closed_session_refuses_a_turn_but_still_reads(support_doc) -> None:
    """닫힌 대화는 이어 쓸 수 없고(410 session_closed) 읽기는 200 이다(D46)."""
    async with run_app(support_doc) as running:
        _, sse = await chat(running, "hi")
        sid = sid_of(sse, "done")
        await running.runtime.store.index.update(sid, status=STATUS_CLOSED)
        response, _ = await chat(running, "hi", session_id=sid)
        assert response.status_code == 410 and response.json()["code"] == "session_closed"

        detail = await running.client.get(f"/sessions/{sid}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["session"]["status"] == STATUS_CLOSED
        assert [i["kind"] for i in body["transcript"]["items"]][0] == "user"


@pytest.mark.asyncio
async def test_memory_index_claims_one_turn_at_a_time() -> None:
    """진행 중 턴 막기가 색인의 몫이다.

    파드 로컬 집합으로 막던 것을 색인으로 옮겼다. Postgres 색인에서는 조건부 UPDATE 가
    같은 세션의 두 턴을 파드가 달라도 하나로 막는다. 여기서는 계약만 고정한다.
    """
    index = MemorySessionIndex()
    assert await index.claim_turn("s1", lease_seconds=150, token="a") is True
    assert await index.claim_turn("s1", lease_seconds=150, token="b") is False, (
        "두 번째 턴이 통과했다"
    )
    # 다른 세션은 서로 막지 않는다
    assert await index.claim_turn("s2", lease_seconds=150, token="c") is True
    await index.release_turn("s1", "a")
    assert await index.claim_turn("s1", lease_seconds=150, token="d") is True
    # 놓은 적 없는 세션을 놓아도 조용하다
    await index.release_turn("nope", "x")


async def test_memory_index_cancel_mark_follows_the_lease_token() -> None:
    await assert_cancel_mark_follows_the_lease_token(MemorySessionIndex())


@needs_pg
async def test_real_postgres_cancel_mark_follows_the_lease_token() -> None:
    """실제 표에서 리스·표시의 UPDATE 셋이 돈다. 색인 행이 있어야 UPDATE 가 행을 잡는다."""
    async with schema_store("inst-1") as store:
        await assert_cancel_mark_follows_the_lease_token(store.index)


async def test_memory_index_reports_running_and_the_named_stop() -> None:
    await assert_running_and_the_named_stop(MemorySessionIndex())


@needs_pg
async def test_real_postgres_reports_running_and_the_named_stop() -> None:
    """실제 표에서 running 은 SELECT 의 계산 열이고, 이름을 준 중지와 cancel_all 은 UPDATE
    하나다."""
    async with schema_store("inst-1") as store:
        await assert_running_and_the_named_stop(store.index)


async def test_memory_index_renews_turns_by_token() -> None:
    await assert_renews_turns_by_token(MemorySessionIndex())


@needs_pg
async def test_real_postgres_renews_turns_by_token_and_never_revives_a_lapsed_lease() -> None:
    """실제 표에서 갱신은 UPDATE ... FROM unnest 한 문장이다. 늘린 리스는 그만큼 살고, 이미 만료된
    리스는 토큰이 같아도 되살리지 않는다. 다른 인스턴스의 행은 늘리지 않는다."""
    async with schema_store("inst-1") as store:
        index = store.index
        await assert_renews_turns_by_token(index)
        for sid in ("s-renew", "s-lapsed"):
            await index.create(session_record(sid, "alice"))
        assert await index.claim_turn("s-renew", lease_seconds=1, token="t1") is True
        assert await index.renew_turns({"s-renew": "t1"}, lease_seconds=60) == {"s-renew"}
        assert await index.claim_turn("s-lapsed", lease_seconds=-1, token="t2") is True
        assert await index.renew_turns({"s-lapsed": "t2"}, lease_seconds=60) == set()
        async with store._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, turn_lease > now() + interval '30 seconds' AS long "
                "FROM daegam_sessions WHERE session_id IN ('s-renew', 's-lapsed') "
                "ORDER BY session_id"
            )
            assert [(r["session_id"], r["long"]) for r in await cur.fetchall()] == [
                ("s-lapsed", False),
                ("s-renew", True),
            ]
        other = PostgresSessionIndex(store._pool, "inst-2")
        assert await other.renew_turns({"s-renew": "t1"}, lease_seconds=60) == set()


@needs_pg
@pytest.mark.parametrize(
    "contract",
    [
        assert_filters_updates_and_counts,
        assert_purges_only_closed_rows,
        assert_holds_one_session_per_run,
        assert_a_task_session_holds_several_runs,
        assert_lists_run_sessions_on_request,
        assert_expire_skips_a_leased_session,
    ],
    ids=[
        "filters",
        "purge",
        "one-session-per-run",
        "task-runs",
        "runs-filter",
        "expire-skips-leased",
    ],
)
async def test_real_postgres_index_keeps_the_shared_contract(
    contract: Callable[[SessionIndex], Awaitable[None]],
) -> None:
    """메모리 색인과 같은 계약을 실제 표에서 본다. 이 테스트만의 스키마라 고정한 sid 가 다른
    테스트와 겹치지 않는다."""
    async with schema_store("inst-1") as store:
        await contract(store.index)


@needs_pg
async def test_real_postgres_instances_sharing_a_database_do_not_mix() -> None:
    """같은 Database 를 나눠 쓰는 인스턴스끼리 색인이 섞이지 않는다. 조회·갱신·리스·중지·sweep·
    삭제·개수가 모두 instance_id 로 갈린다. 한 곳만 빠져도 남의 세션을 보거나 조용히 건드린다.
    인스턴스를 주지 않은 색인(같은 Database 를 혼자 쓰는 배포본)은 전체를 본다."""
    async with schema_store("inst-a") as store:
        mine, pool = store.index, store._pool
        theirs = PostgresSessionIndex(pool, "inst-b")
        await theirs.create(session_record("open", "alice", age=4000))
        await theirs.create(session_record("shut", "alice", age=4000, status=STATUS_CLOSED))
        await theirs.create(run_record("ran", "alice", "run-b", status=STATUS_CLOSED))
        assert await theirs.claim_turn("open", lease_seconds=30, token="t1") is True
        assert await theirs.request_cancel("open") == "t1"

        # 다른 인스턴스의 행은 보이지 않고 어떤 호출로도 바뀌지 않는다
        assert await mine.get("open") is None
        assert await mine.list() == [] and await mine.list(user_sub="alice") == []
        assert await mine.list(runs="only") == [] and await mine.find_run("run-b") is None
        assert await mine.update("open", turns=5) is None
        assert await mine.claim_turn("shut", lease_seconds=30, token="t2") is False
        assert await mine.request_cancel("open") is None
        assert await mine.cancel_requested("open", "t1") is False
        await mine.release_turn("open", "t1")
        assert await mine.expire(utcnow()) == []
        assert await mine.closed_before(utcnow()) == []
        await mine.delete("shut")
        assert await mine.counts() == {"active": 0, "pending": 0, "closed": 0}
        assert await mine.totals() == {
            "sessions": 0,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

        # 그 인스턴스의 행·리스·중지 표시는 그대로다
        row = await theirs.get("open")
        assert row is not None and (row.status, row.turns) == ("active", 0)
        assert await theirs.cancel_requested("open", "t1") is True
        assert await theirs.claim_turn("open", lease_seconds=30, token="t3") is False
        assert await theirs.get("shut") is not None
        assert (await theirs.find_run("run-b")).session_id == "ran"
        assert await PostgresSessionIndex(pool).counts() == {"active": 1, "pending": 0, "closed": 2}


@needs_pg
async def test_real_postgres_lease_lapses() -> None:
    """리스에는 만료가 있다. 만료된 리스의 턴은 더 중지할 수 없다. 풀지 못한 리스의 토큰은 열린
    표시로 남아(orphan) 되살리기 전에는 다음 턴이 잡지 못하고(P79 1절), 풀린 리스는 바로 잡힌다."""
    async with schema_store("inst-1") as store:
        index = store.index
        await index.create(session_record("s1", "alice"))
        assert await index.claim_turn("s1", lease_seconds=0.05, token="t1") is True
        assert await index.claim_turn("s1", lease_seconds=30, token="t2") is False
        await asyncio.sleep(0.1)
        assert await index.request_cancel("s1") is None
        assert await index.claim_turn("s1", lease_seconds=30, token="t2") is False
        lapsed = await index.get("s1")
        assert lapsed is not None and (lapsed.running, lapsed.orphan) == (False, "t1")
        assert "orphan" not in lapsed.to_dict()
        await index.release_turn("s1", "t1")
        assert (await index.get("s1")).orphan is None
        assert await index.claim_turn("s1", lease_seconds=30, token="t2") is True


@needs_pg
async def test_real_postgres_refuses_a_turns_write_under_its_own_lapsed_lease() -> None:
    """토큰이 같아도 리스가 만료됐으면 턴의 쓰기는 거절된다. 다른 파드가 이미 잡을 수 있는
    세션이다."""
    async with schema_store("inst-1") as store:
        index = store.index
        await index.create(session_record("s-lapsed", "alice"))
        assert await index.claim_turn("s-lapsed", lease_seconds=-1, token="t1") is True
        with pytest.raises(TurnFenced):
            await index.update_held("s-lapsed", "t1", turn_id="t-1")
        with pytest.raises(TurnFenced):
            await store.transcripts.append_held("s-lapsed", [{"kind": "user", "text": "a"}], "t1")
        assert (await index.get("s-lapsed")).turn_id is None
        assert await store.transcripts.read("s-lapsed") == []
