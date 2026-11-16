"""평가 실행과 그 결과 (P45 9절, D109). 컬렉션 `evaluation_runs`, `evaluation_results`,
`evaluation_run_days`.

    evaluation_runs    {_id, tenant, agent_id, set_id, set_digest, revision, spec_digest?, grader?,
                        cases[{id, digest}], repeats, by{sub, username, tv}, header_mode, state,
                        reason?, stopped?, cancelled_by?, preview_id?, driver?,
                        step{index, session_id?, retried, attempts, graded}, verdicts[], totals,
                        active?, day?, created_at, running_at?, ended_at?}
    evaluation_results {_id, tenant, agent_id, run_id, set_id, case_id, case_digest, repeat,
                        started_by, session_id?, answer, tool_calls[], checks[], criteria[],
                        grader_call?, grader_error?, turns[], tokens, cost_usd?, created_at,
                        expires_at}
    evaluation_run_days {_id: <tenant>:<UTC 날>, count, expires_at}

- **실행 하나.** Agent 하나에 도는 실행은 하나다. 도는 동안만 `active` 가 참이고 그 칸의 부분 고유
  인덱스가 지킨다(409 `evaluation_run_active`). 끝나면 칸을 지운다.
- **걸음.** 실행은 다음 걸음(`step.index`, 사례와 반복의 차례)과 실행을 맡은 레플리카(`driver`)를
  가진다. 걸음을 옮기는 쓰기와 상태를 옮기는 쓰기는 모두 그 둘과 도는 상태를 조건으로 건다. 그래서
  리스를 잃은 레플리카는 아무것도 쓰지 못한다.
- **사례.** 시작할 때 사례마다의 id 와 지문만 적고 내용은 적지 않는다. 사례는 그 지문 그대로일
  때만 돌고 채점된다.
- **채점.** 사례가 다 돌면 `grading` 으로 옮겨 채점 기준이 있는 사례와 반복을 차례로 채점한다
  (`step.graded`). 시작할 때의 채점 모델(`grader`)과 리비전 내용의 지문(`spec_digest`)을 적어 둔다.
  일찍 멈춘 실행은 멈춘 까닭(`stopped`)을 들고 채점한 뒤 그 까닭으로 `error` 가 된다.
- **판정과 결과.** 실행 문서는 사례와 반복마다 내용 없는 판정(`verdicts`)을 Agent 가 사는 동안
  든다. 결과 문서는 내용(최종 답, Tool 호출과 그 답한 꼴, 확인 항목과 기준의 판정, 토큰, 금액)을
  들고 `DAEGAM_EVALUATION_RESULT_DAYS` 뒤 TTL 로 지워지며, 사례를 지우면 함께 지워진다
  (`store/evaluation.py`).
- **하루 상한.** 테넌트의 UTC 하루 칸(`evaluation_run_days`)에 시작이 자리를 잡고, 미리 보기가
  준비되기 전에 끝난 실행은 자리를 돌려준다. 그래서 상한은 미리 보기가 뜬 실행만 센다.

저장 모델은 모르는 칸을 버리고 상태, 까닭, 답한 꼴을 글자로 읽는다. 뒤 판이 값을 더해도 이 판이
읽게 하려는 것이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from daegam.store.base import (
    Document,
    UtcDatetime,
    ensure_ttl_index,
    new_id,
    take_in_window,
    utcnow,
)

EVALUATION_RUNS = "evaluation_runs"
EVALUATION_RESULTS = "evaluation_results"
EVALUATION_RUN_DAYS = "evaluation_run_days"

RunState = Literal["queued", "starting", "running", "grading", "finished", "error", "cancelled"]
# 도는 상태. 이 상태의 실행만 `active` 칸을 가진다
ACTIVE_STATES: tuple[str, ...] = ("queued", "starting", "running", "grading")
# 하루 칸과 그 칸을 남기는 시간
DAY = timedelta(days=1)
DAY_KEEP = timedelta(days=1)

_READ = ConfigDict(extra="ignore")


class Grader(BaseModel):
    """세트의 채점 모델. provider 는 등록된 provider 의 이름이다. 세트(`store/evaluation.py`)와
    실행이 함께 쓴다."""

    model_config = _READ

    provider: str
    model: str


class RunCase(BaseModel):
    """시작할 때의 사례 하나. 내용은 적지 않는다."""

    model_config = _READ

    id: str
    digest: str


class RunActor(BaseModel):
    """실행을 시작한 사람. tv 는 local 모드에서 턴 위임을 열 때 맞댈 토큰 판이고, groups 는 시작할
    때의 IdP 그룹이다. 미리 보기를 열 때 그 사람의 지금 소속과 함께 그 사람의 주체가 된다."""

    model_config = _READ

    sub: str
    username: str = ""
    tv: int | None = None
    groups: list[str] = Field(default_factory=list)


class RunStep(BaseModel):
    """다음 걸음. index 는 사례와 반복의 차례(사례 하나의 반복을 모두 돈 뒤 다음 사례)이고,
    session_id 는 그 걸음이 연 대화, retried 는 미리 보기 파드가 다시 떠서 그 걸음을 한 번 더
    돌았는지, attempts 는 그 걸음을 시작한 수다. graded 는 `grading` 에서 다음에 채점할 판정의
    차례(`verdicts` 의 자리)다."""

    model_config = _READ

    index: int = 0
    session_id: str | None = None
    retried: bool = False
    attempts: int = 0
    graded: int = 0


class CaseVerdict(BaseModel):
    """사례와 반복 하나의 내용 없는 판정. state 는 `done`(돌았다), `error`(돌다 실패했다),
    `changed`(도는 동안 바뀌거나 지워져 돌지 않았다)다. counted 는 사례에 `done` 밖의 확인 항목이
    있는가다. checks_passed 는 확인 항목이 모두 지났는가다. criteria 는 사례의 채점 기준 수,
    criteria_passed 는 기준이 모두 지났는가이고 채점 전이나 기준이 없으면 None 이다. grader_error 는
    채점 모델의 답을 읽지 못했다는 것이고 기준이 지나지 못한 것으로 센다 (11절)."""

    model_config = _READ

    case_id: str
    case_digest: str
    repeat: int
    state: str
    counted: bool = False
    checks_passed: bool | None = None
    criteria: int = 0
    criteria_passed: bool | None = None
    grader_error: bool = False


class RunTotals(BaseModel):
    """실행의 합계. cases 는 판정이 난 사례 수, counted 는 그 가운데 센 사례, passed 는 지난
    사례다. tokens 와 cost_usd 는 실행이 쓴 원장(사례의 턴과 채점)의 합이다. 금액은 값을 매긴 행만
    더하고, 모두 값이 없으면 None 이다."""

    model_config = _READ

    cases: int = 0
    counted: int = 0
    passed: int = 0
    tokens: int = 0
    cost_usd: float | None = None


class EvaluationRun(Document):
    id: str = Field(default_factory=new_id)
    tenant: str
    agent_id: str
    set_id: str
    set_digest: str
    revision: int
    # 고른 리비전 내용의 지문(`services/agent_rollback.py` `spec_digest`). 배포 게이트와 기준선이
    # 리비전 번호 대신 이것으로 견준다 (12절)
    spec_digest: str | None = None
    # 시작할 때 세트의 채점 모델. 세트의 지문이 이것을 품으므로 시작한 사람이 본 것이다
    grader: Grader | None = None
    cases: list[RunCase] = Field(default_factory=list)
    repeats: int = 1
    by: RunActor
    # header 모드는 턴에 사람이 없어 Agent 의 자격으로 돈다 (9절)
    header_mode: bool = False
    state: str = "queued"
    reason: str | None = None
    # 사례가 일찍 멈춘 까닭. 돈 것을 채점한 뒤 이 까닭으로 `error` 가 된다 (9절)
    stopped: str | None = None
    cancelled_by: str | None = None
    preview_id: str | None = None
    driver: str | None = None
    step: RunStep = Field(default_factory=RunStep)
    verdicts: list[CaseVerdict] = Field(default_factory=list)
    totals: RunTotals = Field(default_factory=RunTotals)
    # 도는 동안만 참이다. 만들 때 참으로 두고 끝나면 칸을 지운다
    active: bool | None = None
    # 하루 상한에서 잡은 칸의 id. 돌려주면 지운다
    day: str | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    running_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None

    @property
    def steps(self) -> int:
        return len(self.cases) * self.repeats

    def step_case(self, index: int) -> tuple[RunCase, int]:
        """걸음의 (사례, 반복)이다."""
        return self.cases[index // self.repeats], index % self.repeats


class ToolCallRecord(BaseModel):
    """Tool 호출 하나와 그것을 답한 꼴. answer 는 `ran`(불렸다), `simulated`(가상 결과를
    돌려줬다), `rejected`(거부했다), `refused`(런타임이 거절했다)이고, 답하기 전에 턴이 끝났으면 빈
    글이다."""

    model_config = _READ

    id: str | None = None
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    answer: str = ""
    status: str | None = None
    result: str | None = None


class CheckVerdict(BaseModel):
    """확인 항목 하나의 판정. gap 은 지나지 못한 까닭이다."""

    model_config = _READ

    kind: str
    passed: bool
    gap: str | None = None


class CriterionVerdict(BaseModel):
    """채점 기준 하나의 판정 (11절). gap 은 채점 모델이 적은 모자란 점이다."""

    model_config = _READ

    criterion: str
    passed: bool
    gap: str | None = None


class EvaluationResult(Document):
    """사례와 반복 하나의 결과. 내용은 실행을 시작한 사람만 본다 (9절)."""

    id: str = Field(default_factory=new_id)
    tenant: str
    agent_id: str
    run_id: str
    set_id: str
    case_id: str
    case_digest: str
    repeat: int
    started_by: str
    # 그 걸음이 사례를 돈 대화. 원장에서 이 사례의 턴을 고르는 열쇠다(`ref`)
    session_id: str | None = None
    answer: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    checks: list[CheckVerdict] = Field(default_factory=list)
    criteria: list[CriterionVerdict] = Field(default_factory=list)
    # 채점 호출의 원장 행 id 와, 채점 모델의 호출이 실패했거나 답을 읽지 못했으면 그 코드
    grader_call: str | None = None
    grader_error: str | None = None
    # 턴마다 끝난 꼴(`done`·`error:<code>`)
    turns: list[str] = Field(default_factory=list)
    # 턴의 토큰(`input`·`output`)과 채점 호출의 토큰(`grader_input`·`grader_output`)
    tokens: dict[str, int] = Field(default_factory=dict)
    # 그 대화의 턴과 채점 호출의 원장 금액. 실행이 끝날 때 원장에서 적는다
    cost_usd: float | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)
    expires_at: UtcDatetime


class RunActive(Exception):
    """그 Agent 에 도는 실행이 이미 있다."""


def _active(run_id: str, owner: str | None = None, **more: Any) -> dict[str, Any]:
    query: dict[str, Any] = {"_id": run_id, "state": {"$in": list(ACTIVE_STATES)}, **more}
    if owner is not None:
        query["driver"] = owner
    return query


def _ended(state: str, now: datetime, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "$set": {"state": state, "ended_at": now, "driver": None, **fields},
        "$unset": {"active": ""},
    }


class MongoEvaluationRunStore:
    def __init__(self, db: AsyncDatabase):
        self.runs = db[EVALUATION_RUNS]
        self.results = db[EVALUATION_RESULTS]
        self.days = db[EVALUATION_RUN_DAYS]

    async def init_schema(self) -> None:
        await self.runs.create_index(
            [("agent_id", ASCENDING)],
            unique=True,
            name="uq_active_run",
            partialFilterExpression={"active": True},
        )
        await self.runs.create_index(
            [("tenant", ASCENDING), ("agent_id", ASCENDING), ("set_id", ASCENDING)],
            name="ix_set_runs",
        )
        await self.runs.create_index([("state", ASCENDING)], name="ix_state")
        # 배포 게이트와 기준선이 내용마다 끝난 실행을 찾는다 (12절)
        await self.runs.create_index(
            [
                ("agent_id", ASCENDING),
                ("set_id", ASCENDING),
                ("spec_digest", ASCENDING),
                ("ended_at", DESCENDING),
            ],
            name="ix_content_runs",
        )
        await self.results.create_index([("run_id", ASCENDING)], name="ix_run")
        await self.results.create_index([("case_id", ASCENDING)], name="ix_case")
        await ensure_ttl_index(self.results, "expires_at", 0, "ttl_expires_at")
        await ensure_ttl_index(self.days, "expires_at", 0, "ttl_expires_at")

    # -- 실행 ---------------------------------------------------------------------------

    async def create(self, run: EvaluationRun) -> EvaluationRun:
        """도는 실행 하나를 넣는다. 그 Agent 에 도는 실행이 있으면 `RunActive` 다."""
        run.active = True
        try:
            await self.runs.insert_one(run.to_doc())
        except DuplicateKeyError as e:
            raise RunActive(run.agent_id) from e
        return run

    async def get(self, tenant: str, agent_id: str, run_id: str) -> EvaluationRun | None:
        doc = await self.runs.find_one({"_id": run_id, "tenant": tenant, "agent_id": agent_id})
        return EvaluationRun.from_doc(doc) if doc else None

    async def by_id(self, run_id: str) -> EvaluationRun | None:
        doc = await self.runs.find_one({"_id": run_id})
        return EvaluationRun.from_doc(doc) if doc else None

    async def finished_runs(
        self,
        tenant: str,
        agent_id: str,
        set_id: str,
        *,
        spec_digest: str,
        set_digest: str | None = None,
        since: datetime | None = None,
        limit: int = 0,
    ) -> list[EvaluationRun]:
        """그 세트의 끝난 실행 가운데 그 내용(spec_digest)의 것. 새로 끝난 것이 앞이다.
        set_digest 와 since 를 주면 그 세트 지문과 그 뒤에 끝난 것만이다 (12절)."""
        query: dict[str, Any] = {
            "tenant": tenant,
            "agent_id": agent_id,
            "set_id": set_id,
            "state": "finished",
            "spec_digest": spec_digest,
        }
        if set_digest is not None:
            query["set_digest"] = set_digest
        if since is not None:
            query["ended_at"] = {"$gte": since}
        cursor = self.runs.find(query).sort([("ended_at", DESCENDING), ("_id", DESCENDING)])
        return [EvaluationRun.from_doc(d) async for d in cursor.limit(limit)]

    async def runs_of(self, tenant: str, agent_id: str, set_id: str) -> list[EvaluationRun]:
        """그 세트의 실행. 새것이 앞이다."""
        query = {"tenant": tenant, "agent_id": agent_id, "set_id": set_id}
        cursor = self.runs.find(query).sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        return [EvaluationRun.from_doc(d) async for d in cursor]

    async def latest(
        self,
        tenant: str,
        agent_id: str,
        set_id: str,
        *,
        sub: str | None = None,
        spec_digest: str | None = None,
    ) -> EvaluationRun | None:
        """그 세트의 가장 새 실행. 상태를 가리지 않는다. sub 를 주면 그 사람이 시작한 것,
        spec_digest 를 주면 그 내용을 평가한 것만이다."""
        query: dict[str, Any] = {"tenant": tenant, "agent_id": agent_id, "set_id": set_id}
        if sub is not None:
            query["by.sub"] = sub
        if spec_digest is not None:
            query["spec_digest"] = spec_digest
        newest = [("created_at", DESCENDING), ("_id", DESCENDING)]
        doc = await self.runs.find_one(query, sort=newest)
        return EvaluationRun.from_doc(doc) if doc else None

    async def active_runs(self) -> list[EvaluationRun]:
        """모든 테넌트의 도는 실행. 오래된 것이 앞이다."""
        cursor = self.runs.find({"state": {"$in": list(ACTIVE_STATES)}})
        cursor = cursor.sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        return [EvaluationRun.from_doc(d) async for d in cursor]

    async def claim(self, run_id: str, owner: str) -> EvaluationRun | None:
        """리스를 쥔 레플리카가 실행을 모는 쪽으로 저를 적는다. 끝난 실행이면 None 이다."""
        doc = await self.runs.find_one_and_update(
            _active(run_id),
            {"$set": {"driver": owner}},
            return_document=ReturnDocument.AFTER,
        )
        return EvaluationRun.from_doc(doc) if doc else None

    async def update(
        self,
        run_id: str,
        owner: str,
        fields: dict[str, Any],
        *,
        index: int | None = None,
        graded: int | None = None,
        push: CaseVerdict | None = None,
    ) -> bool:
        """실행을 맡은 레플리카가 칸을 쓴다. index 를 주면 다음 걸음이, graded 를 주면 다음 채점이
        그것일 때만이고, push 는 판정 하나를 더한다."""
        more: dict[str, Any] = {"step.index": index} if index is not None else {}
        if graded is not None:
            more["step.graded"] = graded
        update: dict[str, Any] = {"$set": fields}
        if push is not None:
            update["$push"] = {"verdicts": push.model_dump()}
        res = await self.runs.update_one(_active(run_id, owner, **more), update)
        return res.modified_count == 1

    async def end(
        self, run_id: str, owner: str | None, state: RunState, now: datetime, **fields: Any
    ) -> bool:
        """도는 실행을 끝낸다. owner 를 주면 그 레플리카가 몰 때만이다."""
        res = await self.runs.update_one(_active(run_id, owner), _ended(state, now, fields))
        return res.modified_count == 1

    async def cancel(
        self, tenant: str, agent_id: str, run_id: str, by: str | None, now: datetime
    ) -> bool:
        query = {**_active(run_id), "tenant": tenant, "agent_id": agent_id}
        res = await self.runs.update_one(query, _ended("cancelled", now, {"cancelled_by": by}))
        return res.modified_count == 1

    # -- 하루 상한 ------------------------------------------------------------------------

    async def take_day(self, tenant: str, now: datetime, cap: int) -> str | None:
        """테넌트의 오늘(UTC) 칸에 하나를 더한다. 자리를 잡으면 칸 id 를, 이미 cap 이면 None 을
        돌려준다."""
        return await take_in_window(self.days, tenant, now, DAY, DAY_KEEP, cap)

    async def release_day(self, key: str) -> None:
        """잡은 칸 하나를 돌려준다. 실행을 넣지 못한 시작이 부른다."""
        await self.days.update_one({"_id": key}, {"$inc": {"count": -1}})

    async def release_run_day(self, run_id: str) -> None:
        """미리 보기가 준비되기 전에 끝난 실행이 잡은 칸을 돌려준다. 한 번만 돌려준다. 준비된
        실행은 `running` 으로 옮길 때 칸 id 를 지워 상한에 남긴다."""
        doc = await self.runs.find_one_and_update(
            {"_id": run_id, "day": {"$type": "string"}}, {"$set": {"day": None}}
        )
        if doc is not None and doc.get("day"):
            await self.release_day(doc["day"])

    # -- 결과 ---------------------------------------------------------------------------

    async def put_result(self, result: EvaluationResult) -> None:
        """결과를 넣거나 같은 id 의 것을 바꾼다. 다시 돈 걸음이 앞의 결과를 바꾼다."""
        await self.results.replace_one({"_id": result.id}, result.to_doc(), upsert=True)

    async def get_result(self, result_id: str) -> EvaluationResult | None:
        doc = await self.results.find_one({"_id": result_id})
        return EvaluationResult.from_doc(doc) if doc else None

    async def update_result(self, result_id: str, fields: dict[str, Any]) -> None:
        """결과에 채점과 금액을 적는다. 지워진 결과는 되살리지 않는다."""
        await self.results.update_one({"_id": result_id}, {"$set": fields})

    async def delete_result(self, result_id: str) -> None:
        await self.results.delete_one({"_id": result_id})

    async def results_of(self, run_id: str) -> list[EvaluationResult]:
        cursor = self.results.find({"run_id": run_id}).sort(
            [("created_at", ASCENDING), ("_id", ASCENDING)]
        )
        return [EvaluationResult.from_doc(d) async for d in cursor]


__all__ = [
    "ACTIVE_STATES",
    "EVALUATION_RESULTS",
    "EVALUATION_RUNS",
    "CaseVerdict",
    "CheckVerdict",
    "CriterionVerdict",
    "EvaluationResult",
    "EvaluationRun",
    "Grader",
    "MongoEvaluationRunStore",
    "RunActive",
    "RunActor",
    "RunCase",
    "RunState",
    "RunStep",
    "RunTotals",
    "ToolCallRecord",
]
