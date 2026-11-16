import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, vi } from "vitest";

import { resetAssistantStore } from "@/lib/assistant";
import { resetAuthState } from "@/lib/auth";
import { resetLanguage } from "@/lib/language";
import { resetTenantStore } from "@/lib/tenant";
import { resetTheme } from "@/lib/theme";
import { resetMswState, server } from "./msw";
import { resetOidcStub } from "./stubs/oidc-client-ts";

/**
 * findBy·waitFor 가 기다리는 상한. 기본 1초는 기계가 바쁠 때(다른 프로세스나 병렬 테스트) 화면 하나를 그리는
 * 데 모자라 결함 없는 테스트가 실패할 수 있다. 통과하는 테스트는 찾는 즉시 끝나므로 상한만 늘린다.
 */
configure({ asyncUtilTimeout: 3000 });

/** Node 26 + jsdom 조합에서는 window.localStorage 가 비어 있을 수 있다. 메모리 구현으로 채운다. */
class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length() {
    return this.map.size;
  }
  clear() {
    this.map.clear();
  }
  getItem(key: string) {
    return this.map.has(key) ? (this.map.get(key) as string) : null;
  }
  key(index: number) {
    return [...this.map.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.map.delete(key);
  }
  setItem(key: string, value: string) {
    this.map.set(key, String(value));
  }
}

let hasStorage = false;
try {
  hasStorage = typeof window.localStorage?.getItem === "function";
} catch {
  hasStorage = false;
}
if (!hasStorage) {
  Object.defineProperty(window, "localStorage", { value: new MemoryStorage(), configurable: true });
}

/*
 * @xyflow/react 가 jsdom 에 없는 것을 쓴다 (phase-12 F4). ResizeObserver 는 노드 크기 측정, DOMMatrixReadOnly 는
 * 뷰포트 transform 의 scale 을 읽는 데 쓴다. 노드 크기는 0 이면 렌더를 건너뛰므로 offsetWidth/Height 를 style 값
 * (없으면 1) 으로 준다. xyflow 문서의 테스트 셋업과 같다.
 */
class ResizeObserverPolyfill {
  private readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    const rect = target.getBoundingClientRect();
    this.callback([{ target, contentRect: rect, borderBoxSize: [], contentBoxSize: [], devicePixelContentBoxSize: [] } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
}
class DOMMatrixReadOnlyPolyfill {
  readonly m22: number;
  constructor(transform?: string) {
    const scale = transform ? /scale\(([\d.]+)\)/.exec(transform)?.[1] : undefined;
    this.m22 = scale !== undefined ? Number(scale) : 1;
  }
}
if (typeof window.ResizeObserver === "undefined") {
  Object.defineProperty(window, "ResizeObserver", { value: ResizeObserverPolyfill, configurable: true });
  Object.defineProperty(globalThis, "ResizeObserver", { value: ResizeObserverPolyfill, configurable: true });
}
if (typeof window.DOMMatrixReadOnly === "undefined") {
  Object.defineProperty(window, "DOMMatrixReadOnly", { value: DOMMatrixReadOnlyPolyfill, configurable: true });
  Object.defineProperty(globalThis, "DOMMatrixReadOnly", { value: DOMMatrixReadOnlyPolyfill, configurable: true });
}
Object.defineProperties(window.HTMLElement.prototype, {
  offsetHeight: {
    configurable: true,
    get(this: HTMLElement) {
      return parseFloat(this.style.height) || 1;
    },
  },
  offsetWidth: {
    configurable: true,
    get(this: HTMLElement) {
      return parseFloat(this.style.width) || 1;
    },
  },
});
if (!(window.SVGElement.prototype as unknown as { getBBox?: unknown }).getBBox) {
  (window.SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () => ({ x: 0, y: 0, width: 0, height: 0 }) as DOMRect;
}

/**
 * jsdom 의 FormData/File 은 Node fetch(undici) 가 알아보지 못해 "[object FormData]" 문자열로 나간다.
 * MSW 가 request.formData() 로 파싱할 수 있게 여기서 직접 multipart 본문을 만든다.
 * MSW 가 fetch 를 패치한 뒤(listen 이후) 바깥에 감싸야 하므로 beforeAll 에서 건다.
 */
function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result ?? ""));
    r.onerror = () => reject(r.error);
    r.readAsText(blob);
  });
}

