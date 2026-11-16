"""사용량·가격표 응답 모델 (P37 6절·13절)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.store.base import UtcDatetime
from daegam.store.pricing import DefaultPriceRule, PriceRule
from daegam.store.usage import SourceKind


class PricingIn(BaseModel):
    """표 전체를 바꾼다. expected_version 은 읽은 판이다."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int
    rules: list[PriceRule]


class PricingOut(BaseModel):
    """관리자 규칙과 그 판(저장할 때마다 오르는 수), 그리고 읽기 전용 기본 가격. 기본 가격은 확인한
    날짜를 든다."""

    model_config = REQUIRED_DEFAULTS

    version: int
    rules: list[PriceRule]
    defaults: list[DefaultPriceRule]
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None


class UsageMetrics(BaseModel):
    """묶음의 합. 금액은 매긴 행만 더하고 매기지 못한 호출은 `unpriced_calls` 로 센다."""

    model_config = REQUIRED_DEFAULTS

    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0
    unpriced_calls: int = 0
    unjoined_calls: int = 0


class UsageRow(UsageMetrics):
    """기준 하나의 줄. 기준에 따라 칸 일부만 찬다. 사람이 없는 줄(`user_sub` null)은 잇지 못한
    호출이다."""

    day: UtcDatetime | None = None
    source_kind: SourceKind | None = None
    source_id: str | None = None
    user_sub: str | None = None
    provider_kind: str | None = None
    provider_name: str | None = None
    model: str | None = None
    tenant: str | None = None
    route: str | None = None
    name: str | None = None


class UsageSeriesItem(BaseModel):
    model_config = REQUIRED_DEFAULTS

    key: list[str | None] | None  # 기준의 칸 값. null 은 나머지(other)다
    cost_usd: float
    calls: int


class UsageSeriesDay(BaseModel):
    model_config = REQUIRED_DEFAULTS

    day: UtcDatetime
    items: list[UsageSeriesItem]
    cost_usd: float


class UsageReportOut(BaseModel):
    """`GET /usage`. 날마다 금액 순 다섯과 other, 기준별 줄 200개까지다. 줄이 더 많아 잘렸으면
    `truncated` 다."""

    model_config = REQUIRED_DEFAULTS

    totals: UsageMetrics
    series: list[UsageSeriesDay]
    rows: list[UsageRow]
    truncated: bool = False


class UnpricedModel(BaseModel):
    model_config = REQUIRED_DEFAULTS

    provider_kind: str | None
    provider_name: str | None
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int


class LedgerHealth(BaseModel):
    """0 이어야 하는 원장 건강의 수와 옛 이미지 Agent 수, 원장 컬렉션 크기(바이트)."""

    model_config = REQUIRED_DEFAULTS

    gateway_missing: int = 0
    gateway_missing_cancelled: int = 0
    gateway_missing_failed: int = 0
    writer_conflicts: int = 0
    gateway_extra_records: int = 0
    join_mismatch: int = 0
    agents_without_telemetry: int = 0
    collection_bytes: dict[str, int] = {}
    tick_at: UtcDatetime | None = None


class AdminUsageOut(UsageReportOut):
    """`GET /admin/usage`. 테넌트·경로 기준, 가격 없는 모델, 원장 건강이 더해진다."""

    unpriced: list[UnpricedModel]
    health: LedgerHealth


class RepriceIn(BaseModel):
    """다시 계산할 기간. until 은 들지 않는다."""

    model_config = ConfigDict(extra="forbid")

    since: UtcDatetime
    until: UtcDatetime


class RepriceOut(BaseModel):
    """다시 계산의 진행. 남은 시간은 지금까지의 속도로 어림한 초다."""

    model_config = REQUIRED_DEFAULTS

    since: UtcDatetime
    until: UtcDatetime
    done: int
    left: int
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    eta_seconds: float | None


class GatewayTokenRevokeIn(BaseModel):
    """그 클러스터의 게이트웨이가 지금까지 받은 사용량 토큰을 모두 무른다."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str


class GatewayTokenRevokeOut(BaseModel):
    """이 시각보다 먼저 낸 토큰은 401 이다. 다른 레플리카에는 30초 안에 먹는다."""

    model_config = REQUIRED_DEFAULTS

    cluster_id: str
    not_before: UtcDatetime
