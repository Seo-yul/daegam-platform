"""Agent 의 실행, 스케줄, 웹훅 트리거 라우트 (P41 2절).

`agent:read` 가 읽고, `agent:write` 와 `agent:chat` 이 스케줄과 트리거를 만들고 바꾸고 멈추고 다시
켜고 지우며 지금 실행한다. 트리거의 서명 비밀은 만들 때와 새로 만들 때 한 번만 보인다. 실행을
멈추는 것은 `agent:read` 를 가진 그 실행의 묶인 사람과 `agent:approve` 다. `/me/runs-as-me` 는
나에게 묶인 소스를 테넌트를 가로질러 보고 멈춘다. 지금 role 과 상관없다(R9). 기능을 켜기 전에는
(`DAEGAM_RUNS_ENABLED`) 모든 라우트가 404 다. OpenAPI 와 UI 타입에는 늘 보인다. 묶인 사람이 비서의
사용자가 되고 트리거는 비밀을 돌려주므로 비서의 도구 표 밖이다(D31).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from daegam.api._agent_proxy import (
    APPROVE_SCOPE,
    get_agent_or_404,
    get_run_service,
    is_approver,
)
from daegam.api._common import (
    audit_target,
    forbidden,
    not_found,
    or_404,
    patched_fields,
    patched_values,
    with_agent_names,
)
from daegam.api.agents import CHAT, READ, WRITE, Agents, Traces
from daegam.api.deps import Tenant, User
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.agent_runs import (
    AgentRunOut,
    AgentRunsOut,
    BoundScheduleOut,
    BoundTriggerOut,
    RunNowIn,
    RunsAsMeOut,
    ScheduleIn,
    ScheduleOut,
    SchedulePatch,
    SchedulesOut,
    TriggerIn,
    TriggerOut,
    TriggerPatch,
    TriggerSecretOut,
    TriggersOut,
)
from daegam.auth.principal import Principal
from daegam.services.runs.service import PAUSED_BY_HAND, RunService, ScheduleInput
from daegam.services.runs.triggers import RunTriggers, TriggerInput
from daegam.services.telemetry.traces import AgentTraces
from daegam.store.agent_runs import AgentRun, RunStatus
from daegam.store.agent_schedules import AgentSchedule
from daegam.store.agent_triggers import AgentTrigger
from daegam.store.agents import Agent
from daegam.store.run_sources import RunSource
from daegam.urls import hook_path


def require_runs_enabled(request: Request) -> None:
    """기능을 켜기 전에는 라우트가 없는 것처럼 404 다."""
    if not request.app.state.settings.runs_enabled:
        raise not_found("background runs are not enabled")


def get_run_triggers(request: Request) -> RunTriggers:
    return request.app.state.run_triggers


Runs = Annotated[RunService, Depends(get_run_service)]
Triggers = Annotated[RunTriggers, Depends(get_run_triggers)]

router = APIRouter(
    prefix="/agents",
    tags=["agent-runs"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_runs_enabled)],
)
# 나를 대신하는 소스. 테넌트 밖의 경로라 Agent 권한을 보지 않는다
me_router = APIRouter(
    prefix="/me/runs-as-me",
    tags=["agent-runs"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_runs_enabled)],
)


def may_stop(run: AgentRun, user: Principal, tenant: str) -> bool:
    """실행을 멈출 수 있는가. 그 실행의 묶인 사람이거나 테넌트의 `agent:approve` 다 (2절)."""
    return run.acts_as.sub == user.sub or is_approver(user, tenant)


async def _schedule(service: RunService, tenant: str, agent_id: str, sid: str) -> AgentSchedule:
    return or_404(await service.schedules.get(tenant, agent_id, sid), "schedule", sid)


async def _trigger(service: RunService, tenant: str, agent_id: str, tid: str) -> AgentTrigger:
    return or_404(await service.triggers.get(tenant, agent_id, tid), "trigger", tid)


def trigger_out(trigger: AgentTrigger, secret: str | None = None) -> dict[str, Any]:
    out = {**trigger.model_dump(), "path": hook_path(trigger.id)}
    return {**out, "secret": secret} if secret is not None else out


async def _run(service: RunService, tenant: str, agent_id: str, run_id: str) -> AgentRun:
    return or_404(await service.runs.get(tenant, agent_id, run_id), "run", run_id)


async def with_usage(
    traces: AgentTraces, agent: Agent, runs: list[AgentRun]
) -> list[dict[str, Any]]:
    """실행마다 그 세션의 원장 몫(`usage`, `cost_usd`)을 붙인다. 세션이 없거나 원장에 행이 없으면
    둘 다 null 이다 (11절)."""
    sums = await traces.session_usage(agent, [r.session_id for r in runs if r.session_id])
    empty = {"usage": None, "cost_usd": None}
    return [{**r.model_dump(), **sums.get(r.session_id or "", empty)} for r in runs]


# -- 실행 -----------------------------------------------------------------------------


@router.get("/{agent_id}/runs", response_model=AgentRunsOut, dependencies=[READ])
async def list_runs(
    tenant: Tenant,
    store: Agents,
    service: Runs,
    traces: Traces,
    agent_id: str,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """실행 기록. 마지막으로 움직인 것부터다. 건너뛴 울림은 소스와 까닭마다 `skipped` 행 하나로
    센다. 실행마다 세션의 원장 토큰과 금액이 붙는다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    runs = await service.runs.list_for_agent(tenant, agent_id, limit=limit, status=status_filter)
    return {"runs": await with_usage(traces, agent, runs)}


