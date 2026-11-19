"""Gateway·MCP Server 메트릭 보고 (P51 4절, D90). 시간 묶음(`mcp_hourly`)만 읽는다.

창은 24시간(시간 단위 24칸, 마지막 칸은 지금까지), 7일과 30일(요청의 tz 로 자른 날
단위)이다. `calls`·`errors`·결과 수는 Tool 호출이라 막대를 더하면 합계와 같다. 리소스 읽기와
프롬프트 가져오기는 따로 센다. 걸린 시간의 p50·p95 는 구간 칸을 더해 어림한 값이다.

Tool 별 행은 문서가 지금 광고하는 이름만 보인다. Tool 이름은 클라이언트가 보낸 문자열이고
viewer 는 감사 기록을 읽을 수 없으므로, 나머지 이름은 접힌 묶음과 함께 `tool: null` 한 행으로
모은다. Gateway 의 백엔드별 행은 배포 스냅샷의 Entry 이름으로 허브 항목을 찾고, 스냅샷에 없는
Entry 는 이름만, 백엔드를 정하지 못한 호출(vMCP 에 없는 Tool)은 `entry: null` 행이다. 그래서
행을 더하면 합계다.

`source.audit` 는 Gateway 의 클러스터가 지금 감사 게이트를 지나지 못하면 off, 지나는데 적용한
렌더에 감사가 없었으면(렌더 번호가 audit 을 빠뜨렸거나 렌더 번호가 생기기 전) pending 이고
나머지는 on 이다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from daegam.audit.mcp_metrics import (
    OUTCOME_CLASSES,
    RANGE_EDGES,
    RANGE_FIELDS,
    MongoMcpMetricsStore,
)
from daegam.audit.store import MCP_PROMPT_GET, MCP_RESOURCE_READ, MCP_TOOL_CALL
from daegam.k8s.gates import Feature, passing_features
from daegam.k8s.labels import APP_MCPSERVER
from daegam.k8s.templates.vmcp import CODE_MODE_TOOL
from daegam.services.advertised_names import advertised_tools, advertised_tools_of
from daegam.services.data_plane_auth import KIND_GATEWAY, missed_gates
from daegam.services.mcp_metrics.rollup import counted_from
from daegam.services.mcp_servers import version_refs_of
from daegam.store.auth_reapply import rendered_epoch
from daegam.store.backend_apis import MongoBackendAPIStore
from daegam.store.base import iso_utc, naive_utc, utcnow
from daegam.store.clusters import MongoClusterStore
from daegam.store.gateways import Gateway
from daegam.store.hour_marks import HOUR, hour_of
from daegam.store.mcp_servers import MCPServer
from daegam.ttl_cache import TTLCache

WINDOW_DAYS = {"24h": None, "7d": 7, "30d": 30}
TOP_TOOLS = 50
CACHE_SECONDS = 15.0
CACHE_ENTRIES = 1000


class McpMetricsReports:
    def __init__(
        self,
        store: MongoMcpMetricsStore,
        clusters: MongoClusterStore,
        backend_apis: MongoBackendAPIStore,
        *,
        collector_enabled: bool,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.clusters = clusters
        self.backend_apis = backend_apis
        self.collector_enabled = collector_enabled
        self._cache: TTLCache[tuple[str, str, str], dict[str, Any]] = TTLCache(
            CACHE_SECONDS, clock=clock, max_entries=CACHE_ENTRIES
        )

    async def mcp_server(self, doc: MCPServer, *, window: str, tz: str) -> dict[str, Any]:
        """MCP Server 하나의 메트릭. 이름은 배포한 리비전이 광고하는 Tool 이다. 서버 자신의 프록시
        (`mcpserver`)가 남긴 것만 센다. 노출한 서버의 vMCP 앞단(`virtualmcpserver`)도 그 서버의
        이벤트로 모이지만, 앞단을 거친 호출은 서버의 프록시에도 닿으므로 한 번만 센다 (P54 11절)."""
        if (hit := self._cache.get((doc.id, window, tz))) is not None:
            return hit
        rev = doc.deployed
        names: set[str] = set()
        if rev is not None:
            versions = await self.backend_apis.get_versions(version_refs_of(rev))
            names = set(advertised_tools_of(rev, versions))
        out = await self._report(doc.id, window, tz, names, backends=None, proxy_kind=APP_MCPSERVER)
        out["source"]["audit"] = "on"
        self._cache.put((doc.id, window, tz), out)
        return out

    async def gateway(self, doc: Gateway, *, window: str, tz: str) -> dict[str, Any]:
        """Gateway 하나의 메트릭. 이름은 배포 스냅샷이 광고하는 Tool 이고 백엔드별 행이 더 있다.
        도는 렌더가 code mode 를 실었으면(`Gateway.code_mode_applied`) 스크립트 Tool 도 이름에
        든다. 스크립트 하나는 vMCP 감사에 그 이름의 호출 한 건으로 남기 때문이다(P56 6.6절). 이름이
        없으면 이름 없는 행에 든다."""
        if (hit := self._cache.get((doc.id, window, tz))) is not None:
            return hit
        names = set(advertised_tools(doc)) if doc.deployed_revision is not None else set()
        if doc.code_mode_applied:
            names.add(CODE_MODE_TOOL)
        snapshot = {b.workload: b for b in doc.deployed_backends}
        out = await self._report(doc.id, window, tz, names, backends=snapshot)
        out["source"]["audit"] = await self._gateway_audit(doc)
        self._cache.put((doc.id, window, tz), out)
        return out

    async def _gateway_audit(self, doc: Gateway) -> str:
        cluster = await self.clusters.get(doc.cluster_id) if doc.cluster_id else None
        if cluster is None:
            return "on"
        if Feature.AUDIT not in passing_features(cluster):
            return "off"
        missed = missed_gates(KIND_GATEWAY, doc.render_epoch)
        if rendered_epoch(doc.render_epoch) == 0 or (
            missed is not None and Feature.AUDIT in missed
        ):
            return "pending"
        return "on"

    async def _report(
        self,
        instance_id: str,
        window: str,
        tz: str,
        names: set[str],
        *,
        backends: dict[str, Any] | None,
        proxy_kind: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        since, steps, day_tz = window_steps(window, tz, now)
        sums = await self.store.window_sums(
            instance_id, since, now, names=sorted(names), day_tz=day_tz, proxy_kind=proxy_kind
        )
        types = {row["_id"]: row for row in sums["types"]}
        calls = types.get(MCP_TOOL_CALL)
        state = await self.store.get_state()
        out: dict[str, Any] = {
            "window": window,
            "since": iso_utc(since),
            "until": iso_utc(now),
            "step": "hour" if day_tz is None else "day",
            "totals": {
                **_counts(calls),
                "resource_reads": _calls_of(types.get(MCP_RESOURCE_READ)),
                "prompt_gets": _calls_of(types.get(MCP_PROMPT_GET)),
                "duration_ms": _durations(calls),
            },
            "series": _series(sums["series"], steps),
            "by_tool": _by_tool(sums["tools"]),
            "source": {
                "collector": "on" if self.collector_enabled else "off",
                "rolled_up_at": _iso(state.get("rolled_up_at")),
                "counted_from": _iso(counted_from(state, now)),
            },
        }
        if backends is not None:
            out["by_backend"] = _by_backend(sums["backends"], backends)
        return out


def window_steps(
    window: str, tz: str, now: datetime
) -> tuple[datetime, list[datetime], str | None]:
    """창의 시작, 단계 시작들, 날로 자를 시간대(시간 단위면 None). 날은 tz 의 자정에서 시작하되
    묶음이 시간 단위라 30분 어긋난 시간대는 그 다음 정시부터다."""
    days = WINDOW_DAYS[window]
    if days is None:
        start = hour_of(now) - 23 * HOUR
        return start, [start + i * HOUR for i in range(24)], None
    zone = ZoneInfo(tz)
    today = now.replace(tzinfo=UTC).astimezone(zone).date()
    starts = []
    for i in range(days - 1, -1, -1):
        local = datetime.combine(today - timedelta(days=i), datetime.min.time(), zone)
        starts.append(naive_utc(local))
    first = starts[0]
    since = first if first == hour_of(first) else hour_of(first) + HOUR
    return since, starts, tz


def _iso(value: datetime | None) -> str | None:
    return None if value is None else iso_utc(value)


def _calls_of(row: dict[str, Any] | None) -> int:
    return 0 if row is None else row["calls"]


def _counts(row: dict[str, Any] | None) -> dict[str, Any]:
    outcomes = {c: 0 if row is None else row[f"outcomes_{c}"] for c in OUTCOME_CLASSES}
    calls = _calls_of(row)
    return {"calls": calls, "errors": calls - outcomes["success"], "outcomes": outcomes}


def _durations(row: dict[str, Any] | None) -> dict[str, float | None]:
    if row is None or not row["duration_count"]:
        return {"avg": None, "p50": None, "p95": None, "max": None}
    return {
        "avg": round(row["duration_sum_ms"] / row["duration_count"], 1),
        "p50": percentile(row, 0.5),
        "p95": percentile(row, 0.95),
        "max": row["duration_max_ms"],
    }


def percentile(row: dict[str, Any], q: float) -> float | None:
    """구간 칸을 더한 행에서 q 분위의 걸린 시간을 어림한다. 순위가 든 구간 안을 곧게 잇는다. 열린
    마지막 구간은 가장 긴 값까지다. 구간의 위 끝은 실제로 잰 가장 긴 값보다 클 수 있어 그 값을
    넘지 않게 자른다."""
    count = row["duration_count"]
    if not count:
        return None
    longest = row["duration_max_ms"]
    rank = q * count
    seen, low = 0, 0
    highs = (*RANGE_EDGES, longest or RANGE_EDGES[-1])
    estimate = float(low)
    for high, field in zip(highs, RANGE_FIELDS, strict=True):
        n = row[f"duration_{field}"]
        if n and seen + n >= rank:
            estimate = low + (high - low) * (rank - seen) / n
            break
        seen += n
        low = high
    return round(estimate if longest is None else min(estimate, longest), 1)


def _merge(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """더한 행 여럿을 하나로. 가장 긴 값만 max 다."""
    out: dict[str, Any] | None = None
    for row in rows:
        if out is None:
            out = dict(row)
            continue
        for key, value in row.items():
            if key == "_id":
                continue
            if key == "duration_max_ms":
                out[key] = max((v for v in (out[key], value) if v is not None), default=None)
            else:
                out[key] += value
    return out


def _call_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **_counts(row),
        "p95_ms": percentile(row, 0.95),
        "max_ms": row["duration_max_ms"],
    }


def _by_tool(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool 별 행. 묶음 읽기가 광고하지 않는 이름을 이미 null 한 행으로 모았다. 호출이 많은
    TOP_TOOLS 개만 이름을 두고 나머지를 그 행에 더한다."""
    named = sorted((r for r in rows if r["_id"] is not None), key=lambda r: (-r["calls"], r["_id"]))
    rest = _merge([*named[TOP_TOOLS:], *(r for r in rows if r["_id"] is None)])
    out = [{"tool": r["_id"], **_call_row(r)} for r in named[:TOP_TOOLS]]
    if rest is not None:
        out.append({"tool": None, **_call_row(rest)})
    return out


def _by_backend(rows: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in sorted(rows, key=lambda r: (-r["calls"], r["_id"] or "")):
        entry = row["_id"]
        backend = snapshot.get(entry) if entry is not None else None
        out.append(
            {
                "entry": entry,
                "hub_item_id": None if backend is None else backend.hub_item_id,
                "name": None if backend is None else backend.hub_item_name,
                **_counts(row),
                "p95_ms": percentile(row, 0.95),
            }
        )
    return out


def _series(rows: list[dict[str, Any]], steps: list[datetime]) -> list[dict[str, Any]]:
    """단계마다의 Tool 호출. 호출이 없는 단계도 0 으로 둔다."""
    by_start = {row["_id"]: row for row in rows}
    out = []
    for start in steps:
        counts = _counts(by_start.get(start))
        out.append(
            {"start": iso_utc(start), "calls": counts["calls"], "outcomes": counts["outcomes"]}
        )
    return out
