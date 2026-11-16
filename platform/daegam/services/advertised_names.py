"""노출 이름 (P39b 13·14절). 서버나 Gateway 가 광고하는 도구 이름과 그 이름이 백엔드에서
무엇이었는지를 한 곳에서 계산한다. 순수 함수만 있다.

MCP Server 는 Backend API 의 operationId 를 필터와 이름 오버라이드로 노출하고, Gateway 는 백엔드가
광고하는 이름을 우선순위의 새 이름이나 `<workload>_<tool>` 로 다시 노출한다. 허브 도구 이름(D49),
충돌 계획, 도구 효과(`services/tool_effects.py`)가 모두 이 규칙을 읽으므로 여기 한 벌만 둔다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from daegam.services.gateway_plan import PREFIX_WARNING, priority_unknown_warning
from daegam.services.toolnames import fastmcp_default_name
from daegam.store.backend_apis import BackendAPIVersion
from daegam.store.gateways import Gateway, GatewayBackendSnapshot
from daegam.store.mcp_servers import BackendSelection, MCPServerRevision


@dataclass(frozen=True)
class ExposedTool:
    """노출된 도구 하나. backend 는 MCP Server 에서 backend_profile_id, Gateway 에서 workload 다.
    original 은 그 백엔드 안의 이름으로, MCP Server 에서 operationId, Gateway 에서 백엔드가 광고하는
    이름이다."""

    backend: str
    original: str
    exposed: str


def exposed_pairs(sel: BackendSelection, available: Sequence[str]) -> list[tuple[str, str]]:
    """MCP Server 백엔드 하나의 (노출 이름, operationId)다. 필터가 비면 available 전부다. 이름
    오버라이드가 없는 도구는 FastMCP 가 주는 이름이다(`fastmcp_default_name`)."""
    out: list[tuple[str, str]] = []
    for op in sel.tools or available:
        ov = sel.tool_overrides.get(op)
        out.append((ov.name if ov is not None and ov.name else fastmcp_default_name(op), op))
    return out


def server_tools(
    rev: MCPServerRevision, versions: Mapping[tuple[str, int], BackendAPIVersion]
) -> list[ExposedTool]:
    """리비전과 미리 읽어 둔 버전들로 MCP Server 의 노출 도구를 만든다(`exposed_pairs`). 필터가
    비고 버전을 못 찾은 백엔드는 내놓지 않는다."""
    out: list[ExposedTool] = []
    for sel in rev.backends:
        version = versions.get((sel.backend_profile_id, sel.spec_version))
        pairs = exposed_pairs(sel, version.tool_names if version else [])
        out.extend(ExposedTool(sel.backend_profile_id, op, name) for name, op in pairs)
    return out


def advertised_tools_of(
    rev: MCPServerRevision, versions: Mapping[tuple[str, int], BackendAPIVersion]
) -> list[str]:
    """MCP Server 의 노출 이름. 허브 항목 여럿의 이름을 한 번에 계산하는 자리(D49)가 버전을 일괄로
    읽고 부른다."""
    return [t.exposed for t in server_tools(rev, versions)]


def free_name(name: str, taken: set[str], *, sep: str = "_") -> str:
    """taken 에 없는 이름. name 이 쓰였으면 `_2`, `_3` 을 붙여 비는 것을 찾는다. sep 은 그 사이에
    두는 글자다(Agent 간선 id 는 캔버스처럼 `-` 다)."""
    if name not in taken:
        return name
    n = 2
    while f"{name}{sep}{n}" in taken:
        n += 1
    return f"{name}{sep}{n}"


def plan_conflicts(
    snapshots: list[GatewayBackendSnapshot], mode: str = "auto"
) -> tuple[str, dict[str, dict[str, str]], list[str]]:
    """(실제 conflict_resolution, {workload: {tool: 새 이름}}, warnings).

    auto: 모든 백엔드의 tool_names 를 알면 priority, 하나라도 모르면 prefix.
    priority: 접두사 없이 backends 순으로 우선하고 둘 이상이 노출하는 툴만 `<item>_<tool>` 로
      바꾼다. 바꾼 이름이 이미 광고되는 이름이거나 그 백엔드의 툴 이름(고르지 않은 것까지)이면 `_2`
      부터 수를 붙인다 (P50 6절). vMCP 가 바꾼 뒤의 이름으로도 필터를 맞춰 보기 때문이다.
      tool_names 를 모르는 백엔드는 override 를 만들 수 없어 ToolHive 가 그 충돌 툴을 조용히
      버린다 (첫 워크로드 승리). 그 경우 경고한다.
    prefix: ToolHive 가 `<workload>_<tool>` 로 이름을 바꾼다. override 없음."""
    unknown = [(b.hub_item_name, b.kind) for b in snapshots if not b.tool_names]
    if mode == "prefix" or (mode == "auto" and (not snapshots or unknown)):
        warnings = [PREFIX_WARNING] if mode == "auto" and snapshots else []
        return "prefix", {}, warnings
    exposed = {b.workload: b.exposed_names() for b in snapshots}
    counts = Counter(t for names in exposed.values() for t in set(names))
    # 바꾸지 않고 광고하는 이름과 이미 정한 새 이름
    taken = {t for names in exposed.values() for t in names if counts[t] == 1}
    overrides: dict[str, dict[str, str]] = {}
    for b in snapshots:
        clash: dict[str, str] = {}
        for t in exposed[b.workload]:
            if counts[t] > 1:
                clash[t] = free_name(f"{b.hub_item_name}_{t}", taken | set(b.tool_names))
                taken.add(clash[t])
        if clash:
            overrides[b.workload] = clash
    warnings = []
    if unknown:
        warnings.append(priority_unknown_warning(unknown))
    return "priority", overrides, warnings


def gateway_exposed_name(
    workload: str,
    tool: str,
    resolution: str | None,
    overrides: Mapping[str, Mapping[str, str]],
) -> str:
    """Gateway 가 백엔드 workload 의 도구 tool 을 광고하는 이름. prefix 는 `<workload>_<tool>`,
    priority 는 충돌 override 가 있으면 그것이다."""
    if resolution == "prefix":
        return f"{workload}_{tool}"
    return (overrides.get(workload) or {}).get(tool, tool)


def gateway_tools(
    backends: list[GatewayBackendSnapshot],
    resolution: str | None,
    overrides: Mapping[str, Mapping[str, str]],
) -> list[ExposedTool]:
    """Gateway 스냅샷이 광고하는 도구. priority 는 필터를 지난 이름에 충돌 override 를, prefix 는
    `<workload>_<tool>` 을 준다. tool_names 를 모르는 백엔드는 필터를 그대로 광고 후보로 둔다. 같은
    노출 이름이 둘이면 둘 다 내놓는다(먼저 나온 것이 vMCP 에서 이긴다)."""
    out: list[ExposedTool] = []
    for b in backends:
        names = b.exposed_names()
        if not names and b.tools:
            names = list(b.tools)
        for t in names:
            exposed = gateway_exposed_name(b.workload, t, resolution, overrides)
            out.append(ExposedTool(b.workload, t, exposed))
    return out


def advertised_tools(doc: Gateway) -> list[str]:
    """이 문서의 vMCP 가 광고하는 툴 이름 (게시 항목의 tool_names 스냅샷). 같은 이름은 한 번만
    내놓는다."""
    out: list[str] = []
    seen: set[str] = set()
    for t in gateway_tools(
        doc.deployed_backends, doc.deployed_conflict_resolution, doc.tool_overrides
    ):
        if t.exposed not in seen:
            seen.add(t.exposed)
            out.append(t.exposed)
    return out
