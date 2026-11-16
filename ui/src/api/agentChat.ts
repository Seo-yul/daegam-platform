/**
 * Agent 채팅 세션의 이름 바꾸기·지우기, 마지막 턴 다시 생성, 답변 피드백을 부르는 API 감싸개 (D61). 큰 `resources.ts` 에
 * 덧붙이지 않고 여기에 둔다. 모양은 모두 생성된 스키마에서 온다.
 *
 * 세션 이름 바꾸기·지우기와 다시 생성은 세션 소유자만 부른다. 승인자라도 남의 세션은 403 not_session_owner 다. 배포된
 * 런타임이 그 능력(`session_admin`, `regenerate`)을 알리지 않았으면 409 runtime_feature_missing 이므로 부르는 쪽은
 * `lib/runtimeFeatures.ts` 로 먼저 가른다.
 *
 * 피드백(P36 6절)은 플랫폼에 있어 능력 이름이 없다. 평가는 세션 소유자만 매기고, 목록과 내보내기는 `agent:approve` 다.
 *
 * 인용 원문(P36 7절)은 세션 상세와 같이 소유자나 `agent:approve` 가 받고 `knowledge:read` 도 요구한다. 배포된 런타임이
 * `citations` 를 알리지 않았으면 409 runtime_feature_missing 이므로 원문 다운로드 단추는 그 이름이 있을 때만 선다.
 *
 * 내 장기 기억(P36 8절)은 늘 호출자 자신의 것이다. 목록·읽기·하나 삭제는 런타임 능력 `memory_files` 에 기대고, 모두 삭제는
 * 런타임 없이도 도는 D34 라우트에 내 sub 를 넣어 부른다.
 *
 * 첨부(P36 9절)는 파일을 선택하는 순간 업로드하고 보낼 때는 `/chat` 의 `attachments` 에 id 만 싣는다. 업로드는 런타임 능력
 * `attachments` 에 기대고, 업로드한 사람만 그 업로드를 싣거나 삭제한다.
 *
 * 세션 파일(P36 10절)은 그 대화에 업로드한 파일과 에이전트가 파일 도구로 쓴 파일이다. 세션 상세와 같이 소유자나 `agent:approve`
 * 가 받고 런타임 능력 `session_files` 에 기댄다. 경로는 조각마다 인코딩해 `%2F` 를 막는 프록시를 피한다.
 *
 * 다시 생성, 인용 원문, 첨부, 세션 파일은 채팅 패널이 부르므로 Agent id 대신 채팅 라우트의 바탕(`ChatBase`)을 받는다. 미리 보기도
 * 같은 이름의 라우트를 갖는다(P44b 6절). 피드백과 기억은 Agent 에만 있어 id 그대로다.
 */
import { downloadFile } from "@/lib/download";
import { memoryKey } from "@/lib/memories";
import { sessionFileKey, sessionFileName } from "@/lib/sessionFiles";

import { agentPath, api, chatPath, chatRoute, chatSessionPath, cleanQuery, filePath, openSse, postFile, rawFetch, rawJson, throwIfNotOk, unwrap, type ChatBase } from "./client";
import type { AgentFeedback, AgentFeedbackBody, AgentFeedbackList, AgentMemories, AgentMemoryFile, AgentSession, AgentSessionFeedback, AgentSessionFiles, AgentUpload, FeedbackRating, FeedbackTag, Schemas } from "./types";

export type AgentRegenerateBody = Schemas["AgentRegenerateBody"];

/** 런타임이 도는 턴의 세션 id 를 싣는 응답 머리 (agent-runtime/runtime/turn.py 의 SESSION_HEADER). */
export const SESSION_ID_HEADER = "X-Daegam-Session-Id";
/** 턴 시도의 id 를 싣는 응답 머리 (P78 1절). 중단이 이 시도만 멈추게 싣는다. */
export const TURN_ID_HEADER = "X-Daegam-Turn-Id";
/** 이 파드가 턴을 연결과 떼어 돌린다는 응답 머리 (P78 1절). 있으면 연결을 닫아도 턴이 멈추지 않는다. */
export const DETACHED_TURN_HEADER = "X-Daegam-Detached-Turn";

/** 세션 이름을 바꾼다. 공백·제어 문자를 뺀 1~100자가 아니면 422 invalid_title 이다. 바꾼 세션 한 줄을 돌려준다. */
export async function renameAgentSession(agentId: string, sessionId: string, title: string): Promise<AgentSession> {
  const body = unwrap(
    await api.PATCH("/agents/{agent_id}/sessions/{session_id}", {
      params: { path: { agent_id: agentId, session_id: sessionId } },
      body: { title },
    }),
  );
  return body.session;
}

