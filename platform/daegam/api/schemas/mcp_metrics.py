"""Gateway·MCP Server 메트릭 응답 모델 (P51 4절, D90)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from daegam.api.schemas import REQUIRED_DEFAULTS

McpMetricsWindow = Literal["24h", "7d", "30d"]


class OutcomeCountsOut(BaseModel):
    """결과별 Tool 호출 수. ToolHive 의 결과 다섯과, 없거나 그 밖의 값인 unknown 이다."""

    model_config = REQUIRED_DEFAULTS

    success: int = 0
    application_error: int = 0
    denied: int = 0
    failure: int = 0
    error: int = 0
    unknown: int = 0


class DurationOut(BaseModel):
    """걸린 시간(ms). p50·p95 는 구간으로 어림한 값이다. 걸린 시간이 없으면 칸이 null 이다."""

    model_config = REQUIRED_DEFAULTS

    avg: float | None = None
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


class CallCountsOut(BaseModel):
    """Tool 호출 수. errors 는 success 를 뺀 모든 결과다."""

    model_config = REQUIRED_DEFAULTS

    calls: int
    errors: int
    outcomes: OutcomeCountsOut


class McpMetricsTotalsOut(CallCountsOut):
    resource_reads: int
    prompt_gets: int
    duration_ms: DurationOut


class McpMetricsStepOut(BaseModel):
    """한 시간이나 한 날의 Tool 호출. 호출이 없는 단계도 0 으로 온다."""

    model_config = REQUIRED_DEFAULTS

    start: str
    calls: int
    outcomes: OutcomeCountsOut


class McpMetricsToolOut(CallCountsOut):
    """Tool 하나. tool 이 null 이면 광고하지 않는 이름과 접힌 나머지를 모은 행이다."""

    tool: str | None
    p95_ms: float | None = None
    max_ms: float | None = None


class McpMetricsBackendOut(CallCountsOut):
    """Gateway 의 백엔드 하나. entry 가 null 이면 백엔드를 정하지 못한 호출이고, hub_item_id 가
    null 이면 지금 배포에 없는 Entry 다."""

    entry: str | None
    hub_item_id: str | None = None
    name: str | None = None
    p95_ms: float | None = None


class McpMetricsSourceOut(BaseModel):
    """숫자의 출처. audit 은 감사 기록이 남는가(off: 클러스터가 남기지 않는다, pending: 이 Gateway
    가 남기도록 다시 적용되기를 기다린다), collector 는 수집기가 도는가, counted_from 은 지금 묶음
    판이 센 가장 오래된 시간이다(다 셌으면 null)."""

    model_config = REQUIRED_DEFAULTS

    audit: Literal["on", "off", "pending"]
    collector: Literal["on", "off"]
    rolled_up_at: str | None = None
    counted_from: str | None = None


class McpMetricsOut(BaseModel):
    model_config = REQUIRED_DEFAULTS

    window: McpMetricsWindow
    since: str
    until: str
    step: Literal["hour", "day"]
    totals: McpMetricsTotalsOut
    series: list[McpMetricsStepOut]
    by_tool: list[McpMetricsToolOut]
    source: McpMetricsSourceOut


class GatewayMetricsOut(McpMetricsOut):
    by_backend: list[McpMetricsBackendOut]
