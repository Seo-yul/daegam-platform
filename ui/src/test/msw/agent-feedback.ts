/**
 * 답변 피드백 핸들러 (P36 6절). 플랫폼 `api/agent_feedback.py` 의 다섯 라우트를 흉내 낸다. 평가는 `agentFeedbackState.items`
 * 에 쌓이고, 세션 평가 읽기는 `agentFeedbackState.retentionDays` 를 설치의 보존 기간으로 싣는다(기본은 플랫폼 기본값).
 *
 * 턴 확인은 런타임 전사 대신 목의 턴 id 규칙(agent-sse.ts 의 `turnIdPrefix`)으로 한다. 그 세션의 턴이 아니면 404 turn_not_found
 * 다. 목록은 새것부터이고 서버처럼 만든 시각과 id 를 이은 `next_before` 로 쪽을 넘기며, 내보내기는 서버처럼 사람 식별자 없는
 * NDJSON 을 내려받기 헤더로 낸다.
 */
import type { HttpHandler } from "msw";

import type { AgentFeedback, AgentFeedbackBody, AgentFeedbackList, AgentSessionFeedback } from "@/api/types";
import { FEEDBACK_RETENTION_DEFAULT_DAYS } from "@/lib/feedback";

import { agentSessionStore, turnIdPrefix } from "./agent-sse";
import { agentStore } from "./agents";
import { meAdmin } from "./auth";
import { HttpResponse, NOW, apiError, downloadHeaders, http, record } from "./core";

const PAGE_DEFAULT = 100;
/** 쪽 넘김 표지에서 만든 시각과 id 를 잇는 글자다(서버 `services/agent_feedback.py` 의 PAGE_TOKEN_SEP). */
const PAGE_TOKEN_SEP = "_";

export const agentFeedbackState: { items: AgentFeedback[]; retentionDays: number } = { items: [], retentionDays: FEEDBACK_RETENTION_DEFAULT_DAYS };

export function resetAgentFeedbackState(items: AgentFeedback[] = [], retentionDays = FEEDBACK_RETENTION_DEFAULT_DAYS): void {
  agentFeedbackState.items = items;
  agentFeedbackState.retentionDays = retentionDays;
}

/** 목록·탭 테스트가 쓰는 평가 한 건이다. */
export function feedbackFixture(over: Partial<AgentFeedback> = {}): AgentFeedback {
  return {
    id: "fb-1",
    tenant: "default",
    agent_id: "ag-support",
    session_id: "s-1",
    turn_id: `${turnIdPrefix("s-1")}1`,
    turn: 1,
    user_sub: "dev@example.com",
    rating: "down",
    tags: ["inaccurate"],
    comment: "환불 기한이 틀렸습니다.",
    ephemeral: false,
    revision: 1,
    superseded: false,
    evaluation_use: false,
    created_at: NOW,
    updated_at: NOW,
    expires_at: NOW,
    ...over,
  };
}

function key(f: Pick<AgentFeedback, "agent_id" | "session_id" | "turn_id">): string {
  return `${f.agent_id}/${f.session_id}/${f.turn_id}`;
}

/** 서버의 차례다. 새것부터이고 만든 시각이 같으면 id 가 큰 것부터다. */
function newestFirst(a: AgentFeedback, b: AgentFeedback): number {
  return b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id);
}

function pageToken(f: AgentFeedback): string {
  return `${f.created_at}${PAGE_TOKEN_SEP}${f.id}`;
}

/** 표지가 가리키는 행보다 차례가 뒤인가. */
function after(token: string): (f: AgentFeedback) => boolean {
  const cut = token.lastIndexOf(PAGE_TOKEN_SEP);
  const [at, id] = [token.slice(0, cut), token.slice(cut + 1)];
  return (f) => f.created_at < at || (f.created_at === at && f.id < id);
}

