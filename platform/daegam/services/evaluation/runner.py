"""평가 실행기 (P45 9절, 10절, 11절, D109).

리스 워커(`EvaluationRunWorker`)가 리스를 쥔 실행마다 한 태스크를 띄우고, 태스크는 그 실행의 걸음을
시간 조각(`SLICE`)만큼 차례로 몬다. 걸음은 미리 보기의 시작, 준비 기다리기, 사례와 반복 하나, 채점
하나다.
걸음과 결과의 쓰기는 모두 실행을 맡은 레플리카와 다음 걸음과 도는 상태를 조건으로 건다. 조건부
쓰기가 하나라도 지면 그 조각을 멈춘다. 결과는 걸음을 옮기는 쓰기가 이긴 뒤, 사례가 기록한 지문
그대로일 때만 쓴다. 그래서 리스를 잃은 레플리카는 아무것도 쓰지 못하고, 실행 중에 동의가 끝난 사례는
결과를 남기지 않는다.

- **시작** (`queued`). 시작한 사람의 지금 소속과 시작할 때의 그룹으로 그 사람의 주체를 짓고(header
  모드는 header 의 주체), 그 사람이 아직 그 테넌트의 `agent:deploy` 를 가졌는지 본다. 아니면
  `acts_as_unavailable` 이다. 그 주체로 고른 리비전의 평가 미리 보기를 연다. 자리가 없으면 30분까지
  `queued` 로 기다렸다가 `preview_limit` 로 끝난다. 하루 시작을 다 썼으면 `preview_start_limit`,
  리비전이 지금 검증을 지나지 못하면 `invalid_graph`, 이름을 모르는 소스가 있으면
  `evaluation_tools_unknown` 이다.
- **준비** (`starting`). 미리 보기의 파드가 Ready 가 되면 `running` 이고 하루 상한의 자리를 남긴다.
  init 이 파드 안의 승인 기능을 모르는 이미지를 거절했으면 `runtime_image_too_old` 다(미리 보기의
  `last_error`). 미리 보기가 끝났거나 끝날 때가 지났으면 `preview_ended` 다.
- **사례와 반복 하나** (`running`). 걸음을 시작한 수를 세고 `MAX_ATTEMPTS` 를 넘으면
  `case_not_started` 다. 이 실행이 쓴 토큰(원장 합)이 `DAEGAM_EVALUATION_RUN_TOKENS` 의 80% 이상이면
  `token_limit`, 원본의 일일 예산이 다 찼으면 `daily_budget` 이다. 앞선 시도가 남긴 대화가 있으면 그
  턴을 멈추고 지운다. 사례가 기록한 지문이 아니면 `changed` 로 세지 않고 넘어간다. 그 밖이면 새 대화
  하나에서 질문을 차례로 보낸다. 턴마다 쉬기의 게이트를 지나고 시작한 사람의 `run` 위임을 연다. 토큰
  인증 모드에서 Tool 소스가 있는 미리 보기에 위임이 열리지 않거나 파드가 위임을 받지 않았으면 Agent
  의 이름으로 돌지 않고 `acts_as_unavailable` 로 끝난다. header 모드는 턴에 사람이 없어 Agent 의
  자격으로 돈다(`header_mode`). 첫 답의 세션 id 를 걸음에 적고, 스트림을 끝까지 읽고, 승인 대기는
  이렇게 답한다: 결정에 `approve` 가 있으면 승인한다(잡히지 않은 이름이다). 없으면 사례의 가상
  결과로 `respond` 하고, 가상 결과가 없으면 `reject` 한다. 기억한 선택은 어느 턴에도 싣지 않는다.
  끝나면 대화를 지운다. 첫 질문을 런타임이 받지 않으면 `case_not_started` 로 끝나고, 파드가 다시
  떠 세션이 없으면(`session_not_found`, 질문이든 결정이든) 그 걸음을 한 번 더 돈다.
- **결과.** 사례와 반복마다 결과 문서 하나(그 대화의 id, 최종 답, Tool 호출과 그 답한 꼴, 확인
  항목의 판정, 토큰)와 실행 문서의 내용 없는 판정 하나를 남긴다. 토큰은 `done` 으로 끝난 턴의 사용량
  합이고, 승인 대기로 멈춘 시도는 런타임이 사용량을 내지 않아 들지 않는다.
- **채점** (`grading`). 마지막 사례가 돌면 미리 보기를 먼저 끝내 자리를 놓고, 채점 기준이 있는
  사례와 반복을 차례로 채점한다(`grader.py`). 채점 모델의 provider 가 리비전이 쓰는 것이 아니게
  됐으면 `evaluation_grader_provider` 로 끝난다. 실행의 원장 합이 `DAEGAM_EVALUATION_RUN_TOKENS` 에
  닿으면 `token_limit` 로 끝난다. 사례가 그 사이 바뀌었으면 `changed` 로 세지 않고 결과를 지운다.
  답을 읽지 못했거나 결과가 없으면 `grader_error` 다. 사례가 일찍 멈췄으면(`token_limit`,
  `daily_budget`, `preview_ended`, `case_not_started`) 돈 것을 채점한 뒤 그 까닭으로 `error` 가
  된다.
- **끝.** 채점이 끝나면 원장에서 금액을 읽어 결과마다(그 대화의 턴과 그 채점 호출)와 합계에 적고
  `finished` 로 끝난다. 합계는 `DAEGAM_EVALUATION_GATE_CRITERIA` 가 켜졌을 때만 채점 기준을 센다.
  어느 까닭으로 끝나든(`error`, `cancelled`) 미리 보기도 끝내고, 준비 전이면 하루 자리를
  돌려준다(`stopped`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from daegam.auth.principal import Principal, header_principal
from daegam.config import Settings
from daegam.errors import ServiceError
from daegam.services.agent_features import RUNTIME_IMAGE_TOO_OLD
from daegam.services.agent_graph import GraphError
from daegam.services.agent_preview import (
    DEPLOY_SCOPE,
    PREVIEW_LIMIT,
    PREVIEW_START_LIMIT,
    AgentPreviews,
    due_reason,
)
from daegam.services.agent_runtime import AGENT_WAKING, ProxyResponse, read_code, sse_events
from daegam.services.agent_sleep import TURN
from daegam.services.agent_status import READY_PHASE
from daegam.services.evaluation import checks
from daegam.services.evaluation.grader import (
    EVALUATION_GRADER_PROVIDER,
    EvaluationGrader,
    GraderUnavailable,
    provider_allowed,
)
from daegam.services.evaluation.holds import EVALUATION_TOOLS_UNKNOWN
from daegam.services.evaluation.verdicts import CHANGED, totals
from daegam.services.lease_worker import QueuedLeaseWorker
from daegam.services.runs.person import ACTS_AS_UNAVAILABLE, BoundPersonCheck
from daegam.services.turn_grants import (
    GrantPerson,
    OpenedGrant,
    TurnTokensUnavailable,
    tool_targets,
)
from daegam.services.turn_person import (
    SESSION_HEADER,
    TURN_GRANT_HEADER,
    TurnCall,
    TurnDelegation,
    TurnPlan,
)
from daegam.services.usage.costs import priced_sum, priced_sums
from daegam.store.agents import Agent, MongoAgentStore
from daegam.store.base import utcnow
from daegam.store.evaluation import CaseContent, MongoEvaluationStore
from daegam.store.evaluation_runs import (
    CaseVerdict,
    EvaluationResult,
    EvaluationRun,
    MongoEvaluationRunStore,
    RunTotals,
    ToolCallRecord,
)
from daegam.store.leases import Lease
from daegam.store.memberships import MongoMembershipStore
from daegam.store.usage import MongoUsageStore, agent_records

if TYPE_CHECKING:
    from daegam.services.agents import AgentService

logger = logging.getLogger("daegam.evaluation.runner")

# 한 레플리카가 함께 맡는 실행 수와 한 번에 모는 시간
MAX_PARALLEL = 5
SLICE = timedelta(seconds=60)
# 자리가 없을 때 queued 로 기다리는 시간 (9절)
QUEUED_FOR = timedelta(minutes=30)
# 사례가 멈추는 원장의 몫. 나머지는 채점 몫이다 (9절)
CASE_SHARE = 0.8
# 한 걸음을 시작하는 수의 상한. 넘으면 그 걸음이 늘 실패하는 것이라 실행을 끝낸다
MAX_ATTEMPTS = 3
# 턴 하나에서 답하는 승인 대기의 수 상한. 넘으면 그 턴은 오류다
MAX_INTERRUPTS = 20
# 결과에 싣는 Tool 결과 글의 길이 상한
RESULT_CHARS = 4000
# 가상 결과가 없는 잡힌 호출의 거부 글
REJECTED = (
    "Evaluation run: this write tool was not called because the case has no simulated result "
    "for it."
)
# 런타임이 Tool 을 부르지 않고 거절한 결과의 코드. `agent-runtime/runtime/tool_safety.py` 의 거절
# 층(`MESSAGES`)과 `agent-runtime/runtime/web_guard.py` 의 거절이 `guards.refusal_result` 로 낸다.
# `outside_mark_failed` 는 Tool 이 돈 뒤 결과만 막은 것이라 넣지 않는다
RUNTIME_REFUSALS = frozenset(
    {
        "tool_choice_rejected",
        "outside_writes_refused",
        "approval_unavailable",
        "url_not_in_conversation",
        "url_blocked",
        "url_not_allowed",
        "rate_limited",
        "web_turn_limit",
        "web_no_turn",
    }
)

# 실행이 끝난 까닭 (13절). `ACTS_AS_UNAVAILABLE` 은 `services/runs/person.py` 의 것이다
CASE_NOT_STARTED = "case_not_started"
DAILY_BUDGET = "daily_budget"
PREVIEW_ENDED = "preview_ended"
TOKEN_LIMIT = "token_limit"
EVALUATIONS_DISABLED = "evaluations_disabled"
# 미리 보기의 시작과 준비가 그대로 실행의 까닭이 되는 코드
START_REASONS = frozenset(
    {PREVIEW_START_LIMIT, GraphError.code, EVALUATION_TOOLS_UNKNOWN, RUNTIME_IMAGE_TOO_OLD}
)
SESSION_NOT_FOUND = "session_not_found"
# 사례가 일찍 멈춘 까닭 가운데 돈 것을 채점하고 끝나는 것 (9절)
GRADE_AFTER = frozenset({TOKEN_LIMIT, DAILY_BUDGET, PREVIEW_ENDED, CASE_NOT_STARTED})

# 턴이 끝난 꼴
DONE = checks.DONE
ERROR = "error"
INTERRUPT = "interrupt"
GUARDRAIL = "guardrail"


class _Ended(Exception):
    """실행을 그 까닭으로 끝낸다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Wait(Exception):
    """이 걸음을 지금은 할 수 없다. 다음 틱에 다시 한다."""


