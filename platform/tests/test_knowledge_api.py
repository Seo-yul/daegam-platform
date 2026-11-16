"""Knowledge Base API (D32, F2). pg 는 psycopg 목이고 Database Secret 은 fake 클러스터에 있다.

422 표 전부, POST 가 확장·테이블을 만들고 실패하면 리소스가 없다, 업로드 413·형식, URL 의 SSRF 거부,
chunking PATCH → pending·indexing 409, 문서 DELETE → deleting, KB DELETE 순서·orphaned_table·in_use,
deleting_at 인 KB 의 쓰기 409, Database 삭제 409.
"""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg.errors as pe
import pytest

from daegam.services.knowledge import service as service_mod
from daegam.services.references import Reference, ReferenceService
from daegam.store.base import utcnow
from tests.helpers.clusters import default_cluster
from tests.helpers.consts import DS, KB, LLM_KEY_SECRET, T2
from tests.helpers.data_services import created_database
from tests.helpers.fake_pg import EXT_AVAILABLE, EXT_PRESENT, FakeConn
from tests.helpers.http import detail_code

PROVIDERS = [
    {"name": "stub-e2e", "kind": "stub"},
    {"name": "oai", "kind": "openai", "api_key_secret": LLM_KEY_SECRET},
    {"name": "oai-nokey", "kind": "openai"},
    {"name": "claude", "kind": "anthropic", "api_key_secret": LLM_KEY_SECRET},
]


def _bootstrap(client) -> str:
    """기본 클러스터와 PROVIDERS 의 provider 넷을 둔다."""
    cid = default_cluster(client)
    assert client.patch("/settings", json={"llm_providers": PROVIDERS}).status_code == 200
    return cid


def _data_service(client, name="pg", kind="postgres", **extra) -> dict:
    """pg.svc:5432 의 Database. 접속 비밀번호 pg-pass 와 postgres 사용자 alice 를 테스트가 본다."""
    if kind == "postgres":
        extra = {"username": "alice", **extra}
    return created_database(
        client, name, kind, host="pg.svc", port=5432, password="pg-pass", **extra
    )


def _create(client, ds_id: str, name="faq", provider="stub-e2e", **extra):
    body = {"name": name, "data_service_id": ds_id, "embedding": {"provider": provider}}
    body.update(extra)
    return client.post(KB, json=body)


def _kb(client, fake_pg, name="faq", **extra) -> dict:
    ds = _data_service(client, name=f"ds-{name}")
    r = _create(client, ds["id"], name=name, **extra)
    assert r.status_code == 201, r.text
    return r.json()


def _upload(client, kb_id: str, name="a.md", data=b"# hello\n\nworld", content_type=None):
    files = {"file": (name, data, content_type or "application/octet-stream")}
    return client.post(f"{KB}/{kb_id}/documents", files=files)


def _executed(fake_pg) -> list[str]:
    return [q for q, _ in fake_pg.conn.executed]


# -- 생성 --------------------------------------------------------------------------------


