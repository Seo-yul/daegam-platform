"""인스턴스의 drain 표시(P44 7절)를 세 색인과 라우트에서 본다. 세우기와 잡기의 계약은
store_fixtures.py 에 있다. 여기서는 그 계약을 세 색인에 걸고, 다시 연 저장소가 표시를 지키는지와,
Postgres 에서 잡기와 세우기의 트랜잭션이 엇갈려도 잡기가 셈에 들거나 물러서는지를 본다. 라우트 쪽은
`POST`·`DELETE /drain` 이 플랫폼 경로만 받고 표시의 id 를 실어야 하는지, 늦은 지우기가 새
표시를 두는지, 표시가 선 동안 모든 턴과 세션 지우기가 503
agent_draining 이고 아무것도 쓰지 않는지, 표시 앞에 잡힌 턴이 셈에 들고 끝까지 도는지, 첫 잡기에서
물린 새 채팅이 세션 행을 남기지 않는지다. 실제 저장소 테스트는 주소가 없으면 건너뛴다."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from runtime.dsn import redis_prefix
from runtime.session_index_redis import RedisSessionIndex, open_redis_store
from runtime.sessions import (
    STATUS_PENDING,
    DrainMarked,
    PostgresSessionIndex,
    SessionRecord,
    drain_lock_key,
    open_postgres_store,
)
from runtime.turn_gate import AGENT_DRAINING
from tests.app_fixtures import (
    INSTANCE_TOKEN,
    bearer,
    decide,
    regenerate,
    sid_of,
    sleeping_turn,
    status_and_code,
)
from tests.conftest import REDIS_URL, chat, needs_pg, needs_redis, run_app, wait_for
from tests.idp_fixtures import FakeIdp, Signer, external_app
from tests.safety_fixtures import ADD, RULE, safety_app, send, turns_doc
from tests.store_fixtures import (
    assert_drain_mark_fences_claims,
    contract_store,
    pg_schema,
    schema_store,
    session_record,
    store_for,
)

NO_TOOL = '{"sleep": 0}'
"""스텁이 Tool 을 부르지 않고 글로 답하는 말이다. 승인 규칙이 있는 문서에서 대기 없이 끝난다."""
MARK = "8d0f6a1e-5b7c-4c39-9a51-0e2b7d4f1c11"
"""플랫폼이 재우기마다 정하는 drain 표시의 id 다."""
PLATFORM = {"user_sub": "system:sleep", "mark": MARK}
"""재우기 워커가 `POST /drain` 에 싣는 본문이다. 프록시 경로의 POST 는 본문에 주체가 있어야 한다."""
CLEAR = {"mark": MARK}
"""같은 재우기의 `DELETE /drain` 쿼리다."""


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_the_mark_counts_the_leases_before_it_and_refuses_the_claims_after(kind: str) -> None:
    async with contract_store(kind) as store:
        await assert_drain_mark_fences_claims(store.index)


@needs_pg
async def test_a_reopened_postgres_store_keeps_the_mark_of_its_instance_only() -> None:
    """기동은 표시를 지우지 않는다. 다시 연 저장소(새 파드)도 표시를 보고 물러서며, 같은 Database 의
    다른 인스턴스는 표시와 잠금을 나누지 않는다."""
    async with pg_schema() as schema:
        first = await open_postgres_store(schema.dsn, instance_id="inst-1")
        try:
            await first.index.create(session_record("s1", "alice"))
            assert await first.index.mark_drain(mark=MARK, seconds=60) == 0
        finally:
            await first.close()
        second = await open_postgres_store(schema.dsn, instance_id="inst-1")
        try:
            assert await second.index.drain_marked() is True
            with pytest.raises(DrainMarked):
                await second.index.claim_turn("s1", lease_seconds=30, token="t1")
            other = PostgresSessionIndex(second._pool, "inst-2")
            await other.create(session_record("s2", "alice"))
            assert await other.drain_marked() is False
            assert await other.claim_turn("s2", lease_seconds=30, token="t2") is True
            assert drain_lock_key("inst-1") != drain_lock_key("inst-2")
        finally:
            await second.close()


@needs_redis
async def test_a_reopened_redis_store_keeps_the_mark_of_its_instance_only(redis_store) -> None:
    store, instance_id = redis_store
    await store.index.create(session_record("s1", "alice"))
    assert await store.index.mark_drain(mark=MARK, seconds=60) == 0
    second = await open_redis_store({"url": REDIS_URL}, instance_id=instance_id)
    try:
        assert await second.index.drain_marked() is True
        with pytest.raises(DrainMarked):
            await second.index.claim_turn("s1", lease_seconds=30, token="t1")
    finally:
        await second.close()
    other_id = f"{instance_id}-other"
    other = RedisSessionIndex(store._client, redis_prefix(other_id), other_id)
    try:
        await other.create(session_record("s2", "alice"))
        assert await other.drain_marked() is False
        assert await other.claim_turn("s2", lease_seconds=30, token="t2") is True
    finally:
        keys = [k async for k in store._client.scan_iter(f"{redis_prefix(other_id)}*")]
        await store._client.delete(*keys)


# --- Postgres 에서 엇갈린 트랜잭션 -------------------------------------------------------------


class GatedPool:
    """풀을 감싸 그 연결이 보낸 문장 가운데 marker 가 든 첫 문장이 끝난 자리에서 멈춘다. reached 가
    서면 그 자리이고 go 를 세우면 이어 간다. 그 밖은 감싼 풀 그대로다."""

    def __init__(self, pool: Any, marker: str) -> None:
        self._pool = pool
        self._marker = marker
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as conn:
            yield _GatedConnection(conn, self)

    async def passed(self, query: Any) -> None:
        if self._marker in str(query) and not self.reached.is_set():
            self.reached.set()
            await self.go.wait()


class _GatedConnection:
    def __init__(self, conn: Any, gate: GatedPool) -> None:
        self._conn = conn
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: Any, params: Any = None) -> Any:
        cursor = await self._conn.execute(query, params)
        await self._gate.passed(query)
        return cursor


def _lock_halves(instance_id: str) -> tuple[int, int]:
    """bigint advisory 잠금이 pg_locks 에 보이는 classid·objid 다(위 32비트와 아래 32비트)."""
    key = drain_lock_key(instance_id) & 0xFFFF_FFFF_FFFF_FFFF
    return key >> 32, key & 0xFFFF_FFFF


async def _waits_on_the_drain_lock(pool: Any, instance_id: str) -> None:
    """그 인스턴스의 drain 잠금을 기다리는 연결이 생길 때까지 기다린다."""
    classid, objid = _lock_halves(instance_id)

    async def waiting() -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                " AND classid::bigint = %s AND objid::bigint = %s",
                [classid, objid],
            )
            return int((await cur.fetchone())["n"]) > 0

    await wait_for(waiting, what="drain 잠금을 기다리는 연결")


@needs_pg
async def test_a_claim_holding_the_shared_lock_is_counted_by_a_drain_that_waits_for_it() -> None:
    """잡기가 공유 잠금을 쥔 채 아직 UPDATE 를 보내지 않았을 때 세우기가 온다. 세우기는 배타 잠금을
    기다리고, 잡기가 커밋한 뒤에 세므로 그 리스가 셈에 든다."""
    async with schema_store("inst-1") as store:
        await store.index.create(session_record("s1", "alice"))
        gate = GatedPool(store._pool, "pg_advisory_xact_lock_shared")
        claimer = PostgresSessionIndex(gate, "inst-1")
        claim = asyncio.create_task(claimer.claim_turn("s1", lease_seconds=30, token="t1"))
        await asyncio.wait_for(gate.reached.wait(), timeout=5)
        drain = asyncio.create_task(store.index.mark_drain(mark=MARK, seconds=60))
        await _waits_on_the_drain_lock(store._pool, "inst-1")
        gate.go.set()
        assert await claim is True
        assert await drain == 1


@needs_pg
async def test_a_claim_that_starts_before_the_drain_commits_is_refused() -> None:
    """세우기가 배타 잠금 아래 표시를 썼으나 아직 세지도 커밋하지도 않았을 때 잡기가 온다. 잡기는
    공유 잠금을 기다리고, 잠금 뒤의 문장이 커밋된 표시를 보아 물러선다. 셈은 0 이다. READ COMMITTED
    에서 잠금 없이는 이 잡기가 표시도 셈도 비켜 간다."""
    async with schema_store("inst-1") as store:
        await store.index.create(session_record("s1", "alice"))
        gate = GatedPool(store._pool, "INSERT INTO daegam_drains")
        drainer = PostgresSessionIndex(gate, "inst-1")
        drain = asyncio.create_task(drainer.mark_drain(mark=MARK, seconds=60))
        await asyncio.wait_for(gate.reached.wait(), timeout=5)
        claim = asyncio.create_task(store.index.claim_turn("s1", lease_seconds=30, token="t1"))
        await _waits_on_the_drain_lock(store._pool, "inst-1")
        gate.go.set()
        assert await drain == 0
        with pytest.raises(DrainMarked):
            await claim
        row = await store.index.get("s1")
        assert row is not None and (row.running, row.orphan) == (False, None)


# --- 라우트와 턴 입구 ---------------------------------------------------------------------------


async def test_the_drain_routes_answer_the_platform_path_only(support_doc) -> None:
    signer = Signer()
    async with external_app(support_doc, FakeIdp(signer)) as running:
        index = running.runtime.store.index
        external = bearer(signer.token())
        posted = await running.client.post("/drain", json=PLATFORM, headers=external)
        assert status_and_code(posted) == (403, "forbidden")
        deleted = await running.client.delete("/drain", params=CLEAR, headers=external)
        assert status_and_code(deleted) == (403, "forbidden")
        assert await index.drain_marked() is False
        platform = bearer(INSTANCE_TOKEN)
        anonymous = await running.client.post("/drain", json={"mark": MARK}, headers=platform)
        assert status_and_code(anonymous) == (422, "user_sub_required")
        posted = await running.client.post("/drain", json=PLATFORM, headers=platform)
        assert posted.status_code == 200 and posted.json() == {"running": 0}
        assert await index.drain_marked() is True
        deleted = await running.client.delete("/drain", params=CLEAR, headers=platform)
        assert deleted.status_code == 204 and deleted.content == b""
        assert await index.drain_marked() is False


async def test_a_drain_names_its_mark_and_a_stale_clear_leaves_the_newer_one(support_doc) -> None:
    """세우기와 지우기는 표시의 id 를 실어야 하고(없거나 꼴이 틀리면 422) 지우기는 그 id 의 표시만
    지운다. 앞 재우기의 늦은 지우기는 다음 재우기의 표시를 두고 204 로 답하며 턴은 계속 물러선다."""
    support_doc["main"]["tools"] = []
    async with run_app(support_doc) as running:
        index = running.runtime.store.index
        for body in ({"user_sub": "system:sleep"}, {**PLATFORM, "mark": "a b"}):
            response = await running.client.post("/drain", json=body)
            assert status_and_code(response) == (422, "validation_error"), body
        for params in ({}, {"mark": ""}):
            response = await running.client.delete("/drain", params=params)
            assert status_and_code(response) == (422, "validation_error"), params
        assert await index.drain_marked() is False
        assert (await running.client.post("/drain", json=PLATFORM)).status_code == 200
        newer = {**PLATFORM, "mark": "next-sleep"}
        assert (await running.client.post("/drain", json=newer)).status_code == 200
        stale = await running.client.delete("/drain", params=CLEAR)
        assert stale.status_code == 204
        assert await index.drain_marked() is True
        response, _ = await chat(running, "hi")
        assert status_and_code(response) == (503, AGENT_DRAINING)
        cleared = await running.client.delete("/drain", params={"mark": "next-sleep"})
        assert cleared.status_code == 204
        assert await index.drain_marked() is False
        response, sse = await chat(running, "hi")
        assert response.status_code == 200 and sse[-1][0] == "done"


async def test_under_the_mark_every_claim_answers_agent_draining_and_writes_nothing(
    fake_mcp,
) -> None:
    """새 말, 이어지는 말, 다시 생성, 결정, 세션 지우기가 모두 503 agent_draining 과 Retry-After
    이고, 세션 수와 상태, Tool 호출이 그대로다. 표시를 지우면 같은 말이 받아진다."""
    async with safety_app(turns_doc(fake_mcp.url, approval=RULE)) as running:
        index = running.runtime.store.index
        open_sid = sid_of(await send(running, NO_TOOL), "done")
        pending_sid = sid_of(await send(running, ADD), "interrupt")
        drained = await running.client.post("/drain", json=PLATFORM)
        assert drained.status_code == 200 and drained.json() == {"running": 0}
        before = await index.counts()
        refusals = [
            (await chat(running, "새 말"))[0],
            (await chat(running, "이어서", session_id=open_sid))[0],
            (await regenerate(running, open_sid))[0],
            await decide(running, pending_sid, [{"type": "approve"}]),
            await running.client.delete(f"/sessions/{open_sid}", params={"user_sub": "alice"}),
        ]
        for response in refusals:
            assert status_and_code(response) == (503, AGENT_DRAINING), response.text
            assert response.headers["Retry-After"] == "5"
        assert await index.counts() == before
        assert (await index.get(pending_sid)).status == STATUS_PENDING
        assert fake_mcp.calls["Add"] == 0
        assert (await running.client.delete("/drain", params=CLEAR)).status_code == 204
        response, sse = await chat(running, NO_TOOL, session_id=open_sid)
        assert response.status_code == 200 and sse[-1][0] == "done"


async def test_a_turn_that_holds_its_lease_is_counted_and_runs_to_its_end(support_doc) -> None:
    support_doc["main"]["tools"] = []
    async with run_app(support_doc, over_http=True) as running:
        turn = await sleeping_turn(running, 1)
        drained = await running.client.post("/drain", json=PLATFORM)
        assert drained.json() == {"running": 1}
        ended = await turn
        assert ended.status_code == 200 and "event: done" in ended.text
        assert (await running.client.post("/drain", json=PLATFORM)).json() == {"running": 0}


@pytest.mark.parametrize("kind", ["memory", "postgres", "redis"])
async def test_a_new_chat_whose_first_claim_meets_the_mark_leaves_no_session(
    kind: str, support_doc, monkeypatch
) -> None:
    """입구가 표시를 본 뒤 세션 행을 만들고 잡기 전에 표시가 선다. 잡기가 물리면 그 새 세션을
    지우므로 재우기 직전에 세는 세션 수에 빈 세션이 들지 않는다."""
    support_doc["main"]["tools"] = []
    async with store_for(kind, support_doc) as store, run_app(support_doc, store=store) as running:
        index = running.runtime.store.index
        create = index.create

        async def create_then_drain(record: SessionRecord) -> None:
            await create(record)
            await index.mark_drain(mark=MARK, seconds=60)

        monkeypatch.setattr(index, "create", create_then_drain)
        response, _ = await chat(running, "hi")
        assert status_and_code(response) == (503, AGENT_DRAINING)
        assert await index.list(runs="include") == []
        assert await index.counts() == {"active": 0, "pending": 0, "closed": 0}