class _Again(Exception):
    """이 걸음을 처음부터 한 번 더 돈다(파드가 다시 떠 세션이 없다)."""


class _Lost(Exception):
    """조건부 쓰기가 졌다. 다른 레플리카가 맡았거나 실행이 끝났다."""


@dataclass
class _Seen:
    """사례와 반복 하나에서 본 것. tokens 는 `done` 으로 끝난 턴의 사용량 합이다."""

    turns: list[str] = field(default_factory=list)
    calls: list[ToolCallRecord] = field(default_factory=list)
    answer: str = ""
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    # 가드레일이 인자를 막은 Tool 이름. 그 다음 오류 결과가 런타임의 거절이다
    blocked: list[str] = field(default_factory=list)

    def observed(self) -> checks.Observed:
        return checks.Observed(turns=self.turns, calls=self.calls, answer=self.answer)


@dataclass
class _Step:
    """사례와 반복 하나의 걸음. session_id 는 이 걸음이 연 대화다."""

    run: EvaluationRun
    owner: str
    preview: Agent
    content: CaseContent
    index: int
    seen: _Seen = field(default_factory=_Seen)
    session_id: str | None = None


def answers(
    payload: dict[str, Any], tool_results: dict[str, str]
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any], str]]]:
    """승인 대기의 결정과, 요청마다의 (이름, 인자, 답한 꼴)이다 (10절). 결정에 `approve` 가 있으면
    잡히지 않은 이름이라 승인한다. 없으면 가상 결과로 `respond` 하고, 없으면 `reject` 한다."""
    configs = payload.get("review_configs") or []
    decisions: list[dict[str, Any]] = []
    answered: list[tuple[str, dict[str, Any], str]] = []
    for i, action in enumerate(payload.get("action_requests") or []):
        name = str(action.get("name") or "")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        config = configs[i] if i < len(configs) and isinstance(configs[i], dict) else {}
        allowed = config.get("allowed_decisions") or []
        if "approve" in allowed:
            decisions.append({"type": "approve"})
            answered.append((name, args, "ran"))
        elif name in tool_results and "respond" in allowed:
            decisions.append({"type": "respond", "message": tool_results[name]})
            answered.append((name, args, "simulated"))
        else:
            decisions.append({"type": "reject", "message": REJECTED})
            answered.append((name, args, "rejected"))
    return decisions, answered


