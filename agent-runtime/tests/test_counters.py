"""공유 카운터(F2d): 창 계산, 인메모리 동시 증가의 원자성, Postgres SQL 문자열·파라미터, 정리,
실제 Redis 의 INCRBY·EXPIREAT 이다. 실제 Redis 는 `DAEGAM_TEST_REDIS_URL` 이 없으면 건너뛴다."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from runtime.counters import (
    GET_SQL,
    PURGE_SQL,
    MemoryCounters,
    PostgresCounters,
    daily_tokens_key,
    day_window,
    minute_window,
    rate_key,
)
from runtime.dsn import redis_prefix
from runtime.sessions import utcnow
from tests.conftest import needs_redis
from tests.pg_fakes import FakePool

NOW = datetime(2026, 9, 6, 12, 34, 56, tzinfo=UTC)
CLOCK_SKEW_MS = 10_000


def test_minute_window_and_retry_after() -> None:
    window = minute_window(NOW)
    assert window.start == datetime(2026, 9, 6, 12, 34, tzinfo=UTC)
    assert window.ends_at == datetime(2026, 9, 6, 12, 35, tzinfo=UTC)
    assert window.expires_at == datetime(2026, 9, 6, 12, 36, tzinfo=UTC)
    assert window.suffix == "20260906T1234"
    assert window.retry_after(NOW) == 4
    # 창 끝에 닿아도 최소 1초다
    assert window.retry_after(window.ends_at) == 1
    assert rate_key("ai-1", "alice", window) == "ai-1:rate:alice:20260906T1234"


def test_day_window_and_retry_after() -> None:
    window = day_window(NOW)
    assert window.start == datetime(2026, 9, 6, tzinfo=UTC)
    assert window.ends_at == datetime(2026, 9, 7, tzinfo=UTC)
    assert window.expires_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert window.suffix == "20260906"
    assert window.retry_after(NOW) == (24 - 12) * 3600 - 34 * 60 - 56
    assert daily_tokens_key("ai-1", window) == "ai-1:tokens:20260906"


async def test_memory_counters_concurrent_increments_are_exact() -> None:
    counters = MemoryCounters()
    window = minute_window(NOW)

    async def worker(n: int) -> None:
        for _ in range(n):
            await counters.incr("k", 1, window=window)
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(25) for _ in range(40)))
    assert await counters.get("k") == 1000
    assert await counters.incr("k", 5, window=window) == 1005
    assert await counters.get("missing") == 0


async def test_memory_counters_purge_expired_only() -> None:
    counters = MemoryCounters()
    old = minute_window(NOW - timedelta(minutes=10))
    new = minute_window(NOW)
    await counters.incr("old", 1, window=old)
    await counters.incr("new", 1, window=new)
    assert await counters.purge(NOW) == 1
    assert await counters.get("old") == 0 and await counters.get("new") == 1


async def test_postgres_counters_incr_is_single_upsert_statement() -> None:
    pool = FakePool([{"value": 7}])
    counters = PostgresCounters(pool)
    window = minute_window(NOW)
    assert await counters.incr("ai-1:rate:alice:20260906T1234", 1, window=window) == 7
    sql, params = pool.calls[0]
    # 문장 하나여야 원자적이다. 읽고 쓰는 두 문장이면 파드 둘이 같은 값을 덮어쓴다
    assert sql.strip().count(";") == 0
    assert "INSERT INTO daegam_counters" in sql
    assert "ON CONFLICT (key) DO UPDATE SET value = daegam_counters.value + EXCLUDED.value" in sql
    assert "RETURNING value" in sql
    assert params == ("ai-1:rate:alice:20260906T1234", 1, window.start, window.expires_at)


async def test_postgres_counters_get_and_purge() -> None:
    pool = FakePool(rowcount=3)
    counters = PostgresCounters(pool)
    assert await counters.get("nothing") == 0
    assert pool.calls[0] == (GET_SQL, ("nothing",))
    assert await counters.purge(NOW) == 3
    assert pool.calls[1] == (PURGE_SQL, (NOW,))
    assert "expires_at < %s" in PURGE_SQL

    pool = FakePool([{"value": 42}])
    assert await PostgresCounters(pool).get("k") == 42


@needs_redis
async def test_real_redis_counters_expire_with_their_window(redis_store) -> None:
    """카운터는 INCRBY 와 EXPIREAT 다. 키는 인스턴스 접두어 아래에 있다."""
    store, instance_id = redis_store
    now = utcnow()
    window = minute_window(now)
    assert await store.counters.incr("k", 1, window=window) == 1
    assert await store.counters.incr("k", 2, window=window) == 3
    assert await store.counters.get("k") == 3
    assert await store.counters.get("missing") == 0
    ttl = await store._client.pttl(f"{redis_prefix(instance_id)}counter:k")
    # 만료 시각은 이 프로세스의 시계로 정하고 TTL 은 Redis 컨테이너의 시계로 잰다.
    # 두 시계가 몇 초 어긋날 수 있다
    assert 0 < ttl <= 120_000 + CLOCK_SKEW_MS
    # 지난 창은 Redis 가 만료로 지우므로 정리할 것이 없다
    assert await store.counters.purge(now) == 0
