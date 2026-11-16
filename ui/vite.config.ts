/// <reference types="vitest/config" />
import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

import pkg from "./package.json" with { type: "json" };

// UI 가 루트를 쓰고 API 는 /api 아래에 있다. 개발 서버는 Ingress 처럼 이 접두사들만 플랫폼으로 넘긴다.
// /.well-known 은 발급자 디스커버리·JWKS·OAuth 인가 서버 메타데이터(P33)다. 브라우저가 따로 닿는 곳은 없다.
const API_PREFIXES = ["/api", "/.well-known", "/healthz"];

/**
 * ui/ 의 구성요소 도장 (D52). scripts/build-stamp.sh ui 와 같은 규칙으로, 커밋된 ui/ 트리 id 의 앞 8자다. 플랫폼만 바꾼
 * 커밋에 열린 탭이 괜히 "오래된 탭" 이 되지 않게 HEAD 가 아니라 ui/ 기준이다. 이미지 안에서는 GIT_SHA(build-stamp.sh 가
 * 낸 도장 그대로)를 답하는 git 심이 대신하므로, 40자 트리 id 일 때만 자른다.
 */
function commitSha(): string | null {
  try {
    const out = execSync("git rev-parse HEAD:./", { cwd: import.meta.dirname, stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
    return /^[0-9a-f]{40}$/.test(out) ? out.slice(0, 8) : out;
  } catch {
    return null;
  }
}

/** dist/build.json 을 남긴다. UI 컨테이너(nginx)가 /build.json 으로 낸다(D52). */
function buildInfo(build: string, sha: string | null, release: string | null): Plugin {
  return {
    name: "daegam-build-info",
    apply: "build",
    writeBundle(options) {
      const dir = options.dir ?? "dist";
      mkdirSync(dir, { recursive: true });
      writeFileSync(
        resolve(dir, "build.json"),
        JSON.stringify(
          { ui_build: build, version: pkg.version, commit: sha, release, built_at: new Date().toISOString() },
          null,
          2,
        ),
      );
    },
  };
}

/**
 * Monaco 는 DOMPurify 를 npm 패키지로 부르지 않고 자기 안에 복사본(esm/vs/base/browser/dompurify/dompurify.js)을 싣는다.
 * 그 복사본은 고친 판보다 낮을 수 있어, domSanitize.js 가 부르는 복사본을 npm 의 dompurify 로 바꿔 끼운다.
 * 그래야 package.json overrides 가 정한 판이 실제 번들에 들어가고, 의존성 감사가 보는 판과 번들의 판이 같다.
 */
export const MONACO_DOMPURIFY = "./dompurify/dompurify.js";
export const MONACO_SANITIZER = "monaco-editor/esm/vs/base/browser/domSanitize.js";

export function monacoDompurify(): Plugin {
  return {
    name: "daegam-monaco-dompurify",
    enforce: "pre",
    async resolveId(source, importer) {
      if (source !== MONACO_DOMPURIFY || !importer?.replaceAll("\\", "/").endsWith(MONACO_SANITIZER)) return null;
      return this.resolve("dompurify", importer, { skipSelf: true });
    },
  };
}

const sha = commitSha();
const UI_BUILD = `${pkg.version}${sha ? `+${sha}` : ""}`;
/** 이 번들을 낸 릴리스 태그. ui/Dockerfile 이 push-images 의 RELEASE 를 넘긴다. 릴리스가 아닌 빌드는 null 이다. */
const RELEASE = process.env.RELEASE?.trim() || null;
const apiTarget = process.env.VITE_API_PROXY ?? "http://127.0.0.1:9100";

export default defineConfig({
  base: "/",
  plugins: [monacoDompurify(), react(), tailwindcss(), buildInfo(UI_BUILD, sha, RELEASE)],
  define: { __UI_BUILD__: JSON.stringify(UI_BUILD), __UI_RELEASE__: JSON.stringify(RELEASE) },
  resolve: { alias: { "@": resolve(import.meta.dirname, "src") } },
  // 산출물은 ui/dist 다. ui/Dockerfile 이 이것을 nginx 이미지에 담는다(D52)
  build: {
    outDir: resolve(import.meta.dirname, "dist"),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      API_PREFIXES.map((p) => [p, { target: apiTarget, changeOrigin: true }]),
    ),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // 워커마다 jsdom 과 모듈을 한 번만 띄워 파일끼리 나눠 쓴다(정리 UI-11, 스위트 약 16초에서 8초). 파일 사이에 새는
    // 상태는 setup.ts 의 resetMockState 가 테스트마다 되돌리고, 늦게 도착하는 요청은 settleRequests 가 기다린다
    isolate: false,
    // 한 화면에서 여러 단계를 이어 보는 테스트가 있어, 기계가 바쁠 때 기본 5초를 넘긴다. 상한만 늘린다
    testTimeout: 15_000,
    css: false,
    // e2e/*.spec.ts 는 Playwright 몫이다
    include: ["src/**/*.test.{ts,tsx}"],
    // Monaco 는 jsdom 에서 돌지 않고 vite 가 monaco-editor 엔트리를 해석하지 못한다. 테스트는 스텁(textarea)을 쓴다.
    alias: [
      { find: /^@monaco-editor\/react$/, replacement: resolve(import.meta.dirname, "src/test/stubs/monaco-react.tsx") },
      { find: /^monaco-editor\/editor\/editor\.api$/, replacement: resolve(import.meta.dirname, "src/test/stubs/monaco-editor.ts") },
      { find: /^monaco-editor\/languages\/definitions\/(yaml|markdown)\/register$/, replacement: resolve(import.meta.dirname, "src/test/stubs/empty.ts") },
      { find: /^\.\/monacoWorker$/, replacement: resolve(import.meta.dirname, "src/test/stubs/worker.ts") },
      { find: /^oidc-client-ts$/, replacement: resolve(import.meta.dirname, "src/test/stubs/oidc-client-ts.ts") },
    ],
  },
});
