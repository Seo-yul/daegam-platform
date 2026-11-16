import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * 코드 실행(P40) 캔버스의 브라우저 확인. 목 API(mock-server.ts) 위에서 jsdom 이 못 보는 실제 배치와 누름을 본다. 메인
 * 에이전트 Tool 탭에서 클러스터 게이트가 닫혔으면 코드 실행 칸을 켤 수 없고 그 까닭이 칸 아래에 선다. 게이트가 열리면 켜고,
 * 명령 실행 전 승인을 고르고, 끈다. Cluster 화면의 격리 카드는 jsdom 테스트(src/test/code-execution.test.tsx)가 본다.
 *
 * 목 서버의 상태는 프로세스 전역이라 건드리지 않는다. 게이트는 이 테스트의 page.route 로만 바꾸고, 저장(PUT)도 목에 보내지
 * 않고 본문만 읽어 지금 문서로 답한다. 다른 스펙이 같은 ag-support 를 처음 상태로 보기 때문이다.
 */

const AGENT_DEFINITION = "/agents/ag-support?tab=definition";

/** GET /clusters/cl-home/sandbox 의 답. 목 픽스처(src/test/msw/clusters.ts clusterSandboxFixture)와 같은 꼴이다. */
function sandboxStatus(gate: { open: boolean; reason: string | null }, extra: Record<string, unknown> = {}) {
  return {
    gate,
    capacity: 3,
    nodes: [{ name: "n1", allocatable: 16 * 1024 ** 3, others: 4 * 1024 ** 3, cap: 3, used: 0 }],
    assumed: 2,
    assumed_pods: ["daegam-default/a", "daegam-default/b"],
    shared_nodes: null,
    void_reason: null,
    override: false,
    check_running: false,
    open_sandboxes: 0,
    run: { started_at: null, finished_at: null, by: null, error: null },
    ...extra,
  };
}

/** 경로가 정확히 path 인 GET 만 답을 바꾼다. /clusters/cl-home 이 /clusters/cl-home/sandbox 를 집지 않게 경로 전체로 맞댄다. */
async function answer(page: Page, path: string, handle: (route: Route) => Promise<void>) {
  await page.route(
    (url) => url.pathname === `/api${path}`,
    async (route) => (route.request().method() === "GET" ? handle(route) : route.fallback()),
  );
}

async function openMainTools(page: Page) {
  await page.goto(AGENT_DEFINITION);
  await page.getByTestId("node-agent-main").click();
  await expect(page.getByTestId("inspector-node-id")).toHaveText("agent-main");
  await page.getByTestId("inspector-tabs-tools").click();
  const field = page.getByTestId("agent-code-execution");
  await field.scrollIntoViewIfNeeded();
  return field;
}

test("캔버스: 게이트가 닫힌 클러스터에서는 코드 실행을 켤 수 없고 칸 아래에 그 까닭이 선다", async ({ page }) => {
  const field = await openMainTools(page);
  await expect(field).toContainText("코드 실행");
  await expect(page.getByTestId("agent-code-execution-on")).toBeDisabled();
  await expect(page.getByTestId("agent-code-execution-reason")).toHaveText(/격리 점검을 아직 하지 않았거나 통과하지 못해/);
  await expect(page.getByTestId("agent-code-execution-approval")).toHaveCount(0);
});

test("캔버스: 게이트가 열리면 코드 실행을 켜고 끄며, 명령 실행 전 승인을 고른 그래프를 저장한다", async ({ page }) => {
  await answer(page, "/clusters/cl-home/sandbox", (route) => route.fulfill({ json: sandboxStatus({ open: true, reason: null }) }));
  // 저장은 목에 보내지 않는다. 지금 문서를 읽어 보낸 그래프로 답한다
  await page.route(
    (url) => url.pathname === "/api/agents/ag-support",
    async (route) => {
      if (route.request().method() !== "PUT") return route.fallback();
      const current = await (await page.request.get(route.request().url())).json();
      const sent = route.request().postDataJSON();
      await route.fulfill({ json: { ...current, graph: sent.graph, layout: sent.layout, issues: [], revision_created: false } });
    },
  );
  const field = await openMainTools(page);
  const on = page.getByTestId("agent-code-execution-on");
  await expect(on).toBeEnabled();
  await expect(page.getByTestId("agent-code-execution-reason")).toHaveCount(0);
  await on.click();
  await expect(on).toBeChecked();
  const approval = page.getByTestId("agent-code-execution-approval");
  await expect(approval).not.toBeChecked();
  await on.click();
  await expect(on).not.toBeChecked();
  await expect(approval).toHaveCount(0);
  await on.click();
  await approval.click();
  await expect(approval).toBeChecked();
  await expect(field).toContainText("명령 실행 전 승인");
  await expect(page.getByTestId("canvas-dirty")).toBeVisible();
  const isSave = (r: { method(): string; url(): string }) => r.method() === "PUT" && new URL(r.url()).pathname === "/api/agents/ag-support";
  const saved = page.waitForRequest(isSave);
  const answered = page.waitForResponse((r) => isSave(r.request()));
  await page.getByTestId("canvas-save").click();
  // 저장의 답까지 기다린다. 테스트가 먼저 끝나면 가로챈 저장이 답을 짓다가 끊긴다
  expect((await answered).status()).toBe(200);
  const graph = (await saved).postDataJSON().graph as { nodes: { type: string; data: { role?: string; code_execution?: { approval: string[] } } }[] };
  const main = graph.nodes.find((n) => n.type === "agent" && n.data.role === "main");
  expect(main?.data.code_execution?.approval).toEqual(["approve", "reject"]);
});
