"""Postgres 풀(psycopg_pool.AsyncConnectionPool)의 가짜다.

실제 서버 없이 문장과 파라미터를 보는 테스트(카운터, knowledge 검색)와, 풀이 연결을 내주지 못할 때
실패를 가르는 테스트(세션 저장소·장기 기억·knowledge 의 기동)가 같이 쓴다. 실제 Postgres 로 도는
테스트는 store_fixtures 의 real_store·pg_schema 를 쓴다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class FakeCursor:
    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self) -> list[Any]:
        return list(self._rows)

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    async def execute(self, query: Any, params: Any = None) -> FakeCursor:
        """문장(sql.Composed 면 펼친 글)과 파라미터를 풀의 calls 에 적는다. 풀에 error 가 있으면
        연결 확인(SELECT 1) 말고는 그 예외를 낸다."""
        pool = self._pool
        text = query.as_string(None) if hasattr(query, "as_string") else str(query)
        pool.calls.append((text, params))
        if pool.error is not None and text != "SELECT 1":
            raise pool.error
        return FakeCursor(pool.rows, pool.rowcount)


class FakePool:
    """connection()·close()·conninfo 만 흉내 낸다.

    연결이 실행한 문장과 파라미터를 calls 에 적고 rows 를 돌려준다. rowcount 는 execute 가 돌려주는
    커서의 rowcount 로 그대로 간다. failing 이면 연결을 내주지 못한다. 실제 풀처럼 실패 까닭을
    감추고 시간 초과만 낸다. conninfo 에 비밀번호가 있어 오류 문구가 DSN 원문을 싣지 않는지 볼 수
    있다. closed 는 close 를 불렀는지다.
    """

    conninfo = "postgresql://u:secret@db:5432/agent"

    def __init__(
        self,
        rows: list[Any] | None = None,
        *,
        error: Exception | None = None,
        rowcount: int = 0,
        failing: bool = False,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.rowcount = rowcount
        self.failing = failing
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    @asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[FakeConnection]:
        if self.failing:
            raise TimeoutError("pool timeout")
        yield FakeConnection(self)

    async def close(self) -> None:
        self.closed = True