def test_create_makes_extension_table_and_index(client, mongo, fake_pg):
    """POST 는 그 자리에서 접속해 capabilities 를 갱신하고 확장·테이블·인덱스를 만든 뒤 저장한다."""
    _bootstrap(client)
    ds = _data_service(client)
    assert ds["capabilities"] == {}
    fake_pg.conn = FakeConn(EXT_AVAILABLE)
    r = _create(client, ds["id"], display_name="FAQ", chunking={"size": 500, "overlap": 50})
    assert r.status_code == 201, r.text
    kb = r.json()
    assert kb["name"] == "faq" and kb["display_name"] == "FAQ"
    assert kb["table"] == f"daegam_kb_{kb['id'].replace('-', '')}"
    assert kb["embedding"] == {"provider": "stub-e2e", "model": "stub", "dimensions": 64}
    assert kb["chunking"] == {"size": 500, "overlap": 50}
    assert kb["limits"] == {"max_chunks": 100000}
    assert kb["state"] == "ready" and kb["deleting_at"] is None
    assert kb["status"] == {
        "documents": 0,
        "indexed": 0,
        "failed": 0,
        "chunks": 0,
        "tokens": 0,
        "last_indexed_at": None,
    }
    assert "pg-pass" not in r.text
    # 접속: 비밀번호는 Secret 에서, DDL 문장 타임아웃 30초
    conninfo, kwargs = fake_pg.calls[0]
    assert "password=pg-pass" in conninfo and "user=alice" in conninfo and "dbname=app" in conninfo
    assert kwargs["connect_timeout"] == 3 and kwargs["options"] == "-c statement_timeout=30000"
    executed = _executed(fake_pg)
    assert any("pg_extension" in q for q in executed)
    assert any("pg_available_extensions" in q for q in executed)
    assert 'CREATE EXTENSION IF NOT EXISTS "vector"' in executed
    create = next(q for q in executed if q.startswith("CREATE TABLE"))
    assert f'"{kb["table"]}"' in create and "vector(64)" in create
    index = next(q for q in executed if q.startswith("CREATE INDEX"))
    assert "hnsw (embedding vector_cosine_ops)" in index
    assert executed.index('CREATE EXTENSION IF NOT EXISTS "vector"') < executed.index(create)
    assert fake_pg.conn.closed
    # capabilities 는 verify 와 같은 조회로 갱신된다
    assert client.get(f"{DS}/{ds['id']}").json()["capabilities"] == {"pgvector": "available"}
    doc = mongo["knowledge_bases"].find_one({"_id": kb["id"]})
    assert doc["tenant"] == "default" and "status" not in doc and doc["table"] == kb["table"]
    # 목록·상세도 집계를 싣고 다른 테넌트에는 보이지 않는다
    assert [k["id"] for k in client.get(KB).json()] == [kb["id"]]
    assert client.get(f"{KB}/{kb['id']}").json()["status"]["documents"] == 0
    assert client.get(KB, headers=T2).json() == []
    assert client.get(f"{KB}/{kb['id']}", headers=T2).status_code == 404
    # 같은 이름은 409
    assert _create(client, ds["id"]).status_code == 409
    assert _create(client, ds["id"]).json()["detail"]["code"] == "name_conflict"


