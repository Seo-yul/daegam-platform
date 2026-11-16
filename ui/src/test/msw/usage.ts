/**
 * 사용량과 가격표 (P37): /usage, /admin/usage, /usage/reprice, /settings/pricing.
 * 모양은 플랫폼 테스트로 실제 응답을 떠서 맞췄다(platform/tests/test_usage_rollup.py 와 같은 표본).
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { AdminUsage, Pricing, PriceRule, RepriceStatus, UsageMetrics, UsageReport, UsageRow } from "@/api/types";

import { apiError, http, record } from "./core";

const DAY = "2026-10-20T00:00:00+00:00";

function metrics(over: Partial<UsageMetrics> = {}): UsageMetrics {
  return {
    calls: 0,
    errors: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    reasoning_tokens: 0,
    requests: 0,
    cost_usd: 0,
    unpriced_calls: 0,
    unjoined_calls: 0,
    ...over,
  };
}

function row(over: Partial<UsageRow>): UsageRow {
  return {
    ...metrics(),
    day: null,
    source_kind: null,
    source_id: null,
    user_sub: null,
    provider_kind: null,
    provider_name: null,
    model: null,
    tenant: null,
    route: null,
    name: null,
    ...over,
  };
}

/** group 마다의 줄. 사람 기준에는 잇지 못한 줄(user_sub null)이 있다. */
export const usageRows: Record<string, UsageRow[]> = {
  day: [row({ day: DAY, calls: 3, input_tokens: 3_000_000, cost_usd: 5, unpriced_calls: 1 })],
  source: [
    row({ source_kind: "agent", source_id: "ag1", name: "=faq-bot", calls: 1, input_tokens: 1_000_000, cost_usd: 2.5 }),
    row({ source_kind: "assistant", source_id: "daegam", name: "daegam", calls: 1, input_tokens: 1_000_000, cost_usd: 2.5 }),
  ],
  user: [row({ user_sub: "u1", name: "kim", calls: 2, cost_usd: 2.5 }), row({ user_sub: null, calls: 1, cost_usd: 0 })],
  tenant: [row({ tenant: "default", calls: 3, cost_usd: 5 }), row({ tenant: "acme", calls: 1, cost_usd: 2.5 })],
};

export function usageReport(group: string): UsageReport {
  const rows = usageRows[group] ?? usageRows.day;
  return {
    totals: metrics({ calls: 3, input_tokens: 3_000_000, cost_usd: 5, unpriced_calls: 1 }),
    series: [{ day: DAY, items: [{ key: ["agent", "ag1"], cost_usd: 2.5, calls: 1 }, { key: null, cost_usd: 2.5, calls: 2 }], cost_usd: 5 }],
    rows,
    truncated: false,
  };
}

export const usageState = {
  health: { gateway_missing: 0, gateway_missing_cancelled: 0, gateway_missing_failed: 0, writer_conflicts: 0, gateway_extra_records: 0, join_mismatch: 0, agents_without_telemetry: 0 },
  pricing: { version: 0, rules: [] as PriceRule[] },
  reprice: null as RepriceStatus | null,
};

export function resetUsageState(): void {
  usageState.health = { gateway_missing: 0, gateway_missing_cancelled: 0, gateway_missing_failed: 0, writer_conflicts: 0, gateway_extra_records: 0, join_mismatch: 0, agents_without_telemetry: 0 };
  usageState.pricing = { version: 0, rules: [] };
  usageState.reprice = null;
}

