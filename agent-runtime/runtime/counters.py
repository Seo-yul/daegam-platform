"""공유 카운터다(D26, D33). 사용자별 분당 요청 한도, 인스턴스 일당 토큰 예산, 한 턴의 다시 생성
횟수를 센다.

memory 가 postgres 면 daegam_counters 에 INSERT ... ON CONFLICT DO UPDATE 로 원자 증가하므로
파드가 여럿이어도 같은 값을 본다. 표는 이 파일이 정의하고 세션 색인의 setup 이 만든다.
redis 면 인스턴스 접두어 아래 `counter:{key}` 에 MULTI 하나로 INCRBY 와 EXPIREAT 를 해 같은 값을
보고 만료는 Redis 가 지운다. memory 가 없으면 파드 안 dict 라 replicas 만큼 느슨해진다(기동 경고
no_shared_state). 창(분·일, UTC)은 키에 들어가고 만료된 Postgres 행은 세션 sweep 이 지운다.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

MINUTE_GRACE = timedelta(minutes=1)
DAY_GRACE = timedelta(days=1)

# 표는 여기서 정의하고 세션 색인의 setup 이 같은 DB 에 만든다(sessions.py). 세션 색인과 한 풀을
# 나눠 쓰므로 DDL 도 그 한 번의 setup 에 실린다
COUNTERS_DDL = """
CREATE TABLE IF NOT EXISTS daegam_counters (
    key          text PRIMARY KEY,
    value        bigint NOT NULL DEFAULT 0,
    window_start timestamptz NOT NULL,
    expires_at   timestamptz
);
"""

INCR_SQL = """
INSERT INTO daegam_counters (key, value, window_start, expires_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (key) DO UPDATE SET value = daegam_counters.value + EXCLUDED.value
RETURNING value
"""
GET_SQL = "SELECT value FROM daegam_counters WHERE key = %s"
PURGE_SQL = "DELETE FROM daegam_counters WHERE expires_at IS NOT NULL AND expires_at < %s"


@dataclass(frozen=True)
class Window:
    """카운터 창 하나다. suffix 는 키에 붙고 ends_at 은 Retry-After 를 만든다."""

    start: datetime
    ends_at: datetime
    expires_at: datetime
    suffix: str

    def retry_after(self, now: datetime) -> int:
        """창이 끝날 때까지 남은 초다. 최소 1 이다."""
        return max(1, math.ceil((self.ends_at - now).total_seconds()))


def minute_window(now: datetime) -> Window:
    start = now.replace(second=0, microsecond=0)
    ends = start + timedelta(minutes=1)
    return Window(start, ends, ends + MINUTE_GRACE, start.strftime("%Y%m%dT%H%M"))


def day_window(now: datetime) -> Window:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ends = start + timedelta(days=1)
    return Window(start, ends, ends + DAY_GRACE, start.strftime("%Y%m%d"))


def rate_key(instance_id: str, user_sub: str, window: Window) -> str:
    return f"{instance_id}:rate:{user_sub}:{window.suffix}"


def daily_tokens_key(instance_id: str, window: Window) -> str:
    return f"{instance_id}:tokens:{window.suffix}"


def web_key(instance_id: str, tool: str, user_sub: str, window: Window) -> str:
    """세션 소유자 하나가 웹 Tool 하나를 부른 수다(P39b 14절 6부, web_guard.py)."""
    return f"{instance_id}:web:{tool}:{user_sub}:{window.suffix}"


def regenerate_key(instance_id: str, session_id: str, turn: int, window: Window) -> str:
    """한 턴을 다시 생성한 횟수다(P36, regenerate.py). 다음 턴은 번호가 달라 새로 센다."""
    return f"{instance_id}:regen:{session_id}:{turn}:{window.suffix}"


class Counters(Protocol):
    async def incr(self, key: str, amount: int, *, window: Window) -> int:
        """amount 만큼 더하고 더한 뒤의 값을 돌려준다. 원자적이어야 한다."""
        ...

    async def get(self, key: str) -> int: ...

    async def purge(self, before: datetime) -> int:
        """expires_at 이 before 보다 앞선 행을 지우고 개수를 돌려준다."""
        ...


class MemoryCounters:
    """파드 하나 안에서만 보이는 카운터다. memory 노드가 없을 때 쓴다."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[int, datetime]] = {}
        self._lock = asyncio.Lock()

    async def incr(self, key: str, amount: int, *, window: Window) -> int:
        async with self._lock:
            value, _ = self._rows.get(key, (0, window.expires_at))
            value += amount
            self._rows[key] = (value, window.expires_at)
            return value

    async def get(self, key: str) -> int:
        row = self._rows.get(key)
        return row[0] if row else 0

    async def purge(self, before: datetime) -> int:
        expired = [key for key, (_, expires_at) in self._rows.items() if expires_at < before]
        for key in expired:
            del self._rows[key]
        return len(expired)


class PostgresCounters:
    """memory 의 Postgres 에 두는 카운터다. 증가는 문장 하나라 파드 사이에서도 원자적이다."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def incr(self, key: str, amount: int, *, window: Window) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(INCR_SQL, (key, amount, window.start, window.expires_at))
            row = await cur.fetchone()
        return int(row["value"])

    async def get(self, key: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(GET_SQL, (key,))
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def purge(self, before: datetime) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(PURGE_SQL, (before,))
            return int(cur.rowcount)


class RedisCounters:
    """memory 의 Redis 에 두는 카운터다. 키는 접두어 아래 `counter:{key}` 다.

    증가는 MULTI 하나에 INCRBY 와 EXPIREAT 다. 같은 창 키에 늘 같은 절대 시각을 다시 두는 것이라
    멱등이고, 파이프라인 없이 두 명령이면 첫 INCRBY 뒤 파드가 죽을 때 TTL 없는 키가 남는다.
    """

    def __init__(self, client: Any, prefix: str) -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}counter:{key}"

    async def incr(self, key: str, amount: int, *, window: Window) -> int:
        name = self._key(key)
        pipe = self._client.pipeline(transaction=True)
        pipe.incrby(name, amount)
        pipe.expireat(name, int(window.expires_at.timestamp()))
        results = await pipe.execute()
        return int(results[0])

    async def get(self, key: str) -> int:
        value = await self._client.get(self._key(key))
        return int(value) if value is not None else 0

    async def purge(self, before: datetime) -> int:
        """할 일이 없다. EXPIREAT 가 지운다."""
        return 0
