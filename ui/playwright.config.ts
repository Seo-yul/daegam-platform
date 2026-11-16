import { defineConfig, devices } from "@playwright/test";

/**
 * P7 단위 8: MSW 목 API 위의 브라우저 스모크. 목 서버(9300) + vite dev(4173, 루트 base) 를
 * playwright 가 직접 띄운다. 목 상태가 프로세스 전역이라 직렬로 돈다.
 */
export default defineConfig({
  testDir: "./e2e",
  // 실제 클러스터를 향하는 스펙은 여기서 뺀다. full.spec.ts·agent.spec.ts 는 playwright.full.config.ts
  // (make e2e-ui-full) 로 돈다.
  // 빼지 않으면 목 API 위의 스모크가 집어 들어 E2E_UI_BASE 없이 실행되고 Invalid URL 로 죽는다
  testIgnore: ["full.spec.ts", "agent.spec.ts"],
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    // 화면은 사용자가 고르기 전까지 한국어다(P49). 첫 언어가 브라우저 언어를 읽지 않으므로 지키는 줄일 뿐이다
    locale: "ko-KR",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "npx vite-node e2e/mock-server.ts",
      url: "http://127.0.0.1:9300/healthz",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      // macOS 에서 localhost 가 ::1 만 잡을 수 있어 127.0.0.1 을 명시한다
      command: "npx vite dev --host 127.0.0.1 --port 4173 --strictPort",
      url: "http://127.0.0.1:4173/",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { VITE_API_PROXY: "http://127.0.0.1:9300" },
    },
  ],
});