export const defaultPrices: Pricing["defaults"] = [
  { id: "default:openai::gpt-4o:2024-01-01", provider_kind: "openai", provider_name: null, model: "gpt-4o", input: 2.5, output: 10, cache_read: 1.25, cache_write: null, request: null, effective_from: "2024-01-01", note: null, source: "https://openai.com/api/pricing/", checked_at: "2026-06-01" },
  { id: "default:anthropic::claude-sonnet-4*:2024-01-01", provider_kind: "anthropic", provider_name: null, model: "claude-sonnet-4*", input: 3, output: 15, cache_read: 0.3, cache_write: 3.75, request: null, effective_from: "2024-01-01", note: null, source: "https://www.anthropic.com/pricing#api", checked_at: "2026-06-01" },
  { id: "default:web_search::brave:2024-01-01", provider_kind: "web_search", provider_name: null, model: "brave", input: 0, output: 0, cache_read: null, cache_write: null, request: 0.005, effective_from: "2024-01-01", note: null, source: "https://brave.com/search/api/", checked_at: "2026-06-01" },
  { id: "default:anthropic::claude-haiku-5-5*:2024-01-01", provider_kind: "anthropic", provider_name: null, model: "claude-haiku-5-5*", input: 0.1, output: 0.5, cache_read: 0.01, cache_write: 0.125, request: null, effective_from: "2024-01-01", note: "a request over the tier's input threshold (cache reads and writes included) pays the tier's prices", source: "https://platform.claude.com/docs/en/about-claude/pricing", checked_at: "2026-11-16", tier: { above_input_tokens: 100000, input: 0.5, output: 2.5, cache_read: 0.05, cache_write: 0.625 } },
];

function pricing(): Pricing {
  return { version: usageState.pricing.version, rules: usageState.pricing.rules, defaults: defaultPrices, updated_at: null, updated_by: null };
}

export const usageHandlers: HttpHandler[] = [
  http.get("/usage/reprice", async ({ request }) => {
    await record(request);
    const s = usageState.reprice;
    if (s === null) return apiError(404, "not_found", "no reprice has been requested");
    // 두 번째로 읽을 때 끝난다
    usageState.reprice = { ...s, done: s.done + s.left, left: 0, finished_at: DAY, eta_seconds: 0 };
    return HttpResponse.json(s);
  }),
  http.post("/usage/reprice", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { since: string; until: string };
    if (usageState.reprice && !usageState.reprice.finished_at) return apiError(409, "reprice_running", "a reprice is still running");
    usageState.reprice = { since: body.since, until: body.until, done: 0, left: 4, started_at: DAY, finished_at: null, eta_seconds: null };
    return HttpResponse.json(usageState.reprice, { status: 202 });
  }),
  http.get("/usage", async ({ request }) => {
    const r = await record(request);
    const group = new URL(r.path, "http://localhost").searchParams.get("group") ?? "day";
    return HttpResponse.json(usageReport(group));
  }),
  http.get("/admin/usage", async ({ request }) => {
    const r = await record(request);
    const group = new URL(r.path, "http://localhost").searchParams.get("group") ?? "day";
    const body: AdminUsage = {
      ...usageReport(group),
      unpriced: [
        { provider_kind: "openai", provider_name: "p", model: "mystery-model", calls: 1, input_tokens: 1_000_000, output_tokens: 0 },
        { provider_kind: "web_search", provider_name: "brave", model: "brave", calls: 2, input_tokens: 0, output_tokens: 0 },
      ],
      health: { ...usageState.health, collection_bytes: { usage_records: 2409, usage_hourly: 1879, usage_joins: 0, usage_anomalies: 0 }, tick_at: DAY },
    };
    return HttpResponse.json(body);
  }),
  http.get("/settings/pricing", async ({ request }) => {
    await record(request);
    return HttpResponse.json(pricing());
  }),
  http.put("/settings/pricing", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { expected_version: number; rules: PriceRule[] };
    if (body.expected_version !== usageState.pricing.version) return apiError(409, "revision_conflict", "price table was saved by another request; reload and retry save");
    usageState.pricing = { version: body.expected_version + 1, rules: body.rules.map((rule, i) => ({ ...rule, id: rule.id ?? `rule-${i}` })) };
    return HttpResponse.json(pricing());
  }),
];
