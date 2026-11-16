"""Agent 평가 세트, 사례와 실행의 라우트 (P45 7절·9절·13절, D109).

읽기는 남의 대화를 읽는 범위와 같은 `agent:approve` 이고 쓰기는 `agent:write` 를 더한다. 사례 목록은
대화에서 복사한 질문과 답을 담을 수 있어 인용 원문 내려받기처럼 읽기도 감사에 남는다
(`audit/middleware.py` `AUDITED_READ_PATHS`). 실행의 시작과 취소는 `agent:deploy` 를 더하고, 실행
하나의 읽기도 결과의 내용을 담을 수 있어 감사에 남는다. 미리 보기의 id 는 Agent 가 아니라 404 다.
배포 게이트(12절)의 설정은 `agent:deploy` 와 `agent:approve` 다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from starlette.responses import Response

from daegam.api._agent_proxy import get_agent_or_404, owned_session
from daegam.api.agents import APPROVE, DEPLOY, WRITE, Agents, Service
from daegam.api.deps import Tenant, User
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.agent_evaluation import (
    EvaluationCaseBody,
    EvaluationCaseDraftOut,
    EvaluationCaseOut,
    EvaluationCasesOut,
    EvaluationCaseUpdateBody,
    EvaluationGateAnswer,
    EvaluationGateBody,
    EvaluationRunBody,
    EvaluationRunDetailOut,
    EvaluationRunOut,
    EvaluationRunsOut,
    EvaluationSetBody,
    EvaluationSetOut,
    FromTurnBody,
)
from daegam.services.evaluation.copies import EvaluationCopies
from daegam.services.evaluation.gate import DeployGate
from daegam.services.evaluation.inputs import DEFAULT_MAX_AGE_DAYS
from daegam.services.evaluation.runs import EvaluationRuns
from daegam.services.evaluation.sets import CaseInput, EvaluationSets, SetInput
from daegam.store.evaluation import EvaluationSet
from daegam.store.evaluation_runs import EvaluationRun

router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])

SETS = "/{agent_id}/evaluation-sets"
SET = f"{SETS}/{{set_id}}"
CASES = f"{SET}/cases"
CASE = f"{CASES}/{{case_id}}"
RUNS = f"{SET}/runs"
RUN = f"{RUNS}/{{run_id}}"


def get_evaluation_sets(request: Request) -> EvaluationSets:
    return request.app.state.evaluation_sets


def get_evaluation_copies(request: Request) -> EvaluationCopies:
    return request.app.state.evaluation_copies


def get_evaluation_runs(request: Request) -> EvaluationRuns:
    return request.app.state.evaluation_runs


def get_deploy_gate(request: Request) -> DeployGate:
    return request.app.state.deploy_gate


Sets = Annotated[EvaluationSets, Depends(get_evaluation_sets)]
Runs = Annotated[EvaluationRuns, Depends(get_evaluation_runs)]
Copies = Annotated[EvaluationCopies, Depends(get_evaluation_copies)]
Gate = Annotated[DeployGate, Depends(get_deploy_gate)]


def _set_out(item: EvaluationSet, digest: str) -> dict[str, Any]:
    return {**item.model_dump(), "digest": digest}


def _set_input(body: EvaluationSetBody) -> SetInput:
    grader = body.grader.to_stored() if body.grader is not None else None
    return SetInput(name=body.name, grader=grader, repeats=body.repeats)


@router.get(SETS, response_model=list[EvaluationSetOut], dependencies=[APPROVE])
async def list_evaluation_sets(
    tenant: Tenant, store: Agents, sets: Sets, agent_id: str
) -> list[dict[str, Any]]:
    """그 Agent 의 평가 세트와 지문이다. 만든 차례다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    return [_set_out(item, digest) for item, digest in await sets.sets(agent)]


@router.post(
    SETS,
    status_code=status.HTTP_201_CREATED,
    response_model=EvaluationSetOut,
    dependencies=[APPROVE, WRITE],
)
async def create_evaluation_set(
    tenant: Tenant, user: User, store: Agents, sets: Sets, agent_id: str, body: EvaluationSetBody
) -> dict[str, Any]:
    agent = await get_agent_or_404(store, tenant, agent_id)
    item = await sets.create_set(agent, _set_input(body), user)
    return _set_out(*await sets.with_digest(item))


@router.get(SET, response_model=EvaluationSetOut, dependencies=[APPROVE])
async def get_evaluation_set(
    tenant: Tenant, store: Agents, sets: Sets, agent_id: str, set_id: str
) -> dict[str, Any]:
    agent = await get_agent_or_404(store, tenant, agent_id)
    return _set_out(*await sets.with_digest(await sets.get_set(agent, set_id)))


