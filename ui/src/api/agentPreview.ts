/**
 * Agent 미리 보기 (P44b 6절, D107). 부르는 사람의 미리 보기 하나(`/agents/{id}/preview`)와 그 Agent 에 열린 미리 보기 모두
 * (`/agents/{id}/previews`)다. 모두 그 테넌트의 `agent:deploy` 가 있어야 한다. 미리 보기와의 채팅은 Agent 의 채팅 함수에 채팅
 * 라우트의 바탕 `chatBase(id, true)` 를 주어 부른다(client.ts).
 *
 * 시작은 저장한 최신 리비전으로 열거나 옮기고 파드가 뜨기를 기다리지 않고 답한다(202). 테넌트의 자리가 찼으면 409 preview_limit,
 * 오늘 시작하고 옮긴 수가 상한이면 429 preview_start_limit 이다.
 */
import { ApiError, api, unwrap } from "./client";
import type { AgentPreview, PreviewEnded } from "./types";

/** 미리 보기가 없다는 답의 코드. 하루 안에 끝났으면 본문의 `ended` 가 까닭이다. */
export const PREVIEW_NOT_FOUND = "preview_not_found";

/** 내 미리 보기. 열려 있으면 `preview` 이고, 없으면 null 과 하루 안에 끝난 까닭(`ended`, 없으면 null)이다. */
export interface MyPreview {
  preview: AgentPreview | null;
  ended: PreviewEnded | null;
}

/** 내 미리 보기를 읽는다. 없다는 404 는 오류가 아니라 `preview: null` 이다. */
export async function getMyPreview(agentId: string): Promise<MyPreview> {
  try {
    const preview = unwrap(await api.GET("/agents/{agent_id}/preview", { params: { path: { agent_id: agentId } } }));
    return { preview, ended: null };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404 && e.code === PREVIEW_NOT_FOUND) {
      return { preview: null, ended: e.extra<PreviewEnded>("ended") ?? null };
    }
    throw e;
  }
}

/** 저장한 최신 리비전으로 내 미리 보기를 시작하거나, 이미 있으면 그 리비전으로 옮긴다. 옮기면 파드가 다시 떠 대화가 사라진다. */
export async function startMyPreview(agentId: string): Promise<AgentPreview> {
  return unwrap(await api.POST("/agents/{agent_id}/preview", { params: { path: { agent_id: agentId } } }));
}

/** 내 미리 보기를 끝낸다. 파드와 대화가 사라진다. */
export async function endMyPreview(agentId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/preview", { params: { path: { agent_id: agentId } } }));
}

/** 그 Agent 에 열린 미리 보기 모두. 처음 시작한 차례다. */
export async function listAgentPreviews(agentId: string): Promise<AgentPreview[]> {
  return unwrap(await api.GET("/agents/{agent_id}/previews", { params: { path: { agent_id: agentId } } })).previews;
}

/** 그 Agent 의 미리 보기 하나를 끝낸다. 다른 사람의 것이면 그 사람의 서랍이 다른 사람이 끝냈다고 말한다. */
export async function endAgentPreview(agentId: string, previewId: string): Promise<void> {
  unwrap(await api.DELETE("/agents/{agent_id}/previews/{preview_id}", { params: { path: { agent_id: agentId, preview_id: previewId } } }));
}
