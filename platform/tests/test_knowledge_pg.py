"""psycopg 접속·pgvector 문장과 임베딩 클라이언트 (D32, F1). psycopg 는 목이고 실제 DB 는 없다."""

from __future__ import annotations

import asyncio
import json

import httpx2
import openai
import psycopg
import psycopg.errors as pe
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from daegam.services.data_services import pgvector_capability
from daegam.services.knowledge import pg
from daegam.services.knowledge.embeddings import (
    EmbeddingError,
    StubEmbeddings,
    build_embeddings,
    classify_embedding_error,
)
from daegam.services.knowledge.stub_embedding import hash_embedding
from tests.helpers.docs import data_service_doc
from tests.helpers.fake_pg import EXT_AVAILABLE, EXT_MISSING, EXT_PRESENT, FakeConn

# -- 접속 오류 분류 --------------------------------------------------------------------------


def test_classify_connect_error_without_sqlstate_uses_message_rules():
    """psycopg 는 접속 단계의 OperationalError 에 sqlstate 를 채우지 않는다."""
    cases = {
        'connection failed: FATAL:  password authentication failed for user "alice"': "auth_failed",
        'connection failed: FATAL:  no pg_hba.conf entry for host "10.0.0.9", user "alice", '
        'database "app", no encryption': "auth_failed",
        'connection failed: FATAL:  database "nope" does not exist': "unknown_database",
        'connection failed: connection to server at "h" (10.0.0.1), port 5432 failed: '
        "Connection refused": "connect_failed",
        "connection timeout expired": "connect_failed",
        'could not translate host name "nope.invalid" to address: nodename nor servname '
        "provided": "connect_failed",
    }
    for message, expected in cases.items():
        e = psycopg.OperationalError(message)
        assert e.sqlstate is None
        code, detail = pg.classify_connect_error(e)
        assert code == expected, message
        assert detail == f"{expected} (OperationalError)"
        assert "alice" not in detail and "FATAL" not in detail and "10.0" not in detail


def test_classify_connect_error_with_sqlstate():
    code, detail = pg.classify_connect_error(pe.InvalidPassword("password authentication failed"))
    assert (code, detail) == ("auth_failed", "auth_failed (InvalidPassword 28P01)")
    code, _ = pg.classify_connect_error(pe.lookup("28000")("x"))
    assert code == "auth_failed"
    code, detail = pg.classify_connect_error(pe.InvalidCatalogName('database "x" does not exist'))
    assert (code, detail) == ("unknown_database", "unknown_database (InvalidCatalogName 3D000)")
    # 다른 sqlstate 는 메시지를 보지 않고 connect_failed 다
    code, _ = pg.classify_connect_error(pe.CannotConnectNow("password authentication failed"))
    assert code == "connect_failed"
    code, detail = pg.classify_connect_error(OSError("network unreachable"))
    assert (code, detail) == ("connect_failed", "connect_failed (OSError)")


def test_index_kind_boundary():
    assert pg.index_kind(64) == "vector"
    assert pg.index_kind(2000) == "vector"
    assert pg.index_kind(2001) == "halfvec"
    assert pg.index_kind(3072) == "halfvec"


# -- 접속과 verify ---------------------------------------------------------------------------


def test_connect_puts_timeouts_in_conninfo(fake_pg):
    asyncio.run(pg.connect(data_service_doc(), "p@ss:w/rd#1", statement_timeout_seconds=5))
    conninfo, kwargs = fake_pg.calls[0]
    assert conninfo_to_dict(conninfo) == {
        "host": "h",
        "port": "5432",
        "dbname": "app",
        "user": "u",
        "password": "p@ss:w/rd#1",
    }
    assert kwargs == {
        "autocommit": True,
        "connect_timeout": 3,
        "options": "-c statement_timeout=5000",
    }
    effective = conninfo_to_dict(
        make_conninfo(conninfo, **{k: v for k, v in kwargs.items() if k != "autocommit"})
    )
    assert effective["connect_timeout"] == "3"
    assert effective["options"] == "-c statement_timeout=5000"
    # 비밀번호·dbname·user 가 없으면 conninfo 에서 빠진다
    asyncio.run(
        pg.connect(
            data_service_doc(database=None, username=None), None, statement_timeout_seconds=120
        )
    )
    conninfo, kwargs = fake_pg.calls[1]
    assert conninfo_to_dict(conninfo) == {"host": "h", "port": "5432"}
    assert kwargs["options"] == "-c statement_timeout=120000"