@router.put(SET, response_model=EvaluationSetOut, dependencies=[APPROVE, WRITE])
async def put_evaluation_set(
    tenant: Tenant,
    user: User,
    store: Agents,
    sets: Sets,
    agent_id: str,
    set_id: str,
    body: EvaluationSetBody,
) -> dict[str, Any]:
    """이름, 채점 모델, 반복 수를 바꾼다. 사례 수 × 반복 수가 100 을 넘으면 422
    `evaluation_set_full`, 채점 기준이 있는 사례가 든 세트에서 채점 모델을 빼면 422
    `invalid_value` 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    item = await sets.update_set(agent, set_id, _set_input(body), user)
    return _set_out(*await sets.with_digest(item))


@router.delete(SET, status_code=204, response_class=Response, dependencies=[APPROVE, WRITE])
async def delete_evaluation_set(
    tenant: Tenant, store: Agents, sets: Sets, agent_id: str, set_id: str
) -> Response:
    """세트와 그 사례를 지운다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    await sets.delete_set(agent, set_id)
    return Response(status_code=204)


@router.get(CASES, response_model=EvaluationCasesOut, dependencies=[APPROVE])
async def list_evaluation_cases(
    tenant: Tenant, store: Agents, sets: Sets, agent_id: str, set_id: str
) -> dict[str, Any]:
    """세트의 사례와 지금 세트의 지문이다. 읽기도 감사에 남는다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    _item, cases, digest = await sets.cases(agent, set_id)
    return {"items": [c.model_dump() for c in cases], "set_digest": digest}


@router.post(
    CASES,
    status_code=status.HTTP_201_CREATED,
    response_model=EvaluationCaseOut,
    dependencies=[APPROVE, WRITE],
)
async def create_evaluation_case(
    tenant: Tenant,
    user: User,
    store: Agents,
    sets: Sets,
    agent_id: str,
    set_id: str,
    body: EvaluationCaseBody,
) -> dict[str, Any]:
    """사례 하나를 넣는다. 세트가 차면 422 `evaluation_set_full`, 개인정보로 보이는 것이 있으면
    422 `evaluation_case_pii`(손으로 쓴 사례는 `acknowledgeable: true` 이고 `pii_acknowledged` 로
    저장한다), 복사본의 평가가 동의하지 않으면 409 `evaluation_consent_missing` 이다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    value = CaseInput(
        content=body.content.to_stored(),
        origin=body.origin.to_stored() if body.origin is not None else None,
        pii_acknowledged=body.pii_acknowledged,
    )
    case = await sets.create_case(agent, set_id, value, user)
    return case.model_dump()


@router.post(
    f"{CASES}/from-turn",
    response_model=EvaluationCaseDraftOut,
    dependencies=[APPROVE, WRITE],
)
async def copy_rated_turn(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Service,
    sets: Sets,
    copies: Copies,
    agent_id: str,
    set_id: str,
    body: FromTurnBody,
) -> dict[str, Any]:
    """평가를 받은 턴의 질문과 답을 저장하기 전의 사례로 돌려준다 (8절). 앞선 질문은 복사하지
    않는다. 대화한 사람이 동의하지 않았으면 409 `evaluation_consent_missing`, 다시 생성으로 걷힌
    턴이나 전사의 보존 기간이 지난 턴은 404 `turn_not_found` 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    await sets.get_set(agent, set_id)
    session = await owned_session(service, agent, body.session_id, user, approver=True)
    return await copies.draft(agent, session, session_id=body.session_id, turn_id=body.turn_id)


@router.put(CASE, response_model=EvaluationCaseOut, dependencies=[APPROVE, WRITE])
async def put_evaluation_case(
    tenant: Tenant,
    user: User,
    store: Agents,
    sets: Sets,
    agent_id: str,
    set_id: str,
    case_id: str,
    body: EvaluationCaseUpdateBody,
) -> dict[str, Any]:
    """사례의 내용을 바꾼다. 복사한 사례는 출처를 그대로 두고 동의를 다시 본다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    value = CaseInput(content=body.content.to_stored(), pii_acknowledged=body.pii_acknowledged)
    case = await sets.update_case(agent, set_id, case_id, value, user)
    return case.model_dump()


@router.delete(CASE, status_code=204, response_class=Response, dependencies=[APPROVE, WRITE])
async def delete_evaluation_case(
    tenant: Tenant, store: Agents, sets: Sets, agent_id: str, set_id: str, case_id: str
) -> Response:
    agent = await get_agent_or_404(store, tenant, agent_id)
    await sets.delete_case(agent, set_id, case_id)
    return Response(status_code=204)


# -- 실행 (9절) ------------------------------------------------------------------------------