def _refusal(content: Any) -> bool:
    """런타임이 Tool 을 부르지 않고 거절한 결과인가(`RUNTIME_REFUSALS`)."""
    try:
        body = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and body.get("code") in RUNTIME_REFUSALS


class EvaluationRunner:
    """실행 하나의 걸음을 모는 쪽이다(모듈 머리). 워커가 리스를 쥔 채 `drive` 를 부른다."""

    def __init__(
        self,
        runs: MongoEvaluationRunStore,
        cases: MongoEvaluationStore,
        agents: MongoAgentStore,
        previews: AgentPreviews,
        service: AgentService,
        delegation: TurnDelegation,
        person: BoundPersonCheck,
        memberships: MongoMembershipStore,
        usage: MongoUsageStore,
        settings: Settings,
        grader: EvaluationGrader,
    ):
        self.grader = grader
        self.runs = runs
        self.cases = cases
        self.agents = agents.with_previews()
        self.previews = previews
        self.service = service
        self.delegation = delegation
        self.person = person
        self.memberships = memberships
        self.usage = usage
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.evaluations_enabled

    # -- 걸음 -----------------------------------------------------------------------------

    async def drive(self, run_id: str, owner: str, until: datetime) -> None:
        """리스를 쥔 실행을 until 까지 몬다. 끝났거나, 지금 할 수 없거나, 조건부 쓰기가 졌으면
        멈춘다. 다시 읽은 실행이 끝났으면 그 미리 보기를 끝낸다."""
        run = await self.runs.claim(run_id, owner)
        while run is not None and utcnow() < until:
            try:
                moved = await self._step(run, owner)
            except _Ended as e:
                if run.state != "running" or e.reason not in GRADE_AFTER:
                    await self.fail(run, owner, e.reason)
                    return
                # 일찍 멈춘 사례는 돈 것을 채점하고 그 까닭으로 끝난다 (9절)
                moved = await self._to_grading(run, owner, stopped=e.reason)
            except (_Wait, _Lost):
                moved = False
            if not moved:
                break
            run = await self.runs.by_id(run_id)
            if run is None or run.driver != owner:
                break
        await self.stopped(run_id)

    async def _step(self, run: EvaluationRun, owner: str) -> bool:
        if run.state == "queued":
            return await self._start(run, owner)
        if run.state == "starting":
            return await self._ready(run, owner)
        if run.state == "running":
            if run.step.index >= run.steps:
                return await self._to_grading(run, owner)
            return await self._case(run, owner)
        if run.state == "grading":
            return await self._grade(run, owner)
        return False

    async def _starter(self, run: EvaluationRun) -> Principal:
        """시작한 사람의 지금 주체다. 소속은 지금의 것, 그룹은 시작할 때의 것이다. header 모드는
        header 의 주체다."""
        if run.header_mode:
            return header_principal()
        memberships = await self.memberships.list_for_sub(run.by.sub)
        return Principal(sub=run.by.sub, groups=list(run.by.groups), memberships=memberships)

    async def _start(self, run: EvaluationRun, owner: str) -> bool:
        source = await self.agents.get(run.agent_id, tenant=run.tenant)
        if source is None or source.preview is not None:
            raise _Ended(PREVIEW_ENDED)
        starter = await self._starter(run)
        if DEPLOY_SCOPE not in starter.scopes_in(run.tenant):
            raise _Ended(ACTS_AS_UNAVAILABLE)
        try:
            preview = await self.previews.start_evaluation(source, starter, run.revision, run.id)
        except ServiceError as e:
            if e.code == PREVIEW_LIMIT:
                if utcnow() - run.created_at >= QUEUED_FOR:
                    raise _Ended(PREVIEW_LIMIT) from e
                raise _Wait from e
            raise _Ended(e.code if e.code in START_REASONS else CASE_NOT_STARTED) from e
        return await self.runs.update(
            run.id, owner, {"state": "starting", "preview_id": preview.id}
        )

    async def _ready(self, run: EvaluationRun, owner: str) -> bool:
        preview = await self._preview(run)
        error = preview.last_error or ""
        for reason in START_REASONS:
            if error.startswith(reason):
                raise _Ended(reason)
        status = await self.service.status(preview, with_exposure=False)
        if status is None or status.get("phase") != READY_PHASE:
            raise _Wait
        # 준비된 실행은 하루 상한의 자리를 남긴다(칸 id 를 지운다)
        fields = {"state": "running", "running_at": utcnow(), "day": None}
        return await self.runs.update(run.id, owner, fields)

    async def _preview(self, run: EvaluationRun) -> Agent:
        """실행의 미리 보기. 없거나, 지우는 중이거나, 끝날 때가 지났으면 `preview_ended` 다."""
        preview = await self.agents.get(run.preview_id) if run.preview_id else None
        if preview is None or preview.state in ("deleting", "draft"):
            raise _Ended(PREVIEW_ENDED)
        if due_reason(preview, utcnow(), enabled=self.previews.enabled) is not None:
            raise _Ended(PREVIEW_ENDED)
        return preview

    async def _write(self, step: _Step, fields: dict[str, Any], **kw: Any) -> None:
        """걸음의 조건부 쓰기. 지면 `_Lost` 다."""
        if not await self.runs.update(step.run.id, step.owner, fields, index=step.index, **kw):
            raise _Lost

    # -- 사례와 반복 하나 ------------------------------------------------------------------

    async def _case(self, run: EvaluationRun, owner: str) -> bool:
        index = run.step.index
        recorded, repeat = run.step_case(index)
        if run.step.attempts >= MAX_ATTEMPTS:
            raise _Ended(CASE_NOT_STARTED)
        preview = await self._preview(run)
        await self._limits(run, preview)
        case = await self.cases.get_case(run.tenant, run.set_id, recorded.id)
        step = _Step(run, owner, preview, case.content if case else CaseContent(messages=[]), index)
        await self._write(step, {"step.attempts": run.step.attempts + 1})
        if run.step.session_id is not None:
            await self._forget_session(step, run.step.session_id)
            await self._write(step, {"step.session_id": None})
        verdict = CaseVerdict(
            case_id=recorded.id, case_digest=recorded.digest, repeat=repeat, state=CHANGED
        )
        if case is None or case.digest != recorded.digest:
            await self._advance(step, verdict)
            return True
        try:
            await self._run_case(step)
        except _Again:
            if run.step.retried:
                raise _Ended(CASE_NOT_STARTED) from None
            await self._write(step, {"step.session_id": None, "step.retried": True})
            return True
        verdicts = checks.check_all(case.content, step.seen.observed())
        fresh = await self.cases.get_case(run.tenant, run.set_id, recorded.id)
        if fresh is None or fresh.digest != recorded.digest:
            # 도는 동안 바뀌었거나 동의가 끝났다. 결과를 남기지 않는다
            await self._advance(step, verdict)
            return True
        verdict = verdict.model_copy(
            update={
                "state": ERROR if any(t != DONE for t in step.seen.turns) else DONE,
                "counted": checks.counted(case.content),
                "checks_passed": all(v.passed for v in verdicts),
                "criteria": len(case.content.criteria),
            }
        )
        await self._advance(step, verdict)
        await self._write_result(step, verdict, verdicts)
        return True

    async def _advance(self, step: _Step, verdict: CaseVerdict) -> None:
        fields = {
            "step.index": step.index + 1,
            "step.session_id": None,
            "step.retried": False,
            "step.attempts": 0,
        }
        await self._write(step, fields, push=verdict)

    async def _write_result(
        self, step: _Step, verdict: CaseVerdict, verdicts: list[checks.CheckVerdict]
    ) -> None:
        """결과 하나. 걸음을 옮긴 뒤에 쓴다. id 가 실행과 걸음이라 같은 걸음의 결과는 하나다."""
        run, seen = step.run, step.seen
        now = utcnow()
        result = EvaluationResult(
            id=result_id(run, step.index),
            tenant=run.tenant,
            agent_id=run.agent_id,
            run_id=run.id,
            set_id=run.set_id,
            case_id=verdict.case_id,
            case_digest=verdict.case_digest,
            repeat=verdict.repeat,
            started_by=run.by.sub,
            session_id=step.session_id,
            answer=seen.answer,
            tool_calls=seen.calls,
            checks=verdicts,
            turns=seen.turns,
            tokens=seen.tokens,
            created_at=now,
            expires_at=now + timedelta(days=self.settings.evaluation_result_days),
        )
        await self.runs.put_result(result)

    async def _limits(self, run: EvaluationRun, preview: Agent) -> None:
        """사례 앞의 한도 (9절). 이 실행이 쓴 토큰과 원본의 일일 예산이다."""
        used = await self.run_tokens(run)
        if used >= self.settings.evaluation_run_tokens * CASE_SHARE:
            raise _Ended(TOKEN_LIMIT)
        try:
            await self.previews.check_budget(preview)
        except ServiceError as e:
            raise _Ended(DAILY_BUDGET) from e

    async def run_tokens(self, run: EvaluationRun) -> int:
        """이 실행이 쓴 토큰. 그 미리 보기의 원장 행(사례의 턴과 채점)의 합이다 (11절)."""
        if run.preview_id is None:
            return 0
        return await self.usage.tokens_used(_run_rows(run))

    # -- 대화 -----------------------------------------------------------------------------

    async def _run_case(self, step: _Step) -> None:
        try:
            for n, message in enumerate(step.content.messages):
                outcome = await self._turn(step, message, first=n == 0)
                step.seen.turns.append(outcome)
                if outcome != DONE:
                    break
        finally:
            if step.session_id is not None:
                await self._forget_session(step, step.session_id)

    async def _turn(self, step: _Step, message: str, *, first: bool) -> str:
        """질문 하나의 턴을 끝까지 몬다. 끝난 꼴이다."""
        proxy, run = self.service.proxy, step.run
        chat = partial(
            proxy.chat,
            step.preview,
            user_sub=run.by.sub,
            message=message,
            session_id=step.session_id,
            ephemeral=True,
        )
        try:
            proxied = await self._send(step, chat)
        except ServiceError as e:
            if first:
                raise _Ended(CASE_NOT_STARTED) from e
            return f"{ERROR}:{e.code}"
        if proxied.status >= 400:
            code = await self._code(proxied)
            if code == SESSION_NOT_FOUND:
                raise _Again
            if first:
                raise _Ended(CASE_NOT_STARTED)
            return f"{ERROR}:{code}"
        if step.session_id is None:
            step.session_id = proxied.headers.get(SESSION_HEADER)
            if step.session_id:
                await self._write(step, {"step.session_id": step.session_id})
        for _round in range(MAX_INTERRUPTS + 1):
            outcome, payload = await self._read(proxied, step.seen)
            if outcome != INTERRUPT or step.session_id is None:
                return outcome
            decisions, answered = answers(payload, step.content.tool_results)
            _note(step.seen.calls, answered)
            decide = partial(
                proxy.decisions,
                step.preview,
                step.session_id,
                user_sub=run.by.sub,
                decisions=decisions,
                approver=False,
                ephemeral=True,
            )
            try:
                proxied = await self._send(step, decide)
            except ServiceError as e:
                return f"{ERROR}:{e.code}"
            if proxied.status >= 400:
                code = await self._code(proxied)
                if code == SESSION_NOT_FOUND:
                    raise _Again
                return f"{ERROR}:{code}"
        return f"{ERROR}:too_many_approvals"

    async def _send(self, step: _Step, call: Any) -> ProxyResponse:
        """쉬기의 게이트를 지나고 시작한 사람의 `run` 위임을 열어 부른다 (9절). call 은 프록시의
        채팅이나 결정이고 `turn_grant` 만 남은 것이다. 게이트와 프록시의 `agent_waking` 은 다음
        틱에 다시 하고, 그 밖의 거절은 그대로 올린다. 위임을 실었는데 파드가 받지 않았으면
        `acts_as_unavailable` 이다."""
        try:
            await self.service.sleep.admit(step.preview, TURN)
            person, grant = await self._grant(step)
            plan = TurnPlan(step.preview, step.session_id, utcnow(), grant, confirms=person)
            proxied = await self.delegation.send(plan, _with_grant(call))
        except ServiceError as e:
            if e.code == AGENT_WAKING:
                raise _Wait from e
            raise
        if grant is not None and proxied.status < 400:
            if proxied.headers.get(TURN_GRANT_HEADER) != grant.id:
                await proxied.body.aclose()
                raise _Ended(ACTS_AS_UNAVAILABLE)
        return proxied

    async def _grant(self, step: _Step) -> tuple[GrantPerson | None, OpenedGrant | None]:
        """그 턴의 사람과 위임. header 모드는 둘 다 없다. 토큰 인증 모드에서 Tool 소스가 있는
        미리 보기에 위임이 열리지 않으면(사람이 확인을 지나지 못했거나, 토큰을 낼 수 없다) Agent 의
        이름으로 돌지 않고 `acts_as_unavailable` 로 끝낸다 (9절). Tool 소스가 없으면 누구의
        이름으로도 읽지 않으므로 위임 없이 돈다."""
        run = step.run
        if run.header_mode:
            return None, None
        if not await self.person.runs_as_person(run.by.sub):
            raise _Ended(ACTS_AS_UNAVAILABLE)
        person = GrantPerson(sub=run.by.sub, username=run.by.username, tv=run.by.tv)
        try:
            outcome = await self.delegation.grants.open(
                agent=step.preview, person=person, trigger="run", session_id=step.session_id
            )
        except TurnTokensUnavailable as e:
            raise _Wait from e
        if outcome.grant is None and (outcome.refused is not None or tool_targets(step.preview)):
            raise _Ended(ACTS_AS_UNAVAILABLE)
        return person, outcome.grant

    async def _read(self, proxied: ProxyResponse, seen: _Seen) -> tuple[str, dict[str, Any]]:
        """턴의 스트림을 끝까지 읽고 (끝난 꼴, 승인 대기의 데이터)를 낸다."""
        outcome, payload = f"{ERROR}:stream_ended", {}
        async for event, data in sse_events(proxied):
            if event == "tool_call":
                _announced(seen.calls, data)
            elif event == "tool_result":
                _result(seen, data)
            elif event == GUARDRAIL:
                if data.get("action") == "block" and data.get("stage") == "args":
                    seen.blocked.append(str(data.get("tool") or ""))
            elif event == INTERRUPT:
                outcome, payload = INTERRUPT, data
            elif event == DONE:
                seen.answer = str(data.get("text") or "")
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                seen.tokens["input"] += int(usage.get("input_tokens") or 0)
                seen.tokens["output"] += int(usage.get("output_tokens") or 0)
                outcome = DONE
            elif event == ERROR:
                outcome = f"{ERROR}:{data.get('code') or 'unknown'}"
        return outcome, payload

    @staticmethod
    async def _code(proxied: ProxyResponse) -> str:
        code, _body = await read_code(proxied)
        return code or f"http_{proxied.status}"

    async def _forget_session(self, step: _Step, session_id: str) -> None:
        """대화를 멈추고 지운다. 그 샌드박스도 닫힌다. 실패는 로그만 남긴다. 미리 보기의 끝이 남은
        것을 거둔다."""
        proxy, sub = self.service.proxy, step.run.by.sub
        try:
            await proxy.cancel(step.preview, session_id, user_sub=sub, approver=False)
            await proxy.delete_session(step.preview, session_id, user_sub=sub)
        except Exception as e:  # noqa: BLE001 - 미리 보기의 끝이 거둔다
            run_id = step.run.id
            logger.warning("evaluation run %s: session %s not deleted: %s", run_id, session_id, e)

    # -- 채점 (11절) ----------------------------------------------------------------------

    async def _to_grading(
        self, run: EvaluationRun, owner: str, *, stopped: str | None = None
    ) -> bool:
        """사례가 다 돌았거나 일찍 멈췄다. 미리 보기를 먼저 끝내 자리를 놓고 채점으로 옮긴다
        (9절)."""
        await self._close_preview(run)
        fields: dict[str, Any] = {"state": "grading", "step.graded": 0}
        if stopped is not None:
            fields["stopped"] = stopped
        return await self.runs.update(run.id, owner, fields)

    async def _grade(self, run: EvaluationRun, owner: str) -> bool:
        """다음 판정 하나를 채점한다. 다 채점했으면 끝낸다."""
        i = run.step.graded
        if i >= len(run.verdicts):
            await self._finish(run, owner)
            return False
        verdict = run.verdicts[i]
        if verdict.state != DONE or not verdict.criteria:
            return await self._graded(run, owner, i, {})
        if await self.run_tokens(run) >= self.settings.evaluation_run_tokens:
            raise _Ended(TOKEN_LIMIT)
        source = await self.agents.get(run.agent_id, tenant=run.tenant)
        rev = source.get_revision(run.revision) if source is not None else None
        if source is None or rev is None:
            raise _Ended(PREVIEW_ENDED)
        grader = run.grader
        cfg = await self.grader.provider(grader) if grader is not None else None
        if grader is None or cfg is None or not provider_allowed(grader, rev):
            raise _Ended(EVALUATION_GRADER_PROVIDER)
        result = await self.runs.get_result(result_id(run, i))
        case = await self.cases.get_case(run.tenant, run.set_id, verdict.case_id)
        if case is None or case.digest != verdict.case_digest:
            return await self._unseen(run, owner, i)
        if result is None:
            return await self._graded(run, owner, i, _GRADER_ERROR)
        try:
            grade = await self.grader.grade(run, source.name, cfg, case.content, result)
        except GraderUnavailable as e:
            logger.warning("evaluation run %s: grader unavailable: %s", run.id, e.code)
            raise _Ended(EVALUATION_GRADER_PROVIDER) from e
        fresh = await self.cases.get_case(run.tenant, run.set_id, verdict.case_id)
        if fresh is None or fresh.digest != verdict.case_digest:
            # 채점하는 동안 바뀌었거나 동의가 끝났다. 결과를 남기지 않는다
            return await self._unseen(run, owner, i)
        passed = None if grade.verdicts is None else all(v.passed for v in grade.verdicts)
        fields = _GRADER_ERROR if passed is None else {"criteria_passed": passed}
        if not await self._graded(run, owner, i, fields):
            return False
        written: dict[str, Any] = {f"tokens.{k}": v for k, v in grade.tokens.items()}
        written["criteria"] = [v.model_dump() for v in grade.verdicts or []]
        written["grader_call"] = grade.call
        written["grader_error"] = grade.error
        await self.runs.update_result(result.id, written)
        return True

    async def _graded(
        self, run: EvaluationRun, owner: str, i: int, verdict: dict[str, Any]
    ) -> bool:
        """판정 i 의 채점을 적고 다음 채점으로 옮긴다. 다음 채점이 i 일 때만이다."""
        fields = {f"verdicts.{i}.{k}": v for k, v in verdict.items()}
        return await self.runs.update(run.id, owner, {**fields, "step.graded": i + 1}, graded=i)

    async def _unseen(self, run: EvaluationRun, owner: str, i: int) -> bool:
        """채점 앞이나 사이에 사례가 바뀌거나 지워졌다. `changed` 로 세지 않고 결과를 지운다."""
        moved = await self._graded(run, owner, i, {"state": CHANGED})
        if moved:
            await self.runs.delete_result(result_id(run, i))
        return moved

    # -- 끝 -------------------------------------------------------------------------------

    async def _finish(self, run: EvaluationRun, owner: str) -> None:
        """채점이 끝났다. 일찍 멈춘 실행은 그 까닭으로 `error` 이고, 아니면 금액과 합계를 적고
        `finished` 다."""
        if run.stopped is not None:
            await self.fail(run, owner, run.stopped)
            return
        await self._close_preview(run)
        fields = {"totals": (await self._totals(run)).model_dump()}
        await self.runs.end(run.id, owner, "finished", utcnow(), **fields)

    async def fail(self, run: EvaluationRun, owner: str | None, reason: str) -> None:
        """그 까닭으로 실행을 끝낸다. 끝낸 쪽이 이것이면 로그를 남긴다. 미리 보기와 하루 자리는
        끝난 실행만 거둔다(`stopped`)."""
        fields = {"reason": reason, "totals": (await self._totals(run)).model_dump()}
        if await self.runs.end(run.id, owner, "error", utcnow(), **fields):
            logger.info("evaluation run %s ended: %s", run.id, reason)
        await self.stopped(run.id)

    async def _totals(self, run: EvaluationRun) -> RunTotals:
        """원장의 토큰과 금액을 읽어 결과마다 금액을 적고 합계를 낸다 (9절)."""
        tokens = await self.run_tokens(run)
        cost = await self._write_costs(run)
        criteria = self.settings.evaluation_gate_criteria
        return totals(run, tokens, criteria=criteria, cost_usd=cost)

    async def _write_costs(self, run: EvaluationRun) -> float | None:
        """결과마다 그 대화의 턴(`ref`)과 그 채점 호출의 원장 금액을 적고 실행의 금액 합을 낸다.
        값을 매긴 행만 더하고, 모두 값이 없으면 None 이다(`services/usage/costs.py`)."""
        if run.preview_id is None:
            return None
        rows = _run_rows(run)
        results = await self.runs.results_of(run.id)
        sessions = [r.session_id for r in results if r.session_id]
        calls = [r.grader_call for r in results if r.grader_call]
        by_ref = await priced_sums(self.usage, rows, "ref", sessions)
        by_call = await priced_sums(self.usage, rows, "_id", calls)
        for r in results:
            parts = [
                (by_ref.get(r.session_id) or {}).get("cost_usd") if r.session_id else None,
                (by_call.get(r.grader_call) or {}).get("cost_usd") if r.grader_call else None,
            ]
            priced = [p for p in parts if p is not None]
            await self.runs.update_result(r.id, {"cost_usd": sum(priced) if priced else None})
        groups = await self.usage.record_sums(rows)
        return priced_sum(groups[0]) if groups else None

    async def stopped(self, run_id: str) -> None:
        """끝난 실행의 뒤처리다. 도는 실행이면 아무것도 하지 않는다. 준비 전에 끝났으면 하루 자리를
        돌려주고, 미리 보기가 남았으면 끝낸다."""
        run = await self.runs.by_id(run_id)
        if run is None or run.active is not None:
            return
        await self.runs.release_run_day(run.id)
        await self._close_preview(run)

    async def _close_preview(self, run: EvaluationRun) -> None:
        preview = await self.agents.preview_of_run(run.tenant, run.agent_id, run.id)
        if preview is None or preview.state == "deleting":
            return
        try:
            await self.previews.end(preview, "ended")
        except Exception as e:  # noqa: BLE001 - 끝내기 워커가 때가 되면 끝낸다
            logger.warning("evaluation run %s: preview %s not ended: %s", run.id, preview.id, e)

    async def stop_all(self, reason: str) -> int:
        """도는 실행을 모두 끝낸다(스위치를 껐다). 끝낸 수다."""
        n = 0
        for run in await self.runs.active_runs():
            if await self.runs.end(run.id, None, "cancelled", utcnow(), reason=reason):
                n += 1
                await self.stopped(run.id)
        return n


