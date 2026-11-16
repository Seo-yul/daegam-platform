"""Redis 세션 저장소를 열고 체크포인터를 다룬다(D33 F2). `DAEGAM_TEST_REDIS_URL` 이 없으면 실제
Redis 테스트는 건너뛴다. 가짜(redis_fakes.py)로는 실제 서버로 만들기 어려운 것만 본다.
체크포인터 setup 의 실패 가르기와 되풀이 지우기, open_redis_store 가 클라이언트를 어떻게 만들고
닫는지다. 실제 Redis 에서는 체크포인터와 인스턴스 접두어, 동시 기동, 스레드 지우기를 본다. 색인은
test_redis_index.py 가, 카운터는 test_counters.py 가 본다.

Redis 7 은 게이트가 띄우지 않으므로 따로 띄워 `DAEGAM_TEST_REDIS7_URL` 로 넘길 때만 돈다. 그
주소로는 `memory_redis_modules_missing` 한 가지만 본다.

    docker run --rm -d --name r7 -p 127.0.0.1:16380:6379 redis:7
    DAEGAM_TEST_REDIS7_URL=redis://127.0.0.1:16380/0 uv run pytest -q tests/test_redis_store.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import urlsplit

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError, ResponseError

from runtime.config import ConfigError
from runtime.counters import RedisCounters
from runtime.dsn import redis_prefix, resolve_redis
from runtime.session_index_redis import (
    RedisSessionIndex,
    _thread_documents,
    delete_redis_thread,
    find_response_error,
    open_redis_store,
    setup_redis_saver,
)
from runtime.sessions import CheckpointsRemain
from runtime.stream import thread_config
from runtime.transcript import RedisTranscripts
from runtime.uploads import RedisUploads
from tests.conftest import REDIS_URL, load_fixture, needs_redis
from tests.redis_fakes import (
    ChunkedSaver,
    FakeRedis,
    FakeSaver,
    FakeSaverClass,
    WrappedError,
    cmd,
    index_exists,
    wrapped,
)
from tests.store_fixtures import drop_redis_instance, real_store, session_record

# ---- 체크포인터 setup 과 스레드 지우기 -----------------------------------------------------------


async def test_delete_redis_thread_repeats_until_no_document_is_left() -> None:
    """adelete_thread 가 한 번에 다 지우지 못하면 개수가 0 이 될 때까지 다시 부른다. 상한까지
    남으면 CheckpointsRemain 이다. 두 인덱스를 adelete_thread 와 같은 태그 조건으로 센다."""
    saver = ChunkedSaver(documents=25_000, per_call=10_000)
    await delete_redis_thread(saver, "abc123")  # type: ignore[arg-type]
    assert saver.calls == 3 and saver.left == 0
    for index in (saver.checkpoints_index, saver.checkpoint_writes_index):
        assert index.queries[0] == "@thread_id:{abc123}"

    stuck = ChunkedSaver(documents=50_000, per_call=10_000)
    with pytest.raises(CheckpointsRemain):
        await delete_redis_thread(stuck, "abc123", rounds=2)  # type: ignore[arg-type]
    assert stuck.calls == 2

    odd = ChunkedSaver(documents=0, per_call=1)
    assert await _thread_documents(odd, "a-b.c") == 0  # type: ignore[arg-type]
    assert odd.checkpoints_index.queries[0] == "@thread_id:{a\\-b\\.c}"


def test_find_response_error_walks_cause_and_context() -> None:
    inner = ResponseError("unknown command 'FT.INFO'")
    assert find_response_error(wrapped(inner)) is inner
    chained = WrappedError("ctx")
    chained.__context__ = WrappedError("deeper")
    chained.__context__.__context__ = inner
    assert find_response_error(chained) is inner
    assert find_response_error(WrappedError("bare")) is None


async def test_asetup_gives_up_after_six_index_exists() -> None:
    saver = FakeSaver(*(index_exists() for _ in range(6)))
    with pytest.raises(ConfigError) as info:
        await setup_redis_saver(saver)
    assert info.value.code == "memory_setup_failed"
    assert "WrappedError" in info.value.message
    assert saver.calls == 6
    # 다섯 번은 아직 상한 안이다
    saver = FakeSaver(*(index_exists() for _ in range(5)))
    await setup_redis_saver(saver)
    assert saver.calls == 6


async def test_asetup_noperm_and_other_errors_are_setup_failed() -> None:
    for error in (
        wrapped(NoPermissionError("NOPERM User x has no permissions to run 'ft.info'")),
        RedisConnectionError("Connection closed by server."),
        RuntimeError("boom"),
    ):
        saver = FakeSaver(error)
        with pytest.raises(ConfigError) as info:
            await setup_redis_saver(saver)
        assert info.value.code == "memory_setup_failed"
        assert type(error).__name__ in info.value.message
        assert saver.calls == 1


# ---- open_redis_store 의 배선 (가짜 클라이언트) --------------------------------------------------


async def test_open_redis_store_wires_one_client_and_prefixes(fake_redis_client: FakeRedis) -> None:
    """체크포인터·색인·카운터·전사가 클라이언트 하나를 나눠 쓴다. 첨부 저장소만 본문을 바이트
    그대로 담으려고 디코드하지 않는 클라이언트를 따로 연다(P36 9절). 닫으면 둘 다 닫는다."""
    fake = fake_redis_client
    store = await open_redis_store(
        {"host": "r.svc", "port": 6379, "db": 1, "password": "p w"}, instance_id="ai-1"
    )
    args = {
        "host": "r.svc",
        "port": 6379,
        "db": 1,
        "password": "p w",
        "decode_responses": True,
        "socket_connect_timeout": 3,
        "socket_timeout": 10,
        "health_check_interval": 30,
    }
    assert fake.made == [("args", args), ("args", {**args, "decode_responses": False})]
    assert fake.calls[0] == cmd("ping")
    saver = FakeSaverClass.instances[-1]
    assert saver.kwargs == {
        "redis_client": fake,
        "checkpoint_prefix": "daegam-agent:ai-1:checkpoint",
        "checkpoint_write_prefix": "daegam-agent:ai-1:checkpoint_write",
    }
    assert saver.calls == 1
    assert store.shared is True and store.warnings == [] and store.checkpointer is saver
    assert isinstance(store.index, RedisSessionIndex) and isinstance(store.counters, RedisCounters)
    assert isinstance(store.transcripts, RedisTranscripts)
    assert isinstance(store.uploads, RedisUploads)
    # 첫 명령 전에 연결 클래스가 펜스 하위 클래스가 됐다(P79 2절)
    assert fake.connection_pool.connection_class.__name__ == "FencedConnection"
    await store.close()
    assert fake.calls[-2:] == [cmd("aclose"), cmd("aclose")]


async def test_open_redis_store_url_and_ping_failure(fake_redis_client: FakeRedis) -> None:
    fake = fake_redis_client
    fake.responses["ping"] = lambda: (_ for _ in ()).throw(RedisConnectionError("refused"))
    with pytest.raises(ConfigError) as info:
        await open_redis_store({"url": "redis://:secret@r.svc:6379/0"}, instance_id="ai-1")
    assert info.value.code == "memory_unreachable"
    assert "ConnectionError" in info.value.message and "secret" not in info.value.message
    assert fake.made[0][0] == "url" and fake.made[0][1][0] == "redis://:secret@r.svc:6379/0"
    assert fake.calls[-1] == cmd("aclose")


async def test_open_redis_store_setup_failure_closes_client(fake_redis_client: FakeRedis) -> None:
    fake = fake_redis_client
    FakeSaverClass.errors.append(wrapped(ResponseError("unknown command 'FT.INFO'")))
    with pytest.raises(ConfigError) as info:
        await open_redis_store({"url": "redis://r.svc:6379/0"}, instance_id="ai-1")
    assert info.value.code == "memory_redis_modules_missing"
    # 모듈이 없는 Redis 는 다시 불러도 같으므로 setup 을 한 번만 부른다
    assert FakeSaverClass.instances[-1].calls == 1
    # 색인은 setup 뒤에 만들므로 스크립트 등록 전에 닫힌다. 첨부 저장소의 클라이언트도 닫는다
    assert fake.calls == [cmd("ping"), cmd("aclose"), cmd("aclose")]


# ---- 실제 컨테이너 -------------------------------------------------------------------------

REDIS7_URL = os.environ.get("DAEGAM_TEST_REDIS7_URL")
# 막 쓴 문서를 RediSearch 가 셀 때까지 기다리는 상한
INDEX_WAIT_SECONDS = 5.0


async def _until_counted(saver: Any, thread_id: str, count: int) -> None:
    """그 스레드의 문서를 인덱스가 count 개까지 셀 때까지 기다린다. `adelete_thread` 는 인덱스로
    지울 문서와 최신 포인터를 찾는다. 쓰자마자 지우면 인덱스가 아직 못 본 문서의 최신 포인터가 남아
    `aget_tuple` 이 그 체크포인트를 다시 찾은 적이 있다. 운영의 세션 지우기와 첫 턴
    다시 생성은 끝난 턴의 체크포인트를 지우므로 이 틈을 만나지 않는다."""
    deadline = asyncio.get_running_loop().time() + INDEX_WAIT_SECONDS
    while (seen := await _thread_documents(saver, thread_id)) < count:
        assert asyncio.get_running_loop().time() < deadline, f"the index counted {seen} of {count}"
        await asyncio.sleep(0.02)
    assert seen == count


@needs_redis
async def test_real_checkpointer_put_get_delete(redis_store) -> None:
    store, _ = redis_store
    saver = store.checkpointer
    config = thread_config("thread-1", checkpoint_ns="")
    checkpoint = empty_checkpoint()
    await saver.aput(config, checkpoint, {"source": "input", "step": -1, "parents": {}}, {})
    found = await saver.aget_tuple(config)
    assert found is not None and found.checkpoint["id"] == checkpoint["id"]
    await _until_counted(saver, "thread-1", 1)
    await saver.adelete_thread("thread-1")
    assert await saver.aget_tuple(config) is None
    # 두 번 지워도, 없는 thread 를 지워도 조용하다
    await saver.adelete_thread("thread-1")


@needs_redis
async def test_real_prefixes_do_not_see_each_other(redis_store) -> None:
    store_a, _ = redis_store
    async with real_store("redis", load_fixture("minimal.json")) as store_b:
        config = thread_config("shared-thread", checkpoint_ns="")
        await store_a.checkpointer.aput(
            config, empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {}
        )
        assert await store_a.checkpointer.aget_tuple(config) is not None
        assert await store_b.checkpointer.aget_tuple(config) is None
        await store_a.index.create(session_record("s1", "alice"))
        assert await store_b.index.get("s1") is None
        assert await store_b.index.counts() == {"active": 0, "pending": 0, "closed": 0}


@needs_redis
async def test_real_concurrent_setup_twenty_rounds() -> None:
    """replicas 2 의 동시 기동이다. Index already exists 가 나와도 다시 불러 둘 다 산다."""
    for _ in range(20):
        instance_id = f"g-{uuid.uuid4().hex[:8]}"
        conn = {"url": REDIS_URL}
        first, second = await asyncio.gather(
            open_redis_store(conn, instance_id=instance_id),
            open_redis_store(conn, instance_id=instance_id),
        )
        try:
            assert first.shared and second.shared
        finally:
            await drop_redis_instance(first._client, instance_id)
            await first.close()
            await second.close()


@needs_redis
async def test_real_transcript_stays_under_the_instance_prefix(redis_store) -> None:
    """전사 리스트는 인스턴스 접두어 아래 `transcript:{sid}` 다. 접두어 밖에 쌓이면 인스턴스 단위
    정리(drop_redis_instance, e2e 정리)가 그 키를 놓친다(D33). 전사의 계약은
    test_transcript_store.py 가 세 저장소에 함께 건다."""
    store, instance_id = redis_store
    items = [{"kind": "user", "text": "하나"}, {"kind": "user", "text": "둘"}]
    await store.transcripts.append("s3", items)
    assert await store._client.llen(f"{redis_prefix(instance_id)}transcript:s3") == len(items)


@pytest.mark.skipif(not REDIS7_URL, reason="DAEGAM_TEST_REDIS7_URL 이 없다")
async def test_real_redis7_is_modules_missing() -> None:
    with pytest.raises(ConfigError) as info:
        await open_redis_store({"url": REDIS7_URL}, instance_id="r7")
    assert info.value.code == "memory_redis_modules_missing"


@needs_redis
@pytest.mark.parametrize("dotted", [False, True])
async def test_real_delete_leaves_no_key_of_the_thread(redis_store, dotted) -> None:
    """실제 Redis 에서 지운 스레드의 문서 수가 0 이고 그 스레드 id 가 든 키가 남지 않는다. 인스턴스
    접두어 밖의 `write_keys_zset:*` 도 함께 지워진다. 태그의 특수 문자(`-`·`.`)가 든 id 도
    adelete_thread 와 같은 조건으로 센다."""
    store, _ = redis_store
    sid = f"a-b.{uuid.uuid4().hex}" if dotted else uuid.uuid4().hex
    config = thread_config(sid, checkpoint_ns="")
    for step in range(3):
        config = await store.checkpointer.aput(
            config, empty_checkpoint(), {"source": "loop", "step": step}, {}
        )
        await store.checkpointer.aput_writes(config, [("messages", "x")], task_id=f"t{step}")
    await _until_counted(store.checkpointer, sid, 6)
    await store.delete_checkpoints(sid)
    assert await _thread_documents(store.checkpointer, sid) == 0
    assert [k async for k in store._client.scan_iter(f"*{sid}*")] == []


REDIS_TLS_URL = os.environ.get("DAEGAM_TEST_REDIS_TLS_URL")
REDIS_TLS_CA = os.environ.get("DAEGAM_TEST_REDIS_TLS_CA")


@pytest.mark.skipif(
    not (REDIS_TLS_URL and REDIS_TLS_CA), reason="DAEGAM_TEST_REDIS_TLS_URL·_CA 가 없다"
)
async def test_real_store_over_tls() -> None:
    """TLS 바인딩 env(P61)로 연 저장소의 체크포인터·색인이 TLS 끝점에서 돈다. 개별 env 와 URL
    둘 다 같은 Redis 에 닿는다."""
    assert REDIS_TLS_URL and REDIS_TLS_CA
    tls = {"DAEGAM_DB_0_TLS": "1", "DAEGAM_DB_0_CA_FILE": REDIS_TLS_CA}
    parts = urlsplit(REDIS_TLS_URL)
    parts_env = {
        "DAEGAM_DB_0_HOST": parts.hostname or "",
        "DAEGAM_DB_0_PORT": str(parts.port or 6380),
        "DAEGAM_DB_0_PASSWORD": parts.password or "",
        **tls,
    }
    for env in ({"DAEGAM_DB_0_URL": REDIS_TLS_URL, **tls}, parts_env):
        conn = resolve_redis("DAEGAM_DB_0_URL", env)
        assert conn is not None
        instance_id = f"tls-{uuid.uuid4().hex[:8]}"
        store = await open_redis_store(conn, instance_id=instance_id)
        try:
            config = thread_config("t1", checkpoint_ns="")
            await store.checkpointer.aput(
                config, empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {}
            )
            assert await store.checkpointer.aget_tuple(config) is not None
            await store.index.create(session_record("s1", "alice"))
            assert (await store.index.get("s1")) is not None
        finally:
            await drop_redis_instance(store._client, instance_id)
            await store.close()