def test_create_embedding_models_and_dimensions(client, fake_pg):
    _bootstrap(client)
    ds = _data_service(client)
    # openai 기본 모델은 알려진 차원
    r = _create(client, ds["id"], name="a", provider="oai")
    assert r.status_code == 201, r.text
    assert r.json()["embedding"] == {
        "provider": "oai",
        "model": "text-embedding-3-small",
        "dimensions": 1536,
    }
    # 3072 차원은 halfvec 표현식 인덱스
    r = _create(
        client,
        ds["id"],
        name="b",
        embedding={"provider": "oai", "model": "text-embedding-3-large"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["embedding"]["dimensions"] == 3072
    assert any("halfvec(3072)" in q for q in _executed(fake_pg))
    # 모르는 모델은 dimensions 를 직접 받는다
    r = _create(
        client,
        ds["id"],
        name="c",
        embedding={"provider": "oai", "model": "custom-embed", "dimensions": 256},
    )
    assert r.status_code == 201, r.text
    assert r.json()["embedding"] == {"provider": "oai", "model": "custom-embed", "dimensions": 256}
    # provider 행의 embedding_model 을 따른다
    providers = PROVIDERS + [
        {"name": "gw", "kind": "openai", "api_key_secret": LLM_KEY_SECRET, "embedding_model": "x"}
    ]
    assert client.patch("/settings", json={"llm_providers": providers}).status_code == 200
    r = _create(client, ds["id"], name="d", embedding={"provider": "gw", "dimensions": 8})
    assert r.status_code == 201 and r.json()["embedding"]["model"] == "x"


def test_create_422_table_leaves_no_resource(client, mongo, fake_pg):
    """422 코드 표 전부. 어느 단계에서 실패해도 리소스는 생기지 않는다."""
    _bootstrap(client)
    ds = _data_service(client)
    redis = _data_service(client, name="cache", kind="redis")

    def expect(code: str, status: int = 422, **kw):
        r = _create(client, kw.pop("ds_id", ds["id"]), **kw)
        assert r.status_code == status, r.text
        assert r.json()["detail"]["code"] == code, r.text
        assert client.get(KB).json() == [], code
        assert mongo["knowledge_bases"].count_documents({}) == 0
        return r.json()["detail"]

    expect("invalid_name", name="Bad Name")
    expect("unknown_data_service", ds_id="nope")
    expect("invalid_data_service_kind", ds_id=redis["id"])
    expect("unknown_provider", provider="nope")
    expect("embedding_not_supported", provider="claude")
    expect("provider_no_key", provider="oai-nokey")
    expect("dimensions_required", embedding={"provider": "oai", "model": "custom-embed"})
    # 접속 실패 셋. detail 은 host:port 와 코드·클래스뿐이고 libpq 원문(사용자명)은 없다
    fake_pg.error = psycopg.OperationalError(
        'connection failed: connection to server at "pg.svc" (10.0.0.1), port 5432 failed: '
        "Connection refused"
    )
    d = expect("db_unreachable")
    assert d["detail"] == "pg.svc:5432: connect_failed (OperationalError)"
    fake_pg.error = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "alice"'
    )
    d = expect("auth_failed")
    assert "alice" not in d["detail"]
    fake_pg.error = pe.InvalidCatalogName('database "app" does not exist')
    expect("unknown_database")
    fake_pg.error = None
    # 확장을 못 만들면 pgvector_missing 이고 안내 문구가 붙는다. 테이블은 만들지 않는다
    fake_pg.conn = FakeConn(
        EXT_AVAILABLE,
        fail_on={"CREATE EXTENSION": pe.InsufficientPrivilege("permission denied")},
    )
    d = expect("pgvector_missing")
    assert "슈퍼유저가 미리 만들어야 한다" in d["detail"] and "InsufficientPrivilege" in d["detail"]
    assert not any(q.startswith("CREATE TABLE") for q in _executed(fake_pg))
    # 테이블을 못 만들면 query_failed
    fake_pg.conn = FakeConn(
        EXT_PRESENT, fail_on={"CREATE TABLE": pe.QueryCanceled("statement timeout")}
    )
    d = expect("query_failed")
    assert "QueryCanceled" in d["detail"]
    # 다른 테넌트의 Database 는 보이지 않는다
    r = client.post(
        KB,
        json={"name": "x", "data_service_id": ds["id"], "embedding": {"provider": "stub-e2e"}},
        headers=T2,
    )
    assert detail_code(r) == (422, "unknown_data_service")
    # 본문 형식 오류는 FastAPI 422 (목록)
    r = client.post(KB, json={"name": "x", "data_service_id": ds["id"]})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    r = _create(client, ds["id"], chunking={"size": 100})
    assert r.status_code == 422


# -- 수정 --------------------------------------------------------------------------------


def test_patch_rules_and_chunking_marks_pending(client, mongo, fake_pg):
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    url = f"{KB}/{kb['id']}"
    assert client.patch(url, json={}).json()["detail"]["code"] == "empty_patch"
    r = client.patch(url, json={"embedding": {"provider": "oai"}})
    assert detail_code(r) == (422, "embedding_immutable")
    r = client.patch(url, json={"data_service_id": "other"})
    assert detail_code(r) == (422, "data_service_immutable")
    r = client.patch(url, json={"name": "renamed"})
    assert detail_code(r) == (422, "name_immutable")
    r = client.patch(url, json={"name": "faq", "display_name": "자주 묻는 질문"})
    assert r.status_code == 200 and r.json()["display_name"] == "자주 묻는 질문"
    assert client.patch(url, json={"display_name": "  "}).status_code == 422
    assert client.patch(url, json={"chunking": None}).status_code == 422

    a = _upload(client, kb["id"], "a.md").json()
    b = _upload(client, kb["id"], "b.md").json()
    c = _upload(client, kb["id"], "c.md").json()
    docs = mongo["knowledge_documents"]
    docs.update_one(
        {"_id": a["id"]},
        {"$set": {"state": "indexed", "chunks": 3, "lease": {"owner": "w", "until": utcnow()}}},
    )
    docs.update_one({"_id": b["id"]}, {"$set": {"state": "failed", "error": {"code": "x"}}})
    docs.update_one({"_id": c["id"]}, {"$set": {"state": "deleting"}})
    # limits 만 바꾸면 문서는 그대로다
    r = client.patch(url, json={"limits": {"max_chunks": 10}})
    assert r.status_code == 200 and r.json()["limits"] == {"max_chunks": 10}
    assert docs.find_one({"_id": a["id"]})["state"] == "indexed"
    # 같은 chunking 은 되돌리지 않고, 다른 chunking 은 pending 으로 되돌린다 (deleting 은 제외)
    r = client.patch(url, json={"chunking": {"size": 1200, "overlap": 150}})
    assert r.status_code == 200 and docs.find_one({"_id": a["id"]})["state"] == "indexed"
    r = client.patch(url, json={"chunking": {"size": 800, "overlap": 100}})
    assert r.status_code == 200, r.text
    assert r.json()["chunking"] == {"size": 800, "overlap": 100}
    got_a = docs.find_one({"_id": a["id"]})
    assert got_a["state"] == "pending" and "lease" not in got_a and got_a["chunks"] == 3
    got_b = docs.find_one({"_id": b["id"]})
    assert got_b["state"] == "pending" and got_b["error"] is None
    assert docs.find_one({"_id": c["id"]})["state"] == "deleting"
    # indexing 문서가 있으면 chunking 변경은 409
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "indexing"}})
    r = client.patch(url, json={"chunking": {"size": 600, "overlap": 0}})
    assert detail_code(r) == (409, "indexing")
    assert client.get(url).json()["chunking"] == {"size": 800, "overlap": 100}
    # display_name 만은 indexing 중에도 된다
    assert client.patch(url, json={"display_name": "FAQ"}).status_code == 200