_GRADER_ERROR: dict[str, Any] = {"grader_error": True, "criteria_passed": False}


def result_id(run: EvaluationRun, index: int) -> str:
    """걸음 index 의 결과 id. 걸음마다 판정이 하나라 판정의 자리와 같다."""
    return f"{run.id}:{index}"


def _run_rows(run: EvaluationRun) -> dict[str, Any]:
    """그 실행의 원장 행: 그 미리 보기의 턴과 채점 호출이다 (11절)."""
    return {**agent_records(run.tenant, run.agent_id), "source.preview": run.preview_id}


def _with_grant(call: Any) -> TurnCall:
    """`turn_grant` 만 남은 프록시 호출을 위임을 받는 턴 호출로 바꾼다(`TurnDelegation.send`)."""

    async def turn(turn_grant: dict[str, Any] | None) -> ProxyResponse:
        return await call(turn_grant=turn_grant)

    return turn


def _announced(calls: list[ToolCallRecord], data: dict[str, Any]) -> None:
    """tool_call 하나. 재개한 턴은 대기하던 호출을 같은 id 로 다시 알리므로, 결과가 없는 같은 id 의
    기록이 있으면 이름과 인자만 고친다(답한 꼴은 그대로 둔다)."""
    call_id = data.get("id")
    name = str(data.get("name") or "")
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    found = next(
        (c for c in calls if call_id is not None and c.id == call_id and c.status is None), None
    )
    if found is not None:
        found.name, found.args = name, args
        return
    calls.append(ToolCallRecord(id=call_id, name=name, args=args))


