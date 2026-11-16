/**
 * Agent 채팅·세션·승인·메트릭·이벤트 핸들러 (D26, P36). 플랫폼이 배포된 런타임으로 넘기는 프록시 경로다.
 *   /agents/{id}/chat SSE, sessions(제목 검색·이름 바꾸기·지우기·상세·인용 원문·pending·decisions·다시 생성·중단), metrics, events,
 *   세션의 turns 와 traces.
 * 프록시 계약은 agent-runtime/README.md 의 SSE 표와 resource-model 5절 경로 표(docs/guide/agents.md 4절, docs/guide/chat.md)를 따른다. 대본과 세션 색인은 agent-sse.ts 에 있다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { AgentDecision, AgentEvent, AgentMetrics, AgentTrace, AgentTurn, TraceSpan } from "@/api/types";
import { RUNTIME_CANCEL, RUNTIME_CITATIONS, RUNTIME_REGENERATE, RUNTIME_SESSION_ADMIN } from "@/lib/runtimeFeatures";
import { SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitle";

import { CITATIONS_FIXTURE } from "../runtimeFixtures";
import {
  agentSessionState,
  agentSessionStore,
  cancelRunningTurn,
  chatScript,
  decisionScript,
  DETACHED_TURN_PREFIX,
  detachedTurnResponse,
  inputGuardrailBlock,
  makeTurnId,
  regenerateScript,
  sessionDetailOf,
  SLOW_TURN_PREFIX,
  slowTurnResponse,
  touchSession,
  turnInProgress,
  withReceived,
} from "./agent-sse";
import { agentStore, featureMissing } from "./agents";
import { NOW, apiError, downloadHeaders, http, record } from "./core";
import { sseResponse, type SseEvent } from "./sse";

/**
 * 플랫폼 `GET /agents/{id}/metrics` 의 모양이다 (`platform/daegam/services/telemetry/agent_metrics.py`). 런타임 몫은 지금 값이고
 * 나머지는 기간 안의 모든 레플리카를 센 값이다. 이 파일을 고칠 때는 서버 응답(`AgentMetricsOut`)을 먼저 보고 맞춘다.
 */
export const agentMetricsFixture: AgentMetrics = {
  window: "1h",
  sessions: { active: 2, pending: 1, closed: 10 },
  totals: { sessions: 13, turns: 40, input_tokens: 12000, output_tokens: 3000 },
  daily_tokens: null,
  tools: { count: 2, loaded_at: 1789788966.5954986, last_error: null, protocols: { "tools-calculator": "2026-07-28" } },
  warnings: ["no_shared_state"],
  runtime: { features: ["cancel"] },
  chat: null,
  runtime_error: null,
  turns: { done: 38, pending: 0, turn_failed: 0, turn_timeout: 1, quota_exhausted: 0, budget_exceeded: 0, cancelled: 1 },
  rejected: { total: 2, by_code: { rate_limited: 2 } },
  errors: { turns: 1, error_rate: 0.0256 },
  latency_ms: { avg: 850, p50: 700, p95: 2100, max: 4000 },
  model: { calls: 45, errors: 1, p95_ms: 1800, retries: 1, fallbacks: 0, by_model: [{ model: "gpt-4o", calls: 45, errors: 1, p95_ms: 1800 }] },
  tool_calls: { calls: 57, errors: 2, p95_ms: 120, by_tool: [{ tool: "Add", calls: 57, errors: 2, p95_ms: 120 }] },
  usage: { input_tokens: 12000, output_tokens: 3000, cache_read_tokens: 0, cache_write_tokens: 0, reasoning_tokens: 0, requests: 0 },
  cost: { usd: 0.42, unpriced_calls: 0 },
  cost_daily: [{ day: "2026-10-20T00:00:00Z", cost_usd: 0.42, calls: 45, input_tokens: 12000, output_tokens: 3000 }],
  telemetry: {
    mode: "push",
    pods: [{ name: "agent-support-7d9c5f8b6-x2k4q", uid: "uid-1", last_at: NOW, received: 120, duplicates: 0, refused: 0, dropped: { cost: 0, other: 0 } }],
    dropped: { cost: 0, other: 0 },
    image_pushes: true,
  },
};

export const TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";
export const RESUMED_TRACE_ID = "0af7651916cd43dd8448eb211c80319c";

/** 세션 s-1 의 턴 t-1. 승인 대기로 끝난 실행과 3분 12초 뒤 재개한 실행이다. */
export const agentTurnsFixture: AgentTurn[] = [
  {
    turn_id: "t-1",
    turn: 1,
    regenerated: false,
    runs: [
      { trace_id: TRACE_ID, status: "pending", start: "2026-10-20T01:00:00Z", duration_ms: 3000, resumed: false },
      { trace_id: RESUMED_TRACE_ID, status: "done", start: "2026-10-20T01:03:15Z", duration_ms: 1200, resumed: true },
    ],
    usage: { input_tokens: 1400, output_tokens: 120 },
    cost_usd: 0.0123,
  },
];

