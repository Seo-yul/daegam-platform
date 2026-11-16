"""Agent 의 지표·이벤트·턴·트레이스 (P37 11절·12절, D62). 런타임 관측이라 비서 도구가 아니다.

- 지표(`agent:read`): 런타임의 지금 상태와, 창 안의 모든 레플리카를 이벤트 저장소와 원장에서 센 수다
- 이벤트(`audit:read`): 다른 사람의 `user_sub` 가 보이므로 감사 읽기 scope 다. 답변 피드백과
  본문은 `agent:approve` 만 본다. 피드백은 같은 목록의 `turn_start` 와 이으면 누가 어떤 평가를
  남겼는지 드러나기 때문이다
- 턴과 트레이스(`agent:read`): 세션 소유자나 `agent:approve` 만 본다. 다른 사람의 Tool 결과가
  그대로 들어 있기 때문이다. 소유자는 그 세션(실행)의 `turn_start` 가 실은 `user_sub` 이다. 아직
  정할 수 없으면(turn_start 가 오기 전이거나 보관이 지났다) `agent:approve` 가 아닌 사람에게는
  404 다. 막 끝난 턴을 여는 트레이스 창이 그 404 를 보고 다시 읽는다
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from daegam.api._agent_proxy import get_agent_or_404, is_approver
from daegam.api._common import error, not_found
from daegam.api.agents import READ, Agents, Service, Traces
from daegam.api.audit import parse_ts
from daegam.api.deps import Tenant, User, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.agent_telemetry import (
    AgentEventOut,
    AgentMetricsOut,
    AgentTraceOut,
    AgentTurnsOut,
    MetricsWindow,
)
from daegam.api.usage import Tz, checked_zone
from daegam.auth.principal import Principal
from daegam.services.telemetry.agent_metrics import AgentMetrics
from daegam.services.telemetry.normalize import FEEDBACK_EVENT
from daegam.services.telemetry.traces import visible_event
from daegam.store.agent_events import MongoAgentEventStore

router = APIRouter(responses=ERROR_RESPONSES, prefix="/agents", tags=["agents"])


def get_agent_events(request: Request) -> MongoAgentEventStore:
    return request.app.state.agent_events


def get_metrics(request: Request) -> AgentMetrics:
    return request.app.state.agent_metrics


AgentEvents = Annotated[MongoAgentEventStore, Depends(get_agent_events)]
Metrics = Annotated[AgentMetrics, Depends(get_metrics)]


@router.get("/{agent_id}/metrics", response_model=AgentMetricsOut, dependencies=[READ])
async def get_agent_metrics(
    tenant: Tenant,
    store: Agents,
    service: Service,
    metrics: Metrics,
    agent_id: str,
    window: MetricsWindow = "1h",
    tz: Tz = "UTC",
) -> dict[str, Any]:
    """지표. 런타임 몫은 지금 값이고 나머지는 창(1h, 24h) 안의 모든 레플리카의 수다. `cost_daily`
    는 tz 의 날로 자른 30일이다. 같은 (Agent, 창, 시간대)는 15초 기억한다."""
    checked_zone(tz)
    doc = await get_agent_or_404(store, tenant, agent_id)
    return await metrics.read(doc, window=window, tz=tz, runtime=service.proxy.metrics)


@router.get(
    "/{agent_id}/events",
    response_model=list[AgentEventOut],
    response_model_exclude_unset=True,
    dependencies=[Depends(require_scope("audit:read"))],
)
async def list_agent_events(
    tenant: Tenant,
    user: User,
    store: Agents,
    events: AgentEvents,
    agent_id: str,
    since: str | None = None,
    until: str | None = None,
    event: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    """런타임 이벤트 (최신순). 답변 피드백과 본문은 `agent:approve` 만 본다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    approver = is_approver(user, tenant)
    rows = await events.query(
        tenant=doc.tenant,
        instance_id=doc.id,
        since=parse_ts(since, "since"),
        until=parse_ts(until, "until"),
        events=(event,) if event else (),
        session_id=session_id,
        trace_id=trace_id,
        hide_events=() if approver else (FEEDBACK_EVENT,),
        limit=limit,
    )
    return [visible_event(d, bodies=approver) for d in rows]


@router.get(
    "/{agent_id}/sessions/{session_id}/turns",
    response_model=AgentTurnsOut,
    dependencies=[READ],
)
async def list_agent_turns(
    tenant: Tenant,
    user: User,
    store: Agents,
    traces: Traces,
    agent_id: str,
    session_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """세션의 턴 시도(최신부터)와 그 실행, 토큰과 금액. 소유자나 `agent:approve` 만 본다. 소유자를
    아직 정할 수 없으면 approver 가 아닌 사람에게는 404 다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    if not is_approver(user, tenant):
        owner = await traces.owner(doc, session_id)
        _check_owner(owner, user, f"session {session_id} has no turns yet")
    return {"turns": await traces.turns(doc, session_id, limit=limit)}


@router.get("/{agent_id}/traces/{trace_id}", response_model=AgentTraceOut, dependencies=[READ])
async def get_agent_trace(
    tenant: Tenant, user: User, store: Agents, traces: Traces, agent_id: str, trace_id: str
) -> dict[str, Any]:
    """실행 하나의 트레이스. 그 실행의 소유자나 `agent:approve` 만 본다. 이벤트를 읽기 전에
    소유자를 가린다. 이벤트가 없거나, approver 가 아닌 사람에게 소유자를 아직 정할 수 없으면(실행의
    `turn_start` 가 오기 전) 404 다. 트레이스 창은 그 404 를 보고 다시 읽는다. 다른 사람의 실행은
    403 이다."""
    doc = await get_agent_or_404(store, tenant, agent_id)
    if not is_approver(user, tenant):
        owner = await traces.trace_owner(doc, trace_id)
        _check_owner(owner, user, f"trace {trace_id} not found")
    trace = await traces.trace(doc, trace_id)
    if trace is None:
        raise not_found(f"trace {trace_id} not found")
    return trace


def _check_owner(owner: str | None, user: Principal, missing: str) -> None:
    """소유자를 아직 정할 수 없으면 404(missing), 다른 사람이면 403 이다."""
    if owner is None:
        raise not_found(missing)
    if owner != user.sub:
        raise error(403, "not_session_owner", "session belongs to another user")