def test_verify_postgres_reports_extensions(fake_pg):
    fake_pg.conn = FakeConn(EXT_PRESENT)
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "pw"))
    assert r == pg.PostgresCheck(
        True,
        detail="h:5432: connected",
        extensions={"vector": {"installed": "0.8.6", "available": True}},
    )
    assert fake_pg.calls[0][1]["options"] == "-c statement_timeout=5000"
    assert fake_pg.conn.closed
    executed = [q for q, _ in fake_pg.conn.executed]
    assert executed[0] == "SELECT 1"
    assert any("pg_extension" in q for q in executed)
    assert any("pg_available_extensions" in q for q in executed)
    assert pgvector_capability(r.extensions) == "installed"

    fake_pg.conn = FakeConn(EXT_AVAILABLE)
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "pw"))
    assert r.ok and r.extensions == {"vector": {"installed": None, "available": True}}
    assert pgvector_capability(r.extensions) == "available"

    fake_pg.conn = FakeConn(EXT_MISSING)
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "pw"))
    assert r.ok and r.extensions == {"vector": {"installed": None, "available": False}}
    assert pgvector_capability(r.extensions) == "missing"
    assert pgvector_capability(None) is None


def test_verify_postgres_connect_errors(fake_pg):
    fake_pg.error = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "alice"'
    )
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "wrong"))
    assert r == pg.PostgresCheck(False, "auth_failed", "h:5432: auth_failed (OperationalError)")
    fake_pg.error = pe.InvalidPassword("x")
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "wrong"))
    assert r.error == "auth_failed" and "28P01" in r.detail
    fake_pg.error = psycopg.OperationalError('FATAL:  database "nope" does not exist')
    assert asyncio.run(pg.verify_postgres(data_service_doc(), "pw")).error == "unknown_database"
    fake_pg.error = OSError("boom")
    assert asyncio.run(pg.verify_postgres(data_service_doc(), "pw")).error == "connect_failed"
    # 접속 뒤 문장이 실패하면 query_failed 이고 원문은 없다
    fake_pg.error = None
    fake_pg.conn = FakeConn(fail=pe.QueryCanceled("canceling statement due to statement timeout"))
    r = asyncio.run(pg.verify_postgres(data_service_doc(), "pw"))
    assert r == pg.PostgresCheck(False, "query_failed", "h:5432: QueryCanceled")
    assert fake_pg.conn.closed


# -- 테이블·행 ---------------------------------------------------------------------------


def test_create_table_vector_and_halfvec():
    conn = FakeConn()
    asyncio.run(pg.ensure_extension(conn))
    assert conn.executed[0][0] == 'CREATE EXTENSION IF NOT EXISTS "vector"'
    conn = FakeConn()
    asyncio.run(pg.create_table(conn, "daegam_kb_abc", 64))
    create, index = (q for q, _ in conn.executed)
    assert create.startswith('CREATE TABLE IF NOT EXISTS "daegam_kb_abc" (')
    assert "embedding vector(64) NOT NULL" in create and "PRIMARY KEY (doc_id, chunk_no)" in create
    assert index == (
        'CREATE INDEX IF NOT EXISTS "daegam_kb_abc_embedding_idx" ON "daegam_kb_abc" '
        "USING hnsw (embedding vector_cosine_ops)"
    )
    conn = FakeConn()
    asyncio.run(pg.create_table(conn, "daegam_kb_big", 3072))
    create, index = (q for q, _ in conn.executed)
    assert "embedding vector(3072) NOT NULL" in create
    assert index == (
        'CREATE INDEX IF NOT EXISTS "daegam_kb_big_embedding_idx" ON "daegam_kb_big" '
        "USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)"
    )


def test_replace_chunks_is_one_transaction():
    conn = FakeConn()
    rows = [(0, "first", [0.1, -1, 2], {"name": "a.md"}), (1, "second", [0.0, 0.5, 1], {})]
    assert asyncio.run(pg.replace_chunks(conn, "daegam_kb_abc", "doc-1", rows)) == 2
    assert conn.transactions == ["begin", "commit"]
    assert conn.executed == [('DELETE FROM "daegam_kb_abc" WHERE doc_id = %s', ("doc-1",))]
    query, params = conn.many[0]
    assert query == (
        'INSERT INTO "daegam_kb_abc" (doc_id, chunk_no, content, embedding, meta) '
        "VALUES (%s, %s, %s, %s::vector, %s)"
    )
    assert params[0][:4] == ("doc-1", 0, "first", "[0.1,-1.0,2.0]")
    assert isinstance(params[0][4], Jsonb) and params[0][4].obj == {"name": "a.md"}
    assert params[1][:4] == ("doc-1", 1, "second", "[0.0,0.5,1.0]")
    # 빈 목록은 DELETE 만 한다
    conn = FakeConn()
    assert asyncio.run(pg.replace_chunks(conn, "t", "doc-1", [])) == 0
    assert len(conn.executed) == 1 and conn.many == []
    assert pg.vector_literal([1, 2.5]) == "[1.0,2.5]"


