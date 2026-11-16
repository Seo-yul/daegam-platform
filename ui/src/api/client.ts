/**
 * 플랫폼 API 클라이언트. UI 는 루트에서 서빙되고 API 는 같은 origin 의 /api 아래에 있다
 * (개발 서버는 vite proxy). 모든 요청에 선택한 테넌트를 X-Daegam-Tenant 헤더로 넣는다.
 */
import createClient, { type Middleware } from "openapi-fetch";

import type { paths } from "./schema";
import { getAccessToken, getAuthState, handleUnauthorized, initAuth, markPasswordChangeRequired } from "@/lib/auth";
import { getTenant, TENANT_HEADER } from "@/lib/tenant";

export interface ApiErrorBody {
  code: string;
  detail: string;
  [key: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly body: unknown;
  /** 응답의 Retry-After 그대로다. 플랫폼의 429(login_locked·token_limit)는 남은 초를 싣는다. */
  readonly retryAfter: string | null;

  constructor(status: number, body: unknown, retryAfter: string | null = null) {
    const parsed = parseErrorBody(status, body);
    super(`${parsed.code}: ${parsed.detail}`);
    this.name = "ApiError";
    this.status = status;
    this.code = parsed.code;
    this.detail = parsed.detail;
    this.body = body;
    this.retryAfter = retryAfter;
  }

  /** HTTPException(detail={code, detail, ...extra}) 의 extra 필드. */
  extra<T = unknown>(key: string): T | undefined {
    const d = (this.body as { detail?: Record<string, unknown> } | null)?.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) return d[key] as T;
    return undefined;
  }
}

function parseErrorBody(status: number, body: unknown): ApiErrorBody {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    return {
      code: typeof d.code === "string" ? d.code : `http_${status}`,
      detail: typeof d.detail === "string" ? d.detail : JSON.stringify(d),
    };
  }
  if (Array.isArray(detail)) {
    // pydantic 검증 오류
    const msgs = detail.map((e) => {
      const loc = Array.isArray(e?.loc) ? e.loc.join(".") : "";
      return loc ? `${loc}: ${e?.msg}` : String(e?.msg ?? e);
    });
    return { code: "validation_error", detail: msgs.join("; ") };
  }
  if (typeof detail === "string") return { code: `http_${status}`, detail };
  return { code: `http_${status}`, detail: `request failed with status ${status}` };
}

/** 테넌트 헤더 + (oidc 로그인 뒤) Authorization 헤더. */
function applyAuthHeaders(headers: Headers): void {
  headers.set(TENANT_HEADER, getTenant());
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
}

/** 인증 초기화(/auth/config, 세션 복원)가 끝난 뒤에 헤더를 붙인다. 기동 직후 요청이 토큰 없이 나가지 않게. */
export const tenantHeaderMiddleware: Middleware = {
  async onRequest({ request }) {
    await initAuth();
    applyAuthHeaders(request.headers);
    return request;
  },
  /**
   * local 모드에서 401 은 토큰이 죽은 것(만료·비밀번호 변경·계정 막힘)이다. 지우면 Layout 이 로그인으로 보낸다.
   * 403 password_change_required 는 비밀번호를 바꾸기 전까지 서버가 다른 API 를 막는 것이다. 상태에 적으면
   * Layout 이 비밀번호 화면으로 보낸다.
   */
  async onResponse({ response }) {
    await noteAuthFailure(response);
    return response;
  },
};

/** 미들웨어를 타지 않는 fetch(rawFetch·SSE)도 같은 규칙을 탄다. 본문은 복제해 읽어 호출자의 스트림을 건드리지 않는다. */
export async function noteAuthFailure(response: Response): Promise<void> {
  if (getAuthState().mode !== "local") return;
  if (response.status === 401) handleUnauthorized();
  if (response.status === 403) {
    const body = (await response.clone().json().catch(() => null)) as { detail?: { code?: string } } | null;
    if (body?.detail?.code === "password_change_required") markPasswordChangeRequired();
  }
}

