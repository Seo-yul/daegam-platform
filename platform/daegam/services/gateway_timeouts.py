"""Gateway 백엔드의 도구 호출 타임아웃 (P50 7절, G6).

vMCP 는 백엔드(Entry)마다 `spec.config.operational.timeouts.perWorkload[<Entry 이름>]` 의 시간이
지나면 호출을 끊는다. 정하지 않으면 30초에 끊는다(0.51.2 실측). 그래서 렌더는 배포 스냅샷의
백엔드마다 타임아웃을 적는다.

- 배포 때의 값: 정의의 백엔드가 정한 값이고, 정하지 않았으면 기본값이다. 내부 항목(MCP Server)은 그
  서버의 backend 타임아웃 가운데 가장 긴 것에 5초를 더한다. 서버가 backend 호출을 먼저 끝내고 그
  오류를 돌려줄 틈이다. 나머지는 Agent 런타임의 기본 도구 타임아웃이다. 어느 쪽이든 상한은
  `TIMEOUT_MAX_SECONDS` 다.
- 값이 없는 스냅샷: 공유 문서를 읽지 않고 같은 규칙의 고정값으로 렌더한다(MCP Server 의 기본
  backend 타임아웃에 5초, 또는 Agent 런타임의 기본값).
- 타임아웃 게이트를 지나지 못한 클러스터에는 렌더하지 않는다(`rendered_timeouts`). Advanced 편집은
  그 자리를 잠근다.
"""

from __future__ import annotations

from collections.abc import Iterable

from daegam.store.base import TIMEOUT_MAX_SECONDS
from daegam.store.gateways import GatewayBackendSnapshot
from daegam.store.mcp_servers import MCP_DEFAULT_TIMEOUT_SECONDS, MCPServer

# Agent 런타임이 도구 호출에 거는 기본 타임아웃(초). agent-runtime/runtime/mcp.py 의
# DEFAULT_TOOL_TIMEOUT_SECONDS 와 같다
AGENT_TOOL_TIMEOUT_SECONDS = 60
# code mode 의 스크립트 하나가 도는 ToolHive 의 고정된 1분 (P56 6.4절)
SCRIPT_MINUTE_SECONDS = 60
# 내부 항목의 기본값이 MCP Server 의 backend 타임아웃 위에 두는 틈(초)
INTERNAL_MARGIN_SECONDS = 5


def default_timeout(kind: str, server: MCPServer | None = None) -> int:
    """정의가 정하지 않은 백엔드의 타임아웃. 내부 항목은 그 MCP Server 가 배포한 리비전(없으면 지금
    정의)의 backend 타임아웃 가운데 가장 긴 것에 틈을 더하고, 서버를 모르면 기본 backend 타임아웃에
    더한다. 나머지는 Agent 런타임의 기본값이다."""
    if kind != "internal":
        return AGENT_TOOL_TIMEOUT_SECONDS
    seconds = MCP_DEFAULT_TIMEOUT_SECONDS
    if server is not None:
        rev = server.get_revision(server.deployed_revision or 0) or server.spec
        seconds = max(b.timeout_seconds for b in rev.backends)
    return min(seconds + INTERNAL_MARGIN_SECONDS, TIMEOUT_MAX_SECONDS)


def snapshot_timeout(backend: GatewayBackendSnapshot) -> int:
    """스냅샷 백엔드가 렌더하는 타임아웃. 값이 없는 스냅샷은 공유 문서를 읽지 않는 고정값이다."""
    return backend.timeout_seconds or default_timeout(backend.kind)


def per_workload(backends: list[GatewayBackendSnapshot]) -> dict[str, str]:
    """vMCP `operational.timeouts.perWorkload`. 키는 Entry 이름이고 값은 Go duration 글이다."""
    return {b.workload: f"{snapshot_timeout(b)}s" for b in backends}


def call_timeout(backends: Iterable[GatewayBackendSnapshot]) -> str:
    """code mode 의 `toolCallTimeout` (P56 6.4절). Gateway 의 가장 긴 백엔드 타임아웃이고 스크립트
    하나가 도는 ToolHive 의 고정된 1분을 넘지 않는다. 더 크게 적어도 안쪽 호출은 그 1분 안이다."""
    seconds = max((snapshot_timeout(b) for b in backends), default=SCRIPT_MINUTE_SECONDS)
    return f"{min(seconds, SCRIPT_MINUTE_SECONDS)}s"


def rendered_timeouts(
    backends: list[GatewayBackendSnapshot], gate_passes: bool
) -> dict[str, str] | None:
    """vMCP 에 렌더할 타임아웃. 게이트를 지나면 모든 백엔드의 것이고, 지나지 못하거나 백엔드가
    없으면 None 이다."""
    return (per_workload(backends) or None) if gate_passes else None