/** 세션을 지운다. 대화와 체크포인트, 그 세션의 피드백이 사라지고 장기 기억과 감사 기록은 남는다. 도는 턴이 있으면 409 turn_in_progress 다. */
export async function deleteAgentSession(agentId: string, sessionId: string): Promise<void> {
  unwrap(
    await api.DELETE("/agents/{agent_id}/sessions/{session_id}", {
      params: { path: { agent_id: agentId, session_id: sessionId } },
    }),
  );
}

/**
 * 마지막 답변을 다시 생성한다. 응답은 `/chat` 과 같은 SSE 이고 lib/chatStream 이 읽는다. `message` 가 있으면 마지막 질문을
 * 그 글로 바꿔 보낸다(메시지 수정). 스트림을 열기 전의 거절(409 session_pending·nothing_to_regenerate·turn_in_progress,
 * 429 한도와 예산, 413·422 입력 검사)은 ApiError 다.
 */
export async function openAgentRegenerate(chat: ChatBase, sessionId: string, body: AgentRegenerateBody): Promise<Response> {
  return openSse(chatSessionPath(chat, sessionId, "/regenerate"), body);
}

/** 끝난 턴의 평가를 만들거나 고친다. 런타임 전사에 그 턴이 없으면 404 turn_not_found 다. */
export async function putTurnFeedback(agentId: string, sessionId: string, turnId: string, body: AgentFeedbackBody): Promise<AgentFeedback> {
  return unwrap(
    await api.PUT("/agents/{agent_id}/sessions/{session_id}/turns/{turn_id}/feedback", {
      params: { path: { agent_id: agentId, session_id: sessionId, turn_id: turnId } },
      body,
    }),
  );
}

/** 자기 평가를 거둔다. 거둘 것이 없으면 404 feedback_not_found 다. */
export async function deleteTurnFeedback(agentId: string, sessionId: string, turnId: string): Promise<void> {
  unwrap(
    await api.DELETE("/agents/{agent_id}/sessions/{session_id}/turns/{turn_id}/feedback", {
      params: { path: { agent_id: agentId, session_id: sessionId, turn_id: turnId } },
    }),
  );
}

/** 그 세션의 내 평가와 설치의 평가 보존 기간이다. 다시 연 대화가 엄지 상태를 되살리고 동의 안내가 보존 기간을 적는다. */
export async function listSessionFeedback(agentId: string, sessionId: string): Promise<AgentSessionFeedback> {
  return unwrap(
    await api.GET("/agents/{agent_id}/sessions/{session_id}/feedback", {
      params: { path: { agent_id: agentId, session_id: sessionId } },
    }),
  );
}

export interface AgentFeedbackFilter {
  rating?: FeedbackRating;
  tag?: FeedbackTag;
}

/** 이 Agent 의 평가 한 쪽이다(`agent:approve`). 새것부터이고 다음 쪽은 응답의 `next_before` 를 `before` 로 준다. */
export async function listAgentFeedback(agentId: string, filter: AgentFeedbackFilter, before?: string | null): Promise<AgentFeedbackList> {
  return unwrap(
    await api.GET("/agents/{agent_id}/feedback", {
      params: { path: { agent_id: agentId }, query: { ...filter, before: before ?? undefined } },
    }),
  );
}

/** 평가를 NDJSON 으로 내려받는다(`agent:approve`). 구간은 최대 `FEEDBACK_EXPORT_MAX_DAYS` 일이고 비우면 서버가 지금까지 그만큼으로 잡는다. */
export async function exportAgentFeedback(agentId: string, query: { since?: string; until?: string; rating?: FeedbackRating }): Promise<void> {
  // 빈 값을 뺐으므로 남은 값은 모두 글자다
  const qs = new URLSearchParams(cleanQuery(query) as Record<string, string>).toString();
  await downloadFile(agentPath(agentId, `/feedback/export${qs ? `?${qs}` : ""}`), "feedback.ndjson");
}

/**
 * 답이 인용한 번호 n 의 Knowledge 문서 원문을 내려받는다. 인용되지 않았거나 배포된 Knowledge Base 의 문서가 아니면 404
 * citation_not_found, 도구나 출력 위치에 가림 가드레일이 있는 에이전트는 403 source_masked 다.
 */