async function multipartFromFormData(fd: FormData): Promise<{ body: string; contentType: string }> {
  const boundary = `----daegam-test-${Math.random().toString(16).slice(2)}`;
  const parts: string[] = [];
  for (const [name, value] of fd.entries()) {
    if (typeof value === "string") {
      parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`);
    } else {
      const file = value as File;
      const text = await readBlob(file);
      parts.push(
        `--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${file.name}"\r\n` +
          `Content-Type: ${file.type || "application/octet-stream"}\r\n\r\n${text}\r\n`,
      );
    }
  }
  return { body: parts.join("") + `--${boundary}--\r\n`, contentType: `multipart/form-data; boundary=${boundary}` };
}

let mswFetch: typeof fetch;

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  mswFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    if (init?.body instanceof window.FormData) {
      const { body, contentType } = await multipartFromFormData(init.body);
      const headers = new Headers(init.headers);
      headers.set("Content-Type", contentType);
      return mswFetch(input, { ...init, body, headers });
    }
    return mswFetch(input, init);
  };
});
// 핸들러가 아직 응답을 만들지 않은 요청 수. 언마운트가 보낸 요청(보내지 않은 첨부 지우기 같은)이 다음 테스트의
// resetMockState 뒤에 도착해 그 테스트의 목 상태를 바꾸지 않도록 afterEach 가 이것이 0 이 되기를 기다린다
let pendingRequests = 0;
server.events.on("request:start", () => {
  pendingRequests += 1;
});
server.events.on("request:end", () => {
  pendingRequests -= 1;
});
// 끝나지 않는 응답을 붙잡은 테스트가 있어도 다음 테스트로 넘어가는 상한이다
const SETTLE_LIMIT_MS = 500;

async function settleRequests(): Promise<void> {
  // 가짜 시계가 아직 켜져 있으면 setTimeout 이 돌지 않는다. 그 테스트는 timers.ts 가 시계를 되돌린 뒤에 끝난다
  if (vi.isFakeTimers()) return;
  const until = Date.now() + SETTLE_LIMIT_MS;
  do {
    await new Promise((resolve) => setTimeout(resolve, 0));
  } while (pendingRequests > 0 && Date.now() < until);
}

afterEach(async () => {
  server.resetHandlers();
  cleanup();
  await settleRequests();
});

/**
 * 앞 테스트가 남긴 것을 전부 기본값으로 되돌린다. 목의 요청 기록과 mutable 상태, oidc 스텁, 브라우저 저장소, 저장소 위의
 * 스토어(테넌트·인증·테마·비서), vi.spyOn 으로 바꾼 함수(window·console 같은 전역과 leave 같은 모듈 객체)다.
 * stubProperty 로 바꾼 속성은 globals.ts 가 테스트 끝에 되돌린다. 테스트 파일은 기본값과 다른 것만 세팅한다.
 * 파일마다 beforeEach 로 되돌리면 새로 쓰는 사람이 그 관용구를 어디서도 배울 수 없고, 빠뜨리면 앞 테스트의 상태가 새어
 * 들어 lastRequest 같은 것이 엉뚱한 것을 준다. afterEach 가 아니라 beforeEach 인 것은 테스트가 끝난 뒤에 늦게 도착하는
 * 요청과 그 요청이 바꾼 목 상태까지 확실히 걷으려는 것이다.
 */
function resetMockState(): void {
  resetMswState();
  resetOidcStub();
  window.localStorage.clear();
  window.sessionStorage.clear();
  resetTenantStore();
  resetAuthState();
  resetTheme();
  resetLanguage();
  resetAssistantStore();
  vi.restoreAllMocks();
}
beforeEach(resetMockState);
afterAll(() => server.close());
