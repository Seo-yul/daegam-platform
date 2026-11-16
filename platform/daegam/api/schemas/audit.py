"""감사 조회 응답 스키마 (P6 F5)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from daegam.api.schemas import REQUIRED_DEFAULTS
from daegam.audit.store import AuditEvent


class AuditEventOut(AuditEvent):
    """플랫폼 API 쓰기 요청 기록 (audit_events). store 문서를 그대로 내보낸다."""

    model_config = REQUIRED_DEFAULTS


class MCPAuditEventOut(BaseModel):
    """MCP 프록시 감사 이벤트 (mcp_audit_events). ToolHive 이벤트 필드는 그대로 둔다."""

    model_config = ConfigDict(**REQUIRED_DEFAULTS, extra="allow")

    audit_id: str
    ts: str | None = None
    type: str | None = None
    outcome: str | None = None
    component: str | None = None
    sub: str | None = None
    user: str | None = None
    client_name: str | None = None
    method: str | None = None
    tool: str | None = None
    # Gateway(vMCP) 이벤트가 부른 백엔드의 Entry 이름. metadata.extra.backend_name 에서 읽는다
    # (P50 4절)
    backend: str | None = None
    target: dict[str, Any] | None = None
    subjects: dict[str, Any] | None = None
    # 사람 대신 부른 Agent(위임 사슬의 맨 바깥 행위자 sub)와 사슬 그대로 (P53 7절). 사람은 sub 다
    act: str | None = None
    delegation: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    tenant: str | None = None
    instance_id: str | None = None
    cluster_id: str | None = None
    namespace: str | None = None
    pod: str | None = None
    proxy_kind: str | None = None
    collected_at: str | None = None
