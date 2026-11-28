"""Agent 지표·이벤트·턴·트레이스 응답 모델 (P37 11절·12절)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.api.schemas.agent_runs import RunUsageOut
from daegam.api.schemas.agents import AgentChatOut
from daegam.store.agent_runs import RunStatus
from daegam.store.base import UtcDatetime

MetricsWindow = Literal["1h", "24h"]


class AgentMetricsToolsOut(BaseModel):
    """런타임 `GET /metrics` 의 `tools`. 이름이 아니라 개수와 적재 상태다. `protocols` 는 Tool 소스
    ref 마다 tools/list 를 읽은 연결이 맺은 MCP 규약 판(`2026-07-28` 같은)이고 아직 읽지 못했으면
    None 이다."""

    model_config = ConfigDict(extra="allow")

    count: int = 0
    loaded_at: float | None = None
    last_error: str | None = None
    protocols: dict[str, str | None] = {}


class AgentMetricsRuntimeOut(BaseModel):
    """런타임 `GET /metrics` 의 `runtime`. 도는 이미지가 아는 기능·능력 이름이다(D65). 확인용이고
    판정은 상태의 `runtime`(init 받기 때의 기록)으로 한다."""

    model_config = ConfigDict(extra="allow")

    features: list[str] = []


class RuntimeErrorOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    code: str
    message: str


class DailyTokensOut(BaseModel):
    """일당 토큰 예산(D26)의 공유 카운터. 예산이 없으면 칸이 null 이다."""

    model_config = REQUIRED_DEFAULTS

    used: int | None = None
    limit: int | None = None
    window: str | None = None


class TurnResultsOut(BaseModel):
    """창 안의 턴 결과. pending 은 승인 대기로 끝난 턴이고 cancelled 는 멈춘 턴이다. 모르는 오류
    코드는 turn_failed 로 센다."""

    model_config = REQUIRED_DEFAULTS

    done: int
    pending: int
    turn_failed: int
    turn_timeout: int
    quota_exhausted: int
    budget_exceeded: int
    cancelled: int


class RejectedOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    total: int
    by_code: dict[str, int]


class ErrorsOut(BaseModel):
    """오류로 끝난 턴의 수와 그 비율. 멈춘 턴은 분모에 들지 않는다."""

    model_config = REQUIRED_DEFAULTS

    turns: int
    error_rate: float


class LatencyOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    avg: float
    p50: float
    p95: float
    max: float


class ModelRowOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    model: str | None
    calls: int
    errors: int
    p95_ms: float | None


class ModelCallsOut(BaseModel):
    """채팅 모델 호출(임베딩 제외). 재시도와 대체 모델 전환은 그 이벤트의 수다."""

    model_config = REQUIRED_DEFAULTS

    calls: int
    errors: int
    p95_ms: float | None
    retries: int
    fallbacks: int
    by_model: list[ModelRowOut]


class ToolRowOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    tool: str | None
    calls: int
    errors: int
    p95_ms: float | None


class ToolCallsOut(BaseModel):
    """Tool 실행. `by_tool` 은 호출이 많은 20개다."""

    model_config = REQUIRED_DEFAULTS

    calls: int
    errors: int
    p95_ms: float | None
    by_tool: list[ToolRowOut]


class MetricsCostOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    usd: float
    unpriced_calls: int


class CostDayOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    day: UtcDatetime
    cost_usd: float
    calls: int
    input_tokens: int
    output_tokens: int


class DroppedOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    cost: int
    other: int


class TelemetryPodOut(BaseModel):
    """이벤트를 밀어 보낸 파드 하나. 버린 수는 파드가 알린 것이다."""

    model_config = REQUIRED_DEFAULTS

    name: str | None
    uid: str | None
    last_at: UtcDatetime
    received: int
    duplicates: int
    refused: int
    dropped: DroppedOut


class MetricsTelemetryOut(BaseModel):
    """이벤트가 오는 길. push 는 최근 5분 안에 밀어 보낸 파드가 있는 것이고, log 는 수집기가 파드
    로그에서 읽은 것이며, none 은 창 안에 둘 다 없는 것이다."""

    model_config = REQUIRED_DEFAULTS

    mode: Literal["push", "log", "none"]
    pods: list[TelemetryPodOut]
    dropped: DroppedOut
    # 도는 이미지가 이벤트를 밀어 보낼 줄 아는가. 아직 기록이 없으면 null 이다. 다시 배포하라는
    # 안내는 로그로만 오는 옛 이미지에만 선다
    image_pushes: bool | None


class AgentMetricsOut(BaseModel):
    """Agent 지표 (P37 11절). 런타임 몫(sessions·totals·daily_tokens·tools·warnings·runtime·chat)은
    공유 저장소의 지금 값이고 런타임에 연결할 수 없으면 null 이며 `runtime_error` 가 그 까닭이다.
    나머지는 창 안의 모든 레플리카를 이벤트 저장소와 원장에서 센 것이다. `chat` 은 상태와 같은
    모델이라 런타임이 더한 모르는 칸을 버린다(`AgentChatOut`)."""

    model_config = REQUIRED_DEFAULTS

    window: MetricsWindow
    sessions: dict[str, int] | None
    totals: dict[str, Any] | None
    daily_tokens: DailyTokensOut | None
    tools: AgentMetricsToolsOut | None
    warnings: list[str] | None
    runtime: AgentMetricsRuntimeOut | None
    chat: AgentChatOut | None
    runtime_error: RuntimeErrorOut | None
    turns: TurnResultsOut
    rejected: RejectedOut
    errors: ErrorsOut
    latency_ms: LatencyOut
    model: ModelCallsOut
    tool_calls: ToolCallsOut
    usage: dict[str, int]
    cost: MetricsCostOut
    cost_daily: list[CostDayOut]
    telemetry: MetricsTelemetryOut


class AgentEventOut(BaseModel):
    """`agent_events` 한 건. 런타임이 밀어 보냈거나(`source: push`) 수집기가 파드 로그에서 읽은
    것(`log`)이다. 본문(`body`)은 record_bodies 가 켜졌을 때만 있고 `agent:approve` 만 본다."""

    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="allow")

    id: str
    ts: UtcDatetime
    event: str
    source: str | None = None
    tenant: str | None = None
    instance_id: str | None = None
    # 미리 보기가 낸 이벤트면 그 미리 보기의 id 다. instance_id 는 원본이다 (P44b 9절)
    preview_id: str | None = None
    cluster_id: str | None = None
    namespace: str | None = None
    pod: str | None = None
    session_id: str | None = None
    user_sub: str | None = None
    turn: int | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    start: UtcDatetime | None = None
    duration_ms: float | None = None
    tool: str | None = None
    status: str | None = None
    decided_by: str | None = None
    rejected_code: str | None = None
    profile: dict[str, Any] | None = None
    instance: dict[str, Any] | None = None
    data: dict[str, Any] = {}
    body: dict[str, Any] | None = None
    collected_at: UtcDatetime | None = None


class TurnRunOut(BaseModel):
    """턴 시도의 실행 하나. 승인 뒤 재개는 resumed 다. 도는 중이면 status 가 running 이다."""

    model_config = REQUIRED_DEFAULTS

    trace_id: str | None
    status: str | None
    start: UtcDatetime
    duration_ms: float | None
    resumed: bool


class TurnCallOut(BaseModel):
    """턴이 다른 Agent 를 Tool 로 불러 생긴 실행 하나 (P46 6.5절). `task` 는 답을 기다리지 않고 맡긴
    작업의 실행이다. 토큰과 금액은 호출받은 Agent 의 원장에서 그 실행의 턴 몫이고, 아직 턴이 없거나
    원장에 행이 없으면 null 이다. 호출받은 Agent 가 지워졌으면 `agent_name` 이 null 이다."""

    model_config = REQUIRED_DEFAULTS

    run_id: str
    agent_id: str
    agent_name: str | None
    status: RunStatus
    reason: str | None
    task: bool
    usage: RunUsageOut | None
    cost_usd: float | None


class AgentTurnOut(BaseModel):
    """`calls` 는 이 턴이 부른 실행이고 만든 차례다. 자동 실행이 꺼진 플랫폼에서는 비어 있다."""

    model_config = REQUIRED_DEFAULTS

    turn_id: str
    turn: int | None
    regenerated: bool
    runs: list[TurnRunOut]
    usage: dict[str, int]
    cost_usd: float | None
    calls: list[TurnCallOut] = []


class AgentTurnsOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    turns: list[AgentTurnOut]


class SpanLedgerOut(BaseModel):
    """모델 span 의 원장 행. 게이트웨이 경로면 게이트웨이의 토큰이다."""

    model_config = REQUIRED_DEFAULTS

    writer: str
    route: str
    usage: dict[str, int]
    estimated: bool


class TraceSpanOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    span_id: str | None
    parent_span_id: str | None
    kind: Literal["run", "model", "tool", "subagent", "embeddings"]
    name: str
    start: UtcDatetime
    duration_ms: float | None
    status: str | None
    # 실패한 실행이나 호출이다. 실행과 호출의 결과 규칙(normalize)을 따르고 도는 중은 실패가 아니다
    error: bool
    attributes: dict[str, Any]
    cost_usd: float | None
    ledger: SpanLedgerOut | None = None
    body: dict[str, Any] | None = None


class TracePointOut(BaseModel):
    """span 이 아닌 이벤트(재시도, 대체 모델, 가드레일, 승인 요청 같은)다."""

    model_config = REQUIRED_DEFAULTS

    event: str
    ts: UtcDatetime
    span_id: str | None
    attributes: dict[str, Any]
    body: dict[str, Any] | None = None


class TraceTotalsOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    duration_ms: float | None
    usage: dict[str, Any]
    cost_usd: float | None
    calls: int
    unpriced_calls: int


class AgentTraceOut(BaseModel):
    """실행 하나의 트레이스. run span 밖의 이벤트는 3,000개, span 은 1,000개까지다. 넘으면
    truncated 이고 truncated_by 가 넘은 상한(spans, events), limit 이 그 수다."""

    model_config = REQUIRED_DEFAULTS

    trace_id: str
    session_id: str | None
    turn_id: str | None
    user_sub: str | None
    # 미리 보기의 턴이면 그 미리 보기의 id 다 (P44b 9절)
    preview_id: str | None = None
    root: TraceSpanOut | None
    spans: list[TraceSpanOut]
    points: list[TracePointOut]
    totals: TraceTotalsOut
    truncated: bool
    truncated_by: Literal["spans", "events"] | None
    limit: int