export async function downloadCitationSource(chat: ChatBase, sessionId: string, n: number, fallbackName: string): Promise<void> {
  await downloadFile(chatSessionPath(chat, sessionId, `/citations/${n}/source`), fallbackName);
}

/** 이 에이전트가 기억하는 내 파일 목록이다. 장기 기억을 쓰지 않는 에이전트는 404 long_term_off 다. */
export async function listMyMemories(agentId: string): Promise<AgentMemories> {
  return unwrap(await api.GET("/agents/{agent_id}/memories/me", { params: { path: { agent_id: agentId } } }));
}

function myMemoryPath(agentId: string, path: string): string {
  return agentPath(agentId, `/memories/me/files/${filePath(memoryKey(path))}`);
}

/** 내 기억 파일 하나의 내용이다. `MEMORY_FILE_MAX_BYTES`(lib/memories.ts)를 넘으면 413 file_too_large, 이미 지워졌으면 404 memory_not_found 다. */
export async function readMyMemory(agentId: string, path: string): Promise<AgentMemoryFile> {
  return rawJson<AgentMemoryFile>(myMemoryPath(agentId, path));
}

/** 내 기억 파일 하나를 삭제한다. 그 경로로 시작하는 다른 파일은 남는다. */
export async function deleteMyMemory(agentId: string, path: string): Promise<void> {
  await throwIfNotOk(await rawFetch(myMemoryPath(agentId, path), { method: "DELETE" }));
}

/** 내 기억을 모두 삭제한다(D34). 런타임 없이 저장소에서 지우므로 지운 항목 수를 돌려준다. */
export async function deleteAllMyMemories(agentId: string, userSub: string): Promise<number> {
  const body = unwrap(
    await api.DELETE("/agents/{agent_id}/memories/{user_sub}", {
      params: { path: { agent_id: agentId, user_sub: userSub } },
    }),
  );
  return body.deleted;
}

/**
 * 채팅에 붙일 파일 하나를 업로드한다. 413 too_large, 422 unsupported_format·empty_document·추출 오류, 429 upload_rate_limited·
 * too_many_uploads·upload_busy·extract_busy 다. 요청을 끊어도 서버가 업로드를 만들 수 있으므로 부르는 쪽은 끝까지 기다려 id 를
 * 받은 뒤 지운다.
 */
export async function uploadChatAttachment(chat: ChatBase, file: File): Promise<AgentUpload> {
  return postFile<AgentUpload>(chatPath(chat, "/uploads"), file);
}

/** 보내지 않은 업로드를 삭제한다. 이미 보냈거나 사라졌어도 성공이다. */
export async function deleteChatAttachment(chat: ChatBase, uploadId: string): Promise<void> {
  unwrap(await api.DELETE(chatRoute(chat, "/agents/{agent_id}/uploads/{upload_id}"), { params: { path: { agent_id: chat.agentId, upload_id: uploadId } } }));
}

/** 세션 파일 목록이다. 업로드한 파일을 넣은 차례로, 에이전트가 쓴 파일을 경로순으로 싣는다. 닫힌 대화는 410 session_closed 다. */
export async function listSessionFiles(chat: ChatBase, sessionId: string): Promise<AgentSessionFiles> {
  return unwrap(await api.GET(chatRoute(chat, "/agents/{agent_id}/sessions/{session_id}/files"), { params: { path: { agent_id: chat.agentId, session_id: sessionId } } }));
}

function sessionFilePath(chat: ChatBase, sessionId: string, path: string): string {
  return chatSessionPath(chat, sessionId, `/files/${filePath(sessionFileKey(path))}`);
}

/** 세션 파일 하나를 다운로드한다. 없으면 404 file_not_found, `SESSION_FILE_MAX_BYTES` 를 넘으면 413 file_too_large 다. */
export async function downloadSessionFile(chat: ChatBase, sessionId: string, path: string): Promise<void> {
  await downloadFile(sessionFilePath(chat, sessionId, path), sessionFileName(path));
}

/** 세션 파일 하나의 바이트다. 서랍의 미리 보기가 글로 풀어 보인다. */
export async function readSessionFile(chat: ChatBase, sessionId: string, path: string): Promise<ArrayBuffer> {
  const res = await rawFetch(sessionFilePath(chat, sessionId, path));
  await throwIfNotOk(res);
  return res.arrayBuffer();
}