/** 서버 라우트의 공통 접두사. 생성된 스키마의 경로 키도 이것으로 시작한다. */
export const API_PREFIX = "/api";

/**
 * 스키마 키에서 /api 를 떼어 낸 경로 타입. 접두사는 baseUrl 이 책임지므로 호출부는
 * 리소스 경로("/clusters")만 쓴다. /api 밖에 있는 것(/healthz)은 이 클라이언트로 부르지 않는다.
 */
type ApiPaths = {
  [K in keyof paths as K extends `${typeof API_PREFIX}${infer Rest}` ? Rest : never]: paths[K];
};

/** 같은 origin 의 /api. 브라우저에서는 상대 경로와 같고, 테스트(jsdom + Node fetch)에서는 절대 URL 이 필요하다. */
export const API_BASE: string =
  (typeof window !== "undefined" ? window.location.origin : "") + API_PREFIX;

export const api = createClient<ApiPaths>({
  baseUrl: API_BASE,
  // 호출 시점의 globalThis.fetch 를 쓴다. 모듈 로드 때 잡아 두면 테스트의 MSW 패치를 비껴간다.
  fetch: (request) => globalThis.fetch(request),
});
api.use(tenantHeaderMiddleware);

/** openapi-fetch 결과에서 data 를 꺼내고 오류면 ApiError 를 던진다. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(result.response.status, result.error, result.response.headers.get("Retry-After"));
  }
  return result.data as T;
}

/**
 * 스키마에 requestBody 가 없는 라우트(verify/convert 는 raw request 를 읽는다)용 fetch.
 * 테넌트 헤더를 같은 규칙으로 넣는다.
 */
export async function rawFetch(path: string, init: RequestInit = {}): Promise<Response> {
  await initAuth();
  const headers = new Headers(init.headers);
  applyAuthHeaders(headers);
  const res = await fetch(API_BASE + path, { ...init, headers });
  await noteAuthFailure(res);
  return res;
}

/**
 * 응답 본문을 JSON 으로 읽는다. 비었으면 null 이고, JSON 이 아니면(프록시의 HTML 오류 쪽 같은 것)
 * `{detail: 원문}` 이라 ApiError 가 그 글자를 설명으로 보인다. openapi-fetch 를 타지 않는 fetch 가 쓴다.
 */
export async function readJsonBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

/** 실패한 응답이면 오류 본문과 Retry-After 를 실은 ApiError 를 던진다. openapi-fetch 를 타지 않는 fetch 가 쓴다. */
export async function throwIfNotOk(res: Response): Promise<void> {
  if (!res.ok) throw new ApiError(res.status, await readJsonBody(res), res.headers.get("Retry-After"));
}

export async function rawJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await rawFetch(path, init);
  await throwIfNotOk(res);
  return (await readJsonBody(res)) as T;
}

/**
 * 파일 하나를 multipart `file` 로 POST 하고 JSON 답을 받는다. multipart 라 생성 클라이언트로 부르지 못하는 업로드가 쓴다.
 * `fields` 는 파일과 함께 보내는 폼 필드다.
 */
export async function postFile<T>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  for (const [key, value] of Object.entries(fields)) fd.append(key, value);
  return rawJson<T>(path, { method: "POST", body: fd });
}

/** Agent 하나 아래의 API 경로. SSE 처럼 생성 클라이언트로 부르지 않는 라우트가 쓴다. id 는 조각 하나로 인코딩한다. */
export function agentPath(id: string, rest: string): string {
  return `/agents/${encodeURIComponent(id)}${rest}`;
}

/**
 * 채팅 라우트의 바탕 (P44b 6절). Agent 의 채팅, 업로드, 세션 라우트는 `/agents/{id}` 아래에 있고, 부르는 사람의 미리 보기의 것은
 * `/agents/{id}/preview` 아래에 같은 이름으로 있다. 채팅 패널과 그 훅이 Agent id 대신 이것을 받는다. 생성 클라이언트로 부르는
 * 라우트는 `chatRoute` 가 경로를 고르고, SSE 와 multipart 처럼 생성 클라이언트를 타지 않는 라우트는 `chatPath` 를 쓴다.
 * 미리 보기에는 세션 목록, 답변 피드백, 기억이 없다.
 */
