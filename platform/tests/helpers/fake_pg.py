"""psycopg 목. 실제 Postgres 없이 플랫폼이 보내는 문장과 접속 인자를 기록한다 (D32, D34).

`fake_pg` 는 conftest 가 `pytest_plugins` 로 싣는 픽스처다. 접속마다 `FakeAsyncConnection.conn` 을
돌려주고 처음에는 pgvector 가 켜진 DB(`EXT_PRESENT`)다. 테스트가 `conn`·`error` 를 바꿔 다른 DB 를
흉내 낸다.
"""

from __future__ import annotations

from typing import Any

import pytest

from daegam.services.knowledge import pg

# pgvector 조회(`pg.extensions`)의 응답. 확장이 켜졌다, 설치할 수만 있다, 없다
EXT_PRESENT = {"pg_extension WHERE": [("0.8.6",)], "pg_available_extensions": [(1,)]}
EXT_AVAILABLE = {"pg_extension WHERE": [], "pg_available_extensions": [(1,)]}
EXT_MISSING = {"pg_extension WHERE": [], "pg_available_extensions": []}


def _text(query: Any) -> str:
    return query.as_string(None) if hasattr(query, "as_string") else str(query)


class FakeCursor:
    def __init__(self, rows: list[tuple], rowcount: int | None = None):
        self.rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def executemany(self, query, params_seq):
        self.conn.many.append((_text(query), list(params_seq)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.transactions.append("begin")
        return self

    async def __aexit__(self, exc_type, *a):
        self.conn.transactions.append("rollback" if exc_type else "commit")
        return None


class FakeConn:
    """execute 한 문장을 기록한다. responses 는 문장 조각 → 행 목록이고 그 밖은 (1,) 한 행이다.

    fail 은 모든 문장에서, fail_on 은 조각이 든 문장에서만 던질 예외다. rowcount 는 응답 표에 없는
    문장이 바꾼 행 수(DELETE 가 지운 수)다.
    """

    def __init__(
        self,
        responses: dict[str, list[tuple]] | None = None,
        fail: Exception | None = None,
        *,
        fail_on: dict[str, Exception] | None = None,
        rowcount: int | None = None,
    ):
        self.responses = responses or {}
        self.fail = fail
        self.fail_on = fail_on or {}
        self.rowcount = rowcount
        self.executed: list[tuple[str, Any]] = []
        self.many: list[tuple[str, list]] = []
        self.transactions: list[str] = []
        self.closed = False

    async def execute(self, query, params=None):
        text = _text(query)
        self.executed.append((text, params))
        if self.fail is not None:
            raise self.fail
        for key, exc in self.fail_on.items():
            if key in text:
                raise exc
        for key, rows in self.responses.items():
            if key in text:
                return FakeCursor(rows)
        return FakeCursor([(1,)], self.rowcount)

    def transaction(self):
        return FakeTransaction(self)

    def cursor(self):
        cur = FakeCursor([])
        cur.conn = self
        return cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True
        return None


class FakeAsyncConnection:
    calls: list[tuple[str, dict[str, Any]]] = []
    conn: FakeConn = FakeConn()
    error: Exception | None = None

    @classmethod
    async def connect(cls, conninfo: str, **kwargs):
        cls.calls.append((conninfo, kwargs))
        if cls.error is not None:
            raise cls.error
        return cls.conn


@pytest.fixture
def fake_pg(monkeypatch):
    FakeAsyncConnection.calls = []
    FakeAsyncConnection.conn = FakeConn(EXT_PRESENT)
    FakeAsyncConnection.error = None
    monkeypatch.setattr(pg, "AsyncConnection", FakeAsyncConnection)
    return FakeAsyncConnection
