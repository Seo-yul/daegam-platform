"""Postgres(pgvector) 접속과 벡터 테이블 (D32).

플랫폼의 psycopg 는 전부 비동기다. `AsyncConnection.connect(conninfo, connect_timeout=3,
options="-c statement_timeout=<ms>")` 이고 스레드풀 없이 `await` 한다. libpq 의 기본
`connect_timeout` 은 무한이라 닿지 않는 호스트에 이벤트 루프가 매달리기 때문이다. 문장 타임아웃은
verify 5초, 생성의 DDL 30초, 인덱서 120초로 호출자가 준다.

접속 오류는 psycopg 가 접속 단계에 SQLSTATE 를 주지 않으므로 메시지 규칙으로 가른다
(`classify_connect_error`). detail 은 우리가 만든 한 줄이고 libpq 원문은 싣지 않는다(원문에
사용자명이 든다). SQL 식별자는 `psycopg.sql.Identifier` 로만 넣는다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from daegam.errors import ServiceError
from daegam.store.data_services import DataService


@dataclass(frozen=True)
class PostgresCheck:
    """`verify_postgres` 결과. `services/data_services.py` 가 Database verify 응답
    (`VerifyResult`)으로 옮긴다. pg 가 그 모듈을 import 하지 않아야 순환이 없다."""

    ok: bool
    error: str | None = None
    detail: str | None = None
    extensions: dict[str, Any] | None = None


CONNECT_TIMEOUT = 3
# HNSW 는 vector(n) 2000 차원까지다. 그 위는 halfvec 표현식 인덱스를 쓴다
HNSW_MAX_DIMENSIONS = 2000
EXTENSION = "vector"
AUTH_SQLSTATES = ("28P01", "28000")
UNKNOWN_DATABASE_SQLSTATE = "3D000"
_AUTH_MESSAGES = ("password authentication failed", "no pg_hba.conf entry")
_UNKNOWN_DATABASE_RE = re.compile(r'database ".*" does not exist')

# (chunk_no, content, embedding, meta)
ChunkRow = tuple[int, str, Sequence[float], dict[str, Any]]


# 접속 실패의 응답 코드. classify_connect_error 의 connect_failed 를 화면 문구 하나로 낸다
CONNECT_FAILED_CODE = "db_unreachable"


def response_code(code: str) -> str:
    """접속 오류 코드를 응답 코드로. `connect_failed` 만 `db_unreachable` 이 되고 나머지
    (`auth_failed`·`unknown_database`)는 그대로다."""
    return CONNECT_FAILED_CODE if code == "connect_failed" else code


def classify_connect_error(e: Exception) -> tuple[str, str]:
    """(code, detail). `sqlstate` 가 있으면 그것으로, 없으면 메시지 규칙으로 가른다.
    detail 은 코드와 예외 클래스 이름뿐이다."""
    sqlstate = getattr(e, "sqlstate", None)
    message = str(e)
    if sqlstate in AUTH_SQLSTATES:
        code = "auth_failed"
    elif sqlstate == UNKNOWN_DATABASE_SQLSTATE:
        code = "unknown_database"
    elif sqlstate:
        code = "connect_failed"
    elif any(m in message for m in _AUTH_MESSAGES):
        code = "auth_failed"
    elif _UNKNOWN_DATABASE_RE.search(message):
        code = "unknown_database"
    else:
        code = "connect_failed"
    detail = f"{code} ({type(e).__name__}"
    if sqlstate:
        detail += f" {sqlstate}"
    return code, detail + ")"


def index_kind(dim: int) -> str:
    """2000 이하는 `vector`(vector_cosine_ops), 그 위는 `halfvec` 표현식 인덱스다."""
    return "vector" if dim <= HNSW_MAX_DIMENSIONS else "halfvec"


async def connect(
    ds: DataService, password: str | None, *, statement_timeout_seconds: float
) -> AsyncConnection:
    """autocommit 접속. 트랜잭션이 필요한 곳은 `conn.transaction()` 으로 연다."""
    conninfo = make_conninfo(
        host=ds.host, port=ds.port, dbname=ds.database, user=ds.username, password=password
    )
    ms = int(statement_timeout_seconds * 1000)
    return await AsyncConnection.connect(
        conninfo,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT,
        options=f"-c statement_timeout={ms}",
    )


async def connect_or_raise(
    ds: DataService,
    password: str | None,
    *,
    statement_timeout_seconds: float,
    err: type[ServiceError],
) -> AsyncConnection:
    """`connect` 의 접속 오류를 호출자의 오류 종류(err)로 올린다. 코드는 `response_code`, detail
    은 `host:port: 한 줄` 이다. 호출자가 자기 오류만 좁게 잡을 수 있도록 종류를 받는다."""
    try:
        return await connect(ds, password, statement_timeout_seconds=statement_timeout_seconds)
    except (psycopg.Error, OSError) as e:
        code, detail = classify_connect_error(e)
        raise err(422, response_code(code), f"{ds.host}:{ds.port}: {detail}") from e


async def extensions(conn: AsyncConnection) -> dict[str, Any]:
    """`{vector: {installed: "0.8.6"|None, available: bool}}`. verify 와 Knowledge Base 생성이 같은
    조회로 `capabilities.pgvector` 를 채운다."""
    installed = await (
        await conn.execute("SELECT extversion FROM pg_extension WHERE extname = %s", (EXTENSION,))
    ).fetchone()
    available = await (
        await conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", (EXTENSION,))
    ).fetchone()
    return {
        EXTENSION: {
            "installed": installed[0] if installed else None,
            "available": available is not None,
        }
    }


async def verify_postgres(
    ds: DataService, password: str | None, *, statement_timeout_seconds: float = 5.0
) -> PostgresCheck:
    """접속, SELECT 1, pg_extension·pg_available_extensions 순이다. 예외를 던지지 않는다."""
    target = f"{ds.host}:{ds.port}"
    try:
        conn = await connect(ds, password, statement_timeout_seconds=statement_timeout_seconds)
    except (psycopg.Error, OSError) as e:
        code, detail = classify_connect_error(e)
        return PostgresCheck(False, code, f"{target}: {detail}")
    try:
        async with conn:
            await (await conn.execute("SELECT 1")).fetchone()
            found = await extensions(conn)
    except (psycopg.Error, OSError) as e:
        return PostgresCheck(False, "query_failed", f"{target}: {type(e).__name__}")
    return PostgresCheck(True, detail=f"{target}: connected", extensions=found)


async def ensure_extension(conn: AsyncConnection) -> None:
    """확장은 trusted 가 아니라 슈퍼유저만 만들 수 있다. 실패는 호출자가 pgvector_missing 으로
    접는다."""
    await conn.execute(
        sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(EXTENSION))
    )


def index_name(table: str) -> str:
    return f"{table}_embedding_idx"


async def create_table(conn: AsyncConnection, table: str, dim: int) -> None:
    """CREATE TABLE IF NOT EXISTS + HNSW 코사인 인덱스. 2000 차원 위는 halfvec 표현식 인덱스다."""
    t = sql.Identifier(table)
    await conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {t} ("
            "doc_id text NOT NULL, chunk_no integer NOT NULL, content text NOT NULL, "
            "embedding vector({dim}) NOT NULL, meta jsonb NOT NULL DEFAULT '{{}}'::jsonb, "
            "PRIMARY KEY (doc_id, chunk_no))"
        ).format(t=t, dim=sql.Literal(dim))
    )
    idx = sql.Identifier(index_name(table))
    if index_kind(dim) == "vector":
        stmt = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw (embedding vector_cosine_ops)"
        ).format(i=idx, t=t)
    else:
        stmt = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw "
            "((embedding::halfvec({dim})) halfvec_cosine_ops)"
        ).format(i=idx, t=t, dim=sql.Literal(dim))
    await conn.execute(stmt)


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector 가 읽는 `'[a,b,c]'` 문자열. `::vector` 캐스트와 함께 쓴다."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


async def replace_chunks(
    conn: AsyncConnection, table: str, doc_id: str, rows: Sequence[ChunkRow]
) -> int:
    """한 트랜잭션으로 그 doc_id 의 행을 지우고 다시 넣는다. 돌려주는 값은 넣은 행 수다."""
    t = sql.Identifier(table)
    async with conn.transaction():
        await conn.execute(sql.SQL("DELETE FROM {t} WHERE doc_id = %s").format(t=t), (doc_id,))
        if rows:
            async with conn.cursor() as cur:
                await cur.executemany(
                    sql.SQL(
                        "INSERT INTO {t} (doc_id, chunk_no, content, embedding, meta) "
                        "VALUES (%s, %s, %s, %s::vector, %s)"
                    ).format(t=t),
                    [
                        (doc_id, chunk_no, content, vector_literal(embedding), Jsonb(meta))
                        for chunk_no, content, embedding, meta in rows
                    ],
                )
    return len(rows)


async def delete_doc_rows(conn: AsyncConnection, table: str, doc_id: str) -> int:
    cur = await conn.execute(
        sql.SQL("DELETE FROM {t} WHERE doc_id = %s").format(t=sql.Identifier(table)), (doc_id,)
    )
    return cur.rowcount


async def drop_table(conn: AsyncConnection, table: str) -> None:
    await conn.execute(sql.SQL("DROP TABLE IF EXISTS {t}").format(t=sql.Identifier(table)))


async def table_exists(conn: AsyncConnection, table: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT 1 FROM pg_catalog.pg_tables "
            "WHERE schemaname = ANY (current_schemas(false)) AND tablename = %s",
            (table,),
        )
    ).fetchone()
    return row is not None