def _run_out(run: EvaluationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "set_id": run.set_id,
        "set_digest": run.set_digest,
        "revision": run.revision,
        "spec_digest": run.spec_digest,
        "grader": run.grader.model_dump() if run.grader is not None else None,
        "state": run.state,
        "reason": run.reason,
        "cancelled_by": run.cancelled_by,
        "started_by": run.by.sub,
        "started_by_name": run.by.username,
        "header_mode": run.header_mode,
        "cases": len(run.cases),
        "repeats": run.repeats,
        "done_steps": len(run.verdicts),
        "graded_steps": run.step.graded,
        "verdicts": [v.model_dump() for v in run.verdicts],
        "totals": run.totals.model_dump(),
        "created_at": run.created_at,
        "running_at": run.running_at,
        "ended_at": run.ended_at,
    }


@router.post(
    RUNS,
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EvaluationRunOut,
    dependencies=[APPROVE, DEPLOY],
)
async def start_evaluation_run(
    tenant: Tenant,
    user: User,
    store: Agents,
    runs: Runs,
    agent_id: str,
    set_id: str,
    body: EvaluationRunBody,
) -> dict[str, Any]:
    """그 세트로 리비전 하나를 평가한다. 실행기가 평가 미리 보기를 열어 사례마다 돌린다. 본
    지문과 지금 세트가 다르면 409 `evaluation_set_changed`, 도는 실행이 있으면 409
    `evaluation_run_active`, 테넌트의 오늘 실행을 다 썼으면 429 `evaluation_run_limit`, 평가나 미리
    보기를 껐으면 409 `evaluations_disabled`·`preview_disabled` 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    run = await runs.start(agent, set_id, user, set_digest=body.set_digest, revision=body.revision)
    return _run_out(run)


@router.get(RUNS, response_model=EvaluationRunsOut, dependencies=[APPROVE])
async def list_evaluation_runs(
    tenant: Tenant, user: User, store: Agents, runs: Runs, agent_id: str, set_id: str
) -> dict[str, Any]:
    """그 세트의 실행(새것이 앞)과, 부른 사람이 시작하기 전에 확인할 사례다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    items = await runs.runs_of(agent, set_id)
    return {
        "items": [_run_out(r) for r in items],
        "review": await runs.review(agent, set_id, user),
    }


@router.get(RUN, response_model=EvaluationRunDetailOut, dependencies=[APPROVE])
async def get_evaluation_run(
    tenant: Tenant,
    user: User,
    store: Agents,
    gate: Gate,
    runs: Runs,
    agent_id: str,
    set_id: str,
    run_id: str,
) -> dict[str, Any]:
    """실행 하나. 결과의 내용은 시작한 사람만 받고, 다른 사람은 `content_hidden` 이다. 끝난 실행은
    배포된 내용의 기준선과 견준 사례(`baseline`)를 싣는다. 읽기도 감사에 남는다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    run = await runs.get(agent, set_id, run_id)
    results = await runs.results(run, user)
    baseline = None
    if run.state == "finished":
        setting = agent.evaluation_gate
        age = setting.max_age_days if setting is not None else DEFAULT_MAX_AGE_DAYS
        seen = await gate.against_baseline(agent, run, max_age_days=age)
        baseline = {"worse": seen.worse, "missing": seen.missing}
    return {
        **_run_out(run),
        "results": [r.model_dump() for r in results or []],
        "content_hidden": results is None,
        "baseline": baseline,
    }


@router.delete(RUN, status_code=204, response_class=Response, dependencies=[APPROVE, DEPLOY])
async def cancel_evaluation_run(
    tenant: Tenant,
    user: User,
    store: Agents,
    runs: Runs,
    agent_id: str,
    set_id: str,
    run_id: str,
) -> Response:
    """도는 실행을 취소하고 그 미리 보기를 끝낸다. 끝난 실행이면 409 `state_conflict` 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    await runs.cancel(agent, set_id, run_id, user)
    return Response(status_code=204)


# -- 배포 게이트 (12절) ------------------------------------------------------------------------


@router.put(
    "/{agent_id}/evaluation-gate",
    response_model=EvaluationGateAnswer,
    dependencies=[APPROVE, DEPLOY],
)
async def put_evaluation_gate(
    tenant: Tenant,
    user: User,
    store: Agents,
    gate: Gate,
    agent_id: str,
    body: EvaluationGateBody | None = None,
) -> dict[str, Any]:
    """배포 게이트를 켜거나 바꾸거나 끈다(본문이 null 이면 끄고 `gate` 가 null 이다). 리비전 밖의
    설정이라 리비전이 끌 수 없다.
    없는 세트는 404, 반복이 2 보다 적은 세트에 `no_worse_cases` 는 422 `invalid_value` 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    saved = await gate.set_gate(agent, body.to_stored() if body is not None else None, user)
    return {"gate": saved.model_dump(mode="json") if saved is not None else None}
