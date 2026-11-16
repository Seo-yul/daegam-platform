"""Knowledge Base 와 문서 (D32, P15).

Knowledge Base 는 `knowledge_bases` 컬렉션의 테넌트 리소스이고 문서는 `knowledge_documents` 에
산다. 본문은 문서에 넣지 않고 GridFS 버킷 `knowledge_docs` 에 `{kb_id}/{doc_id}` 로 둔다.

문서 상태는 `pending → indexing → indexed | failed` 이고 삭제는 `deleting`(워커가 행·본문·문서를
지운다), Knowledge Base 삭제는 `purging`(워커가 잡지 않는다) 이다. 인덱서는 `acquire()` 의
findOneAndUpdate 리스로 한 파드만 잡고 `renew()` 로 리스를 늘리며, 종료(`finish_*`)는 리스 소유자만
쓸 수 있다. 리스가 만료된 문서는 다른 워커가 다시 잡는다. `pending` 으로 되돌리는 곳은 리스를
지운다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from gridfs.asynchronous import AsyncGridFSBucket
from gridfs.errors import NoFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import (
    Document,
    UtcDatetime,
    fields_of,
    new_id,
    save_by_seq,
    set_fields_where,
    utcnow,
)

KNOWLEDGE_BASES = "knowledge_bases"
KNOWLEDGE_DOCUMENTS = "knowledge_documents"
BUCKET = "knowledge_docs"

DocState = Literal["pending", "indexing", "indexed", "failed", "deleting", "purging"]
KnowledgeBaseState = Literal["ready", "orphaned_table"]
SourceKind = Literal["upload", "url"]
# 워커가 잡는 상태. purging 은 밖이다
CLAIMABLE_STATES: tuple[DocState, ...] = ("pending", "indexing", "deleting")
# 리스 소유자만 쓸 수 있는 상태
OWNED_STATES: tuple[DocState, ...] = ("indexing", "deleting")
# 주인이 지운 상태. 워커가 본문을 걷기 전이어도 이미 없는 문서다
REMOVED_STATES: tuple[DocState, ...] = ("deleting", "purging")

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_MAX_CHUNKS = 100_000


# 밖에서 pending·deleting 으로 되돌릴 수 있는 상태. indexing 은 워커가, deleting·purging 은 삭제가
# 들고 있다
RESTARTABLE_STATES: tuple[str, ...] = ("pending", "indexed", "failed")


def table_name(kb_id: str) -> str:
    """벡터 테이블 이름. id 의 하이픈을 뺀 32 hex 라 다른 Knowledge Base 와 겹치지 않는다."""
    return f"daegam_kb_{kb_id.replace('-', '')}"


class LeaseLost(Exception):
    """리스가 다른 워커에게 넘어갔다. 이 워커는 문서를 더 건드리면 안 된다."""


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str
    until: datetime


class Embedding(BaseModel):
    """만든 뒤 바꿀 수 없다. 벡터 테이블의 차원과 배포된 agent.json 이 이 값에 묶인다."""

    model_config = ConfigDict(extra="forbid")

    provider: str  # llm_providers 이름
    model: str  # 저장 시 effective_embedding_model 로 굳힌 값
    dimensions: int = Field(ge=1)


class Chunking(BaseModel):
    """글자 수. 바꾸면 모든 문서가 pending 으로 돌아간다."""

    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=200, le=4000)
    overlap: int = Field(default=DEFAULT_CHUNK_OVERLAP, ge=0)

    @model_validator(mode="after")
    def _check_overlap(self) -> Chunking:
        if self.overlap > self.size // 2:
            raise ValueError("overlap must be at most size/2")
        return self


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chunks: int = Field(default=DEFAULT_MAX_CHUNKS, ge=1, le=1_000_000)


class KnowledgeStatus(BaseModel):
    """문서 컬렉션 집계. 저장하지 않고 목록·상세가 `status()` 로 계산해 싣는다."""

    model_config = ConfigDict(extra="forbid")

    documents: int = 0
    indexed: int = 0
    failed: int = 0
    chunks: int = 0
    tokens: int = 0
    last_indexed_at: UtcDatetime | None = None


class KnowledgeBase(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    name: str
    display_name: str  # 없으면 name (Document 가 채운다, D24)
    data_service_id: str  # kind: postgres Database. 만든 뒤 불변
    embedding: Embedding
    chunking: Chunking = Field(default_factory=Chunking)
    limits: Limits = Field(default_factory=Limits)
    table: str = ""  # 생성 때 table_name(id) 로 굳힌다
    state: KnowledgeBaseState = "ready"
    deleting_at: UtcDatetime | None = None  # 있으면 문서 쓰기 동작은 409 deleting
    # 집계. 문서에 저장하지 않는다 (to_doc 이 뺀다). 목록·상세·GraphContext 가 채운다
    status: KnowledgeStatus | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    updated_by: str | None = None

    @model_validator(mode="after")
    def _default_table(self) -> KnowledgeBase:
        if not self.table:
            self.table = table_name(self.id)
        return self

    def to_doc(self) -> dict[str, Any]:
        d = super().to_doc()
        d.pop("status", None)
        return d


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    url: str | None = None


class DocError(BaseModel):
    """detail 은 우리가 만든 코드·클래스 이름뿐이다. provider·libpq 원문은 싣지 않는다."""

    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str | None = None


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: int = 0  # tiktoken cl100k_base 추정치. stub 은 0


class KnowledgeDocument(Document):
    id: str = Field(default_factory=new_id)
    knowledge_base_id: str
    tenant: str
    name: str
    source: Source
    content_type: str
    bytes: int = 0
    sha256: str | None = None  # URL 문서는 본문을 받기 전이면 None
    state: DocState = "pending"
    chunks: int = 0  # 테이블에 있는 이 문서의 행 수 (마지막 성공 기준)
    usage: Usage = Field(default_factory=Usage)
    error: DocError | None = None
    attempts: int = 0
    lease: Lease | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    indexed_at: UtcDatetime | None = None
    updated_by: str | None = None


def file_name(kb_id: str, doc_id: str) -> str:
    return f"{kb_id}/{doc_id}"


class MongoKnowledgeStore:
    def __init__(self, db: AsyncDatabase):
        self.bases = db[KNOWLEDGE_BASES]
        self.documents = db[KNOWLEDGE_DOCUMENTS]

    async def init_schema(self) -> None:
        await self.bases.create_index(
            [("tenant", ASCENDING), ("name", ASCENDING)], unique=True, name="uq_tenant_name"
        )
        await self.bases.create_index([("data_service_id", ASCENDING)], name="ix_data_service")
        await self.documents.create_index(
            [("knowledge_base_id", ASCENDING), ("created_at", ASCENDING)], name="ix_kb_created"
        )
        await self.documents.create_index(
            [("state", ASCENDING), ("lease.until", ASCENDING)], name="ix_claim"
        )

    # -- Knowledge Base ---------------------------------------------------------

    async def create(self, kb: KnowledgeBase) -> KnowledgeBase:
        await self.bases.insert_one(kb.to_doc())
        return kb

    async def get(self, kb_id: str, tenant: str | None = None) -> KnowledgeBase | None:
        q: dict[str, Any] = {"_id": kb_id}
        if tenant is not None:
            q["tenant"] = tenant
        doc = await self.bases.find_one(q)
        return KnowledgeBase.from_doc(doc) if doc else None

    async def get_by_name(self, tenant: str, name: str) -> KnowledgeBase | None:
        doc = await self.bases.find_one({"tenant": tenant, "name": name})
        return KnowledgeBase.from_doc(doc) if doc else None

    async def list(self, tenant: str) -> list[KnowledgeBase]:
        cursor = self.bases.find({"tenant": tenant}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [KnowledgeBase.from_doc(d) for d in await cursor.to_list()]

    async def list_by_data_service(self, ds_id: str) -> list[KnowledgeBase]:
        cursor = self.bases.find({"data_service_id": ds_id}).sort("created_at", ASCENDING)
        return [KnowledgeBase.from_doc(d) for d in await cursor.to_list()]

    async def save_edit(self, kb: KnowledgeBase, names: set[str]) -> bool:
        """사람의 저장 (P68 2절). 서버가 읽은 `seq` 이고 삭제가 시작되지 않았을(`deleting_at` 이
        null) 때만 names 칸만 쓰고 `seq` 를 올린다. 맞지 않으면 False 다."""
        return await save_by_seq(self.bases, kb, fields_of(kb, names), also={"deleting_at": None})

    async def set_fields(self, kb_id: str, fields: dict[str, Any]) -> bool:
        """플랫폼이 쓰는 칸만 쓴다(삭제의 `deleting_at`·`state`). 문서 전체를 쓰는 저장은 없다."""
        return await set_fields_where(self.bases, kb_id, fields)

    async def delete(self, kb_id: str) -> bool:
        r = await self.bases.delete_one({"_id": kb_id})
        return r.deleted_count == 1

    # -- 문서 --------------------------------------------------------------------

    async def add_document(self, doc: KnowledgeDocument) -> KnowledgeDocument:
        await self.documents.insert_one(doc.to_doc())
        return doc

    async def get_document(
        self, doc_id: str, *, kb_id: str | None = None
    ) -> KnowledgeDocument | None:
        q: dict[str, Any] = {"_id": doc_id}
        if kb_id is not None:
            q["knowledge_base_id"] = kb_id
        d = await self.documents.find_one(q)
        return KnowledgeDocument.from_doc(d) if d else None

    async def list_documents(self, kb_id: str) -> list[KnowledgeDocument]:
        cursor = self.documents.find({"knowledge_base_id": kb_id}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [KnowledgeDocument.from_doc(d) for d in await cursor.to_list()]

    async def count_documents(self, kb_id: str, *, states: list[str] | None = None) -> int:
        q: dict[str, Any] = {"knowledge_base_id": kb_id}
        if states is not None:
            q["state"] = {"$in": list(states)}
        return await self.documents.count_documents(q)

    async def delete_document(self, doc_id: str) -> bool:
        """리스를 보지 않는 삭제. 워커의 삭제 run 은 finish_deleted 를 쓴다."""
        r = await self.documents.delete_one({"_id": doc_id})
        return r.deleted_count == 1

    async def delete_documents(self, kb_id: str) -> int:
        r = await self.documents.delete_many({"knowledge_base_id": kb_id})
        return r.deleted_count

    # -- 상태 전이 (리스 밖) ----------------------------------------------------------

    async def _restart_one(self, doc_id: str, state: str) -> bool:
        """다시 시작할 수 있는 문서 하나를 state 로 옮기고 리스·오류를 지운다."""
        r = await self.documents.update_one(
            {"_id": doc_id, "state": {"$in": list(RESTARTABLE_STATES)}},
            {
                "$set": {"state": state, "error": None, "updated_at": utcnow()},
                "$unset": {"lease": ""},
            },
        )
        return r.matched_count == 1

    async def mark_pending(self, doc_id: str) -> bool:
        """다시 인덱싱. 리스를 지우고 오류를 비운다. chunks 는 테이블 행이 아직 있으니 그대로다.
        워커가 든 `indexing` 이나 삭제 중(`deleting`·`purging`)은 건드리지 않고 False 다. 상태를
        안 보면 워커가 든 문서의 리스가 지워져 두 워커가 같은 문서를 든다."""
        return await self._restart_one(doc_id, "pending")

    async def mark_all_pending(self, kb_id: str) -> int:
        """chunking 변경·전체 다시 인덱싱. 삭제 중(`deleting`·`purging`)인 문서는 건드리지 않는다.
        `indexing` 은 호출자가 409 로 막는다."""
        r = await self.documents.update_many(
            {"knowledge_base_id": kb_id, "state": {"$in": list(RESTARTABLE_STATES)}},
            {
                "$set": {"state": "pending", "error": None, "updated_at": utcnow()},
                "$unset": {"lease": ""},
            },
        )
        return r.modified_count

    async def mark_deleting(self, doc_id: str) -> bool:
        """문서 삭제 요청과 "다시 삭제". 리스를 지워 워커가 잡게 한다. `mark_pending` 과 같은 이유로
        `indexing`·`deleting`·`purging` 은 False 다."""
        return await self._restart_one(doc_id, "deleting")

    async def mark_purging(self, kb_id: str) -> int:
        """Knowledge Base 삭제. 워커가 절대 잡지 않는 상태로 옮긴다."""
        r = await self.documents.update_many(
            {"knowledge_base_id": kb_id},
            {"$set": {"state": "purging", "updated_at": utcnow()}, "$unset": {"lease": ""}},
        )
        return r.modified_count

    # -- 리스 ---------------------------------------------------------------------

    async def acquire(self, owner: str, lease_seconds: float) -> KnowledgeDocument | None:
        """pending·indexing·deleting 이면서 리스가 없거나 만료된 문서 하나를 잡는다. 오래된 것부터.

        갱신은 파이프라인 한 단계라 pending 만 indexing 이 되고 deleting 은 그대로다. 리스가 살아
        있는 문서는 상태와 무관하게 필터 밖이라 두 워커가 같은 문서를 잡지 않는다."""
        now = utcnow()
        doc = await self.documents.find_one_and_update(
            {
                "state": {"$in": list(CLAIMABLE_STATES)},
                "$or": [{"lease": None}, {"lease.until": {"$lt": now}}],
            },
            [
                {
                    "$set": {
                        "state": {"$cond": [{"$eq": ["$state", "pending"]}, "indexing", "$state"]},
                        "lease": {
                            "owner": owner,
                            "until": now + timedelta(seconds=lease_seconds),
                        },
                        "attempts": {"$add": ["$attempts", 1]},
                        "updated_at": now,
                    }
                }
            ],
            sort=[("created_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return KnowledgeDocument.from_doc(doc) if doc else None

    def _owned(self, doc_id: str, owner: str) -> dict[str, Any]:
        return {"_id": doc_id, "state": {"$in": list(OWNED_STATES)}, "lease.owner": owner}

    async def renew(self, doc_id: str, owner: str, lease_seconds: float) -> bool:
        now = utcnow()
        r = await self.documents.update_one(
            self._owned(doc_id, owner),
            {"$set": {"lease.until": now + timedelta(seconds=lease_seconds), "updated_at": now}},
        )
        return r.matched_count == 1

    async def set_body(
        self, doc_id: str, owner: str, *, sha256: str, size: int, content_type: str | None = None
    ) -> None:
        """URL 문서의 본문을 받은 뒤 리스 소유자가 sha256·bytes 를 적는다."""
        fields: dict[str, Any] = {"sha256": sha256, "bytes": size, "updated_at": utcnow()}
        if content_type is not None:
            fields["content_type"] = content_type
        r = await self.documents.update_one(self._owned(doc_id, owner), {"$set": fields})
        if r.matched_count != 1:
            raise LeaseLost(doc_id)

    async def _finish(self, doc_id: str, owner: str, fields: dict[str, Any]) -> KnowledgeDocument:
        """리스 소유자가 run 을 끝낸다. 리스를 비우고 fields 를 적는다. 리스를 잃었으면
        LeaseLost 다."""
        doc = await self.documents.find_one_and_update(
            self._owned(doc_id, owner),
            {"$set": {**fields, "lease": None}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseLost(doc_id)
        return KnowledgeDocument.from_doc(doc)

    async def finish_indexed(
        self, doc_id: str, owner: str, *, chunks: int, tokens: int
    ) -> KnowledgeDocument:
        now = utcnow()
        return await self._finish(
            doc_id,
            owner,
            {
                "state": "indexed",
                "chunks": chunks,
                "usage": {"tokens": tokens},
                "error": None,
                "indexed_at": now,
                "updated_at": now,
            },
        )

    async def finish_failed(
        self, doc_id: str, owner: str, code: str, detail: str | None = None
    ) -> KnowledgeDocument:
        return await self._finish(
            doc_id,
            owner,
            {"state": "failed", "error": {"code": code, "detail": detail}, "updated_at": utcnow()},
        )

    async def finish_deleted(self, doc_id: str, owner: str) -> None:
        """삭제 run 의 마지막. 행·본문이 지워진 뒤 리스 소유자만 문서를 지운다."""
        r = await self.documents.delete_one(self._owned(doc_id, owner))
        if r.deleted_count != 1:
            raise LeaseLost(doc_id)

    # -- 집계 ---------------------------------------------------------------------

    async def status(self, kb_id: str) -> KnowledgeStatus:
        """Knowledge Base 하나의 집계. `statuses` 의 한 칸이다."""
        return (await self.statuses([kb_id]))[kb_id]

    async def statuses(self, kb_ids: list[str]) -> dict[str, KnowledgeStatus]:
        """문서 수·인덱싱된 수·실패 수·청크·토큰 합계·마지막 인덱싱 시각을 Knowledge Base 별로
        aggregate 한 번에 센다. 문서가 없는 것은 빈 집계다. chunks·tokens 는 상태와 무관한
        합계다. 실패하거나 다시 인덱싱 중인 문서의 행도 테이블에 남아 있기 때문이다."""
        if not kb_ids:
            return {}
        pipeline = [
            {"$match": {"knowledge_base_id": {"$in": kb_ids}}},
            {
                "$group": {
                    "_id": "$knowledge_base_id",
                    "documents": {"$sum": 1},
                    "indexed": {"$sum": {"$cond": [{"$eq": ["$state", "indexed"]}, 1, 0]}},
                    "failed": {"$sum": {"$cond": [{"$eq": ["$state", "failed"]}, 1, 0]}},
                    "chunks": {"$sum": {"$ifNull": ["$chunks", 0]}},
                    "tokens": {"$sum": {"$ifNull": ["$usage.tokens", 0]}},
                    "last_indexed_at": {"$max": "$indexed_at"},
                }
            },
        ]
        out = {kb_id: KnowledgeStatus() for kb_id in kb_ids}
        for row in await (await self.documents.aggregate(pipeline)).to_list():
            kb_id = row.pop("_id")
            out[kb_id] = KnowledgeStatus.model_validate(row)
        return out


class KnowledgeFiles:
    """문서 본문. GridFS 버킷 `knowledge_docs`, 파일명 `{kb_id}/{doc_id}`."""

    def __init__(self, db: AsyncDatabase, bucket_name: str = BUCKET):
        self.bucket = AsyncGridFSBucket(db, bucket_name=bucket_name)
        self.files = db[f"{bucket_name}.files"]

    async def init_schema(self) -> None:
        await self.files.create_index([("metadata.kb_id", ASCENDING)], name="ix_kb")

    async def put(self, kb_id: str, doc_id: str, data: bytes, content_type: str) -> str:
        name = file_name(kb_id, doc_id)
        if await self.files.find_one({"filename": name}, projection={"_id": 1}) is not None:
            await self.bucket.delete_by_name(name)
        await self.bucket.upload_from_stream(
            name, data, metadata={"kb_id": kb_id, "doc_id": doc_id, "content_type": content_type}
        )
        return name

    async def get(self, kb_id: str, doc_id: str) -> bytes | None:
        try:
            grid_out = await self.bucket.open_download_stream_by_name(file_name(kb_id, doc_id))
        except NoFile:
            return None
        return await grid_out.read()

    async def delete_doc(self, kb_id: str, doc_id: str) -> bool:
        """본문이 없어도(NoFile) 정상이다. URL 문서는 본문을 받기 전일 수 있다."""
        try:
            await self.bucket.delete_by_name(file_name(kb_id, doc_id))
        except NoFile:
            return False
        return True

    async def delete_kb(self, kb_id: str) -> int:
        ids = [d["_id"] async for d in self.files.find({"metadata.kb_id": kb_id}, {"_id": 1})]
        for file_id in ids:
            try:
                await self.bucket.delete(file_id)
            except NoFile:
                pass
        return len(ids)