def test_a_patch_that_read_before_a_delete_started_gets_409_deleting(client, mongo, fake_pg):
    """삭제가 시작되기 전에 읽은 수정은 `deleting_at` 을 지우지 않고 409 deleting 이다. 그사이 다른
    수정이 지나갔으면 409 revision_conflict 다 (P68 2절)."""
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    svc = client.app.state.knowledge_service
    stale = client.portal.call(svc.store.get, kb["id"])
    assert client.patch(f"{KB}/{kb['id']}", json={"display_name": "A"}).status_code == 200
    with pytest.raises(service_mod.KnowledgeError) as e:
        client.portal.call(lambda: svc.patch(stale, display_name="B"))
    assert (e.value.status, e.value.code) == (409, "revision_conflict")
    fresh = client.portal.call(svc.store.get, kb["id"])
    mongo["knowledge_bases"].update_one({"_id": kb["id"]}, {"$set": {"deleting_at": utcnow()}})
    with pytest.raises(service_mod.KnowledgeError) as e:
        client.portal.call(lambda: svc.patch(fresh, display_name="C"))
    assert (e.value.status, e.value.code) == (409, "deleting")
    doc = mongo["knowledge_bases"].find_one({"_id": kb["id"]})
    assert doc["deleting_at"] is not None and doc["display_name"] == "A"


# -- 문서 --------------------------------------------------------------------------------


def test_upload_documents(client, mongo, fake_pg, monkeypatch):
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    body = b"# petstore\n\ninventory is counted nightly"
    r = _upload(client, kb["id"], "faq.md", body)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["knowledge_base_id"] == kb["id"] and doc["tenant"] == "default"
    assert doc["state"] == "pending" and doc["source"] == {"kind": "upload", "url": None}
    assert doc["content_type"] == "text/markdown" and doc["bytes"] == len(body)
    assert doc["sha256"] and doc["attempts"] == 0 and doc["lease"] is None
    assert doc["usage"] == {"tokens": 0} and doc["chunks"] == 0 and doc["error"] is None
    stored = mongo["knowledge_docs.files"].find_one({"filename": f"{kb['id']}/{doc['id']}"})
    assert stored["metadata"] == {
        "kb_id": kb["id"],
        "doc_id": doc["id"],
        "content_type": "text/markdown",
    }
    # content type 만으로도 형식을 가른다
    r = _upload(client, kb["id"], "noext", b"<html><body>hi</body></html>", "text/html")
    assert r.status_code == 201 and r.json()["content_type"] == "text/html"
    r = _upload(client, kb["id"], "data.json", b"{}", "application/octet-stream")
    assert r.status_code == 201 and r.json()["content_type"] == "application/json"
    # 형식 밖은 422(옛 이진 형식과 이미지), 크기 초과는 413
    r = _upload(client, kb["id"], "slides.ppt", b"x")
    assert detail_code(r) == (422, "unsupported_format")
    assert ".hwpx" in r.json()["detail"]["detail"] and ".png" not in r.json()["detail"]["detail"]
    r = _upload(client, kb["id"], "photo.png", b"\x89PNG", "image/png")
    assert detail_code(r) == (422, "unsupported_format")
    r = _upload(client, kb["id"], "big.txt", b"x" * (service_mod.MAX_DOCUMENT_BYTES + 1))
    assert detail_code(r) == (413, "too_large")
    assert mongo["knowledge_documents"].count_documents({}) == 3
    # 문서 수 상한
    monkeypatch.setattr(service_mod, "MAX_DOCUMENTS", 3)
    r = _upload(client, kb["id"], "d.txt", b"x")
    assert detail_code(r) == (422, "too_many_documents")
    # 목록·상세·404
    listed = client.get(f"{KB}/{kb['id']}/documents").json()
    assert [d["name"] for d in listed] == ["faq.md", "noext", "data.json"]
    assert client.get(f"{KB}/{kb['id']}/documents/{doc['id']}").json()["name"] == "faq.md"
    assert client.get(f"{KB}/{kb['id']}/documents/nope").status_code == 404
    assert client.get(f"{KB}/nope/documents").status_code == 404
    assert client.get(f"{KB}/{kb['id']}/documents", headers=T2).status_code == 404
    summary = client.get(f"{KB}/{kb['id']}").json()["status"]
    assert summary["documents"] == 3 and summary["indexed"] == 0


