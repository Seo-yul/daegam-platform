/**
 * Agent 미리 보기 (P44b 6절, D107). 플랫폼 `api/agent_preview.py` 를 따른다.
 *   /agents/{id}/preview          내 미리 보기의 시작(옮기기)·읽기·끝. 없으면 404 preview_not_found 이고 하루 안에 끝났으면 `ended`
 *   /agents/{id}/previews         그 Agent 에 열린 미리 보기 모두와 하나 끝내기(다른 사람의 것이면 그 사람의 까닭은 ended_by_other)
 *   /agents/{id}/preview/chat 등  미리 보기 아래 채팅 SSE, 세션 상세, 보류, 결정. 열린 내 미리 보기가 Ready 여야 하고, 답은 Agent 의
 *                                 같은 이름 라우트의 몸(agent-chat.ts)이 한다
 * 부르는 사람은 `agentPreviewState.me` 다. 시작한 뒤 `readyAfterMs` 가 지나야 파드가 Ready 다(vitest 는 바로, 브라우저 목 서버는
 * 진행 줄을 보이려고 늦춘다).
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { AgentPreview, PreviewEnded, PreviewEndReason } from "@/api/types";
import { PREVIEW_LENGTHS } from "@/lib/agentPreview";

import { chatTurn, sessionDecisions, sessionDetail, sessionPending, type ChatResolver } from "./agent-chat";
import { agentStore } from "./agents";
import { meAdmin } from "./auth";
import { NOW, apiError, http, record } from "./core";

const { idleMinutes: IDLE_MINUTES, maxHours: MAX_HOURS } = PREVIEW_LENGTHS;

export interface PreviewMockState {
  items: AgentPreview[];
  /** `${원본 id}:${sub}` 마다 하루 안에 끝난 까닭. 시작하면 지운다. */
  ended: Record<string, PreviewEnded>;
  /** 부르는 사람의 sub. */
  me: string;
  /** 시작하거나 옮긴 뒤 파드가 Ready 가 되기까지(ms). */
  readyAfterMs: number;
  /** 미리 보기 id 마다 Ready 가 되는 때(ms). */
  readyAt: Record<string, number>;
}

export const agentPreviewState: PreviewMockState = { items: [], ended: {}, me: meAdmin.sub, readyAfterMs: 0, readyAt: {} };

export function resetAgentPreviewState(): void {
  agentPreviewState.items = [];
  agentPreviewState.ended = {};
  agentPreviewState.me = meAdmin.sub;
  agentPreviewState.readyAfterMs = 0;
  agentPreviewState.readyAt = {};
}

/** 미리 보기 하나. 기본은 dev@example.com 이 r3 으로 연 Ready 인 것이고, 시작 핸들러는 부르는 사람의 것을 이것으로 만든다. */
export function previewFixture(over: Partial<AgentPreview> = {}): AgentPreview {
  return {
    id: "pv-other",
    source_id: "ag-support",
    owner: "dev@example.com",
    revision: 3,
    state: "deployed",
    phase: "Ready",
    last_error: null,
    warnings: [],
    started_at: NOW,
    ends_at: new Date(Date.parse(NOW) + MAX_HOURS * 3_600_000).toISOString(),
    last_activity_at: NOW,
    idle_ends_at: new Date(Date.parse(NOW) + IDLE_MINUTES * 60_000).toISOString(),
    idle_minutes: IDLE_MINUTES,
    max_hours: MAX_HOURS,
    run: null,
    ...over,
  };
}

/** 그때의 모습. Ready 가 될 때가 지났으면 phase 가 Ready 다. */
function view(p: AgentPreview): AgentPreview {
  const at = agentPreviewState.readyAt[p.id];
  if (at === undefined || p.state === "failed") return p;
  return Date.now() >= at ? { ...p, state: "deployed", phase: "Ready" } : { ...p, state: "deploying", phase: null };
}

/** 부르는 사람의 미리 보기. 서버의 `preview_of` 처럼 평가 실행의 미리 보기(`run`)는 찾지 않는다. */
function mine(sourceId: unknown): AgentPreview | undefined {
  return agentPreviewState.items.find(
    (p) => p.source_id === String(sourceId) && p.owner === agentPreviewState.me && p.run === null,
  );
}

