"""평가 실행과 결과의 저장 (P45 9절, 8절, 16절). Mongo 는 진짜다.

- 실행 문서는 Agent 마다 도는 것 하나만 둔다(부분 고유 인덱스). 끝나면 다시 시작할 수 있다.
- 걸음과 상태를 옮기는 쓰기는 실행을 맡은 레플리카와 다음 걸음을 조건으로 건다.
- 하루 상한은 칸에 자리를 잡고, 준비되기 전에 끝난 실행은 한 번만 돌려준다.
- 결과는 TTL 로 지워지고, 사례를 지우는 모든 길(사례, 세트, 복사본, 정리, Agent)이 결과도 지운다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from daegam.store.base import utcnow
from daegam.store.evaluation import (
    CaseContent,
    EvaluationCase,
    EvaluationSet,
    MongoEvaluationStore,
)
from daegam.store.evaluation_runs import (
    CaseVerdict,
    EvaluationResult,
    EvaluationRun,
    MongoEvaluationRunStore,
    RunActive,
    RunActor,
    RunCase,
)
from tests.helpers.aio import run_stores
from tests.helpers.docs import TENANT
from tests.helpers.evaluation import case_doc

STORES = (MongoEvaluationStore, MongoEvaluationRunStore)
CONTENT = CaseContent(messages=["질문"])
OWNER = "replica-a"


def run_doc(run_id: str = "run-1", agent_id: str = "ag-1", **kw: Any) -> EvaluationRun:
    return EvaluationRun(
        id=run_id,
        tenant=TENANT,
        agent_id=agent_id,
        set_id="set-1",
        set_digest="d",
        revision=1,
        cases=[RunCase(id="c-1", digest="x"), RunCase(id="c-2", digest="y")],
        repeats=2,
        by=RunActor(sub="dev-a", username="dev-a"),
        **kw,
    )


def result_doc(case_id: str, *, agent_id: str = "ag-1", set_id: str = "set-1") -> EvaluationResult:
    return EvaluationResult(
        tenant=TENANT,
        agent_id=agent_id,
        run_id="run-1",
        set_id=set_id,
        case_id=case_id,
        case_digest="x",
        repeat=0,
        started_by="dev-a",
        expires_at=utcnow() + timedelta(days=30),
    )


def _case(case_id: str, *, rating: str | None = None) -> EvaluationCase:
    return case_doc(case_id, tenant=TENANT, rating=rating, by="u")


def test_the_schema_holds_one_active_run_per_agent(settings):
    async def go(_cases: MongoEvaluationStore, runs: MongoEvaluationRunStore) -> None:
        names = set(await runs.runs.index_information())
        assert {"uq_active_run", "ix_set_runs", "ix_state"} <= names
        results = set(await runs.results.index_information())
        assert {"ix_run", "ix_case", "ttl_expires_at"} <= results
        assert "ttl_expires_at" in set(await runs.days.index_information())
        await runs.create(run_doc())
        with pytest.raises(RunActive):
            await runs.create(run_doc("run-2"))
        # 다른 Agent 는 따로다
        await runs.create(run_doc("run-3", agent_id="ag-2"))
        assert await runs.end("run-1", None, "finished", utcnow())
        await runs.create(run_doc("run-2"))
        assert {r.id for r in await runs.active_runs()} == {"run-2", "run-3"}

    run_stores(settings, STORES, go)


def test_steps_and_ends_are_written_only_by_the_driver_at_its_step(settings):
    async def go(_cases: MongoEvaluationStore, runs: MongoEvaluationRunStore) -> None:
        await runs.create(run_doc())
        claimed = await runs.claim("run-1", OWNER)
        assert claimed is not None and claimed.driver == OWNER
        verdict = CaseVerdict(case_id="c-1", case_digest="x", repeat=0, state="done")
        assert await runs.update("run-1", OWNER, {"step.index": 1}, index=0, push=verdict)
        # 걸음이 이미 옮겨졌거나 남이 몰면 쓰지 않는다
        assert not await runs.update("run-1", OWNER, {"step.index": 2}, index=0, push=verdict)
        assert await runs.claim("run-1", "replica-b") is not None
        assert not await runs.update("run-1", OWNER, {"step.index": 2}, index=1)
        assert not await runs.end("run-1", OWNER, "error", utcnow(), reason="x")
        assert await runs.end("run-1", "replica-b", "error", utcnow(), reason="token_limit")
        run = await runs.by_id("run-1")
        assert run is not None and run.state == "error" and run.reason == "token_limit"
        assert run.active is None and run.driver is None and len(run.verdicts) == 1
        # 끝난 실행은 다시 잡지 못하고 취소되지 않는다
        assert await runs.claim("run-1", OWNER) is None
        assert not await runs.cancel(TENANT, "ag-1", "run-1", "dev-a", utcnow())

    run_stores(settings, STORES, go)


def test_the_daily_cap_takes_a_place_and_gives_it_back_once(settings):
    async def go(_cases: MongoEvaluationStore, runs: MongoEvaluationRunStore) -> None:
        now = utcnow()
        first = await runs.take_day(TENANT, now, 2)
        assert first is not None and await runs.take_day(TENANT, now, 2) == first
        assert await runs.take_day(TENANT, now, 2) is None
        assert await runs.take_day(TENANT, now, 0) is None
        await runs.create(run_doc(day=first))
        await runs.release_run_day("run-1")
        await runs.release_run_day("run-1")
        assert (await runs.days.find_one({"_id": first}))["count"] == 1
        assert await runs.take_day(TENANT, now, 2) == first
        # 준비된 실행은 칸을 남긴다
        await runs.create(run_doc("run-2", agent_id="ag-2", day=first))
        # 준비된 실행은 running 으로 옮길 때 칸 id 를 지운다
        await runs.runs.update_one({"_id": "run-2"}, {"$set": {"day": None}})
        await runs.release_run_day("run-2")
        assert (await runs.days.find_one({"_id": first}))["count"] == 2

    run_stores(settings, STORES, go)


def test_every_way_a_case_goes_takes_its_results(settings):
    async def go(cases: MongoEvaluationStore, runs: MongoEvaluationRunStore) -> None:
        item = EvaluationSet(
            id="set-1", tenant=TENANT, agent_id="ag-1", name="n", created_by="u", updated_by="u"
        )
        await cases.create_set(item)
        for case in (_case("c-1"), _case("c-2", rating="r-1"), _case("c-3")):
            assert await cases.reserve_case(item, "u")
            await cases.insert_case(case)
            await runs.put_result(result_doc(case.id))
        await runs.put_result(result_doc("c-gone"))

        async def left() -> set[str]:
            return {d["case_id"] async for d in runs.results.find()}

        assert await cases.delete_case(TENANT, "set-1", "c-1")
        assert await left() == {"c-2", "c-3", "c-gone"}
        await cases.delete_copies(["r-1"])
        assert await left() == {"c-3", "c-gone"}
        assert len(await cases.orphan_results()) == 1
        assert await left() == {"c-3"}
        assert await cases.delete_set(TENANT, "ag-1", "set-1")
        assert await left() == set()

    run_stores(settings, STORES, go)


def test_forgetting_an_agent_takes_its_runs_and_results(settings):
    async def go(cases: MongoEvaluationStore, runs: MongoEvaluationRunStore) -> None:
        await runs.create(run_doc())
        await runs.create(run_doc("run-2", agent_id="ag-2"))
        await runs.put_result(result_doc("c-1"))
        await runs.put_result(result_doc("c-1", agent_id="ag-2"))
        assert await cases.agent_ids() == {"ag-1", "ag-2"}
        await cases.forget_agent(TENANT, "ag-1")
        await cases.forget_agents(["ag-2"])
        assert await runs.runs.count_documents({}) == 0
        assert await runs.results.count_documents({}) == 0

    run_stores(settings, STORES, go)
