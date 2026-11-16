"""평가 실행의 시작, 목록, 읽기, 취소 (P45 9절, 13절, D109). 실행을 모는 일은 `runner.py` 다.

- **시작.** 평가를 껐으면 409 `evaluations_disabled`, 미리 보기를 껐으면 409 `preview_disabled` 다.
  시작은 시작한 사람이 본 세트의 지문을 싣고, 그 사이 세트가 바뀌었으면 409
  `evaluation_set_changed` 다. 리비전은 주지 않으면 저장한 최신 것이다. 지금의 사례 목록으로 사례
  수 × 반복 수가 100 을 넘으면 422 `evaluation_set_full` 이고, 사례가 없으면 422 `invalid_value`
  다. 채점 기준이 있는 사례가 있으면 세트의 채점 모델은 리비전의 모델 자리가 쓰는 provider 여야
  하고, 아니면 409 `evaluation_grader_provider` 다 (11절). 테넌트의 오늘(UTC) 칸에 자리가 없으면 429
  `evaluation_run_limit`, 그 Agent 에 도는 실행이 있으면 409 `evaluation_run_active` 다. 실행은
  사례마다의 id 와 지문, 리비전 내용의 지문, 채점 모델만 적는다. 동의가 끝난 복사본은 넣지 않는다.
- **확인할 사례.** 시작하는 사람이 그 세트로 마지막으로 시작한 실행이 기록한 (id, 지문)에 없는 사례
  가운데 다른 사람이 쓰거나 고친 것이다. 시작한 적이 없으면 다른 사람이 쓰거나 고친 모든 사례다. 그
  사례는 시작한 사람의 권한으로 돈다.
- **읽기.** 실행의 판정, 합계, 상태는 `agent:approve` 를 가진 모두가 본다. 결과의 내용(답, Tool
  호출과 그 인자와 결과, 확인 항목의 까닭)은 시작한 사람만 본다. 읽기 Tool 이 그 사람의 권한으로
  돌았기 때문이다.
- **취소.** 도는 실행을 `cancelled` 로 끝내고 그 미리 보기를 끝낸다. 열린 미리 보기 목록에서 평가
  미리 보기를 끝내도 그 실행이 취소된다.
"""

from __future__ import annotations

from typing import Any

from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.services.agent_preview import PREVIEW_DISABLED, AgentPreviews
from daegam.services.agent_rollback import spec_digest
from daegam.services.evaluation.errors import EvaluationError
from daegam.services.evaluation.grader import require_provider
from daegam.services.evaluation.runner import EVALUATIONS_DISABLED, EvaluationRunner
from daegam.services.evaluation.sets import EvaluationSets, not_found, set_full
from daegam.services.turn_person import person_of
from daegam.store.agents import Agent
from daegam.store.base import utcnow
from daegam.store.evaluation import MAX_CASE_RUNS, EvaluationCase
from daegam.store.evaluation_runs import (
    EvaluationResult,
    EvaluationRun,
    MongoEvaluationRunStore,
    RunActive,
    RunActor,
    RunCase,
)

EVALUATION_SET_CHANGED = "evaluation_set_changed"
EVALUATION_RUN_ACTIVE = "evaluation_run_active"
EVALUATION_RUN_LIMIT = "evaluation_run_limit"


def changed_by_others(
    cases: list[EvaluationCase], sub: str, last: EvaluationRun | None
) -> list[str]:
    """시작하는 사람이 확인할 사례의 id 다(모듈 머리). 마지막 실행이 기록한 (id, 지문)에 없고
    다른 사람이 쓰거나 고친 사례다. 마지막 실행이 없으면 다른 사람이 쓰거나 고친 모든 사례다."""
    seen = {(c.id, c.digest) for c in last.cases} if last is not None else set()
    return [
        case.id
        for case in cases
        if (case.id, case.digest) not in seen and (case.created_by != sub or case.updated_by != sub)
    ]


