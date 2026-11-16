"""턴의 펜스(P79 2절)다. 펜스 문맥과 마감을 따르는 제한 시간, Redis 연결 클래스 바꾸기, Postgres
커서와 체크포인터, 장기 기억 저장소의 감싸기를 본다. 실제 Postgres·Redis 테스트는
`DAEGAM_TEST_PG_DSN`·`DAEGAM_TEST_REDIS_URL` 이 없으면 건너뛴다. 리스 관리자가 펜스를 세우는 것은
아직 없어 여기서는 펜스를 손으로 세운다."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.store.base import PutOp
from langgraph.store.memory import InMemoryStore
from psycopg.pq import PipelineStatus, TransactionStatus
from redis.asyncio.connection import Connection, SSLConnection, UnixDomainSocketConnection
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from runtime import fence
from runtime.fence import (
    Fence,
    FencedCursor,
    FencedPostgresSaver,
    FencedStore,
    StatementCut,
    TurnFenced,
    fence_redis_client,
    fenced,
)
from runtime.long_term import LongTermMemory
from runtime.session_index_redis import make_redis_client
from runtime.sessions import PG_TRANSPORT_KWARGS, open_postgres_store, postgres_pool_kwargs
from runtime.stream import thread_config
from tests.conftest import REDIS_URL, needs_pg, needs_redis
from tests.store_fixtures import pg_schema, schema_store


def stood_fence() -> Fence:
    """이미 선 펜스다(마감이 지났다)."""
    return Fence(asyncio.get_running_loop().time() - 1)


# 체크포인터에 직접 넣는 첫 체크포인트의 메타데이터다(test_redis_store.py 와 같다)
INPUT_METADATA = {"source": "input", "step": -1, "parents": {}}


def checkpoint_config(session_id: str) -> dict:
    """체크포인터의 aput 이 받는 설정이다. aput 은 checkpoint_ns 를 꺼내므로 빈 이름을 준다."""
    return thread_config(session_id, checkpoint_ns="")


def open_fence(seconds: float = 60) -> Fence:
    """seconds 뒤에 서는 펜스다."""
    return Fence(asyncio.get_running_loop().time() + seconds)


async def test_the_fence_stands_past_its_deadline_or_when_lost() -> None:
    fence.check()  # 펜스 문맥 밖은 막지 않는다
    live = open_fence()
    with fenced(live):
        fence.check()
        assert fence.current() is live
        live.lose()
        with pytest.raises(TurnFenced):
            fence.check()
    assert fence.current() is None
    with fenced(stood_fence()), pytest.raises(TurnFenced):
        fence.check()
    # 거절은 다시 보내기 루프가 다시 보내는 예외가 아니다
    assert not issubclass(TurnFenced, (RedisConnectionError, RedisTimeoutError, OSError))
    assert issubclass(StatementCut, TurnFenced)


async def test_two_turns_on_one_loop_do_not_see_each_others_fence() -> None:
    async def turn(f: Fence) -> bool:
        with fenced(f):
            await asyncio.sleep(0)
            try:
                fence.check()
            except TurnFenced:
                return False
            return True

    assert await asyncio.gather(turn(stood_fence()), turn(open_fence())) == [False, True]


async def test_a_running_bound_follows_the_fence_when_it_moves_or_is_lost() -> None:
    """마감이 앞으로 옮겨지면 돌고 있던 일은 옛 마감에 끊기지 않고, 리스를 잃으면 곧바로 끊긴다.
    선 펜스는 다시 옮겨지지 않는다."""
    loop = asyncio.get_running_loop()

    async def work(f: Fence, seconds: float) -> None:
        with fenced(f):
            async with fence.bounded():
                await asyncio.sleep(seconds)

    moved = open_fence(0.2)
    task = asyncio.create_task(work(moved, 0.4))
    await asyncio.sleep(0.05)
    moved.move(loop.time() + 5)
    await task
    lost = open_fence(5)
    task = asyncio.create_task(work(lost, 5))
    await asyncio.sleep(0.05)
    started = loop.time()
    lost.lose()
    with pytest.raises(StatementCut):
        await task
    assert loop.time() - started < 1
    lost.move(loop.time() + 60)
    assert lost.standing
    # 펜스 문맥 밖은 묶지 않는다
    async with fence.bounded():
        await asyncio.sleep(0)


# --- Redis ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conn", "base"),
    [
        ({"url": "redis://r.svc:6379/0"}, Connection),
        ({"url": "rediss://r.svc:6380/0"}, SSLConnection),
        ({"url": "unix:///tmp/redis.sock"}, UnixDomainSocketConnection),
        ({"host": "r.svc", "port": 6380, "ssl": True}, SSLConnection),
    ],
    ids=["redis", "rediss", "unix", "ssl"],
)
async def test_the_fenced_redis_class_keeps_each_forms_transport(conn: dict, base: type) -> None:
    """주소 꼴마다 풀이 고른 클래스의 펜스 하위 클래스가 된다. 두 번 걸어도 하나다."""
    client = make_redis_client(conn)
    try:
        assert client.connection_pool.connection_class is base
        fence_redis_client(client)
        fenced_class = client.connection_pool.connection_class
        assert issubclass(fenced_class, base) and fenced_class.__name__ == f"Fenced{base.__name__}"
        fence_redis_client(client)
        assert client.connection_pool.connection_class is fenced_class
    finally:
        await client.aclose()


async def test_a_fenced_turn_neither_connects_nor_sends_and_a_handshake_runs_unfenced() -> None:
    """선 펜스에서는 연결을 열지도 보내지도 않는다. 턴의 문맥에서 연 연결의 핸드셰이크는 펜스
    밖에서 돈다. 그래서 핸드셰이크 도중에 펜스가 서도 덜 맺은 연결이 남지 않는다."""

    class Recording(Connection):
        events: list[object] = []

        async def _connect(self) -> None:
            Recording.events.append("connect")
            self._reader = self._writer = object()  # type: ignore[assignment]

        async def on_connect_check_health(self, check_health: bool = True) -> None:
            Recording.events.append(("handshake", fence.current()))

        async def send_packed_command(self, command, check_health=True) -> None:  # type: ignore[no-untyped-def]
            Recording.events.append(command)

    conn = fence.fenced_connection_class(Recording)()
    with fenced(stood_fence()):
        with pytest.raises(TurnFenced):
            await conn.connect()
        with pytest.raises(TurnFenced):
            await conn.send_packed_command(b"PING")
    assert Recording.events == []
    with fenced(open_fence()):
        await conn.connect()
        await conn.send_packed_command(b"PING")
    await conn.send_packed_command(b"PING")
    assert Recording.events == ["connect", ("handshake", None), b"PING", b"PING"]
    conn._reader = conn._writer = None  # 가짜 스트림을 닫으려 하지 않게 한다


class _Writer:
    """연결이 쓴 바이트를 모으는 가짜 스트림이다."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def writelines(self, chunks: list[bytes]) -> None:
        self.sent.extend(chunks)

    async def drain(self) -> None:
        pass


