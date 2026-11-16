"""MCP 메트릭 테스트가 쓰는 감사 이벤트 (P51). 롤업과 API 테스트가 함께 쓴다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from daegam.audit.store import MCP_TOOL_CALL


def mcp_call_event(n: int | str, ts: datetime, instance: str = "s1", **kw: Any) -> dict[str, Any]:
    """저장된 감사 이벤트 하나. 기본은 테넌트 t1 의 MCP Server 프록시가 남긴 성공한 Add 호출이다.
    d 는 걸린 시간(ms, None 이면 없음)이고 backend 는 vMCP 의 `backend_name` 이다."""
    duration = kw.pop("d", 10)
    backend = kw.pop("backend", None)
    extra: dict[str, Any] = {} if duration is None else {"duration_ms": duration}
    if backend is not None:
        extra["backend_name"] = backend
    return {
        "_id": f"e{n}",
        "ts": ts,
        "type": MCP_TOOL_CALL,
        "outcome": "success",
        "tool": "Add",
        "tenant": "t1",
        "instance_id": instance,
        "proxy_kind": "mcpserver",
        "metadata": {"extra": extra},
        **kw,
    }
