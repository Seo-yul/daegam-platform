"""가격표와 사용량 (P37 6절·13절). 가격표 읽기·쓰기는 Platform Settings 와 같은 scope 다.

가격을 바꾸면 비용 보고가 바뀌므로 쓰기는 감사에 남는다(감사 미들웨어가 모든 쓰기를 남긴다).
가격은 행이 들어올 때 매기므로 규칙을 고쳐도 지난 행은 그대로이고, 지난 값을 고치는 것은
다시 계산(`POST /api/usage/reprice`)이다. 게이트웨이 사용량 토큰의 회수
(`POST /api/admin/telemetry/gateway-tokens/revoke`)도 사람이 하는 쓰기라 여기 있다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError

from daegam.api._common import error, not_found
from daegam.api.deps import Tenant, User, require_platform_admin, require_scope
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.usage import (
    AdminUsageOut,
    GatewayTokenRevokeIn,
    GatewayTokenRevokeOut,
    PricingIn,
    PricingOut,
    RepriceIn,
    RepriceOut,
    UsageReportOut,
)
from daegam.errors import revision_conflict
from daegam.services.usage.reports import (
    AdminGroup,
    ReportError,
    TenantGroup,
    UsageQuery,
    check_zone,
    parse_query,
)
from daegam.services.usage.rollup import RepriceError, RepriceRunning
from daegam.store.base import naive_utc
from daegam.store.pricing import PricingTable, default_rules

router = APIRouter(responses=ERROR_RESPONSES, tags=["usage"])


def _pricing_out(table: PricingTable) -> dict[str, Any]:
    return {
        **table.model_dump(mode="json"),
        "version": table.version,
        "defaults": [r.model_dump(mode="json") for r in default_rules()],
    }


@router.get(
    "/settings/pricing",
    response_model=PricingOut,
    dependencies=[Depends(require_scope("settings:read"))],
)
async def get_pricing(request: Request) -> dict[str, Any]:
    """가격표. 관리자 규칙과 그 판, 읽기 전용 기본 가격(출처와 확인 날짜)이다. 금액은 백만 토큰당
    달러이고 cache 값이 비면 input 가격이다."""
    return _pricing_out(await request.app.state.price_book.read())


@router.put(
    "/settings/pricing",
    response_model=PricingOut,
    dependencies=[Depends(require_scope("settings:write"))],
)
async def put_pricing(body: PricingIn, request: Request, user: User) -> dict[str, Any]:
    """규칙 전체를 바꾼다. 읽은 판이 아니면 409 revision_conflict 다. 바꾼 가격은 그 뒤 들어오는
    행부터 매겨진다."""
    try:
        table = await request.app.state.price_book.replace(
            body.rules, expected_version=body.expected_version, updated_by=user.sub
        )
    except ValidationError as e:
        raise error(422, "invalid_value", str(e)) from e
    if table is None:
        raise revision_conflict(error, "price", "table")
    return _pricing_out(table)


# -- 사용량 (P37 13절) ----------------------------------------------------------------------

Since = Annotated[datetime | None, Query(description="포함. 없으면 tz 의 이번 달 1일")]
Until = Annotated[datetime | None, Query(description="제외. 기간은 400일까지")]
Tz = Annotated[str, Query(description="날을 자를 IANA 시간대")]


def checked_zone(tz: str) -> str:
    """IANA 시간대 이름을 확인한다. 모르는 이름이면 422 다."""
    try:
        check_zone(tz)
    except ReportError as e:
        raise error(422, "invalid_value", str(e)) from e
    return tz


def _query(
    since: datetime | None, until: datetime | None, group: AdminGroup, tz: str, tenant: str | None
) -> UsageQuery:
    try:
        return parse_query(since, until, group, tz, tenant=tenant)
    except ReportError as e:
        raise error(422, "invalid_value", str(e)) from e


@router.get(
    "/usage",
    response_model=UsageReportOut,
    dependencies=[Depends(require_scope("usage:read"))],
)
async def get_usage(
    request: Request,
    tenant: Tenant,
    since: Since = None,
    until: Until = None,
    group: TenantGroup = "day",
    tz: Tz = "UTC",
) -> dict[str, Any]:
    """이 테넌트의 사용량과 비용. 날마다 금액 순 다섯과 other, 기준별 줄 200개까지를 준다. 시간
    묶음만 읽으므로 2분까지 늦다."""
    q = _query(since, until, group, tz, tenant)
    return await request.app.state.usage_reports.report(q)


@router.get(
    "/admin/usage",
    response_model=AdminUsageOut,
    dependencies=[Depends(require_platform_admin)],
)
async def get_platform_usage(
    request: Request,
    since: Since = None,
    until: Until = None,
    group: AdminGroup = "day",
    tz: Tz = "UTC",
    tenant: str | None = None,
) -> dict[str, Any]:
    """플랫폼 전체의 사용량. 테넌트·경로 기준이 더해지고, 가격 규칙이 없는 모델과 원장
    건강(0 이어야 하는 수, 원장을 쓰지 못하는 옛 이미지의 Agent 수, 컬렉션 크기)을 함께 준다."""
    q = _query(since, until, group, tz, tenant)
    reports = request.app.state.usage_reports
    return {
        **await reports.report(q),
        "unpriced": await reports.unpriced(q),
        "health": await reports.health(),
    }


@router.post(
    "/usage/reprice",
    response_model=RepriceOut,
    status_code=202,
    dependencies=[Depends(require_platform_admin)],
)
async def post_reprice(body: RepriceIn, request: Request) -> dict[str, Any]:
    """기간(92일까지)의 원장 행을 지금 가격표로 다시 매긴다. 롤업 틱이 5000행씩 이어 간다. 끝나지
    않은 다시 계산이 있으면 409 reprice_running 이다."""
    try:
        return await request.app.state.usage_rollup.request_reprice(
            naive_utc(body.since), naive_utc(body.until)
        )
    except RepriceError as e:
        raise error(422, "invalid_value", str(e)) from e
    except RepriceRunning as e:
        raise error(409, "reprice_running", str(e)) from e


@router.get(
    "/usage/reprice",
    response_model=RepriceOut,
    dependencies=[Depends(require_platform_admin)],
)
async def get_reprice(request: Request) -> dict[str, Any]:
    """마지막 다시 계산의 진행. 한 번도 하지 않았으면 404 다."""
    status = await request.app.state.usage_rollup.reprice_status()
    if status is None:
        raise not_found("no reprice has been requested")
    return status


@router.post(
    "/admin/telemetry/gateway-tokens/revoke",
    response_model=GatewayTokenRevokeOut,
    dependencies=[Depends(require_platform_admin)],
)
async def post_gateway_token_revoke(body: GatewayTokenRevokeIn, request: Request) -> dict[str, Any]:
    """그 클러스터의 LLM 게이트웨이 사용량 토큰을 지금까지 낸 것 모두 무른다(P37 8절). 새 토큰은
    게이트웨이 쪽 교체가 낸다. 등록되지 않은 클러스터는 422 unknown_cluster 다."""
    at = await request.app.state.gateway_usage.revoke(body.cluster_id)
    return {"cluster_id": body.cluster_id, "not_before": at}