class _SlowReconnect(Connection):
    """다시 연결(장애 조치)과 건강 확인이 오래 걸리는 연결이다. 보내기는 redis-py 의 것 그대로라
    명령을 쓰기 직전의 펜스를 본다."""

    def __init__(self) -> None:
        super().__init__()
        self.stream = _Writer()
        self.seen: list[object] = []

    async def _connect(self) -> None:
        self._reader, self._writer = object(), self.stream  # type: ignore[assignment]

    async def on_connect_check_health(self, check_health: bool = True) -> None:
        self.seen.append(("handshake", fence.current()))
        await asyncio.sleep(0.3)

    async def check_health(self) -> None:
        self.seen.append(("health", fence.current()))
        await asyncio.sleep(0.3)


async def test_a_fence_that_stands_during_a_reconnect_or_health_check_keeps_the_command() -> None:
    """보내기 안에서 다시 연결하거나 건강 확인을 하는 동안 펜스가 서면 명령을 쓰지 않는다. 그 둘은
    펜스 밖에서 돌고, 맺은 연결은 그대로 남아 다음 명령이 쓴다."""
    loop = asyncio.get_running_loop()
    conn = fence.fenced_connection_class(_SlowReconnect)()
    with fenced(Fence(loop.time() + 0.1)), pytest.raises(TurnFenced):
        await conn.send_packed_command(b"SET k lost-turn\r\n", check_health=False)
    assert conn.is_connected and conn.stream.sent == []
    with fenced(Fence(loop.time() + 0.1)), pytest.raises(TurnFenced):
        await conn.send_packed_command(b"SET k lost-turn\r\n")
    assert conn.stream.sent == []
    assert conn.seen == [("handshake", None), ("health", None)]
    await conn.send_packed_command(b"GET k\r\n", check_health=False)
    assert conn.stream.sent == [b"GET k\r\n"]
    conn._reader = conn._writer = None  # 가짜 스트림을 닫으려 하지 않게 한다