/** 실행 하나의 트레이스. 실행 아래 모델 호출 둘(첫 호출 실패 뒤 재시도)과 Tool 하나다. */
export function traceFixture(traceId: string): AgentTrace {
  const run: TraceSpan = { span_id: "r1", parent_span_id: null, kind: "run", name: "turn 1", start: "2026-10-20T01:00:00Z", duration_ms: 3000, status: "pending", error: false, attributes: { usage: { input_tokens: 700 } }, cost_usd: null, ledger: null, body: null };
  return {
    trace_id: traceId,
    session_id: "s-1",
    turn_id: "t-1",
    user_sub: "header-admin",
    preview_id: null,
    root: run,
    spans: [
      run,
      { span_id: "m1", parent_span_id: "r1", kind: "model", name: "gpt-4o", start: "2026-10-20T01:00:00.100Z", duration_ms: 400, status: "error", error: true, attributes: { attempt: 1 }, cost_usd: null, ledger: null, body: null },
      { span_id: "m2", parent_span_id: "r1", kind: "model", name: "gpt-4o", start: "2026-10-20T01:00:01Z", duration_ms: 900, status: "ok", error: false, attributes: { attempt: 2, route: "gateway" }, cost_usd: 0.0061, ledger: { writer: "gateway", route: "gateway", usage: { input_tokens: 700, cache_read_tokens: 200 }, estimated: false }, body: null },
      { span_id: "c1", parent_span_id: "m2", kind: "tool", name: "Add", start: "2026-10-20T01:00:02Z", duration_ms: 50, status: "success", error: false, attributes: { call_id: "c1" }, cost_usd: null, ledger: null, body: { content: "<b>7</b>" } },
    ],
    points: [{ event: "model_retry", ts: "2026-10-20T01:00:00.600Z", span_id: "r1", attributes: { attempt: 1 }, body: null }],
    totals: { duration_ms: 3000, usage: { input_tokens: 700 }, cost_usd: 0.0061, calls: 1, unpriced_calls: 0 },
    truncated: false,
    truncated_by: null,
    limit: 1000,
  };
}

/**
 * 수집기가 담은 모양 그대로다. 갈래는 `event`, 주체는 `user_sub`, 턴 본문은 `data` 안이다
 * (플랫폼 `AgentEventOut`). 전에는 런타임 stdout 쪽 이름을 적고 있어 진짜 클러스터에서 표의
 * 갈래·주체·토큰 칸이 늘 비었는데도 테스트는 통과했다.
 */
/** 이벤트 한 줄. 서버는 모든 칸을 싣고 없는 값은 null 이다. */
function eventRow(row: Pick<AgentEvent, "id" | "event" | "ts" | "data"> & Partial<AgentEvent>): AgentEvent {
  const empty = { source: null, tenant: null, instance_id: null, preview_id: null, cluster_id: null, namespace: null, pod: null, session_id: null, user_sub: null, turn: null, turn_id: null, trace_id: null, span_id: null, parent_span_id: null, start: null, duration_ms: null, tool: null, status: null, decided_by: null, rejected_code: null, profile: null, instance: null, body: null, collected_at: null };
  return { ...empty, ...row };
}

export const agentEventsFixture: AgentEvent[] = [
  eventRow({ id: "ev-1", event: "turn_end", ts: NOW, session_id: "s-1", turn: 3, status: "done", user_sub: "header-admin", data: { usage: { input_tokens: 400, output_tokens: 100, total_tokens: 500 }, latency_ms: 820, tool_calls: 1 } }),
  eventRow({ id: "ev-2", event: "interrupt", ts: NOW, session_id: "s-2", turn: 1, tool: "Divide", user_sub: "dev@example.com", data: {} }),
  eventRow({ id: "ev-3", event: "tool_exec", ts: NOW, session_id: "s-1", turn: 3, tool: "Add", status: "success", data: {}, body: { content: "7" } }),
  eventRow({ id: "ev-4", event: "runtime_start", ts: NOW, data: {} }),
];

/** 핸들러가 답하는 몸. 미리 보기 아래의 같은 이름 라우트(agent-previews.ts)가 열린 미리 보기를 확인한 뒤 같은 몸을 쓴다. */
export type ChatResolver = Parameters<typeof http.post>[1];

