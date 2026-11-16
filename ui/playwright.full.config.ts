import { defineConfig, devices } from "@playwright/test";

/**
 * P7 단위 9 + P12 F5: 실제 deploy-local 클러스터를 향한 full e2e.
 * `make e2e-ui-full` 이 ingress(http://daegam.test, E2E_UI_BASE)로 이 설정을 돌린다.
 * UI 는 플랫폼 이미지에 든 정적 빌드를 루트에서 그대로 쓴다. 배포물 자체를 검증한다.
 * 시나리오 둘(허브 → Gateway, Agent 캔버스 → 채팅)을 직렬 1 worker 로 돌리고 Ready 대기(5분 상한)가 있어 타임아웃이 길다.
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: ["full.spec.ts", "agent.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 900_000, // 시나리오 전체 (Ready 5분 x2 + 나머지)
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_UI_BASE ?? "http://127.0.0.1:19100",
    // 화면은 사용자가 고르기 전까지 한국어다(P49). 첫 언어가 브라우저 언어를 읽지 않으므로 지키는 줄일 뿐이다
    locale: "ko-KR",
    trace: "retain-on-failure",
    // 폴링 화면이 많아 액션 타임아웃을 넉넉히 둔다
    actionTimeout: 20_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // webServer 없음: 플랫폼 port-forward 는 make 타깃이 관리한다
});