def _note(calls: list[ToolCallRecord], answered: list[tuple[str, dict[str, Any], str]]) -> None:
    """승인 대기의 요청마다 아직 결과가 없는 같은 이름의 호출에 답한 꼴을 적는다."""
    for name, args, answer in answered:
        for call in reversed(calls):
            if call.name == name and not call.answer and call.status is None:
                if not call.args:
                    call.args = args
                call.answer = answer
                break


def _result(seen: _Seen, data: dict[str, Any]) -> None:
    """tool_result 하나. 가상 결과와 거부는 그 꼴을 지키고, 그 밖의 오류는 런타임의 거절(거절 코드나
    인자를 막은 가드레일)이면 `refused` 다."""
    call_id = data.get("id")
    call = next((c for c in reversed(seen.calls) if c.id == call_id and c.status is None), None)
    if call is None:
        return
    status = str(data.get("status") or "")
    content = data.get("content")
    call.status = status
    call.result = str(content)[:RESULT_CHARS] if content is not None else None
    if call.answer in ("simulated", "rejected"):
        return
    blocked = status == "error" and call.name in seen.blocked
    if blocked:
        seen.blocked.remove(call.name)
    refused = status == "error" and (blocked or _refusal(content))
    call.answer = "refused" if refused else "ran"


class EvaluationRunWorker(QueuedLeaseWorker[EvaluationRun]):
    """실행 워커 (9절). 틱마다 도는 실행을 줄에 세우고, 실행마다 리스 `evaluation-run:<id>` 를 걸어
    한 레플리카만 맡게 한다. 리스를 쥔 실행은 함께 몬다(`MAX_PARALLEL`). 스위치가 꺼졌으면 도는
    실행을 모두 끝낸다."""

    task_prefix = "evaluation-run"
    tick_label = "evaluation run tick"
    item_label = "evaluation run"
    log = logger

    def __init__(
        self,
        runner: EvaluationRunner,
        lease: Lease,
        *,
        interval: float,
        parallel: int = MAX_PARALLEL,
        slice_: timedelta = SLICE,
    ):
        super().__init__(lease, owner=lease.holder, interval=interval, parallel=parallel)
        self.runner = runner
        self.slice = slice_

    async def queue(self, now: datetime) -> list[EvaluationRun]:
        if not self.runner.enabled:
            await self.runner.stop_all(EVALUATIONS_DISABLED)
            return []
        return await self.runner.runs.active_runs()

    async def handle(self, item: EvaluationRun, now: datetime) -> None:
        await self.runner.drive(item.id, self.owner, now + self.slice)


__all__ = [
    "CASE_NOT_STARTED",
    "DAILY_BUDGET",
    "EVALUATIONS_DISABLED",
    "GRADE_AFTER",
    "PREVIEW_ENDED",
    "TOKEN_LIMIT",
    "EvaluationRunWorker",
    "EvaluationRunner",
    "answers",
    "result_id",
]