export const agentFeedbackHandlers: HttpHandler[] = [
  http.put("/agents/:id/sessions/:sid/turns/:tid/feedback", async ({ request, params }) => {
    const r = await record(request);
    const [agentId, sid, tid] = [String(params.id), String(params.sid), String(params.tid)];
    const agent = agentStore.findOr404(agentId);
    if (agent instanceof Response) return agent;
    const session = agentSessionStore.findOr404(sid);
    if (session instanceof Response) return session;
    if (!tid.startsWith(turnIdPrefix(sid))) return apiError(404, "turn_not_found", `session ${sid} has no finished turn ${tid}`);
    const body = r.body as AgentFeedbackBody;
    const prev = agentFeedbackState.items.find((f) => key(f) === key({ agent_id: agentId, session_id: sid, turn_id: tid }));
    const next = feedbackFixture({
      id: prev?.id ?? `fb-${agentFeedbackState.items.length + 1}`,
      agent_id: agentId,
      session_id: sid,
      turn_id: tid,
      user_sub: meAdmin.sub,
      rating: body.rating,
      tags: body.tags ?? [],
      comment: body.comment?.trim() || null,
      ephemeral: body.ephemeral,
      evaluation_use: body.evaluation_use ?? false,
      created_at: prev?.created_at ?? NOW,
    });
    agentFeedbackState.items = [...agentFeedbackState.items.filter((f) => f !== prev), next];
    return HttpResponse.json(next);
  }),
  http.delete("/agents/:id/sessions/:sid/turns/:tid/feedback", async ({ request, params }) => {
    await record(request);
    const k = key({ agent_id: String(params.id), session_id: String(params.sid), turn_id: String(params.tid) });
    const before = agentFeedbackState.items.length;
    agentFeedbackState.items = agentFeedbackState.items.filter((f) => key(f) !== k);
    if (agentFeedbackState.items.length === before) return apiError(404, "feedback_not_found", "no feedback of yours on this turn");
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("/agents/:id/sessions/:sid/feedback", async ({ request, params }) => {
    await record(request);
    const items = agentFeedbackState.items.filter((f) => f.agent_id === params.id && f.session_id === params.sid && f.user_sub === meAdmin.sub);
    const body: AgentSessionFeedback = { items, retention_days: agentFeedbackState.retentionDays };
    return HttpResponse.json(body);
  }),
  http.get("/agents/:id/feedback/export", async ({ request, params }) => {
    await record(request);
    const rating = new URL(request.url).searchParams.get("rating");
    const lines = agentFeedbackState.items
      .filter((f) => f.agent_id === params.id && (!rating || f.rating === rating))
      .map((f) => JSON.stringify({ schema: "daegam.feedback/1", id: f.id, tenant: f.tenant, agent: { id: f.agent_id, name: "support" }, turn_id: f.turn_id, rating: f.rating }));
    return new HttpResponse(lines.map((l) => `${l}\n`).join(""), { headers: downloadHeaders("feedback-support.ndjson", "application/x-ndjson") });
  }),
  http.get("/agents/:id/feedback", async ({ request, params }) => {
    await record(request);
    const q = new URL(request.url).searchParams;
    const [rating, tag, before] = [q.get("rating"), q.get("tag"), q.get("before")];
    const limit = Number(q.get("limit") ?? PAGE_DEFAULT);
    const mine = agentFeedbackState.items.filter((f) => f.agent_id === params.id).sort(newestFirst);
    const behind = before ? after(before) : () => true;
    const matched = mine.filter((f) => (!rating || f.rating === rating) && (!tag || f.tags.includes(tag as AgentFeedback["tags"][number])) && behind(f));
    const items = matched.slice(0, limit);
    const body: AgentFeedbackList = {
      items,
      // 서버처럼 한 쪽이 꽉 찼으면 다음 쪽이 비어 있어도 넘길 자리를 준다
      next_before: items.length === limit ? pageToken(items[items.length - 1]) : null,
      last_7_days: { up: mine.filter((f) => f.rating === "up").length, down: mine.filter((f) => f.rating === "down").length },
    };
    return HttpResponse.json(body);
  }),
];
