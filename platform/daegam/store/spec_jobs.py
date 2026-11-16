"""Spec Generator job 문서와 리스, 입력·초안 본문(GridFS).

job 은 `spec_jobs` 컬렉션에 산다. 워커는 `acquire()` 의 findOneAndUpdate 리스로 한 파드만 잡고,
진행 중에는 `renew()` 로 리스를 늘리며, 진행 기록(iterations)·종료는 리스 소유자만 쓸 수 있다.
리스가 만료된 running job 은 다른 워커가 다시 잡아 마지막 반복 다음부터 이어 간다.
입력과 초안은 문서에 넣지 않고 GridFS 버킷 `spec_jobs` 에 둔다 (16MB 문서 한도).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from gridfs.asynchronous import AsyncGridFSBucket
from gridfs.errors import NoFile
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from daegam.store.base import Document, UtcDatetime, new_id, utcnow

SPEC_JOBS = "spec_jobs"
BUCKET = "spec_jobs"

JobState = Literal["queued", "running", "done", "failed"]
ACTIVE_STATES: tuple[JobState, ...] = ("queued", "running")


class LeaseLost(Exception):
    """리스가 다른 워커에게 넘어갔다. 이 워커는 job 을 더 건드리면 안 된다."""


class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str
    until: datetime


class Iteration(BaseModel):
    """반복 한 번의 기록. 초안 본문은 draft_ref(GridFS) 로만 가리킨다."""

    model_config = ConfigDict(extra="ignore")

    n: int
    draft_ref: str | None = None
    report_summary: dict[str, Any] | None = None  # {ok, complete, summary}
    # 판정 error 코드와 surface.changed:·refine.unknown: 항목 (no_progress 판정용)
    failed_ids: list[str] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)  # {input_tokens, output_tokens}
    note: str | None = None
    # refine 모드에서 이 반복이 다룬 묶음 번호(1부터)와 묶음 수. generate 는 None
    batch: int | None = None
    batch_total: int | None = None
    finished_at: UtcDatetime = Field(default_factory=utcnow)


JobMode = Literal["generate", "refine", "overrides"]


class SpecJob(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    # 만든 사람의 sub. 원장 행의 사람이다 (P37 7절). 이 판 전에 만든 job 은 없다
    created_by: str | None = None
    state: JobState = "queued"
    provider: str | None = None  # llm_providers 이름. None 이면 defaults.spec_generator
    # generate: 입력에서 문서를 만든다. refine: source 버전의 설명 필드를 다듬는다 (D25).
    # overrides: source 리비전의 tool_overrides 이름·설명을 제안한다 (D37)
    mode: JobMode = "generate"
    # refine: {backend_profile_id, version}. overrides: {mcp_server_id, revision}
    source: dict[str, Any] | None = None
    presets: list[str] = Field(default_factory=list)  # refine 프리셋 이름
    instruction: str | None = None  # refine 자유 지시 한 줄
    input_ref: str  # GridFS 파일명 {id}/input.json (refine 은 원본 문서 JSON)
    input_size: int = 0
    hints: dict[str, Any] = Field(default_factory=dict)
    iterations: list[Iteration] = Field(default_factory=list)
    draft_ref: str | None = None
    report: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    error: dict[str, Any] | None = None  # {code, detail}
    usage_total: dict[str, int] = Field(default_factory=dict)
    attempt: int = 0
    lease: Lease | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    finished_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


def input_name(job_id: str) -> str:
    return f"{job_id}/input.json"


def draft_name(job_id: str, n: int) -> str:
    return f"{job_id}/draft-{n}.json"


class MongoSpecJobStore:
    def __init__(self, db: AsyncDatabase):
        self.coll = db[SPEC_JOBS]

    async def init_schema(self) -> None:
        # done/failed 는 expires_at 을 찍고, TTL 인덱스가 그 시각에 지운다
        await self.coll.create_index(
            [("expires_at", ASCENDING)], name="ttl_expires_at", expireAfterSeconds=0
        )
        await self.coll.create_index(
            [("tenant", ASCENDING), ("state", ASCENDING)], name="ix_tenant_state"
        )
        await self.coll.create_index(
            [("state", ASCENDING), ("lease.until", ASCENDING), ("created_at", ASCENDING)],
            name="ix_claim",
        )

    # -- CRUD ------------------------------------------------------------------

    async def create(self, job: SpecJob) -> SpecJob:
        await self.coll.insert_one(job.to_doc())
        return job

    async def get(self, job_id: str, *, tenant: str | None = None) -> SpecJob | None:
        q: dict[str, Any] = {"_id": job_id}
        if tenant is not None:
            q["tenant"] = tenant
        doc = await self.coll.find_one(q)
        return SpecJob.from_doc(doc) if doc else None

    async def list(self, tenant: str) -> list[SpecJob]:
        cursor = self.coll.find({"tenant": tenant}).sort("created_at", -1)
        return [SpecJob.from_doc(d) async for d in cursor]

    async def count_active(self, tenant: str) -> int:
        return await self.coll.count_documents(
            {"tenant": tenant, "state": {"$in": list(ACTIVE_STATES)}}
        )

    async def delete(self, job_id: str) -> bool:
        r = await self.coll.delete_one({"_id": job_id})
        return r.deleted_count == 1

    # -- 리스 ---------------------------------------------------------------------

    async def acquire(self, owner: str, lease_seconds: float) -> SpecJob | None:
        """queued 이거나 running 인데 리스가 만료된 job 하나를 잡는다. 오래된 것부터."""
        now = utcnow()
        doc = await self.coll.find_one_and_update(
            {
                "$or": [
                    {"state": "queued"},
                    {"state": "running", "lease.until": {"$lt": now}},
                ]
            },
            {
                "$set": {
                    "state": "running",
                    "lease": {"owner": owner, "until": now + timedelta(seconds=lease_seconds)},
                    "updated_at": now,
                },
                "$inc": {"attempt": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return SpecJob.from_doc(doc) if doc else None

    def _owned(self, job_id: str, owner: str) -> dict[str, Any]:
        return {"_id": job_id, "state": "running", "lease.owner": owner}

    async def renew(self, job_id: str, owner: str, lease_seconds: float) -> bool:
        now = utcnow()
        r = await self.coll.update_one(
            self._owned(job_id, owner),
            {"$set": {"lease.until": now + timedelta(seconds=lease_seconds), "updated_at": now}},
        )
        return r.matched_count == 1

    async def push_iteration(
        self, job_id: str, owner: str, iteration: Iteration, *, usage_total: dict[str, int]
    ) -> None:
        r = await self.coll.update_one(
            self._owned(job_id, owner),
            {
                "$push": {"iterations": iteration.model_dump()},
                "$set": {"usage_total": usage_total, "updated_at": utcnow()},
            },
        )
        if r.matched_count != 1:
            raise LeaseLost(job_id)

    async def finish(
        self,
        job_id: str,
        owner: str,
        *,
        state: Literal["done", "failed"],
        ttl_seconds: float,
        draft_ref: str | None = None,
        report: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> SpecJob:
        now = utcnow()
        doc = await self.coll.find_one_and_update(
            self._owned(job_id, owner),
            {
                "$set": {
                    "state": state,
                    "lease": None,
                    "draft_ref": draft_ref,
                    "report": report,
                    "verdict": verdict,
                    "error": error,
                    "updated_at": now,
                    "finished_at": now,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise LeaseLost(job_id)
        return SpecJob.from_doc(doc)


class SpecJobFiles:
    """job 의 입력·초안 본문. GridFS 버킷 `spec_jobs`,
    파일명 `{job_id}/input.json` 과 `{job_id}/draft-{n}.json`."""

    def __init__(self, db: AsyncDatabase, bucket_name: str = BUCKET):
        self.bucket = AsyncGridFSBucket(db, bucket_name=bucket_name)
        self.files = db[f"{bucket_name}.files"]

    async def init_schema(self) -> None:
        await self.files.create_index([("metadata.job_id", ASCENDING)], name="ix_job")

    async def put(self, name: str, data: bytes, *, job_id: str, role: str) -> str:
        if await self.files.find_one({"filename": name}, projection={"_id": 1}) is not None:
            await self.bucket.delete_by_name(name)
        await self.bucket.upload_from_stream(name, data, metadata={"job_id": job_id, "role": role})
        return name

    async def get(self, name: str) -> bytes | None:
        try:
            grid_out = await self.bucket.open_download_stream_by_name(name)
        except NoFile:
            return None
        return await grid_out.read()

    async def delete_job(self, job_id: str) -> int:
        ids = [d["_id"] async for d in self.files.find({"metadata.job_id": job_id}, {"_id": 1})]
        for file_id in ids:
            try:
                await self.bucket.delete(file_id)
            except NoFile:
                pass
        return len(ids)