def test_url_documents_pass_ssrf_check(client, mongo, fake_pg):
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    table = {"docs.example.com": ["93.184.216.34"], "intranet.corp": ["10.0.0.9"]}

    def resolver(host: str) -> list[str]:
        return table.get(host, [])

    client.app.state.knowledge_service.resolver = resolver
    url = f"{KB}/{kb['id']}/documents/url"

    def blocked(target: str, code: str):
        r = client.post(url, json={"url": target})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == code, r.text

    blocked("http://10.0.0.1/x", "url_private")
    blocked("http://localhost/x", "url_private")
    blocked("http://intranet.corp/x", "url_private")
    blocked("http://metadata.google.internal/x", "url_private")
    blocked("http://svc.cluster.local/x", "url_private")
    blocked("ftp://docs.example.com/x", "url_scheme")
    blocked("http://user:pw@docs.example.com/x", "url_userinfo")
    blocked("http://nowhere.example/x", "url_unresolvable")
    assert mongo["knowledge_documents"].count_documents({}) == 0

    r = client.post(url, json={"url": "https://docs.example.com/guides/faq.md?v=2"})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["name"] == "faq.md" and doc["content_type"] == "text/markdown"
    assert doc["source"] == {"kind": "url", "url": "https://docs.example.com/guides/faq.md?v=2"}
    assert doc["state"] == "pending" and doc["bytes"] == 0 and doc["sha256"] is None
    r = client.post(url, json={"url": "https://docs.example.com/", "name": "홈"})
    assert r.status_code == 201 and r.json()["name"] == "홈"
    assert r.json()["content_type"] == "application/octet-stream"
    r = client.post(url, json={"url": "https://docs.example.com/"})
    assert r.status_code == 201 and r.json()["name"] == "docs.example.com"
    assert client.post(url, json={"url": "x", "extra": 1}).status_code == 422


