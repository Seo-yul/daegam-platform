/**
 * msw 공통 조각. /api 접두사를 붙이는 `http` 래퍼, 요청 기록(`received`·`record`·`lastRequest`·`lastExact`·`countExact`),
 * 오류 응답 `apiError`, id 로 찾고 바꾸고 지우는 `docStore`, 내려받기 헤더 `downloadHeaders`, 픽스처가 같이 쓰는 시각
 * `NOW`. 리소스 파일은 전부 여기서 가져간다.
 */
import { http as mswHttp, HttpResponse, type HttpHandler } from "msw";

import { fastapiDoc } from "../fastapiDoc";
import { readRuntimeFixture } from "../runtimeFixtures";

export const BASE = "http://localhost:3000";
export const API_PREFIX = "/api";

type Method = "get" | "post" | "put" | "patch" | "delete" | "all";

/** 상대 경로에만 /api 를 붙이는 msw http 래퍼. */
function prefixed<M extends Method>(method: M): (typeof mswHttp)[M] {
  return ((path: string, ...rest: unknown[]) =>
    (mswHttp[method] as (...a: unknown[]) => HttpHandler)(
      path.startsWith("/") ? API_PREFIX + path : path,
      ...rest,
    )) as (typeof mswHttp)[M];
}

export const http = {
  get: prefixed("get"),
  post: prefixed("post"),
  put: prefixed("put"),
  patch: prefixed("patch"),
  delete: prefixed("delete"),
  all: prefixed("all"),
};

export interface ReceivedRequest {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

export const received: ReceivedRequest[] = [];

/**
 * request.formData() 는 jsdom 환경에서 undici 의 File 검사에 걸려 깨진다 (global File 이 jsdom 것).
 * 테스트 setup 이 만드는 단순 multipart 본문을 직접 파싱한다. 파일 파트는 {name, text} 로 남긴다.
 */
function parseMultipart(text: string, ctype: string): Record<string, unknown> {
  const boundary = /boundary=([^;]+)/.exec(ctype)?.[1];
  const out: Record<string, unknown> = {};
  if (!boundary) return out;
  for (const raw of text.split(`--${boundary}`)) {
    const part = raw.replace(/^\r\n/, "");
    if (!part || part.startsWith("--")) continue;
    const sep = part.indexOf("\r\n\r\n");
    if (sep < 0) continue;
    const headers = part.slice(0, sep);
    const value = part.slice(sep + 4).replace(/\r\n$/, "");
    const name = /name="([^"]*)"/.exec(headers)?.[1];
    if (!name) continue;
    const filename = /filename="([^"]*)"/.exec(headers)?.[1];
    out[name] = filename !== undefined ? { name: filename, text: value } : value;
  }
  return out;
}

/** 요청을 `received` 에 남긴다. 테스트가 직접 만드는 핸들러도 lastRequest 로 검사하려면 이것을 불러야 한다. */
export async function record(request: Request): Promise<ReceivedRequest> {
  const url = new URL(request.url);
  const headers: Record<string, string> = {};
  request.headers.forEach((v, k) => (headers[k.toLowerCase()] = v));
  let body: unknown = null;
  const ctype = request.headers.get("content-type") ?? "";
  if (ctype.includes("application/json")) body = await request.json();
  else if (ctype.includes("multipart/form-data")) {
    body = parseMultipart(await request.text(), ctype);
  }
  const r = { method: request.method, path: url.pathname + url.search, headers, body };
  received.push(r);
  return r;
}

/** 기록된 경로는 실제 URL 그대로다. 조회는 리소스 경로("/audit")로도 /api 붙은 경로로도 된다. */
export function lastRequest(method: string, pathPrefix: string): ReceivedRequest | undefined {
  return [...received]
    .reverse()
    .find(
      (r) =>
        r.method === method &&
        (r.path.startsWith(pathPrefix) || r.path.startsWith(API_PREFIX + pathPrefix)),
    );
}

/** 기록이 그 메서드로 그 경로를 정확히 불렀는가. 경로는 리소스 경로로도 /api 붙은 경로로도 된다. */
function hits(r: ReceivedRequest, method: string, path: string): boolean {
  return r.method === method && (r.path === path || r.path === API_PREFIX + path);
}

/**
 * 경로가 정확히 같은 마지막 요청. `lastRequest` 는 접두 일치라 `/assistant/sessions` 를 찾으면 뒤에 온
 * `/assistant/sessions/as-1/turns` 가 잡힌다. 하위 경로가 있는 라우트를 검사할 때 이것을 쓴다.
 */
export function lastExact(method: string, path: string): ReceivedRequest | undefined {
  return [...received].reverse().find((r) => hits(r, method, path));
}

/** 경로가 정확히 같은 요청의 수다(`lastExact` 처럼 정확히 일치). 폴링이 다시 묻는지, 멈췄는지 볼 때 쓴다. */
export function countExact(method: string, path: string): number {
  return received.filter((r) => hits(r, method, path)).length;
}