class EvaluationRuns:
    def __init__(
        self,
        runs: MongoEvaluationRunStore,
        sets: EvaluationSets,
        previews: AgentPreviews,
        runner: EvaluationRunner,
        settings: Settings,
    ):
        self.runs = runs
        self.sets = sets
        self.previews = previews
        self.runner = runner
        self.settings = settings

    # -- 시작 -----------------------------------------------------------------------------

    async def review(self, agent: Agent, set_id: str, principal: Principal) -> dict[str, Any]:
        """시작하는 사람이 확인할 사례와 지금 세트의 지문이다."""
        _item, cases, digest = await self.sets.cases(agent, set_id)
        last = await self.runs.latest(agent.tenant, agent.id, set_id, sub=principal.sub)
        return {
            "set_digest": digest,
            "since": last.created_at if last is not None else None,
            "cases": changed_by_others(cases, principal.sub, last),
        }

    async def start(
        self,
        agent: Agent,
        set_id: str,
        principal: Principal,
        *,
        set_digest: str,
        revision: int | None,
    ) -> EvaluationRun:
        if not self.settings.evaluations_enabled:
            raise EvaluationError(409, EVALUATIONS_DISABLED, "evaluations are turned off")
        if not self.previews.enabled:
            raise EvaluationError(409, PREVIEW_DISABLED, "previews are turned off")
        item, cases, digest = await self.sets.cases(agent, set_id)
        if digest != set_digest:
            raise EvaluationError(
                409,
                EVALUATION_SET_CHANGED,
                "the set changed after it was reviewed",
                set_digest=digest,
            )
        chosen = agent.revision if revision is None else revision
        rev = agent.get_revision(chosen)
        if rev is None:
            raise not_found("revision", str(chosen))
        if not cases:
            raise EvaluationError(422, "invalid_value", "the set has no cases to run")
        if len(cases) * item.repeats > MAX_CASE_RUNS:
            raise set_full()
        if item.grader is not None and any(c.content.criteria for c in cases):
            require_provider(item.grader, rev)
        cap = self.settings.evaluation_runs_per_day
        day = await self.runs.take_day(agent.tenant, utcnow(), cap)
        if day is None:
            raise EvaluationError(
                429,
                EVALUATION_RUN_LIMIT,
                f"a tenant can start {cap} evaluation runs a day",
                limit=cap,
            )
        person = person_of(principal)
        run = EvaluationRun(
            tenant=agent.tenant,
            agent_id=agent.id,
            set_id=set_id,
            set_digest=digest,
            revision=chosen,
            spec_digest=spec_digest(rev),
            grader=item.grader,
            cases=[RunCase(id=c.id, digest=c.digest) for c in cases],
            repeats=item.repeats,
            by=RunActor(
                sub=person.sub,
                username=person.username,
                tv=person.tv,
                groups=list(principal.groups),
            ),
            header_mode=not self.settings.token_auth,
            day=day,
        )
        try:
            return await self.runs.create(run)
        except RunActive as e:
            await self.runs.release_day(day)
            raise EvaluationError(
                409, EVALUATION_RUN_ACTIVE, "an evaluation of this agent is already running"
            ) from e

    # -- 읽기 -----------------------------------------------------------------------------

    async def runs_of(self, agent: Agent, set_id: str) -> list[EvaluationRun]:
        await self.sets.get_set(agent, set_id)
        return await self.runs.runs_of(agent.tenant, agent.id, set_id)

    async def get(self, agent: Agent, set_id: str, run_id: str) -> EvaluationRun:
        run = await self.runs.get(agent.tenant, agent.id, run_id)
        if run is None or run.set_id != set_id:
            raise not_found("evaluation run", run_id)
        return run

    async def results(
        self, run: EvaluationRun, principal: Principal
    ) -> list[EvaluationResult] | None:
        """결과의 내용. 시작한 사람이 아니면 None 이다. 읽기 Tool 이 그 사람의 권한으로 돌았기
        때문이다 (9절)."""
        if principal.sub != run.by.sub:
            return None
        return await self.runs.results_of(run.id)

    # -- 취소 -----------------------------------------------------------------------------

    async def cancel(self, agent: Agent, set_id: str, run_id: str, principal: Principal) -> None:
        run = await self.get(agent, set_id, run_id)
        if not await self.runs.cancel(agent.tenant, agent.id, run.id, principal.sub, utcnow()):
            raise EvaluationError(409, "state_conflict", f"evaluation run {run_id} is not running")
        await self.runner.stopped(run.id)

    async def preview_ended(self, preview: Agent, by: str) -> None:
        """열린 미리 보기 목록에서 평가 미리 보기를 끝냈다. 그 실행을 취소한다."""
        run_id = preview.evaluation_run
        if run_id is None:
            return
        run = await self.runs.by_id(run_id)
        if run is not None and await self.runs.cancel(
            run.tenant, run.agent_id, run.id, by, utcnow()
        ):
            await self.runner.stopped(run.id)


__all__ = [
    "EVALUATIONS_DISABLED",
    "EVALUATION_RUN_ACTIVE",
    "EVALUATION_RUN_LIMIT",
    "EVALUATION_SET_CHANGED",
    "EvaluationRuns",
    "changed_by_others",
]