def test_reindex_and_delete_document(client, mongo, fake_pg):
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    base = f"{KB}/{kb['id']}"
    a = _upload(client, kb["id"], "a.md").json()
    b = _upload(client, kb["id"], "b.md").json()
    docs = mongo["knowledge_documents"]
    docs.update_one(
        {"_id": a["id"]},
        {
            "$set": {
                "state": "failed",
                "error": {"code": "extract_failed", "detail": "X"},
                "lease": {"owner": "w", "until": utcnow()},
            }
        },
    )
    # 문서 하나 다시 인덱싱: pending, 오류·리스 제거
    r = client.post(f"{base}/documents/{a['id']}/reindex")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pending" and r.json()["error"] is None
    assert "lease" not in docs.find_one({"_id": a["id"]})
    # indexing 이면 409
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "indexing"}})
    r = client.post(f"{base}/documents/{a['id']}/reindex")
    assert detail_code(r) == (409, "indexing")
    r = client.post(f"{base}/reindex")
    assert detail_code(r) == (409, "indexing")
    r = client.delete(f"{base}/documents/{a['id']}")
    assert detail_code(r) == (409, "indexing")
    # 전체 다시 인덱싱은 pending·indexed·failed 만 되돌린다
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "indexed", "chunks": 2}})
    docs.update_one({"_id": b["id"]}, {"$set": {"state": "deleting"}})
    r = client.post(f"{base}/reindex")
    assert r.status_code == 200 and r.json() == {"id": kb["id"], "pending": 1}
    assert docs.find_one({"_id": a["id"]})["state"] == "pending"
    assert docs.find_one({"_id": b["id"]})["state"] == "deleting"
    # 삭제 중인 문서의 다시 인덱싱은 409 deleting
    r = client.post(f"{base}/documents/{b['id']}/reindex")
    assert detail_code(r) == (409, "deleting")
    # 문서 DELETE 는 deleting 으로만 바꾼다. 본문과 문서는 남아 워커가 지운다
    r = client.delete(f"{base}/documents/{a['id']}")
    assert r.status_code == 200 and r.json() == {"id": a["id"], "state": "deleting"}
    got = docs.find_one({"_id": a["id"]})
    assert got["state"] == "deleting" and "lease" not in got
    assert mongo["knowledge_docs.files"].find_one({"filename": f"{kb['id']}/{a['id']}"})
    # 실패한 삭제의 "다시 삭제" 는 같은 호출이다
    docs.update_one(
        {"_id": a["id"]}, {"$set": {"state": "failed", "error": {"code": "db_unreachable"}}}
    )
    r = client.delete(f"{base}/documents/{a['id']}")
    assert r.status_code == 200 and r.json()["state"] == "deleting"
    assert docs.find_one({"_id": a["id"]})["error"] is None
    # 이미 deleting 이면 그대로
    assert client.delete(f"{base}/documents/{a['id']}").json()["state"] == "deleting"
    assert client.delete(f"{base}/documents/nope").status_code == 404


# -- 삭제 --------------------------------------------------------------------------------


def test_delete_knowledge_base_order(client, mongo, fake_pg):
    """purging 으로 바꾼 뒤 DROP TABLE, 문서·본문, 리소스 순으로 지운다. indexing 이면 409,
    참조가 있으면 409 in_use 다."""
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    base = f"{KB}/{kb['id']}"
    a = _upload(client, kb["id"], "a.md").json()
    _upload(client, kb["id"], "b.md")
    docs = mongo["knowledge_documents"]
    files = mongo["knowledge_docs.files"]
    assert files.count_documents({"metadata.kb_id": kb["id"]}) == 2

    class Refs(ReferenceService):
        """참조 세기 대역. 가드가 세는 두 쪽(정의·배포)의 Agent 를 테스트가 정한다."""

        defined: list[str] = []
        deployed: list[str] = []

        async def used_by(self, kind: Any, id: str, *, tenant: str | None) -> list[Reference]:
            def ref(agent_id: str, revision: Any) -> Reference:
                return Reference(
                    kind="agent",
                    id=agent_id,
                    name=agent_id,
                    display_name=agent_id,
                    revision=revision,
                )

            if id != kb["id"]:
                return []
            return [ref(a, "current") for a in self.defined] + [
                ref(a, "deployed") for a in self.deployed
            ]

    service = client.app.state.knowledge_service
    refs = Refs.__new__(Refs)
    service.references = refs
    refs.defined = ["ap-1"]
    r = client.delete(base)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {
        "code": "in_use",
        "detail": "knowledge base is used by agents",
        "agent_ids": ["ap-1"],
        "deployed_agent_ids": [],
    }

    refs.defined, refs.deployed = [], ["ai-1"]
    r = client.delete(base)
    assert r.status_code == 409 and r.json()["detail"]["deployed_agent_ids"] == ["ai-1"]
    refs.deployed = []
    # indexing 문서가 있으면 409
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "indexing"}})
    r = client.delete(base)
    assert detail_code(r) == (409, "indexing")
    assert client.get(base).json()["deleting_at"] is None
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "indexed"}})
    # 성공: DROP 이 실행되고 문서·본문·리소스가 사라진다
    fake_pg.calls = []
    fake_pg.conn = FakeConn()
    r = client.delete(base)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": kb["id"], "deleted": True, "table_dropped": True}
    assert _executed(fake_pg) == [f'DROP TABLE IF EXISTS "{kb["table"]}"']
    assert fake_pg.calls[0][1]["options"] == "-c statement_timeout=30000"
    assert client.get(base).status_code == 404
    assert docs.count_documents({"knowledge_base_id": kb["id"]}) == 0
    assert files.count_documents({"metadata.kb_id": kb["id"]}) == 0
    assert mongo["knowledge_bases"].count_documents({}) == 0
    assert client.delete(base).status_code == 404


