/**
 * 세션 파일 핸들러 (P36 10절). 플랫폼 `api/agent_chat.py` 의 세션 파일 두 라우트를 흉내 낸다. 파일은 세션마다 경로 → 내용으로
 * `agentSessionFileState.files` 에 둔다.
 *
 * 기록에 `session_files` 가 없으면 409 runtime_feature_missing, 닫힌 세션은 런타임처럼 410 session_closed, 없는 파일은 404
 * file_not_found 다. 목록의 `origin` 은 런타임처럼 경로가 가르고, 업로드한 파일을 넣은 차례로, 작업 공간 파일과 에이전트 파일을
 * 경로순으로 싣는다. 작업 공간의 `.exe` 는 403 file_refused_executable 이다.
 * 다운로드는 플랫폼과 같은 헤더 다섯(`downloadHeaders`)으로 온다. 파일 경로는 조각마다 인코딩되어 오므로 조각마다 푼다.
 */
import type { HttpHandler } from "msw";

import type { AgentSessionFileRow } from "@/api/types";
import { RUNTIME_SESSION_FILES } from "@/lib/runtimeFeatures";
import { SESSION_FILE_PREVIEW_MAX_BYTES, UPLOADS_ROOT, WORKSPACE_ROOT } from "@/lib/sessionFiles";

import { agentSessionStore } from "./agent-sse";
import { agentStore, featureMissing } from "./agents";
import { HttpResponse, NOW, apiError, downloadHeaders, http, record } from "./core";

const FILES_PREFIX = "/files/";

/** 업로드한 이미지의 바이트다. PNG 머리라 UTF-8 로 풀리지 않는다. */
export const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
/** 미리 보기 상한을 넘는 에이전트 파일의 경로다. 다운로드만 된다. */
export const BIG_FILE = "/data/큰 표.csv";

/** 세션 id → (경로 → 내용). */
export const agentSessionFileState: { files: Record<string, Record<string, string | Uint8Array>> } = { files: defaultSessionFiles() };

export function resetAgentSessionFileState(files: Record<string, Record<string, string | Uint8Array>> = defaultSessionFiles()): void {
  agentSessionFileState.files = structuredClone(files);
}

function defaultSessionFiles(): Record<string, Record<string, string | Uint8Array>> {
  return {
    "s-1": {
      "/uploads/보고서.pdf.txt": "환불은 7일 안에 한다.",
      "/uploads/화면.png": PNG_BYTES,
      "/reports/요약.md": "# 요약\n\n- 환불은 **7일** 안에 한다.",
      "/notes/할 일.txt": "고객에게 다시 연락한다.",
      [BIG_FILE]: "a,b\n".repeat(SESSION_FILE_PREVIEW_MAX_BYTES / 4 + 1),
    },
  };
}

function sizeOf(content: string | Uint8Array): number {
  return typeof content === "string" ? new TextEncoder().encode(content).length : content.length;
}

function rowsOf(files: Record<string, string | Uint8Array>): AgentSessionFileRow[] {
  const row = (path: string, origin: AgentSessionFileRow["origin"]): AgentSessionFileRow => ({ path, size: sizeOf(files[path]!), modified_at: NOW, origin });
  const paths = Object.keys(files);
  const uploads = paths.filter((p) => p.startsWith(UPLOADS_ROOT)).map((p) => row(p, "upload"));
  const workspace = paths
    .filter((p) => p.startsWith(WORKSPACE_ROOT))
    .sort()
    .map((p) => row(p, "workspace"));
  const agent = paths
    .filter((p) => !p.startsWith(UPLOADS_ROOT) && !p.startsWith(WORKSPACE_ROOT))
    .sort()
    .map((p) => row(p, "agent"));
  return [...uploads, ...workspace, ...agent];
}

/** 기록과 세션을 플랫폼·런타임처럼 가른다. 지나가면 null. */
function refused(agentId: string, sessionId: string): Response | null {
  const agent = agentStore.findOr404(agentId);
  if (agent instanceof Response) return agent;
  const missing = featureMissing(agent, RUNTIME_SESSION_FILES);
  if (missing) return missing;
  const session = agentSessionStore.findOr404(sessionId);
  if (session instanceof Response) return session;
  return session.status === "closed" ? apiError(410, "session_closed", "the session is closed") : null;
}

/** 요청 URL 의 파일 경로(`/reports/요약.md`)다. */
function pathOf(request: Request): string {
  const pathname = new URL(request.url).pathname;
  const key = pathname.slice(pathname.indexOf(FILES_PREFIX) + FILES_PREFIX.length);
  return `/${key.split("/").map(decodeURIComponent).join("/")}`;
}

export const agentSessionFileHandlers: HttpHandler[] = [
  http.get("/agents/:id/sessions/:sid/files", async ({ request, params }) => {
    await record(request);
    const denied = refused(String(params.id), String(params.sid));
    if (denied) return denied;
    return HttpResponse.json({ files: rowsOf(agentSessionFileState.files[String(params.sid)] ?? {}) });
  }),
  http.get("/agents/:id/sessions/:sid/files/*", async ({ request, params }) => {
    await record(request);
    const denied = refused(String(params.id), String(params.sid));
    if (denied) return denied;
    const path = pathOf(request);
    const content = agentSessionFileState.files[String(params.sid)]?.[path];
    if (content === undefined) return apiError(404, "file_not_found", `file ${path} not found`);
    // 런타임처럼 열면 코드가 돌 수 있는 작업 공간 파일은 내려받지 않는다(P40 6절). 시험은 확장자만 본다
    if (path.startsWith(WORKSPACE_ROOT) && path.endsWith(".exe")) return apiError(403, "file_refused_executable", "refused");
    return new HttpResponse(content, { headers: downloadHeaders(path) });
  }),
];