/** 채팅 한 턴 (SSE). */
export const chatTurn: ChatResolver = async ({ request, params }) => {
  const r = await record(request);
  const a = agentStore.findOr404(params.id);
  if (a instanceof Response) return a;
  const body = r.body as { session_id?: string | null; message: string; attachments?: string[] };
  if (body.message.length > 4000) return apiError(413, "max_input_chars", "message exceeds max_input_chars (4000)");
  if (body.message === "busy") return apiError(409, "turn_in_progress", "a turn is already running in this session");
  if (body.message === "limit") return HttpResponse.json({ detail: { code: "rate_limited", detail: "per_user_per_minute exceeded", retry_after: "30" } }, { status: 429, headers: { "Retry-After": "30" } });
  const blocked = inputGuardrailBlock(body.message);
  if (blocked) return blocked;
  const sessionId = body.session_id || "s-new";
  const s = touchSession(sessionId, "active", "header-admin", body.message);
  // 새 세션의 첫 이벤트는 배포된 입구의 인사말이다(런타임 routes 의 _greeting_event). 이어지는 턴에는 없다 (D65)
  const greeting: SseEvent[] = !body.session_id && a.status?.greeting ? [{ event: "greeting", data: { session_id: sessionId, text: a.status.greeting } }] : [];
  if (body.message.startsWith(SLOW_TURN_PREFIX)) return slowTurnResponse(sessionId, makeTurnId(sessionId, s.turns), greeting);
  if (body.message.startsWith(DETACHED_TURN_PREFIX)) return detachedTurnResponse(sessionId, makeTurnId(sessionId, s.turns), greeting);
  const script = withReceived(chatScript(sessionId, body.message, s.turns), body);
  if (script.some((e) => e.event === "interrupt")) s.status = "pending";
  return sseResponse([...greeting, ...script]);
};

/** 세션 상세. view=progress 는 도는 턴을 따라가는 가벼운 읽기다. 메시지 요약 없이 다시 읽을 간격을 싣는다 (P78 2절). */
export const sessionDetail: ChatResolver = async ({ request, params }) => {
  const r = await record(request);
  const s = agentSessionStore.findOr404(params.sid);
  if (s instanceof Response) return s;
  const greeting = agentStore.find(params.id)?.status?.greeting;
  const detail = sessionDetailOf(s, agentSessionState.pending[s.session_id], greeting);
  if (new URL(r.path, "http://localhost").searchParams.get("view") === "progress") {
    return HttpResponse.json({ ...detail, messages: [], pending: detail.pending && !s.running, poll_seconds: 3 });
  }
  return HttpResponse.json(detail);
};

/** 보류 승인 상세. */
export const sessionPending: ChatResolver = async ({ request, params }) => {
  await record(request);
  const p = agentSessionState.pending[String(params.sid)];
  return p ? HttpResponse.json(p) : apiError(404, "not_pending", `session ${String(params.sid)} has no pending approval`);
};

/** 보류 결정과 그 재개 스트림 (SSE). */
export const sessionDecisions: ChatResolver = async ({ request, params }) => {
  const r = await record(request);
  const sid = String(params.sid);
  const pending = agentSessionState.pending[sid];
  if (!pending) return apiError(409, "not_pending", `session ${sid} has no pending approval`);
  const body = r.body as { decisions: AgentDecision[] };
  if (!Array.isArray(body.decisions) || body.decisions.length !== pending.action_requests.length) {
    return apiError(422, "decision_count", `expected ${pending.action_requests.length} decision(s), got ${body.decisions?.length ?? 0}`);
  }
  delete agentSessionState.pending[sid];
  const s = touchSession(sid, "active", "header-admin");
  return sseResponse(decisionScript(sid, body.decisions, s.turns));
};