def _redis_db(url: str, db: int) -> str:
    """같은 서버의 다른 DB 주소다. 핸드셰이크의 SELECT 가 빠지면 0 번에 쓰는 것이 드러난다."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{db}"))


@needs_redis
async def test_real_redis_refuses_a_fenced_turns_commands_and_keeps_its_connections_whole() -> None:
    """실제 Redis 에서 선 펜스의 명령, 파이프라인, 스크립트는 거절되고 아무것도 쓰이지 않는다.
    거절 뒤 같은 클라이언트의 펜스 밖 명령은 온전히 맺은 연결(이 테스트의 DB)로 간다."""
    url = _redis_db(REDIS_URL, 1)
    client = fence_redis_client(make_redis_client({"url": url}))
    plain = make_redis_client({"url": url})
    key = f"fence-test:{uuid.uuid4().hex}"
    try:
        # 연결이 없을 때: 턴은 연결을 열지 않고, 다음 명령이 핸드셰이크부터 온전히 연다
        with fenced(stood_fence()), pytest.raises(TurnFenced):
            await client.set(key, "a")
        await client.set(key, "b")
        assert await plain.get(key) == "b"
        # 연결이 있을 때: 보내기 맨 앞에서 거절한다
        with fenced(stood_fence()):
            with pytest.raises(TurnFenced):
                await client.set(key, "c")
            pipe = client.pipeline(transaction=True)
            pipe.set(key, "c")
            with pytest.raises(TurnFenced):
                await pipe.execute()
            with pytest.raises(TurnFenced):
                await client.eval("return redis.call('SET', KEYS[1], 'c')", 1, key)
        assert await plain.get(key) == "b"
        await client.set(key, "d")
        with fenced(open_fence()):
            await client.set(key, "e")
        assert await plain.get(key) == "e"
    finally:
        await plain.delete(key)
        await client.aclose()
        await plain.aclose()


# --- Postgres ------------------------------------------------------------------------------


def test_the_session_pool_carries_the_fenced_cursor_and_transport_timeouts() -> None:
    session = postgres_pool_kwargs(fenced=True)
    long_term = postgres_pool_kwargs(fenced=False)
    assert session["cursor_factory"] is FencedCursor and "cursor_factory" not in long_term
    for kwargs in (session, long_term):
        assert kwargs["autocommit"] is True and kwargs["prepare_threshold"] == 0
        assert {k: kwargs[k] for k in PG_TRANSPORT_KWARGS} == PG_TRANSPORT_KWARGS


@needs_pg
async def test_real_postgres_refuses_a_fenced_turns_statements() -> None:
    """실제 Postgres 의 세션 저장소에서 선 펜스의 execute·executemany·stream·copy 와 체크포인터의
    쓰기는 보내지 않는다. 펜스 마감에 걸린 문장은 그 자리에서 멈춘다."""
    async with schema_store("inst-1") as store:
        pool, saver = store._pool, store.checkpointer
        assert isinstance(saver, FencedPostgresSaver)
        assert pool.kwargs["cursor_factory"] is FencedCursor
        async with pool.connection() as conn:
            await conn.execute("CREATE TABLE fenced (v text)", prepare=False)
            with fenced(stood_fence()):
                with pytest.raises(TurnFenced):
                    await conn.execute("INSERT INTO fenced VALUES ('a')")
                cur = conn.cursor()
                with pytest.raises(TurnFenced):
                    await cur.executemany("INSERT INTO fenced VALUES (%s)", [("b",)])
                with pytest.raises(TurnFenced):
                    async for _ in cur.stream("SELECT v FROM fenced"):
                        pass
                with pytest.raises(TurnFenced):
                    async with cur.copy("COPY fenced FROM STDIN") as copy:
                        await copy.write_row(("c",))
            with fenced(open_fence(0.3)), pytest.raises(StatementCut):
                await conn.execute("SELECT pg_sleep(5)")
            cur = await conn.execute("SELECT count(*) AS n FROM fenced")
            assert (await cur.fetchone())["n"] == 0
            with fenced(open_fence()):
                await conn.execute("INSERT INTO fenced VALUES ('d')")
            cur = await conn.execute("SELECT v FROM fenced")
            assert [row["v"] for row in await cur.fetchall()] == ["d"]
        with fenced(stood_fence()), pytest.raises(TurnFenced):
            await saver.aput(checkpoint_config("s-fenced"), empty_checkpoint(), INPUT_METADATA, {})
        assert await saver.aget_tuple(checkpoint_config("s-fenced")) is None


@needs_pg
async def test_real_postgres_cuts_a_pipelined_checkpoint_put_at_the_fence() -> None:
    """체크포인터의 파이프라인 쓰기는 문장을 큐에 넣고 파이프라인을 닫을 때 보낸다. 표 잠금에 막힌
    그 쓰기도 펜스 마감에 끊기고, 잠금이 풀린 뒤에도 쓰이지 않는다. 연결 하나뿐인 풀이라 끊긴 그
    연결이 쉬는 상태로 돌아와 다음 쓰기를 받는 것까지 본다."""
    loop = asyncio.get_running_loop()
    config = checkpoint_config("s-locked")
    async with pg_schema() as schema:
        store = await open_postgres_store(schema.dsn, instance_id="inst-1", max_size=1)
        try:
            saver = store.checkpointer
            async with store._pool.connection() as conn:
                backend = conn.info.backend_pid
            async with await psycopg.AsyncConnection.connect(schema.dsn) as locker:
                await locker.execute("LOCK TABLE checkpoints IN ACCESS EXCLUSIVE MODE")
                started = loop.time()
                with fenced(open_fence(0.5)), pytest.raises(StatementCut):
                    await saver.aput(config, empty_checkpoint(), INPUT_METADATA, {})
                assert loop.time() - started < 5
                await locker.rollback()
            assert await saver.aget_tuple(config) is None
            async with store._pool.connection() as conn:
                assert conn.info.backend_pid == backend
                assert conn.info.transaction_status == TransactionStatus.IDLE
                assert conn.info.pipeline_status == PipelineStatus.OFF
            await saver.aput(config, empty_checkpoint(), INPUT_METADATA, {})
            assert await saver.aget_tuple(config) is not None
            # 파이프라인이 아닌 읽기 블록은 묶지 않으므로, 내주고 멈춘 alist 가 제한 시간을 남기지
            # 않는다. 남기면 리스를 잃을 때 상관없는 일이 취소된다
            reading = open_fence()
            listed = saver.alist(config)
            with fenced(reading):
                await anext(listed)
            assert not reading._bounds
            await listed.aclose()
        finally:
            await store.close()


# --- 장기 기억 --------------------------------------------------------------------------------


async def test_the_memories_store_refuses_a_fenced_turns_writes_before_they_queue() -> None:
    inner = InMemoryStore()
    store = FencedStore(inner)
    ns = ("t", "i", "u")
    await store.aput(ns, "a", {"v": 1})
    with fenced(stood_fence()):
        for write in (
            lambda: store.put(ns, "b", {"v": 2}),
            lambda: store.delete(ns, "a"),
            lambda: store.batch([PutOp(ns, "c", {"v": 3})]),
        ):
            with pytest.raises(TurnFenced):
                write()
        for awrite in (
            lambda: store.aput(ns, "b", {"v": 2}),
            lambda: store.adelete(ns, "a"),
            lambda: store.abatch([PutOp(ns, "c", None)]),
        ):
            with pytest.raises(TurnFenced):
                await awrite()
        # 읽기는 막지 않는다
        assert (await store.aget(ns, "a")).value == {"v": 1}
        assert [item.key for item in store.search(ns)] == ["a"]
    assert [item.key for item in await inner.asearch(ns)] == ["a"]
    assert store.supports_ttl == inner.supports_ttl


def test_the_memories_route_hands_the_backend_the_fenced_store() -> None:
    memory = LongTermMemory(store=InMemoryStore(), tenant="t", instance_id="i")
    assert isinstance(memory.route()._store, FencedStore)