function endKey(sourceId: string, sub: string): string {
  return `${sourceId}:${sub}`;
}

function notFound(sourceId: unknown) {
  const ended = agentPreviewState.ended[endKey(String(sourceId), agentPreviewState.me)];
  return apiError(404, "preview_not_found", `no open preview of agent ${String(sourceId)}`, ended ? { ended } : {});
}

/** 미리 보기를 끝낸다. 시작한 사람의 서랍이 읽을 까닭을 남긴다. */
function endPreview(p: AgentPreview, reason: PreviewEndReason, by: string | null = agentPreviewState.me): void {
  agentPreviewState.items = agentPreviewState.items.filter((x) => x.id !== p.id);
  delete agentPreviewState.readyAt[p.id];
  agentPreviewState.ended[endKey(p.source_id, p.owner)] = { reason, at: new Date().toISOString(), by };
}

/** 열린 내 미리 보기가 Ready 일 때만 Agent 의 같은 이름 라우트의 몸이 답한다. */
function onMine(resolver: ChatResolver): ChatResolver {
  return (info) => {
    const p = mine(info.params.id);
    if (!p) return notFound(info.params.id);
    if (view(p).phase !== "Ready") return apiError(503, "runtime_unreachable", "the preview pod is not ready");
    return resolver(info);
  };
}

export const agentPreviewHandlers: HttpHandler[] = [
  http.post("/agents/:id/preview", async ({ request, params }) => {
    await record(request);
    const source = agentStore.findOr404(params.id);
    if (source instanceof Response) return source;
    const now = Date.now();
    const found = mine(params.id);
    if (found && found.revision === source.revision) return HttpResponse.json(view(found), { status: 202 });
    delete agentPreviewState.ended[endKey(source.id, agentPreviewState.me)];
    const started = new Date(now).toISOString();
    // 옮기면 같은 문서가 새 리비전으로 다시 뜨고 끝은 처음 시작의 것이다
    const p: AgentPreview = found
      ? { ...found, revision: source.revision, last_activity_at: started }
      : previewFixture({
          id: `pv-${agentPreviewState.me}-${source.id}`,
          source_id: source.id,
          owner: agentPreviewState.me,
          revision: source.revision,
          started_at: started,
          last_activity_at: started,
          ends_at: new Date(now + MAX_HOURS * 3_600_000).toISOString(),
          idle_ends_at: new Date(now + IDLE_MINUTES * 60_000).toISOString(),
        });
    agentPreviewState.items = [...agentPreviewState.items.filter((x) => x.id !== p.id), p];
    agentPreviewState.readyAt[p.id] = now + agentPreviewState.readyAfterMs;
    return HttpResponse.json(view(p), { status: 202 });
  }),
  http.get("/agents/:id/preview", async ({ request, params }) => {
    await record(request);
    const p = mine(params.id);
    return p ? HttpResponse.json(view(p)) : notFound(params.id);
  }),
  http.delete("/agents/:id/preview", async ({ request, params }) => {
    await record(request);
    const p = mine(params.id);
    if (!p) return notFound(params.id);
    endPreview(p, "ended");
    return HttpResponse.json({ id: p.id, ended: true });
  }),
  http.get("/agents/:id/previews", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ previews: agentPreviewState.items.filter((p) => p.source_id === String(params.id)).map(view) });
  }),
  http.delete("/agents/:id/previews/:pid", async ({ request, params }) => {
    await record(request);
    const p = agentPreviewState.items.find((x) => x.id === String(params.pid) && x.source_id === String(params.id));
    if (!p) return apiError(404, "preview_not_found", `no open preview ${String(params.pid)}`);
    endPreview(p, p.owner === agentPreviewState.me ? "ended" : "ended_by_other");
    return HttpResponse.json({ id: p.id, ended: true });
  }),
  http.post("/agents/:id/preview/chat", onMine(chatTurn)),
  http.get("/agents/:id/preview/sessions/:sid", onMine(sessionDetail)),
  http.get("/agents/:id/preview/sessions/:sid/pending", onMine(sessionPending)),
  http.post("/agents/:id/preview/sessions/:sid/decisions", onMine(sessionDecisions)),
];