export const agentChatHandlers: HttpHandler[] = [
  // -- 채팅 (SSE). 오류 코드는 런타임 README 의 /chat 검사 항목이다
  http.post("/agents/:id/chat", chatTurn),
  // 마지막 턴 다시 생성 (P36). 기록에 regenerate 가 없으면 플랫폼이 409 다. 고친 글은 입구 검사를 다시 지난다.
  // "busy" 면 진행 중인 턴이 있다는 409, 4111 이 들면 입력 가드레일의 422 다
  http.post("/agents/:id/sessions/:sid/regenerate", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const missing = featureMissing(a, RUNTIME_REGENERATE);
    if (missing) return missing;
    const sid = String(params.sid);
    const s = agentSessionStore.findOr404(sid);
    if (s instanceof Response) return s;
    if (agentSessionState.pending[sid]) return apiError(409, "session_pending", "the session is waiting for a decision");
    const body = r.body as { message?: string | null };
    if (body.message === "busy") return apiError(409, "turn_in_progress", "a turn is already running in this session");
    const blocked = inputGuardrailBlock(body.message);
    if (blocked) return blocked;
    return sseResponse(regenerateScript(sid, Math.max(s.turns, 1), body.message));
  }),
  // 진행 중 턴 멈추기 (D65). 기록에 cancel 이 없으면 플랫폼이 런타임을 부르지 않고 409 다. 본문의 turn_id 가 다른 시도를
  // 가리키면 아무것도 멈추지 않는다 (P78 2절)
  http.post("/agents/:id/sessions/:sid/cancel", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const missing = featureMissing(a, RUNTIME_CANCEL);
    if (missing) return missing;
    const turnId = (r.body as { turn_id?: string | null } | null)?.turn_id;
    if (!cancelRunningTurn(String(params.sid), turnId)) return apiError(409, "no_turn_in_progress", "no turn is running in this session");
    return HttpResponse.json({ session_id: String(params.sid) }, { status: 202 });
  }),

  // -- 세션·승인. 제목 검색(q)과 이름 바꾸기·지우기는 기록에 session_admin 이 있어야 한다(P36). 없으면 플랫폼이 409 다
  http.get("/agents/:id/sessions", async ({ request, params }) => {
    const r = await record(request);
    const query = new URL(r.path, "http://localhost").searchParams;
    const status = query.get("status");
    const q = query.get("q");
    const missing = q ? featureMissing(agentStore.find(params.id), RUNTIME_SESSION_ADMIN) : null;
    if (missing) return missing;
    // 자동 실행의 세션(run 표시가 있는 줄)은 runs=include·only 일 때만 온다 (P41 7절). only 는 그것만이다
    const runs = query.get("runs");
    const items = agentSessionState.items.filter(
      (s) =>
        (!status || s.status === status) &&
        (!q || (s.title ?? "").toLowerCase().includes(q.toLowerCase())) &&
        (runs === "only" ? Boolean(s.run) : runs === "include" || !s.run),
    );
    // 서버처럼 찾은 글을 돌려준다
    return HttpResponse.json({ sessions: items, ...(q ? { q } : {}) });
  }),
  http.patch("/agents/:id/sessions/:sid", async ({ request, params }) => {
    const r = await record(request);
    const missing = featureMissing(agentStore.find(params.id), RUNTIME_SESSION_ADMIN);
    if (missing) return missing;
    const s = agentSessionStore.findOr404(params.sid);
    if (s instanceof Response) return s;
    const title = String((r.body as { title?: string }).title ?? "").trim();
    if (!title || title.length > SESSION_TITLE_MAX_CHARS) return apiError(422, "invalid_title", "title must be 1 to 100 characters after cleaning");
    s.title = title;
    return HttpResponse.json({ session: s });
  }),
  http.delete("/agents/:id/sessions/:sid", async ({ request, params }) => {
    await record(request);
    const missing = featureMissing(agentStore.find(params.id), RUNTIME_SESSION_ADMIN);
    if (missing) return missing;
    const sid = String(params.sid);
    const s = agentSessionStore.findOr404(sid);
    if (s instanceof Response) return s;
    if (turnInProgress(sid)) return apiError(409, "turn_in_progress", "a turn is already running in this session");
    agentSessionStore.remove(sid);
    delete agentSessionState.pending[sid];
    return HttpResponse.json({ session_id: sid, deleted: true });
  }),
  http.get("/agents/:id/sessions/:sid", sessionDetail),
  // 인용 원문 (P36 7절). 기록에 citations 가 없으면 409 다. 표본의 출처 가운데 URL 이 없는 올린 문서만 원문이 있고 그 밖은
  // 404 citation_not_found 다. 가림 가드레일의 403 은 그 경우를 보는 테스트가 server.use 로 덮는다
  http.get("/agents/:id/sessions/:sid/citations/:n/source", async ({ request, params }) => {
    await record(request);
    const missing = featureMissing(agentStore.find(params.id), RUNTIME_CITATIONS);
    if (missing) return missing;
    const cited = CITATIONS_FIXTURE.events.citations.items.find((c) => c.n === Number(params.n) && !c.url);
    if (!cited) return apiError(404, "citation_not_found", `citation ${String(params.n)} has no source document`);
    return new HttpResponse(`${cited.title} 원문`, { headers: downloadHeaders(cited.title ?? "source") });
  }),
  http.get("/agents/:id/sessions/:sid/pending", sessionPending),
  http.post("/agents/:id/sessions/:sid/decisions", sessionDecisions),
  http.get("/agents/:id/metrics", async ({ request }) => {
    await record(request);
    return HttpResponse.json(agentMetricsFixture);
  }),
  http.get("/agents/:id/events", async ({ request }) => {
    await record(request);
    return HttpResponse.json(agentEventsFixture);
  }),
  http.get("/agents/:id/sessions/:sid/turns", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ turns: agentTurnsFixture });
  }),
  http.get("/agents/:id/traces/:traceId", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json(traceFixture(String(params.traceId)));
  }),
];
