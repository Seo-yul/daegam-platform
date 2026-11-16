/**
 * MCP Server·Gateway 의 메트릭 (P51 4절): /mcp-servers/{id}/metrics, /gateways/{id}/metrics. 모양은 플랫폼의
 * `platform/daegam/services/mcp_metrics/reports.py` 가 내는 답이다. 단계는 호출이 없어도 0 으로 오고, 합계·단계·표의 수는
 * Tool 호출만 센다. 오류는 success 가 아닌 다섯 갈래의 합이다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { GatewayMetrics, McpMetrics, McpMetricsWindow, McpOutcomes } from "@/api/types";

import { NOW, http, record } from "./core";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

export function mcpOutcomes(over: Partial<McpOutcomes> = {}): McpOutcomes {
  return { success: 0, application_error: 0, denied: 0, failure: 0, error: 0, unknown: 0, ...over };
}

/** 마지막 세 단계에만 호출이 있는 기간. 24시간은 시간마다, 7일·30일은 날마다다. */
function series(window: McpMetricsWindow): McpMetrics["series"] {
  const hourly = window === "24h";
  const steps = hourly ? 24 : window === "7d" ? 7 : 30;
  const width = hourly ? HOUR : DAY;
  const last = hourly ? Date.parse(NOW) : Date.parse("2026-09-02T00:00:00Z");
  const busy = [mcpOutcomes({ success: 4, denied: 1 }), mcpOutcomes({ success: 2, application_error: 1 }), mcpOutcomes({ success: 3, error: 1 })];
  return Array.from({ length: steps }, (_, i) => {
    const counts = busy[i - (steps - busy.length)] ?? mcpOutcomes();
    const calls = Object.values(counts).reduce((s, n) => s + n, 0);
    return { start: new Date(last - (steps - 1 - i) * width).toISOString(), calls, outcomes: counts };
  });
}

/** 배포된 MCP Server 의 기간 하나. 그 밖의 Tool 줄(tool null)이 있다. */
export function mcpMetricsFixture(window: McpMetricsWindow = "24h", over: Partial<McpMetrics> = {}): McpMetrics {
  const steps = series(window);
  return {
    window,
    since: steps[0].start,
    until: NOW,
    step: window === "24h" ? "hour" : "day",
    totals: {
      calls: 12,
      errors: 3,
      outcomes: mcpOutcomes({ success: 9, application_error: 1, denied: 1, error: 1 }),
      resource_reads: 0,
      prompt_gets: 0,
      duration_ms: { avg: 42, p50: 25, p95: 180, max: 900 },
    },
    series: steps,
    by_tool: [
      { tool: "CreateOrder", calls: 8, errors: 2, outcomes: mcpOutcomes({ success: 6, denied: 1, error: 1 }), p95_ms: 200, max_ms: 900 },
      { tool: "GetOrder", calls: 3, errors: 0, outcomes: mcpOutcomes({ success: 3 }), p95_ms: 40, max_ms: 60 },
      { tool: null, calls: 1, errors: 1, outcomes: mcpOutcomes({ application_error: 1 }), p95_ms: 12, max_ms: 12 },
    ],
    source: { audit: "on", collector: "on", rolled_up_at: NOW, counted_from: null },
    ...over,
  };
}

/** Gateway 의 기간 하나. 지금 배포의 백엔드, 스냅샷에 없는 Entry, 백엔드를 정하지 못한 호출이 한 줄씩이고 셋을 더하면 합계다. */
export function gatewayMetricsFixture(window: McpMetricsWindow = "24h", over: Partial<GatewayMetrics> = {}): GatewayMetrics {
  return {
    ...mcpMetricsFixture(window),
    by_backend: [
      { entry: "support-calc", hub_item_id: "hub-calc", name: "calc", calls: 7, errors: 1, outcomes: mcpOutcomes({ success: 6, denied: 1 }), p95_ms: 150 },
      { entry: "support-orders", hub_item_id: null, name: null, calls: 4, errors: 1, outcomes: mcpOutcomes({ success: 3, error: 1 }), p95_ms: 220 },
      { entry: null, hub_item_id: null, name: null, calls: 1, errors: 1, outcomes: mcpOutcomes({ application_error: 1 }), p95_ms: 12 },
    ],
    ...over,
  };
}

function windowOf(request: Request): McpMetricsWindow {
  const raw = new URL(request.url).searchParams.get("window");
  return raw === "7d" || raw === "30d" ? raw : "24h";
}

export const mcpMetricsHandlers: HttpHandler[] = [
  http.get("/mcp-servers/:id/metrics", async ({ request }) => {
    await record(request);
    return HttpResponse.json(mcpMetricsFixture(windowOf(request)));
  }),
  http.get("/gateways/:id/metrics", async ({ request }) => {
    await record(request);
    return HttpResponse.json(gatewayMetricsFixture(windowOf(request)));
  }),
];