export interface ChatBase {
  agentId: string;
  /** 부르는 사람의 미리 보기인가. 아니면 Agent 그 자체다. */
  preview: boolean;
}

/** 그 Agent 의 채팅 라우트. `preview` 면 그 Agent 에서 부르는 사람이 연 미리 보기의 것이다. */
export function chatBase(agentId: string, preview = false): ChatBase {
  return { agentId, preview };
}

/** 생성 클라이언트의 Agent 경로를 미리 보기 아래의 같은 이름 경로로 옮긴 타입. */
type PreviewRoute<P extends string> = P extends `/agents/{agent_id}/${infer R}` ? `/agents/{agent_id}/preview/${R}` : never;

/**
 * 채팅 라우트 하나의 생성 클라이언트 경로. 미리 보기면 `/agents/{agent_id}/preview/…` 이다. 두 경로는 모양이 같아 생성
 * 클라이언트가 둘의 합을 받고, 스키마에 없는 미리 보기 경로는 타입 검사가 막는다.
 */
export function chatRoute<P extends `/agents/{agent_id}/${string}`>(chat: ChatBase, path: P): P | PreviewRoute<P> {
  return (chat.preview ? path.replace("/agents/{agent_id}/", "/agents/{agent_id}/preview/") : path) as P | PreviewRoute<P>;
}

/** 채팅 라우트의 바탕 경로(`/agents/{id}` 나 `/agents/{id}/preview`) 아래 경로. */
export function chatPath(chat: ChatBase, rest: string): string {
  return agentPath(chat.agentId, `${chat.preview ? "/preview" : ""}${rest}`);
}

/** 채팅 라우트의 세션 하나 아래 경로. `agentPath` 처럼 세션 id 도 조각 하나로 인코딩한다. */
export function chatSessionPath(chat: ChatBase, sessionId: string, rest = ""): string {
  return chatPath(chat, `/sessions/${encodeURIComponent(sessionId)}${rest}`);
}

/**
 * 파일 경로를 URL 로 옮긴다. `/` 는 두고 조각마다 인코딩한다. 경로 전체를 한 조각으로 인코딩하면 `%2F` 가 되는데, 그것을 막는
 * 프록시가 있어 경로가 끊긴다. 플랫폼도 런타임으로 넘길 때 조각마다 다시 인코딩한다.
 */
export function filePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

/** 빈 문자열 필터는 쿼리에서 뺀다 (서버가 "" 를 값으로 받으면 매칭이 어긋난다). 생성 클라이언트의 query 와 손으로 만든 쿼리 문자열이 같이 쓴다. */
export function cleanQuery<T extends Record<string, string | number | undefined>>(filters: T): T {
  const out: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(filters)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = v;
  }
  return out as T;
}

/**
 * SSE 를 여는 POST. `text/event-stream` 응답의 Response 를 돌려주고 lib/chatStream 이 읽는다. 스트림을 열기 전의 오류는
 * ApiError 이고 Retry-After 는 `retryAfter` 에 싣고 `detail.retry_after` 로도 옮겨 채팅의 오류 줄이 읽는다. 깨우는 중(503
 * agent_waking)의 다시 보내기도 그 값을 읽는다(lib/agentSleep.ts).
 */
export async function openSse(path: string, body: unknown): Promise<Response> {
  const res = await rawFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const parsed = await readJsonBody(res);
    const retryAfter = res.headers.get("Retry-After");
    if (retryAfter && parsed && typeof parsed === "object") {
      const d = (parsed as { detail?: unknown }).detail;
      if (d && typeof d === "object") (d as Record<string, unknown>).retry_after = retryAfter;
    }
    throw new ApiError(res.status, parsed, retryAfter);
  }
  return res;
}
