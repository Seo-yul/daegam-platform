"""Knowledge 스토어 (D32, F1). 리스 원자성과 소유 필터, 집계, GridFS 본문."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from daegam.store.base import utcnow
from daegam.store.knowledge import (
    Chunking,
    KnowledgeBase,
    KnowledgeDocument,
    LeaseLost,
    MongoKnowledgeStore,
    Source,
    table_name,
)
from tests.helpers.aio import run_store
from tests.helpers.docs import knowledge_base_doc


def _doc(kb: KnowledgeBase, name="a.md", state="pending", **kw) -> KnowledgeDocument:
    return KnowledgeDocument(
        knowledge_base_id=kb.id,
        tenant=kb.tenant,
        name=name,
        source=Source(kind="upload"),
        content_type="text/markdown",
        state=state,
        **kw,
    )


# -- 모델 ---------------------------------------------------------------------------


def test_table_name_and_defaults():
    kb = knowledge_base_doc()
    assert kb.table == table_name(kb.id) == f"daegam_kb_{kb.id.replace('-', '')}"
    assert len(kb.table) == len("daegam_kb_") + 32
    assert (kb.chunking.size, kb.chunking.overlap, kb.limits.max_chunks) == (1200, 150, 100000)
    assert kb.state == "ready" and kb.deleting_at is None and kb.status is None
    # status 는 집계라 문서에 저장하지 않는다
    assert "status" not in kb.to_doc()
    with pytest.raises(ValueError):
        Chunking(size=1000, overlap=501)
    with pytest.raises(ValueError):
        Chunking(size=100)
    assert Chunking(size=1000, overlap=500).overlap == 500


def test_bases_crud_and_documents(settings, mongo):
    async def main(store):
        kb = await store.create(knowledge_base_doc())
        assert (await store.get(kb.id)).name == "faq"
        assert await store.get(kb.id, tenant="other") is None
        assert (await store.get_by_name("default", "faq")).id == kb.id
        assert [k.id for k in await store.list("default")] == [kb.id]
        assert await store.list("other") == []
        other = await store.create(
            knowledge_base_doc("faq", tenant="other", data_service_id="ds-2")
        )
        assert [k.id for k in await store.list_by_data_service("ds-pg")] == [kb.id]
        assert [k.id for k in await store.list_by_data_service("ds-2")] == [other.id]
        # 같은 테넌트에 같은 이름은 유일 인덱스가 막는다
        from pymongo.errors import DuplicateKeyError

        with pytest.raises(DuplicateKeyError):
            await store.create(knowledge_base_doc("faq"))
        kb.display_name = "FAQ"
        assert await store.save_edit(kb, {"display_name"})
        assert await store.set_fields(kb.id, {"deleting_at": utcnow()})
        got = await store.get(kb.id)
        assert got.display_name == "FAQ" and got.deleting_at is not None
        # 삭제가 시작된 뒤의 사람의 저장은 맞지 않는다 (P68 2절)
        got.display_name = "late"
        assert not await store.save_edit(got, {"display_name"})

        a = await store.add_document(_doc(kb, "a.md", created_at=utcnow() - timedelta(seconds=1)))
        b = await store.add_document(_doc(kb, "b.md", state="indexed", chunks=3))
        assert (await store.get_document(a.id)).name == "a.md"
        assert await store.get_document(a.id, kb_id="nope") is None
        assert [d.id for d in await store.list_documents(kb.id)] == [a.id, b.id]
        assert await store.count_documents(kb.id) == 2
        assert await store.count_documents(kb.id, states=["indexed"]) == 1
        assert await store.delete_document(a.id) is True
        assert await store.delete_documents(kb.id) == 1
        assert await store.delete(kb.id) is True
        assert await store.get(kb.id) is None

    run_store(settings, MongoKnowledgeStore, main)


# -- 리스 ---------------------------------------------------------------------------


def test_acquire_pending_becomes_indexing_and_lease_blocks_others(settings, mongo):
    async def main(store):
        kb = await store.create(knowledge_base_doc())
        doc = await store.add_document(_doc(kb))
        got = await store.acquire("w1", 60)
        assert got is not None and got.id == doc.id
        assert got.state == "indexing" and got.attempts == 1 and got.lease.owner == "w1"
        # 리스가 살아 있으니 다른 워커는 못 잡는다
        assert await store.acquire("w2", 60) is None
        assert await store.renew(doc.id, "w1", 60) is True
        assert await store.renew(doc.id, "w2", 60) is False
        # 리스를 만료시키면 잡히고 attempts 가 는다
        await store.documents.update_one(
            {"_id": doc.id}, {"$set": {"lease.until": utcnow() - timedelta(seconds=1)}}
        )
        stolen = await store.acquire("w2", 60)
        assert stolen is not None and stolen.lease.owner == "w2" and stolen.attempts == 2
        assert stolen.state == "indexing"
        # 옛 소유자의 쓰기는 전부 거부된다 (_owned)
        assert await store.renew(doc.id, "w1", 60) is False
        with pytest.raises(LeaseLost):
            await store.finish_indexed(doc.id, "w1", chunks=1, tokens=1)
        with pytest.raises(LeaseLost):
            await store.finish_failed(doc.id, "w1", "extract_failed", "X")
        with pytest.raises(LeaseLost):
            await store.finish_deleted(doc.id, "w1")
        with pytest.raises(LeaseLost):
            await store.set_body(doc.id, "w1", sha256="x", size=1)
        assert (await store.get_document(doc.id)).state == "indexing"
        # 새 소유자는 끝낼 수 있다
        await store.set_body(doc.id, "w2", sha256="abc", size=12, content_type="text/plain")
        done = await store.finish_indexed(doc.id, "w2", chunks=4, tokens=99)
        assert done.state == "indexed" and done.lease is None and done.error is None
        assert (done.chunks, done.usage.tokens, done.sha256, done.bytes) == (4, 99, "abc", 12)
        assert done.content_type == "text/plain" and done.indexed_at is not None
        # indexed 는 잡히지 않는다
        assert await store.acquire("w3", 60) is None

    run_store(settings, MongoKnowledgeStore, main)


def test_acquire_is_atomic_for_pending_and_deleting(settings, mongo):
    """두 owner 가 동시에 acquire 하면 하나만 잡는다. pending 과 deleting 둘 다."""

    async def main(store):
        kb = await store.create(knowledge_base_doc())
        for state in ("pending", "deleting"):
            doc = await store.add_document(_doc(kb, f"{state}.md", state=state))
            results = await asyncio.gather(
                *(store.acquire(f"w{i}", 60) for i in range(4)), return_exceptions=False
            )
            won = [r for r in results if r is not None]
            assert len(won) == 1 and won[0].id == doc.id, state
            expected = "indexing" if state == "pending" else "deleting"
            assert won[0].state == expected and won[0].attempts == 1
            owner = won[0].lease.owner
            # deleting 도 리스가 살아 있으면 다시 잡히지 않는다
            assert await store.acquire("late", 60) is None
            if state == "deleting":
                await store.finish_deleted(doc.id, owner)
                assert await store.get_document(doc.id) is None
            else:
                await store.finish_failed(doc.id, owner, "empty_document")
                failed = await store.get_document(doc.id)
                assert failed.state == "failed" and failed.error.code == "empty_document"
                assert failed.error.detail is None and failed.lease is None

    run_store(settings, MongoKnowledgeStore, main)


# -- 집계 ---------------------------------------------------------------------------


# -- GridFS -------------------------------------------------------------------------