def test_delete_knowledge_base_orphaned_table_and_writes_409(client, mongo, fake_pg):
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    base = f"{KB}/{kb['id']}"
    a = _upload(client, kb["id"], "a.md").json()
    docs = mongo["knowledge_documents"]
    docs.update_one({"_id": a["id"]}, {"$set": {"state": "pending", "lease": {"owner": "w"}}})
    fake_pg.error = psycopg.OperationalError("connection refused")
    r = client.delete(base)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": kb["id"],
        "deleted": False,
        "table_dropped": False,
        "warning": "table_not_dropped",
    }
    got = client.get(base).json()
    assert got["state"] == "orphaned_table" and got["deleting_at"] is not None
    assert got["table"] == kb["table"]
    assert [k["state"] for k in client.get(KB).json()] == ["orphaned_table"]
    # 문서 전부 purging 이고 리스가 없다. 워커가 잡지 않는 상태다
    purged = docs.find_one({"_id": a["id"]})
    assert purged["state"] == "purging" and "lease" not in purged
    # deleting_at 인 KB 의 쓰기는 전부 409 deleting
    expect_409 = [
        lambda: _upload(client, kb["id"], "b.md"),
        lambda: client.post(f"{base}/documents/url", json={"url": "https://example.com/x"}),
        lambda: client.post(f"{base}/reindex"),
        lambda: client.post(f"{base}/documents/{a['id']}/reindex"),
        lambda: client.patch(base, json={"chunking": {"size": 600, "overlap": 0}}),
        lambda: client.patch(base, json={"display_name": "x"}),
        lambda: client.delete(f"{base}/documents/{a['id']}"),
    ]
    for call in expect_409:
        r = call()
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "deleting", r.text
    # 읽기는 된다
    assert client.get(f"{base}/documents/{a['id']}").json()["state"] == "purging"
    # DROP 이 접속 뒤 문장에서 실패해도 같다
    fake_pg.error = None
    fake_pg.conn = FakeConn(fail=pe.QueryCanceled("statement timeout"))
    r = client.delete(base)
    assert r.json()["warning"] == "table_not_dropped"
    # 다시 DELETE 하면 같은 순서를 다시 타고 이번에는 끝난다
    fake_pg.conn = FakeConn()
    r = client.delete(base)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get(base).status_code == 404
    assert docs.count_documents({}) == 0


def test_other_tenant_cannot_see_or_touch_a_knowledge_base(client, mongo, fake_pg):
    """모든 경로가 tenant 로 좁힌다. 다른 테넌트의 id 는 목록에도 없고 상세·문서·쓰기가 전부
    404 다."""
    _bootstrap(client)
    kb = _kb(client, fake_pg)
    doc = _upload(client, kb["id"]).json()
    assert client.get(KB, headers=T2).json() == []
    for method, path in [
        ("GET", f"{KB}/{kb['id']}"),
        ("PATCH", f"{KB}/{kb['id']}"),
        ("DELETE", f"{KB}/{kb['id']}"),
        ("GET", f"{KB}/{kb['id']}/documents"),
        ("GET", f"{KB}/{kb['id']}/documents/{doc['id']}"),
        ("POST", f"{KB}/{kb['id']}/documents/{doc['id']}/reindex"),
        ("DELETE", f"{KB}/{kb['id']}/documents/{doc['id']}"),
        ("POST", f"{KB}/{kb['id']}/reindex"),
        ("POST", f"{KB}/{kb['id']}/documents/url"),
    ]:
        kwargs = {"json": {}} if method in ("PATCH", "POST") else {}
        if path.endswith("/url"):
            kwargs = {"json": {"url": "https://example.com/x"}}
        r = client.request(method, path, headers=T2, **kwargs)
        assert r.status_code == 404, (method, path, r.text)
    r = client.post(
        f"{KB}/{kb['id']}/documents", headers=T2, files={"file": ("b.md", b"x", "text/markdown")}
    )
    assert r.status_code == 404
    # 같은 테넌트에서는 그대로 보인다
    assert client.get(f"{KB}/{kb['id']}").status_code == 200