@router.get("/{agent_id}/runs/{run_id}", response_model=AgentRunOut, dependencies=[READ])
async def get_run(
    tenant: Tenant, store: Agents, service: Runs, traces: Traces, agent_id: str, run_id: str
) -> dict[str, Any]:
    agent = await get_agent_or_404(store, tenant, agent_id)
    (out,) = await with_usage(traces, agent, [await _run(service, tenant, agent_id, run_id)])
    return out


@router.post(
    "/{agent_id}/runs",
    response_model=AgentRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[WRITE, CHAT],
)
async def run_now(
    tenant: Tenant, user: User, store: Agents, service: Runs, agent_id: str, body: RunNowIn
) -> AgentRun:
    """지금 실행. 요청한 사람으로 도는 실행 기록을 `starting` 으로 넣고 바로 답한다. Agent 의
    상한에 걸리면 429 agent_run_limit 이다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    return await service.run_now(agent, user, body.message)


@router.post(
    "/{agent_id}/runs/{run_id}/cancel",
    response_model=AgentRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[READ],
)
async def cancel_run(
    tenant: Tenant, user: User, store: Agents, service: Runs, agent_id: str, run_id: str
) -> AgentRun:
    """실행을 멈춘다. 묶인 사람은 자기에게 묶인 실행을, `agent:approve` 는 테넌트의 모든 실행을
    멈춘다. 이미 끝난 실행에는 아무것도 하지 않고 그 기록을 그대로 답한다."""
    await get_agent_or_404(store, tenant, agent_id)
    run = await _run(service, tenant, agent_id, run_id)
    if not may_stop(run, user, tenant):
        raise forbidden(f"only the bound person or a holder of {APPROVE_SCOPE} stops this run")
    return await service.stop(run)


# -- 스케줄 ---------------------------------------------------------------------------


@router.get("/{agent_id}/schedules", response_model=SchedulesOut, dependencies=[READ])
async def list_schedules(
    tenant: Tenant, store: Agents, service: Runs, agent_id: str
) -> dict[str, Any]:
    await get_agent_or_404(store, tenant, agent_id)
    return {"schedules": await service.schedules.list_for_agent(tenant, agent_id)}


@router.post(
    "/{agent_id}/schedules",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WRITE, CHAT],
)
async def create_schedule(
    tenant: Tenant, user: User, store: Agents, service: Runs, agent_id: str, body: ScheduleIn
) -> AgentSchedule:
    """스케줄을 만든다. 만든 사람이 묶인 사람이 된다. 규칙의 두 울림이 1년 안에 5분보다 가까우면
    422 schedule_too_frequent 이고, 실행의 전제 조건이 맞지 않으면 409 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    return await service.create_schedule(agent, user, ScheduleInput(**body.model_dump()))


