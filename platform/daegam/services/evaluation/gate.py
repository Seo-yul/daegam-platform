"""배포 게이트 (P45 12절, D109).

Agent 의 `evaluation_gate` 는 리비전 밖의 설정이다. 리비전이 게이트를 실으면 그 리비전이 게이트를 끌
수 있기 때문이다. 배포, 재배포, 되돌리기가 핀하는 내용에 대해 판정한다(`DeployGate.judge`).

- **새 내용에만.** 핀할 내용의 지문(`spec_digest`)이 배포된 리비전의 것도, 결과가 `deployed` 인
  배포 기록 줄의 것도 아닐 때만 걸린다. 복제 수, 자원, 노출, 0 으로 줄이기만 바꾼 재배포와, 전에 돈
  내용으로 돌아가는 배포는 지난다. 그 비상길에는 나이 한도도 없다. 미리 보기에는 걸리지 않는다.
- **지나는 것.** 게이트 세트의 지금 지문으로 그 내용을 평가한 끝난 실행이 `max_age_days` 안에
  끝났고, 센 사례가 하나 이상이며, 지난 비율이 `min_pass_rate` 이상이고, `no_worse_cases` 를
  켰으면 나빠진 사례가 없다. `no_worse_cases` 를 켰으면 센 사례마다 기준선이 있어야 하고, 결과가
  `deployed` 인 기록 줄이 없는 처음 배포만 기준선 없이 지난다. 기준선의 내용은 배포된 리비전이고,
  해제한 Agent 는 마지막으로 배포된 기록 줄의 내용이다(`baseline_content`). 기준선이
  `max_age_days` 보다 오래됐으면 없는 것으로 센다.
- **거절.** 409 `evaluation_required` 에 까닭(`no_run`, `stale`, `nothing_counted`, `pass_rate`,
  `worse_cases`, `baseline_missing`), 그 내용의 가장 새 실행(`run`, 상태를 가리지 않는다)과 판정한
  실행(`judged`)의 id·상태·합계, 사례를 싣는다. 그 내용의 끝난 실행이 세트의 다른 지문에만 있으면
  그 뒤에 세트가 바뀐 것이라 `stale` 이다. `worse_cases` 와 `baseline_missing` 은 기준선의
  리비전으로 세트를 돌리라는 제안을 함께 싣는다.
- **건너뛰기.** 이유(1에서 500자)를 주면 게이트를 건너뛰고 배포 기록 줄이 그 이유와 사람을 남긴다.
  게이트가 받아들인 배포는 그 실행의 id 를 남긴다.
- 평가를 끈 설치(`DAEGAM_EVALUATIONS_ENABLED` false)는 게이트가 걸리지 않고 경고
  `evaluation_gate_off` 를 싣는다. 판정은 채점 기준을 `DAEGAM_EVALUATION_GATE_CRITERIA` 가 켜졌을
  때만 센다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daegam.auth.principal import Principal
from daegam.config import Settings
from daegam.services.agent_graph import Issue
from daegam.services.agent_rollback import spec_digest
from daegam.services.evaluation.errors import EvaluationError
from daegam.services.evaluation.outcome import GateOutcome
from daegam.services.evaluation.sets import EvaluationSets, check_baseline_repeats
from daegam.services.evaluation.verdicts import Comparison, baselines, compare, totals
from daegam.store.agents import DEPLOYED, Agent, AgentSpec, EvaluationGate, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.evaluation_runs import EvaluationRun, MongoEvaluationRunStore

EVALUATION_REQUIRED = "evaluation_required"
EVALUATION_GATE_OFF = "evaluation_gate_off"
# 거절의 까닭 (12절)
NO_RUN = "no_run"
STALE = "stale"
NOTHING_COUNTED = "nothing_counted"
PASS_RATE = "pass_rate"
WORSE_CASES = "worse_cases"
BASELINE_MISSING = "baseline_missing"
# 기준선을 찾는 실행 수의 상한. 사례마다 가장 새 실행을 쓰므로 오래된 것은 거의 읽히지 않는다
BASELINE_RUNS = 50


def new_content(agent: Agent, content: AgentSpec) -> bool:
    """게이트가 걸리는 새 내용인가. 배포된 리비전(기록이 아니라 리비전에서 짓는다)과 결과가
    `deployed` 인 기록 줄의 내용이 아니다."""
    digest = spec_digest(content)
    deployed = agent.deployed
    if deployed is not None and spec_digest(deployed) == digest:
        return False
    return not any(r.spec_digest == digest and r.outcome == DEPLOYED for r in agent.deploy_history)


def baseline_content(agent: Agent) -> tuple[str, int] | None:
    """기준선을 찾는 내용의 (지문, 리비전)이다. 배포된 리비전이 있으면 그것이고, 해제한 Agent 는
    결과가 `deployed` 인 마지막 기록 줄의 것이다. 그런 줄도 없는 처음 배포만 None 이다 (12절)."""
    deployed = agent.deployed
    if deployed is not None:
        return spec_digest(deployed), deployed.revision
    ran = [r for r in agent.deploy_history if r.outcome == DEPLOYED]
    return (ran[-1].spec_digest, ran[-1].revision) if ran else None


def run_summary(run: EvaluationRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"id": run.id, "state": run.state, "totals": run.totals.model_dump()}


class DeployGate:
    def __init__(
        self,
        runs: MongoEvaluationRunStore,
        sets: EvaluationSets,
        agents: MongoAgentStore,
        settings: Settings,
    ):
        self.runs = runs
        self.sets = sets
        self.agents = agents
        self.settings = settings

    async def set_gate(
        self, agent: Agent, gate: EvaluationGate | None, principal: Principal
    ) -> EvaluationGate | None:
        """게이트를 켜거나 바꾸거나(None 이면) 끈다. 세트가 그 Agent 의 것이어야 하고,
        `no_worse_cases` 는 세트의 반복이 `BASELINE_REPEATS` 이상이어야 한다. 쓴 뒤에 세트를 한 번
        더 읽어, 그 사이 세트가 지워졌거나 반복이 줄었으면 게이트를 다시 끄고 같은 오류를 낸다.
        세트의 삭제와 반복 저장이 쓰기 앞의 게이트를 보기 때문이다."""
        if gate is not None:
            await self._check_set(agent, gate)
            gate = gate.model_copy(update={"updated_by": principal.sub, "updated_at": utcnow()})
        if not await self.agents.set_evaluation_gate(agent.tenant, agent.id, gate):
            raise EvaluationError(404, "not_found", f"agent {agent.id} not found")
        if gate is not None:
            try:
                await self._check_set(agent, gate)
            except EvaluationError:
                await self.agents.set_evaluation_gate(agent.tenant, agent.id, None)
                raise
        agent.evaluation_gate = gate
        return gate

    async def _check_set(self, agent: Agent, gate: EvaluationGate) -> None:
        item = await self.sets.get_set(agent, gate.set_id)
        if gate.no_worse_cases:
            check_baseline_repeats(item.repeats, field="no_worse_cases")

    @property
    def criteria(self) -> bool:
        return self.settings.evaluation_gate_criteria

    async def judge(
        self, agent: Agent, content: AgentSpec, *, skip: str | None = None
    ) -> GateOutcome:
        """핀할 내용 content 의 판정이다. 지나지 못하면 409 `evaluation_required` 다."""
        gate = agent.evaluation_gate
        if gate is None or agent.preview is not None:
            return GateOutcome()
        if not self.settings.evaluations_enabled:
            message = "evaluations are turned off, so the deploy gate does not apply"
            return GateOutcome(warnings=[_warning(message)])
        if not new_content(agent, content):
            return GateOutcome()
        if skip is not None:
            return GateOutcome(skipped=skip)
        run = await self._passed(agent, gate, spec_digest(content))
        return GateOutcome(run=run.id)

    async def _passed(self, agent: Agent, gate: EvaluationGate, digest: str) -> EvaluationRun:
        """게이트를 지난 실행이다. 없으면 까닭과 함께 409 다. 그 내용의 끝난 실행이 다른 세트
        지문에만 있으면 그 뒤에 세트가 바뀐 것이라 `stale` 이다."""
        set_digest = await self._set_digest(agent, gate.set_id)
        found: list[EvaluationRun] = []
        if set_digest is not None:
            found = await self._finished(agent, gate, digest, set_digest=set_digest)
        if not found:
            other = await self._finished(agent, gate, digest)
            raise await self._refusal(STALE if other else NO_RUN, agent, gate, digest)
        run = found[0]
        since = utcnow() - timedelta(days=gate.max_age_days)
        if run.ended_at is None or run.ended_at < since:
            raise await self._refusal(STALE, agent, gate, digest, run)
        counted = totals(run, run.totals.tokens, criteria=self.criteria)
        if not counted.counted:
            raise await self._refusal(NOTHING_COUNTED, agent, gate, digest, run)
        rate = counted.passed / counted.counted
        if rate < gate.min_pass_rate:
            raise await self._refusal(
                PASS_RATE,
                agent,
                gate,
                digest,
                run,
                pass_rate=rate,
                min_pass_rate=gate.min_pass_rate,
            )
        base = baseline_content(agent)
        if gate.no_worse_cases and base is not None:
            seen = await self.against_baseline(agent, run, max_age_days=gate.max_age_days)
            offer = {"revision": base[1]}
            if seen.worse:
                raise await self._refusal(
                    WORSE_CASES, agent, gate, digest, run, cases=seen.worse, offer=offer
                )
            if seen.missing:
                raise await self._refusal(
                    BASELINE_MISSING, agent, gate, digest, run, cases=seen.missing, offer=offer
                )
        return run

    async def _finished(
        self, agent: Agent, gate: EvaluationGate, digest: str, *, set_digest: str | None = None
    ) -> list[EvaluationRun]:
        return await self.runs.finished_runs(
            agent.tenant, agent.id, gate.set_id, spec_digest=digest, set_digest=set_digest, limit=1
        )

    async def against_baseline(
        self, agent: Agent, run: EvaluationRun, *, max_age_days: int
    ) -> Comparison:
        """실행을 기준선과 견준다(`baseline_content` 의 내용). 기준선은 max_age_days 안에 끝난
        실행이고 그 실행 자신은 빠진다. 처음 배포는 기준선이 없다."""
        base = baseline_content(agent)
        if base is None:
            return compare(run, {}, criteria=self.criteria)
        since = utcnow() - timedelta(days=max_age_days)
        found = await self.runs.finished_runs(
            agent.tenant,
            agent.id,
            run.set_id,
            spec_digest=base[0],
            since=since,
            limit=BASELINE_RUNS,
        )
        return compare(
            run,
            baselines([r for r in found if r.id != run.id], criteria=self.criteria),
            criteria=self.criteria,
        )

    async def _refusal(
        self,
        reason: str,
        agent: Agent,
        gate: EvaluationGate,
        digest: str,
        judged: EvaluationRun | None = None,
        **extra: Any,
    ) -> EvaluationError:
        """409 `evaluation_required`. run 은 그 내용을 평가한 가장 새 실행(상태를 가리지 않는다)이고
        judged 는 판정한 끝난 실행이다 (12절)."""
        latest = await self.runs.latest(agent.tenant, agent.id, gate.set_id, spec_digest=digest)
        return EvaluationError(
            409,
            EVALUATION_REQUIRED,
            f"the deploy needs a passed evaluation run of its content ({reason})",
            reason=reason,
            run=run_summary(latest),
            judged=run_summary(judged),
            **extra,
        )

    async def _set_digest(self, agent: Agent, set_id: str) -> str | None:
        """세트의 지금 지문이다. 세트가 없으면 None 이다."""
        try:
            _item, _cases, digest = await self.sets.cases(agent, set_id)
        except EvaluationError as e:
            if e.status == 404:
                return None
            raise
        return digest


def _warning(message: str) -> Issue:
    return Issue(code=EVALUATION_GATE_OFF, message=message, level="warning")


__all__ = [
    "BASELINE_MISSING",
    "EVALUATION_GATE_OFF",
    "EVALUATION_REQUIRED",
    "NOTHING_COUNTED",
    "NO_RUN",
    "PASS_RATE",
    "STALE",
    "WORSE_CASES",
    "DeployGate",
    "GateOutcome",
    "baseline_content",
    "new_content",
]