def test_delete_drop_and_exists():
    conn = FakeConn({"DELETE": [(1,), (1,)]})
    assert asyncio.run(pg.delete_doc_rows(conn, "daegam_kb_abc", "doc-1")) == 2
    assert conn.executed[0] == ('DELETE FROM "daegam_kb_abc" WHERE doc_id = %s', ("doc-1",))
    asyncio.run(pg.drop_table(conn, "daegam_kb_abc"))
    assert conn.executed[1][0] == 'DROP TABLE IF EXISTS "daegam_kb_abc"'
    assert asyncio.run(pg.table_exists(conn, "daegam_kb_abc")) is True
    assert conn.executed[2][1] == ("daegam_kb_abc",)
    assert asyncio.run(pg.table_exists(FakeConn({"pg_tables": []}), "daegam_kb_abc")) is False


# -- 임베딩 클라이언트 ----------------------------------------------------------------------


def _embedding_response(body: dict, dim: int = 3) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.1] * dim}
                for i in range(len(body["input"]))
            ],
            "model": body["model"],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


def test_build_embeddings_stub():
    emb = build_embeddings("stub", api_key=None, model="stub", dimensions=8)
    assert isinstance(emb, StubEmbeddings)
    assert asyncio.run(emb.aembed_documents(["a", "b"])) == [
        hash_embedding("a", 8),
        hash_embedding("b", 8),
    ]
    assert asyncio.run(emb.aembed_query("a")) == hash_embedding("a", 8)
    assert emb.embed_query("a") == hash_embedding("a", 8)
    with pytest.raises(EmbeddingError) as ei:
        build_embeddings("anthropic", api_key="k", model="x", dimensions=1)
    assert ei.value.code == "embedding_not_supported"


def test_build_embeddings_openai_sends_strings_not_token_ids():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _embedding_response(json.loads(request.content))

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    emb = build_embeddings(
        "openai",
        api_key="sk-x",
        model="text-embedding-3-small",
        dimensions=3,
        base_url="http://gw/v1",
        http_async_client=http,
    )
    assert emb.check_embedding_ctx_length is False
    # SDK 재시도는 없다. 호출 id 하나가 요청 하나다 (P37 2절)
    assert (emb.chunk_size, emb.max_retries, emb.request_timeout) == (64, 0, 60.0)
    assert emb.dimensions == 3
    out = asyncio.run(emb.aembed_documents(["hello world", "안녕하세요"]))
    assert out == [[0.1] * 3, [0.1] * 3]
    body = json.loads(seen[0].content)
    assert body["input"] == ["hello world", "안녕하세요"]  # 토큰 id 배열이 아니다
    assert body["model"] == "text-embedding-3-small" and body["dimensions"] == 3
    assert str(seen[0].url) == "http://gw/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer sk-x"
    # 64 개씩 나눠 보낸다
    seen.clear()
    asyncio.run(emb.aembed_documents([f"t{i}" for i in range(130)]))
    assert [len(json.loads(r.content)["input"]) for r in seen] == [64, 64, 2]
    # text-embedding-3 계열이 아니면 dimensions 를 싣지 않는다
    seen.clear()
    other = build_embeddings(
        "openai", api_key="k", model="custom-embed", dimensions=3, http_async_client=http
    )
    assert other.dimensions is None
    asyncio.run(other.aembed_query("q"))
    assert "dimensions" not in json.loads(seen[0].content)
    assert json.loads(seen[0].content)["input"] == ["q"]


def test_classify_embedding_error_keeps_only_class_and_status():
    request = httpx2.Request("POST", "http://gw/v1/embeddings")
    e = openai.AuthenticationError(
        "Incorrect API key provided: sk-secr***",
        response=httpx2.Response(401, request=request),
        body={"error": {"message": "Incorrect API key provided: sk-secr***"}},
    )
    err = classify_embedding_error(e)
    assert (err.code, err.detail) == ("embedding_failed", "AuthenticationError 401")
    err = classify_embedding_error(openai.APIConnectionError(request=request))
    assert err.detail == "APIConnectionError"
    err = classify_embedding_error(RuntimeError("leak sk-secret"))
    assert err.detail == "RuntimeError" and "sk-" not in str(err)