@router.patch(
    "/{agent_id}/schedules/{schedule_id}",
    response_model=ScheduleOut,
    dependencies=[WRITE, CHAT],
)
async def update_schedule(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Runs,
    agent_id: str,
    schedule_id: str,
    body: SchedulePatch,
) -> AgentSchedule:
    """보낸 칸만 바꾼다. 바꾼 사람에게 다시 묶고 만료와 다음 울림을 지금부터 센다. 만료 길이를
    보내지 않으면 지난번에 고른 길이로 센다."""
    sent = patched_fields(body)
    agent = await get_agent_or_404(store, tenant, agent_id)
    schedule = await _schedule(service, tenant, agent_id, schedule_id)
    return await service.update_schedule(
        agent, schedule, user, **patched_values(body, sent, skip=())
    )


@router.delete(
    "/{agent_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[WRITE, CHAT],
)
async def delete_schedule(
    tenant: Tenant, store: Agents, service: Runs, agent_id: str, schedule_id: str
) -> None:
    """지우고 그 스케줄의 열린 실행을 중지처럼 끝낸다."""
    await get_agent_or_404(store, tenant, agent_id)
    await service.delete_schedule(await _schedule(service, tenant, agent_id, schedule_id))


@router.post(
    "/{agent_id}/schedules/{schedule_id}/pause",
    response_model=ScheduleOut,
    dependencies=[WRITE, CHAT],
)
async def pause_schedule(
    tenant: Tenant, store: Agents, service: Runs, agent_id: str, schedule_id: str
) -> AgentSchedule:
    """멈추고 그 스케줄의 열린 실행을 중지처럼 끝낸다. 묶인 사람은 바꾸지 않는다."""
    await get_agent_or_404(store, tenant, agent_id)
    schedule = await _schedule(service, tenant, agent_id, schedule_id)
    return await service.pause_schedule(schedule, PAUSED_BY_HAND)


@router.post(
    "/{agent_id}/schedules/{schedule_id}/resume",
    response_model=ScheduleOut,
    dependencies=[WRITE, CHAT],
)
async def resume_schedule(
    tenant: Tenant, user: User, store: Agents, service: Runs, agent_id: str, schedule_id: str
) -> AgentSchedule:
    """다시 켠다. 다시 켠 사람에게 묶고 만료와 다음 울림을 지금부터 센다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    schedule = await _schedule(service, tenant, agent_id, schedule_id)
    return await service.resume_schedule(agent, schedule, user)


# -- 웹훅 트리거 (4절) ---------------------------------------------------------------------


@router.get("/{agent_id}/triggers", response_model=TriggersOut, dependencies=[READ])
async def list_triggers(
    tenant: Tenant, store: Agents, service: Runs, agent_id: str
) -> dict[str, Any]:
    await get_agent_or_404(store, tenant, agent_id)
    triggers = await service.triggers.list_for_agent(tenant, agent_id)
    return {"triggers": [trigger_out(t) for t in triggers]}


@router.post(
    "/{agent_id}/triggers",
    response_model=TriggerSecretOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[WRITE, CHAT],
)
async def create_trigger(
    tenant: Tenant, user: User, store: Agents, triggers: Triggers, agent_id: str, body: TriggerIn
) -> dict[str, Any]:
    """트리거를 만든다. 만든 사람이 묶인 사람이 되고 서명 비밀은 이 답에만 있다. 실행의 전제
    조건이나 트리거 규칙(쓰기 Tool 의 승인 규칙)이 맞지 않으면 409 다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    trigger, secret = await triggers.create(agent, user, TriggerInput(**body.model_dump()))
    return trigger_out(trigger, secret)


@router.patch(
    "/{agent_id}/triggers/{trigger_id}",
    response_model=TriggerOut,
    dependencies=[WRITE, CHAT],
)
async def update_trigger(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Runs,
    triggers: Triggers,
    agent_id: str,
    trigger_id: str,
    body: TriggerPatch,
) -> dict[str, Any]:
    """보낸 칸만 바꾼다. 이름 말고 무엇이든 바꾸면 바꾼 사람에게 다시 묶고 만료를 새로 센다."""
    sent = patched_fields(body)
    agent = await get_agent_or_404(store, tenant, agent_id)
    trigger = await _trigger(service, tenant, agent_id, trigger_id)
    updated = await triggers.update(agent, trigger, user, **patched_values(body, sent, skip=()))
    return trigger_out(updated)


