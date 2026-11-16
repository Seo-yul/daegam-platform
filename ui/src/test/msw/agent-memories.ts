/**
 * 내 장기 기억 핸들러 (P36 8절). 플랫폼 `api/agent_chat.py` 의 `memories/me` 세 라우트와 D34 의 모두 삭제를 흉내 낸다. 기억은
 * 목의 사용자(header-admin) 한 사람 것만 `agentMemoryState.files` 에 둔다.
 *
 * 기록에 `memory_files` 가 없으면 409 runtime_feature_missing 이고, 도는 리비전에 장기 기억이 없으면(상태의 `long_term_memory`)
 * 런타임처럼 404 long_term_off 다.
 * 파일 경로는 조각마다 인코딩되어 오므로 URL 에서 키를 떼어 조각마다 푼다.
 */
import type { HttpHandler } from "msw";

import type { Agent, AgentMemoryFileRow } from "@/api/types";
import { MEMORY_ROOT } from "@/lib/memories";
import { RUNTIME_MEMORY_FILES } from "@/lib/runtimeFeatures";

import { agentStore, agentWithRuntimeFeatures, featureMissing } from "./agents";
import { meAdmin } from "./auth";
import { HttpResponse, NOW, apiError, http, record } from "./core";

const FILES_PREFIX = "/memories/me/files/";

/** 경로(`/memories/…`) → 내용. */
export const agentMemoryState: { files: Record<string, string> } = { files: defaultMemories() };

export function resetAgentMemoryState(files: Record<string, string> = defaultMemories()): void {
  agentMemoryState.files = { ...files };
}

function defaultMemories(): Record<string, string> {
  return {
    "/memories/prefs.md": "# 취향\n\n- 커피는 **아메리카노**",
    "/memories/notes/today.txt": "오늘 환불 문의를 남겼다.",
  };
}

/** 장기 기억을 켜고 배포한 Agent 다. 정의와 상태의 도는 리비전 사실이 함께 켜진다. 런타임 능력 이름은 `agentWithRuntimeFeatures` 처럼 준다. */
export function agentWithLongTerm(...features: string[]): Agent {
  const agent = agentWithRuntimeFeatures(...features);
  const nodes = agent.graph.nodes.map((n) => (n.type === "memory" ? { ...n, data: { ...(n.data as object), long_term: { retention_days: 30 } } } : n));
  return { ...agent, graph: { ...agent.graph, nodes }, status: agent.status && { ...agent.status, long_term_memory: true } };
}

/** 기록과 정의를 플랫폼·런타임처럼 가른다. 지나가면 null. */
function refused(agentId: string): Response | null {
  const agent = agentStore.findOr404(agentId);
  if (agent instanceof Response) return agent;
  const missing = featureMissing(agent, RUNTIME_MEMORY_FILES);
  if (missing) return missing;
  return agent.status?.long_term_memory ? null : apiError(404, "long_term_off", "this agent does not keep long-term memory");
}

/** 요청 URL 의 파일 경로(`/memories/…`)다. */
function pathOf(request: Request): string {
  const pathname = new URL(request.url).pathname;
  const key = pathname.slice(pathname.indexOf(FILES_PREFIX) + FILES_PREFIX.length);
  return MEMORY_ROOT + key.split("/").map(decodeURIComponent).join("/");
}

function row(path: string, content: string): AgentMemoryFileRow {
  return { path, size: new TextEncoder().encode(content).length, created_at: NOW, modified_at: NOW };
}

export const agentMemoryHandlers: HttpHandler[] = [
  http.get("/agents/:id/memories/me", async ({ request, params }) => {
    await record(request);
    const denied = refused(String(params.id));
    if (denied) return denied;
    const files = Object.entries(agentMemoryState.files)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([path, content]) => row(path, content));
    return HttpResponse.json({ enabled: true, retention_days: 30, files });
  }),
  http.get("/agents/:id/memories/me/files/*", async ({ request, params }) => {
    await record(request);
    const denied = refused(String(params.id));
    if (denied) return denied;
    const path = pathOf(request);
    const content = agentMemoryState.files[path];
    if (content === undefined) return apiError(404, "memory_not_found", `memory ${path} not found`);
    return HttpResponse.json({ path, encoding: "utf-8", content });
  }),
  http.delete("/agents/:id/memories/me/files/*", async ({ request, params }) => {
    await record(request);
    const denied = refused(String(params.id));
    if (denied) return denied;
    const path = pathOf(request);
    if (agentMemoryState.files[path] === undefined) return apiError(404, "memory_not_found", `memory ${path} not found`);
    delete agentMemoryState.files[path];
    return HttpResponse.json({ path, deleted: true });
  }),
  // D34 의 모두 삭제. 런타임을 거치지 않으므로 능력 이름을 보지 않는다
  http.delete("/agents/:id/memories/:sub", async ({ request, params }) => {
    await record(request);
    // 목의 사용자 것만 지운다. 다른 sub 의 기억은 모두 삭제도 거절한다
    if (params.sub !== meAdmin.sub) return apiError(403, "not_memory_owner", "long-term memory belongs to its user");
    const deleted = Object.keys(agentMemoryState.files).length;
    agentMemoryState.files = {};
    return HttpResponse.json({ agent_id: String(params.id), deleted });
  }),
];