/** 플랫폼 꼴의 오류 응답 `{detail: {code, detail, ...extra}}`. init 은 머리(Retry-After 같은 것)를 더할 때 쓴다. */
export function apiError(status: number, code: string, detail: string, extra: Record<string, unknown> = {}, init: ResponseInit = {}) {
  return HttpResponse.json({ detail: { code, detail, ...extra } }, { ...init, status });
}

/** `{ items }` 목 상태에서 id 로 문서를 찾고 바꾸고 지우는 도구다. */
export interface DocStore<T> {
  /** id 의 문서다. 없으면 null 이다. */
  find(id: unknown): T | null;
  /** id 의 문서다. 없으면 플랫폼처럼 404 응답이다. 핸들러는 `instanceof Response` 로 가른다. */
  findOr404(id: unknown): T | Response;
  /** 같은 id 의 문서를 next 로 바꾸고 next 를 돌려준다. */
  replace(next: T): T;
  /** id 의 문서를 지운다. 없으면 아무것도 하지 않는다. */
  remove(id: unknown): void;
}

/** docStore 의 선택. key 는 id 를 든 칸, code 는 없을 때 404 의 코드다. */
export interface DocStoreOptions<K extends string> {
  key?: K;
  code?: string;
}

/**
 * 리소스 목의 문서 조회·교체·삭제다. 테스트가 `state.items` 배열을 통째로 갈아 끼우므로 부를 때마다 다시 읽는다.
 * noun 은 404 문구의 리소스 이름이다("mcp server ms-x not found").
 */
export function docStore<T extends Record<K, string>, K extends string = "id">(
  state: { items: T[] },
  noun: string,
  { key = "id" as K, code = "not_found" }: DocStoreOptions<K> = {},
): DocStore<T> {
  const find = (id: unknown): T | null => state.items.find((d) => d[key] === String(id)) ?? null;
  return {
    find,
    findOr404: (id) => find(id) ?? apiError(404, code, `${noun} ${String(id)} not found`),
    replace(next) {
      state.items = state.items.map((d) => (d[key] === next[key] ? next : d));
      return next;
    },
    remove(id) {
      state.items = state.items.filter((d) => d[key] !== String(id));
    },
  };
}

export const NOW = "2026-09-02T10:00:00+00:00";

/** 내려받기 헤더 표본의 한 경우다. 플랫폼과 런타임의 테스트도 같은 표본에 댄다(D61). */
export interface DownloadHeaderCase {
  path: string;
  media_type: string;
  filename: string;
  headers: Record<string, string>;
}

export const DOWNLOAD_HEADER_CASES = readRuntimeFixture<{ cases: DownloadHeaderCase[] }>("chat/download-headers.json").cases;

const OCTET_STREAM = "application/octet-stream";
/** 파일 이름에서 빼는 글자(CR·LF·따옴표)다. */
const UNSAFE_NAME = /[\r\n"]/g;
/** ASCII 대체 이름에서 `_` 로 바꾸는 글자(인쇄할 수 있는 ASCII 밖과 역슬래시)다. */
const NOT_PLAIN_ASCII = /[^\x20-\x7e]|\\/gu;

/**
 * 플랫폼 `api/_download.py` 의 `download_headers` 와 같은 내려받기 헤더다. 이름 칸은 같은 규칙으로 만들고 나머지 칸은 표본에서
 * 형식이 같은 경우의 것을 그대로 쓴다. download.test 가 표본의 경우마다 이것으로 다시 만들어 견준다.
 */
export function downloadHeaders(path: string, mediaType = OCTET_STREAM): Record<string, string> {
  const sample = DOWNLOAD_HEADER_CASES.find((c) => c.media_type === mediaType);
  if (!sample) throw new Error(`내려받기 헤더 표본에 ${mediaType} 가 없다`);
  const name = path.slice(path.lastIndexOf("/") + 1).replace(UNSAFE_NAME, "") || "download";
  // 파이썬 quote(safe="") 처럼 encodeURIComponent 가 남기는 !'()* 도 인코딩한다
  const encoded = encodeURIComponent(name).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
  return { ...sample.headers, "content-disposition": `attachment; filename="${name.replace(NOT_PLAIN_ASCII, "_")}"; filename*=UTF-8''${encoded}` };
}

/** 플랫폼 메타. 다른 리소스에 속하지 않는 엔드포인트만 둔다. */
export const versionHandlers: HttpHandler[] = [
  http.get("/version", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ api_version: "0.1.0", api_build: "0.1.0+test-api" });
  }),
  // 플랫폼 자신의 OpenAPI 문서 (P59). FastAPI 꼴을 줄여 담은 픽스처다
  http.get("/openapi.json", async ({ request }) => {
    await record(request);
    return HttpResponse.json(fastapiDoc);
  }),
  // UI 컨테이너가 내는 /build.json (D52). API 가 아니라 /api 밖이고 received 에도 넣지 않는다
  mswHttp.get(`${BASE}/build.json`, () => HttpResponse.json({ ui_build: "test" })),
];

export { HttpResponse };
