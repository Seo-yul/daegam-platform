"""감사 조회 API. scope audit:read. platform-admin 은 테넌트 무관하게 본다."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from daegam.api._common import error, forbidden
from daegam.api.deps import Settings_, User, get_tenant
from daegam.api.schemas import ERROR_RESPONSES
from daegam.api.schemas.audit import AuditEventOut, MCPAuditEventOut
from daegam.audit.store import MongoAuditStore
from daegam.store.base import iso_utc, parse_iso_utc

router = APIRouter(prefix="/audit", tags=["audit"], responses=ERROR_RESPONSES)

SCOPE = "audit:read"


def get_audit_store(request: Request) -> MongoAuditStore:
    return request.app.state.audit


AuditStore = Annotated[MongoAuditStore, Depends(get_audit_store)]


def parse_ts(value: str | None, name: str) -> datetime | None:
    """ISO 8601. 시간대가 있으면 UTC naive 로 맞춘다(저장 형식과 같게). 비었으면 None 이고, 값이
    있는데 읽을 수 없으면 422 다."""
    if value is None or not value.strip():
        return None
    dt = parse_iso_utc(value)
    if dt is None:
        raise error(422, "invalid_value", f"{name} must be ISO 8601")
    return dt


async def audit_tenant(
    request: Request,
    principal: User,
    settings: Settings_,
    tenant: Annotated[str | None, Query()] = None,
) -> str | None:
    """조회 대상 테넌트. platform-admin 은 query tenant(없으면 전체). 그 외는 자기 테넌트 안에서
    audit:read 가 있어야 하고, query tenant 가 있으면 그것과 같아야 한다."""
    if principal.is_platform_admin:
        return tenant or None
    own = get_tenant(request, settings, principal)
    if tenant and tenant != own:
        raise forbidden(f"not a member of tenant {tenant}", tenant=tenant)
    if SCOPE not in principal.scopes_in(own):
        raise forbidden(f"scope {SCOPE} required", scope=SCOPE, tenant=own)
    return own


def _out(e: Any) -> dict[str, Any]:
    return e.model_dump(mode="json")


@router.get("", response_model=list[AuditEventOut], response_model_exclude_unset=True)
async def list_audit_events(
    store: AuditStore,
    tenant: Annotated[str | None, Depends(audit_tenant)],
    since: str | None = None,
    until: str | None = None,
    action: str | None = None,
    sub: str | None = None,
    resource: str | None = None,
    via: str | None = None,
    client_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    """플랫폼 API 쓰기 기록. `via` 는 정확 일치이고, 값이 `/` 로 끝나면 접두 일치다
    (`via=assistant/` 는 비서가 한 모든 쓰기, `via=assistant/<session_id>` 는 그 세션의 것).
    `client_id` 는 MCP 클라이언트의 client_ref 와 정확 일치다(OAuth 토큰·회수 행)."""
    rows = await store.query(
        tenant=tenant,
        since=parse_ts(since, "since"),
        until=parse_ts(until, "until"),
        action=action,
        sub=sub,
        resource=resource,
        via=via,
        client_id=client_id,
        limit=limit,
    )
    return [_out(e) for e in rows]


def _mcp_out(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for k in ("ts", "collected_at"):
        if isinstance(out.get(k), datetime):
            out[k] = iso_utc(out[k])
    out["backend"] = backend_of(out.get("metadata"))
    return out


def backend_of(metadata: Any) -> str | None:
    """vMCP 감사 이벤트의 `metadata.extra.backend_name`(그 호출이 간 Entry 이름). 다른 프록시의
    이벤트에는 없다 (P50 4절, F0 실측)."""
    extra = metadata.get("extra") if isinstance(metadata, dict) else None
    name = extra.get("backend_name") if isinstance(extra, dict) else None
    return name if isinstance(name, str) and name else None


@router.get("/mcp", response_model=list[MCPAuditEventOut], response_model_exclude_unset=True)
async def list_mcp_audit_events(
    store: AuditStore,
    tenant: Annotated[str | None, Depends(audit_tenant)],
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    sub: str | None = None,
    type: str | None = None,  # noqa: A002 - query 이름
    component: str | None = None,
    outcome: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    rows = await store.query_mcp(
        tenant=tenant,
        since=parse_ts(since, "since"),
        until=parse_ts(until, "until"),
        tool=tool,
        sub=sub,
        type_=type,
        component=component,
        outcome=outcome,
        limit=limit,
    )
    return [_mcp_out(d) for d in rows]