@router.delete(
    "/{agent_id}/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[WRITE, CHAT],
)
async def delete_trigger(
    tenant: Tenant, store: Agents, service: Runs, triggers: Triggers, agent_id: str, trigger_id: str
) -> None:
    """지우고 그 열린 실행을 중지처럼 끝내며 서명 비밀을 뺀다."""
    await get_agent_or_404(store, tenant, agent_id)
    await triggers.delete(await _trigger(service, tenant, agent_id, trigger_id))


@router.post(
    "/{agent_id}/triggers/{trigger_id}/rotate-secret",
    response_model=TriggerSecretOut,
    dependencies=[WRITE, CHAT],
)
async def rotate_trigger_secret(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Runs,
    triggers: Triggers,
    agent_id: str,
    trigger_id: str,
) -> dict[str, Any]:
    """서명 비밀을 새로 만든다. 새 비밀은 이 답에만 있고 앞 비밀은 24시간 더 받는다. 다시 묶는다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    trigger = await _trigger(service, tenant, agent_id, trigger_id)
    rotated, secret = await triggers.rotate(agent, trigger, user)
    return trigger_out(rotated, secret)


@router.post(
    "/{agent_id}/triggers/{trigger_id}/pause",
    response_model=TriggerOut,
    dependencies=[WRITE, CHAT],
)
async def pause_trigger(
    tenant: Tenant, store: Agents, service: Runs, triggers: Triggers, agent_id: str, trigger_id: str
) -> dict[str, Any]:
    """멈추고 그 열린 실행을 중지처럼 끝낸다. 묶인 사람은 바꾸지 않는다."""
    await get_agent_or_404(store, tenant, agent_id)
    trigger = await _trigger(service, tenant, agent_id, trigger_id)
    return trigger_out(await triggers.pause(trigger))


@router.post(
    "/{agent_id}/triggers/{trigger_id}/resume",
    response_model=TriggerOut,
    dependencies=[WRITE, CHAT],
)
async def resume_trigger(
    tenant: Tenant,
    user: User,
    store: Agents,
    service: Runs,
    triggers: Triggers,
    agent_id: str,
    trigger_id: str,
) -> dict[str, Any]:
    """다시 켠다. 다시 켠 사람에게 묶고 만료를 새로 센다."""
    agent = await get_agent_or_404(store, tenant, agent_id)
    trigger = await _trigger(service, tenant, agent_id, trigger_id)
    return trigger_out(await triggers.resume(agent, trigger, user))


# -- 나를 대신하는 소스 (6절) --------------------------------------------------------------------


@me_router.get("", response_model=RunsAsMeOut)
async def list_runs_as_me(user: User, store: Agents, service: Runs) -> dict[str, Any]:
    """나에게 묶인 스케줄과 트리거를 모든 테넌트에서 본다."""
    return {
        "schedules": await with_agent_names(
            store, await service.schedules.list_bound(user.sub), RunSource.model_dump
        ),
        "triggers": await with_agent_names(
            store, await service.triggers.list_bound(user.sub), trigger_out
        ),
    }


@me_router.post("/schedules/{schedule_id}/pause", response_model=BoundScheduleOut)
async def pause_my_schedule(
    request: Request, user: User, store: Agents, service: Runs, schedule_id: str
) -> dict[str, Any]:
    """나에게 묶인 스케줄을 멈추고 그 열린 실행을 중지처럼 끝낸다. 남의 것은 404 다. 멈춤은 묶인
    사람이 그대로일 때만 쓴다."""
    paused = or_404(
        await service.pause_own(service.schedules, user.sub, schedule_id), "schedule", schedule_id
    )
    audit_target(request, paused.id, paused.tenant)
    (out,) = await with_agent_names(store, [paused], RunSource.model_dump)
    return out


@me_router.post("/triggers/{trigger_id}/pause", response_model=BoundTriggerOut)
async def pause_my_trigger(
    request: Request, user: User, store: Agents, service: Runs, trigger_id: str
) -> dict[str, Any]:
    """나에게 묶인 트리거를 멈춘다. 스케줄과 같은 규칙이다."""
    paused = or_404(
        await service.pause_own(service.triggers, user.sub, trigger_id), "trigger", trigger_id
    )
    audit_target(request, paused.id, paused.tenant)
    (out,) = await with_agent_names(store, [paused], trigger_out)
    return out
