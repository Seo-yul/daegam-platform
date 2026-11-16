"""Spec Generator 워커 루프.

P3 F7b reconcile 루프와 같은 실행 모델이다. 앱 시작 시와 주기(기본 5초)마다 `acquire()` 로
queued 또는 리스 만료된 running job 을 잡고, 잡은 동안 하트비트로 리스를 갱신하며 runner 를 돌린다.
수명·하트비트는 Knowledge 인덱서와 함께 `services/lease_worker.py` 의 `LeaseWorker` 에 있다.
runner 는 반복마다 `JobContext.push_iteration` 으로 진행을 기록하므로 파드가 죽어도 다음 워커가
마지막 반복 다음부터 이어 간다. 결과는 runner 가 돌려준 Outcome 으로 워커가 `finish()` 한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from daegam.services.lease_worker import LeaseWorker
from daegam.store.spec_jobs import (
    Iteration,
    LeaseLost,
    MongoSpecJobStore,
    SpecJob,
    SpecJobFiles,
    draft_name,
)

logger = logging.getLogger("daegam.spec_generator")


@dataclass
class Outcome:
    state: Literal["done", "failed"]
    draft_ref: str | None = None
    report: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def failed(cls, code: str, detail: str) -> Outcome:
        return cls(state="failed", error={"code": code, "detail": detail})


@dataclass
class JobContext:
    """runner 가 job 을 진행하며 쓰는 손잡이. 모든 쓰기는 리스 소유자 검사를 거친다."""

    store: MongoSpecJobStore
    files: SpecJobFiles
    owner: str
    lease_seconds: float
    job: SpecJob
    usage_total: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.usage_total = dict(self.job.usage_total)

    async def read_input(self) -> bytes | None:
        return await self.files.get(self.job.input_ref)

    async def read_draft(self, ref: str) -> bytes | None:
        return await self.files.get(ref)

    async def save_draft(self, n: int, data: bytes) -> str:
        return await self.files.put(
            draft_name(self.job.id, n), data, job_id=self.job.id, role="draft"
        )

    async def push_iteration(self, iteration: Iteration) -> None:
        for k, v in iteration.usage.items():
            self.usage_total[k] = self.usage_total.get(k, 0) + int(v)
        await self.store.push_iteration(
            self.job.id, self.owner, iteration, usage_total=self.usage_total
        )
        self.job.iterations.append(iteration)

    async def renew(self) -> bool:
        return await self.store.renew(self.job.id, self.owner, self.lease_seconds)


class SpecJobRunner(Protocol):
    async def run(self, ctx: JobContext) -> Outcome: ...


class SpecGeneratorWorker(LeaseWorker[SpecJob]):
    task_prefix = "spec-worker"
    tick_label = "spec worker tick"
    item_label = "spec job"
    log = logger

    def __init__(
        self,
        store: MongoSpecJobStore,
        files: SpecJobFiles,
        runner: SpecJobRunner,
        *,
        owner: str | None = None,
        interval: float = 5.0,
        lease_seconds: float = 90.0,
        ttl_seconds: float = 7 * 24 * 3600,
    ):
        super().__init__(owner=owner, interval=interval, lease_seconds=lease_seconds)
        self.store = store
        self.files = files
        self.runner = runner
        self.ttl_seconds = ttl_seconds

    # -- LeaseWorker ----------------------------------------------------------------

    async def acquire(self) -> SpecJob | None:
        return await self.store.acquire(self.owner, self.lease_seconds)

    async def process(self, item: SpecJob) -> None:
        await self.run_job(item)

    async def renew(self, item_id: str) -> bool:
        return await self.store.renew(item_id, self.owner, self.lease_seconds)

    # -- job 하나 -------------------------------------------------------------------

    async def run_job(self, job: SpecJob) -> None:
        ctx = JobContext(self.store, self.files, self.owner, self.lease_seconds, job)
        async with self.heartbeating(job.id):
            try:
                outcome = await self.runner.run(ctx)
            except LeaseLost:
                logger.warning("spec job %s: lease lost, another worker took over", job.id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - runner 실패는 job 실패로 기록한다
                logger.exception("spec job %s: runner failed", job.id)
                outcome = Outcome.failed("runner_error", f"{type(e).__name__}: {e}")
            try:
                await self.store.finish(
                    job.id,
                    self.owner,
                    state=outcome.state,
                    ttl_seconds=self.ttl_seconds,
                    draft_ref=outcome.draft_ref,
                    report=outcome.report,
                    verdict=outcome.verdict,
                    error=outcome.error,
                )
            except LeaseLost:
                logger.warning("spec job %s: lease lost before finish", job.id)
