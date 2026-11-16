import { expect, test, type Page } from "@playwright/test";

import { openSelect, selectValue } from "./select";

/**
 * P7 단위 8 스모크: 목 API(mock-server.ts) 위에서 사이드바 전 항목 렌더와, jsdom 이 못 보는 실제 브라우저의 동작
 * (Monaco, 배치, 클립보드, 포커스, 끌기)을 거치는 흐름. 같은 msw 처리기를 jsdom 테스트가 이미 모는 흐름은 여기 두지 않는다.
 * 요소는 data-testid 로만 찾는다. 목 상태가 서버 프로세스 전역이라 직렬(workers 1)이다.
 *
 * 리소스 넷이 정의와 배포를 함께 가지면서(D42) 화면이 여덟에서 넷으로 줄었다. 배포는 화면 이동이
 * 아니라 상세의 배포 탭에서 거는 동작이라 스모크도 그 흐름을 따라간다.
 */

const SCREENS: { nav: string; page: string }[] = [
  { nav: "nav-backend-apis", page: "backend-apis-page" },
  { nav: "nav-mcp-servers", page: "mcp-servers-page" },
  { nav: "nav-gateways", page: "gateways-page" },
  { nav: "nav-agents", page: "agents-page" },
  { nav: "nav-skills", page: "skills-page" },
  { nav: "nav-data-services", page: "data-services-page" },
  { nav: "nav-knowledge-bases", page: "knowledge-base-list-page" },
  { nav: "nav-clusters", page: "clusters-page" },
  { nav: "nav-mcp-hub", page: "hub-page" },
  { nav: "nav-settings", page: "tenant-settings-page" },
];

test("every sidebar item renders its screen in header auth mode", async ({ page }) => {
  await page.goto("/");
  // header 모드: 로그인 버튼 없이 자리 표시만, /me 로 admin 사이드바가 붙는다
  await expect(page.getByTestId("login-placeholder")).toContainText("헤더 인증 모드");
  await expect(page.getByTestId("nav-admin-settings")).toBeVisible();
  for (const s of SCREENS) {
    await page.getByTestId(s.nav).click();
    await expect(page.getByTestId(s.page)).toBeVisible();
  }
  // 부가 화면: 감사 로그와 admin 설정 폼
  await page.getByTestId("nav-audit").click();
  await expect(page.getByTestId("audit-page")).toBeVisible();
  await page.getByTestId("nav-admin-settings").click();
  await expect(page.getByTestId("platform-settings-page")).toBeVisible();
});

test("붙여넣은 여러 줄 스펙이 한 글자도 바뀌지 않고 서버로 간다", async ({ page, context }) => {
  // Monaco 는 붙여넣은 본문을 모델의 줄바꿈으로 맞춘다. 그것이 CRLF 면 같은 문서를 붙여넣어도
  // source_sha256 이 달라져 새 버전이 생긴다. jsdom 의 스텁은 그냥 textarea 라 여기서만 잡힌다
  const spec = ['openapi: 3.0.0', 'info:', '  title: 펫스토어', '  version: "1.0"', "paths:", "  /pets:", "    get:", "      operationId: listPets"].join("\n");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/backend-apis/new");
  const editor = page.getByTestId("spec-text");
  await expect(editor).toBeVisible();
  await page.evaluate((t) => navigator.clipboard.writeText(t), spec);
  await editor.click();
  await page.keyboard.press("ControlOrMeta+V");

  const req = page.waitForRequest((r) => r.url().includes("/backend-apis/verify") && r.method() === "POST");
  await page.getByTestId("verify-button").click();
  expect(JSON.parse((await (await req).postData()) ?? "{}").text).toBe(spec);
});

test("편집 화면은 등록된 원본 문서를 코드 편집기로 보여 주고 아래로 가져온다", async ({ page }) => {
  // jsdom 은 Monaco 를 돌리지 못해 단위 테스트가 스텁 textarea 만 본다. 실제로 그려지는지는 여기서 본다
  await page.goto("/backend-apis/bp-orders/edit");
  await expect(page.getByTestId("page-title")).toContainText("Orders SOAP");
  const registered = page.getByTestId("registered-spec-text");
  await expect(registered).toContainText("<definitions");
  await expect(page.getByTestId("registered-spec-format")).toContainText("source.wsdl");

  await page.getByTestId("use-registered-spec").click();
  await expect(page.getByTestId("spec-text")).toContainText("<definitions");
  await expect(page.getByTestId("spec-filename")).toHaveValue("spec.wsdl");
});

test("custom select opens a glass listbox under the trigger and picks by mouse and keyboard", async ({ page }) => {
  await page.goto("/data-services/new");
  const trigger = page.getByTestId("ds-kind");
  await expect(page.getByTestId("ds-port")).toHaveValue("5432");

  // 마우스: 패널은 body 에 portal 로, 트리거 바로 아래에 트리거 폭으로 열린다
  const listbox = await openSelect(page, "ds-kind");
  // 등장 애니메이션(scale) 이 끝난 뒤 자리를 잰다
  await listbox.evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
  const t = (await trigger.boundingBox())!;
  const l = (await listbox.boundingBox())!;
  expect(l.y).toBeGreaterThanOrEqual(t.y + t.height);
  expect(l.y - (t.y + t.height)).toBeLessThanOrEqual(8);
  expect(Math.abs(l.x - t.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(l.width - t.width)).toBeLessThanOrEqual(1);
  await expect(listbox.locator("xpath=..")).toHaveJSProperty("tagName", "BODY");
  await selectValue(page, "ds-kind", "redis");
  await expect(page.getByTestId("ds-port")).toHaveValue("6379");
  await expect(trigger).toHaveText(/redis/);

  // 키보드: ArrowDown 으로 열고(현재 값 redis 가 활성), ArrowUp 으로 postgres 로 옮겨 Enter 로 고른다
  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await expect(listbox).toBeVisible();
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await expect(listbox).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(page.getByTestId("ds-port")).toHaveValue("5432");

  // 스크롤: 닫히지 않고 트리거를 따라 다시 자리 잡는다 (D43 다섯째). 뷰포트를 줄여 main 이 스크롤하게 만든다
  await page.setViewportSize({ width: 1280, height: 380 });
  const main = page.locator("main");
  await expect(main).toHaveJSProperty("scrollTop", 0);
  await openSelect(page, "ds-kind");
  await listbox.evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
  const before = (await listbox.boundingBox())!;
  await main.evaluate((el) => el.scrollBy(0, 120));
  await expect(main).not.toHaveJSProperty("scrollTop", 0);
  // 재배치는 rAF 안에서 돈다
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(null)))));
  await expect(listbox).toBeVisible();
  const after = (await listbox.boundingBox())!;
  const anchor = (await trigger.boundingBox())!;
  expect(after.y).not.toBeCloseTo(before.y, 0);
  expect(Math.abs(after.x - anchor.x)).toBeLessThanOrEqual(1);
  // 트리거 바로 아래이거나 바로 위에 붙어 있다
  expect(Math.min(Math.abs(after.y - (anchor.y + anchor.height)), Math.abs(anchor.y - (after.y + after.height)))).toBeLessThanOrEqual(8);
});

test("mcp server Advanced tab lazy-loads the real Monaco editor", async ({ page }) => {
  await page.goto("/mcp-servers/ms-calc");
  await page.getByTestId("tab-advanced").click();
  await expect(page.getByTestId("advanced-editor")).toBeVisible();
  // 스텁이 아니라 실제 lazy 청크: monaco 편집기 DOM 이 뜬다
  await expect(page.getByTestId("yaml-editor").locator(".monaco-editor").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("locked-list")).toContainText("spec.image");
});

test("assistant panel streams a scripted turn and confirms a deploy", async ({ page }) => {
  // 오른쪽 청지기 패널 (D31). 목 API 의 대본: 도구 호출 → 리소스 카드 → 승인 카드 → 승인 → 새 리소스 카드 → done 배지
  await page.goto("/mcp-servers");
  await expect(page.getByTestId("mcp-servers-page")).toBeVisible();
  await expect(page.getByTestId("assistant-panel")).toHaveCount(0);
  await page.getByTestId("assistant-toggle").click();
  const panel = page.getByTestId("assistant-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("assistant-wordmark")).toHaveText("청지기");
  await panel.getByTestId("assistant-input").fill("calc 를 배포해 줘");
  await panel.getByTestId("assistant-send").click();
  await expect(panel.getByTestId("chat-message-0")).toHaveAttribute("data-role", "user");
  const call = panel.locator('[data-role="tool_call"]').first();
  await expect(call).toContainText("get_mcp_server");
  await expect(call).toHaveAttribute("data-status", "success");
  await expect(panel.getByTestId("resource-card").first()).toHaveAttribute("href", "/mcp-servers/ms-calc");
  const card = panel.getByTestId("approval-card");
  await expect(card).toContainText("deploy_mcp_server");
  await expect(card.getByTestId("decision-approve")).toBeEnabled();
  await card.getByTestId("decision-approve").click();
  await expect(card).toHaveAttribute("data-resolved", "true");
  await expect(panel.getByTestId("resource-card").last()).toHaveAttribute("href", "/mcp-servers/ms-new");
  await expect(panel.locator('[data-role="done"]').last()).toContainText("턴 2");
  // 세션은 테넌트별로 기억되고 다시 열면 전사를 복원한다
  await page.getByTestId("assistant-close").click();
  await expect(page.getByTestId("assistant-panel")).toHaveCount(0);
  await page.getByTestId("assistant-toggle").click();
  await expect(page.getByTestId("assistant-panel").locator('[data-role="done"]').last()).toContainText("턴 2");
});

/** 포트 점에서 노드로 끄는 동작. 포트는 캔버스가 축소돼 있으면 7px 남짓이라 좌표를 직접 계산해
 * 누르면 CI 처럼 느리거나 배율이 다른 환경에서 빗나간다. 그러면 연결이 시작되지 않아 뒤따르는
 * 단언이 "힌트가 없다" 로 엉뚱하게 실패한다. hover() 로 요소가 안정될 때까지 기다렸다가 누르고,
 * 연결선이 실제로 생겼는지 여기서 확인해 실패 지점을 제자리로 돌린다. */
async function dragPort(page: Page, portTestId: string, nodeTestId: string) {
  await page.getByTestId(portTestId).hover();
  await page.mouse.down();
  const to = await page.getByTestId(nodeTestId).boundingBox();
  if (!to) throw new Error(`${nodeTestId} is not visible`);
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 });
  await expect(page.locator(".react-flow__connectionline"), `${portTestId} 에서 연결이 시작되지 않았다`).toHaveCount(1);
  return { release: () => page.mouse.up() };
}

/** 화면 맞추기·줌은 애니메이션이라 변환이 멎은 뒤에 재야 값이 흔들리지 않는다. */
async function settleViewport(page: Page) {
  let prev = "";
  await expect
    .poll(
      async () => {
        const now = (await page.locator(".react-flow__viewport").getAttribute("style")) ?? "";
        const same = now !== "" && now === prev;
        prev = now;
        return same;
      },
      { timeout: 5_000, intervals: [120] },
    )
    .toBe(true);
}

/** 기본 줌에 바닥이 생겨(P24 F4) 노드가 늘면 그래프가 화면보다 넓어진다. 포트를 끄는 동작은 양 끝이
 * 같이 보여야 하므로, 사용자가 하듯 줌 컨트롤을 눌러 다 보일 때까지 내린다. */
async function showWholeGraph(page: Page, testIds: string[]) {
  const zoomOut = page.locator(".react-flow__controls-zoomout");
  const pane = (await page.locator(".react-flow__pane").boundingBox())!;
  const allInside = async () => {
    const boxes = await Promise.all(testIds.map((id) => page.getByTestId(id).boundingBox()));
    return boxes.every((b) => b !== null && b.x >= pane.x && b.x + b.width <= pane.x + pane.width && b.y >= pane.y && b.y + b.height <= pane.y + pane.height);
  };
  await settleViewport(page);
  for (let i = 0; i < 10 && !(await allInside()); i++) {
    if (!(await zoomOut.isEnabled())) break;
    await zoomOut.click();
    await settleViewport(page);
  }
  expect(await allInside(), `줌을 내려도 ${testIds.join(", ")} 가 다 보이지 않는다`).toBe(true);
}

/** 캔버스는 넓은 화면에서 쓰는 화면이다(접근성 마무리에서 좁은 폭 캔버스는 하지 않기로 정했다).
 * 기본 1280 에서는 팔레트와 인스펙터를 빼면 캔버스가 358px 이라 노드 하나가 폭을 채운다. */
const CANVAS_VIEWPORT = { width: 1600, height: 900 };

/** 저장된 Agent 의 캔버스는 상세의 정의 탭 안이다 (D42). */
const AGENT_DEFINITION = "/agents/ag-support?tab=definition";

test("agent canvas ports: a drag from a kind port connects only nodes that kind can attach to (D28)", async ({ page }) => {
  await page.setViewportSize(CANVAS_VIEWPORT);
  await page.goto(AGENT_DEFINITION);
  await expect(page.getByTestId("node-agent-main")).toBeVisible();
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok");
  // 아무것도 고르지 않은 채 모델을 추가하면 붙지 않은 모델(경고)이고, 서브 에이전트는 모델이 없다(model_count)
  await page.getByTestId("palette-model").click();
  await expect(page.getByTestId("node-model")).toBeVisible();
  await page.getByTestId("agent-canvas").click({ position: { x: 5, y: 5 } });
  await page.getByTestId("palette-agent").click();
  await expect(page.getByTestId("node-agent")).toBeVisible();
  await expect(page.getByTestId("node-model-agent")).toContainText("모델 없음");
  // 자동 배치: 모델·메모리는 에이전트 위(메모리가 왼쪽), 도구는 오른쪽, 가드레일은 입구 아래
  await page.getByTestId("canvas-autolayout").click();
  await expect
    .poll(async () => {
      const box = async (id: string) => (await page.getByTestId(id).boundingBox())!;
      const [agent, model, memory, tools, entry, guard] = await Promise.all(["node-agent-main", "node-model-main", "node-memory", "node-tools-calculator", "node-entry", "node-guardrail-input"].map(box));
      return model.y + model.height <= agent.y && memory.y + memory.height <= agent.y && memory.x + memory.width <= model.x && tools.x >= agent.x + agent.width && guard.y >= entry.y + entry.height;
    })
    .toBe(true);
  await showWholeGraph(page, ["node-agent-main", "node-model", "node-agent"]);
  // 도구 포트를 모델 노드에 놓으면 거부한다 (흐려지고 힌트가 보인다)
  const wrong = await dragPort(page, "port-agent-main-tools-out", "node-model");
  await expect(page.getByTestId("node-hint-model")).toContainText("Tool 포트는 모델에 연결할 수 없습니다.");
  await wrong.release();
  await expect(page.getByTestId("node-agents-model")).toContainText("에이전트 0");
  // 서브 에이전트의 모델 포트를 모델 노드에 놓으면 붙는다
  const right = await dragPort(page, "port-agent-model-out", "node-model");
  await expect(page.getByTestId("node-hint-model")).toHaveCount(0);
  await right.release();
  await expect(page.getByTestId("node-agents-model")).toContainText("에이전트 1");
  await expect(page.getByTestId("node-model-agent")).toContainText("model");
  // 메인은 이미 모델이 있으니 두 번째 모델은 거부한다
  const second = await dragPort(page, "port-agent-main-model-out", "node-model");
  await expect(page.getByTestId("node-hint-model")).toContainText("모델은 하나만 붙일 수 있습니다.");
  await second.release();
  await expect(page.getByTestId("node-agents-model")).toContainText("에이전트 1");
  await expect(page.getByTestId("canvas-dirty")).toBeVisible();
});

test("agent canvas: 팔레트로 놓은 노드로 화면이 따라가고 선택 방법을 캔버스가 말한다", async ({ page }) => {
  await page.goto(AGENT_DEFINITION);
  await expect(page.getByTestId("node-agent-main")).toBeVisible();
  const pane = (await page.locator(".react-flow__pane").boundingBox())!;
  const visible = async (testId: string) => {
    const b = await page.getByTestId(testId).boundingBox();
    return Boolean(b && b.x + b.width > pane.x && b.x < pane.x + pane.width && b.y + b.height > pane.y && b.y < pane.y + pane.height);
  };
  const zoom = async () => Number((await page.locator(".react-flow__viewport").getAttribute("style"))?.match(/scale\(([\d.]+)\)/)?.[1] ?? NaN);

  // 한쪽만 보이게 끝까지 확대한다. 기본 줌이 올라가(P24 F4) 최대 배율에 더 적게 눌러도 닿으므로
  // 버튼이 잠기면 멈춘다
  const zoomIn = page.locator(".react-flow__controls-zoomin");
  for (let i = 0; i < 6 && (await zoomIn.isEnabled()); i++) await zoomIn.click();
  const before = await zoom();
  // 팔레트로 놓으면 화면 밖에 앉을 수 있다. 배율은 그대로 두고 그 노드로 옮겨야 추가된 것이 보인다
  await page.getByTestId("palette-knowledge").click();
  await expect.poll(() => visible("node-knowledge")).toBe(true);
  await expect.poll(zoom).toBeCloseTo(before, 2);

  // 여러 개 고르기는 눌러 보기 전에는 알 수 없으므로 캔버스가 직접 적는다
  const hint = page.getByTestId("canvas-select-hint");
  await expect(hint).toContainText("영역 선택");
  await expect(hint).toContainText("여러 개");
});

test("agent canvas: 반대로 끈 연결을 맞는 방향으로 돌려 잇는다 (D28)", async ({ page }) => {
  await page.setViewportSize(CANVAS_VIEWPORT);
  await page.goto(AGENT_DEFINITION);
  await expect(page.getByTestId("node-agent-main")).toBeVisible();
  // 아직 아무것에도 안 붙은 모델 노드와 서브 에이전트를 하나씩 둔다
  await page.getByTestId("palette-model").click();
  await expect(page.getByTestId("node-model")).toBeVisible();
  await page.getByTestId("agent-canvas").click({ position: { x: 5, y: 5 } });
  await page.getByTestId("palette-agent").click();
  await expect(page.getByTestId("node-agent")).toBeVisible();
  await expect(page.getByTestId("node-model-agent")).toContainText("모델 없음");
  // 방금 놓은 노드는 화면 밖에 앉을 수 있다. 자리를 잡아야 포트를 끌 수 있다
  await page.getByTestId("canvas-autolayout").click();
  await expect.poll(async () => Boolean(await page.getByTestId("port-model-model-in").boundingBox())).toBe(true);
  await showWholeGraph(page, ["node-model", "node-agent"]);

  // 모델의 "도착" 포트에서 에이전트로 끈다. 표에 있는 방향은 에이전트에서 모델로 가는 것 하나뿐이라 흐려지지 않고 뒤집어 붙는다
  const back = await dragPort(page, "port-model-model-in", "node-agent");
  await expect(page.getByTestId("node-hint-agent")).toHaveCount(0);
  await back.release();
  await expect(page.getByTestId("node-model-agent")).toContainText("model");
  await expect(page.getByTestId("node-agents-model")).toContainText("에이전트 1");
});

// 목 상태(backendApiState)에 bp-pets v2 를 붙이므로 bp-pets 를 보는 테스트들 뒤에 둔다
test("backend api version Generate menu refines with rules, shows the side-by-side diff and applies a new version", async ({ page }) => {
  await page.goto("/backend-apis/bp-pets");
  await page.getByTestId("version-row-1").getByTestId("generate-menu").click();
  await page.getByTestId("generate-minify").click();
  await expect(page.getByTestId("refine-page")).toHaveAttribute("data-mode", "minify");
  await page.getByTestId("enrich-errors").check();
  await page.getByTestId("refine-run").click();
  await expect(page.getByTestId("spec-diff-summary")).toContainText("operation 4개 중 2개의 설명이 바뀌었습니다. 호출 표면은 바뀌지 않았습니다.");
  await expect(page.getByTestId("change-group-schema:Error")).toContainText("operation 1개가 참조");
  // 스텁이 아니라 실제 lazy 청크: Monaco diff 편집기 DOM 이 뜨고, 채택을 풀면 오른쪽이 바뀐다
  const editor = page.getByTestId("spec-diff-editor");
  await expect(editor.locator(".monaco-diff-editor").first()).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("change-m2-accept").uncheck();
  await expect(page.getByTestId("spec-diff-count")).toContainText("채택 4개");

  // 좁은 칸에서는 좌우로 나누지 않는다. Monaco 는 나란히 두기를 고집해 원본 쪽을 몇 픽셀로
  // 찌그러뜨리고, 그러면 글자 한 칸씩만 비어져 나오는 띠가 남는다
  await expect(page.getByTestId("diff-layout")).toBeDisabled();
  await expect(page.getByTestId("diff-layout-note")).toHaveText("지운 줄과 더한 줄을 한 칸에 이어서 봅니다");
  await expect(page.getByTestId("spec-diff-editor").locator(".editor.original")).toBeHidden();
  // 넓어지면 좌우로 볼 수 있고, 그때 원본 칸이 실제로 자리를 얻는다
  await page.setViewportSize({ width: 2200, height: 900 });
  await expect(page.getByTestId("diff-layout")).toBeEnabled();
  await expect(page.getByTestId("diff-layout-note")).toHaveText("왼쪽 원본 · 오른쪽 채택 적용");
  const original = page.getByTestId("spec-diff-editor").locator(".editor.original");
  await expect(original).toBeVisible();
  expect((await original.boundingBox())!.width).toBeGreaterThan(300);
  await page.getByTestId("diff-layout").click();
  await expect(page.getByTestId("diff-layout")).toHaveText("좌우로 보기");

  await page.getByTestId("apply-draft").click();
  await expect(page.getByTestId("backend-api-detail-page")).toBeVisible();
  await expect(page).toHaveURL(/\/backend-apis\/bp-pets\?version=2$/);
  await expect(page.getByTestId("version-row-2")).toHaveAttribute("data-selected", "true");
  await expect(page.getByTestId("summary-latest-version")).toHaveText("2");
});

// 아래 둘은 목 상태(mcpServerState·agentState)를 바꾸므로 그 리소스를 보는 테스트들 뒤에 둔다
// 목 상태(agentState)에 ag-new 를 붙이므로 마지막에 둔다
test("agent canvas: template → inspector fills provider, tool source and database → validate → save → deploy tab", async ({ page }) => {
  await page.goto("/agents/new?template=support-agent");
  await expect(page.getByTestId("agent-canvas")).toBeVisible();
  await expect(page.getByTestId("node-agent-main")).toBeVisible();
  await expect(page.getByTestId("node-model-main")).toBeVisible();
  await expect(page.getByTestId("canvas-legend")).toContainText("모델");
  await expect(page.getByTestId("canvas-legend")).toContainText("서브 에이전트");
  // 종류별 포트 (D28): 에이전트의 모델 포트는 위, 모델 노드의 도착 포트는 아래
  await expect(page.getByTestId("port-agent-main-model-out")).toHaveClass(/react-flow__handle-top/);
  await expect(page.getByTestId("port-model-main-model-in")).toHaveClass(/react-flow__handle-bottom/);
  // 템플릿의 빈 자리 셋이 오류로 표시되고 저장은 잠긴다. provider 는 모델 노드의 것이라 표식도 모델 노드에 붙는다
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "error");
  await expect(page.getByTestId("node-mark-model-main")).toBeVisible();
  await expect(page.getByTestId("node-mark-agent-main")).toHaveCount(0);
  await expect(page.getByTestId("canvas-save")).toBeDisabled();
  await page.getByTestId("ap-name").fill("smoke");
  // 인스펙터: 에이전트에는 모델 탭이 없고 붙은 모델이 보인다. 가드레일 탭 이름
  await page.getByTestId("node-agent-main").click();
  await expect(page.getByTestId("inspector-node-id")).toHaveText("agent-main");
  await expect(page.getByTestId("inspector-tabs-model")).toHaveCount(0);
  await expect(page.getByTestId("attached-model-main")).toBeVisible();
  await expect(page.getByTestId("inspector-tabs-guardrail")).toHaveText("가드레일");
  // 모델 노드를 골라 provider 를 고르면 temperature(openai) 가 나오고 에이전트 카드의 모델 칩에도 보인다
  await page.getByTestId("node-model-main").click();
  await expect(page.getByTestId("inspector-node-id")).toHaveText("model-main");
  await selectValue(page, "model-provider", "openai-main");
  await expect(page.getByTestId("model-temperature")).toBeVisible();
  await expect(page.getByTestId("node-provider-model-main")).toContainText("openai-main");
  await expect(page.getByTestId("node-model-agent-main")).toContainText("openai-main");
  // 도구 소스는 배포된 것만 고를 수 있다 (D42)
  await page.getByTestId("node-tools-calculator").click();
  await selectValue(page, "tools-source", "gw-support");
  await expect(page.getByTestId("tool-allow-Add")).toBeChecked();
  await page.getByTestId("node-memory").click();
  await selectValue(page, "memory-data-service", "ds-pg");
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok");
  // 서버 검증 → 배지가 서버 결과를 보인다
  await page.getByTestId("canvas-validate").click();
  await expect(page.getByTestId("validation-badge")).toContainText("서버");
  await page.getByTestId("canvas-save").click();
  // 저장하면 상세로 간다. 만든 것은 아직 안 올라간 상태라 배포 탭이 그 길을 준다
  await expect(page).toHaveURL(/\/agents\/ag-new$/);
  await expect(page.getByTestId("state-badge")).toHaveText("미배포");
  await page.getByTestId("tab-deployment").click();
  await expect(page.getByTestId("deployment-empty")).toContainText("아직 배포하지 않았습니다");
  await page.getByTestId("deployment-deploy").click();
  await expect(page.getByTestId("deploy-dialog")).toBeVisible();
  await expect(page.getByTestId("ag-preview")).toContainText("agent-smoke");
});

/**
 * 목록 카드는 이름 글자가 아니라 카드 전체가 과녁이다 (D47).
 *
 * 이름만 링크였을 때는 카드를 눌러도 아무 일이 없었다.
 * jsdom 은 레이아웃을 계산하지 않아 덮개가 무엇을 가리는지 판정하지 못하므로 이 단정은 브라우저에만 산다.
 */
test("목록 카드는 본문을 눌러도 상세로 가고, 더보기는 덮개 위에 남는다", async ({ page }) => {
  await page.goto("/hub");
  await expect(page.getByTestId("hub-row-calc")).toBeVisible();
  await page.getByTestId("hub-row-calc").click({ position: { x: 200, y: 60 } });
  await expect(page).toHaveURL(/\/hub\/hub-calc/);

  await page.goto("/hub");
  await page.getByTestId("card-menu-weather").click();
  await expect(page.getByTestId("action-menu-panel")).toBeVisible();
  await expect(page).toHaveURL(/\/hub$/);

  // 설명이 놓인 몸을 눌러도 상세로 간다. 몸은 덮개 아래라 카드의 그 자리를 누른다
  await page.goto("/agents");
  await expect(page.getByTestId("agent-description-support")).toBeVisible();
  await page.getByTestId("agent-row-support").click({ position: { x: 200, y: 70 } });
  await expect(page).toHaveURL(/\/agents\/ag-support/);
});

/**
 * 복사는 평문 http 에서도 돼야 한다.
 *
 * 브라우저는 `navigator.clipboard` 를 보안 컨텍스트(https·localhost)에만 준다. 평문 http 로 여는 배포(로컬의
 * `http://daegam.test`, sslip.io 같은 시험 주소)에서는 그 API 가 아예 없어, 대체 경로가 없으면 복사 버튼이 아무
 * 일도 하지 않는다.
 *
 * jsdom 은 execCommand 를 구현하지 않아 단위 테스트로는 이 경로를 못 본다.
 */
test("복사는 보안 컨텍스트가 아니어도 클립보드에 실제로 들어간다", async ({ page }) => {
  // 상세 셋이 서로 다른 복사 버튼을 쓰다가 Gateway·Agent 쪽만 옛 방식으로 남아 평문 http 에서
  // 조용히 아무 일도 안 했다. 세 자리를 다 본다
  for (const [path, urlId, copyId] of [
    ["/hub/hub-calc", "hub-backend-url", "copy-backend-url"],
    ["/gateways/gw-support", "status-url", "copy-url"],
    ["/agents/ag-support", "status-chat-url", "copy-chat-url"],
  ] as const) {
    await page.goto(path);
    const shown = ((await page.getByTestId(urlId).innerText()) ?? "").trim();
    expect(shown.length).toBeGreaterThan(0);
    await page.getByTestId(copyId).click();
    await expect(page.getByTestId(copyId)).toHaveAttribute("title", "복사됨");

    // 진짜 키보드 붙여넣기로 확인한다. 아이콘이 바뀌는 것만으로는 클립보드에 들어갔다고 할 수 없다
    await page.goto("/hub");
    const search = page.getByPlaceholder("Search hub…");
    await search.click();
    await page.keyboard.press("ControlOrMeta+V");
    expect(await search.inputValue()).toBe(shown);
  }
});

/**
 * 떠 있는 패널의 첫 포커스 (P36). 자리 잡기 훅(usePopover)은 자리를 재기 전 커밋에 패널을 숨겨 두는데, 브라우저는
 * `visibility: hidden` 인 요소에 포커스를 주지 않는다. jsdom 은 그것을 보지 않아 인용 작은 창이 포커스를 못 받고 Escape 로
 * 닫히지 않던 결함을 단위 테스트가 놓쳤다. 같은 훅을 쓰는 동작 메뉴의 첫 항목 포커스도 함께 본다.
 */
test("인용 작은 창과 동작 메뉴는 열리자마자 포커스를 받고 Escape 로 닫혀 단추로 돌아간다", async ({ page }) => {
  await page.goto("/agents/ag-support?tab=chat");
  await page.getByTestId("chat-input").fill("환불 기한이 언제까지야?");
  await page.getByTestId("chat-send").click();
  const chip = page.getByTestId("chat-transcript").getByTestId("citation-chip-1");
  const popover = page.getByTestId("citation-popover");

  // 키보드로 연다
  await chip.focus();
  await page.keyboard.press("Enter");
  await expect(popover).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await expect(chip).toBeFocused();

  // 마우스로 연다
  await chip.click();
  await expect(popover).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);

  await page.goto("/hub");
  const trigger = page.getByTestId("card-menu-weather");
  await trigger.click();
  await expect(page.getByTestId("action-menu-panel").getByRole("menuitem").first()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("action-menu-panel")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

/**
 * 청지기의 Agent 정의 제안 (P48 6.4·6.5절). 목 API 의 제안 대본(ag-support r2 위의 바꾸기)을 패널의 카드에서 캔버스로 열고
 * "제안대로 저장" 한다. 캔버스는 패널을 접지 않고, 승인은 지문을 싣는다. 목 서버의 ag-support 는 다른 스펙과 함께 쓰므로
 * 결정은 이 테스트의 page.route 가 받아 대본의 결과(r3 저장)로 답하고 목에 보내지 않는다(sandbox.spec.ts 와 같다).
 */
test("assistant opens Steward's Agent proposal from the card on the canvas and saves it as proposed", async ({ page }) => {
  await page.setViewportSize(CANVAS_VIEWPORT);
  let decision: unknown = null;
  await page.route("**/api/assistant/sessions/*/decisions", async (route) => {
    decision = route.request().postDataJSON();
    const events = [
      { event: "tool_call", data: { id: "call-proposal-1", name: "update_agent", args: { agent_id: "ag-support" } } },
      { event: "tool_result", data: { id: "call-proposal-1", name: "update_agent", status: "success", content: JSON.stringify({ id: "ag-support", revision: 3 }) } },
      { event: "token", data: { text: "상담 에이전트를 r3으로 저장했습니다." } },
      { event: "done", data: { session_id: "", turn: 2, usage: { input_tokens: 80, output_tokens: 12, total_tokens: 92, cached_input_tokens: 0 }, tool_calls: 1, text: "상담 에이전트를 r3으로 저장했습니다." } },
    ];
    await route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body: events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("") });
  });
  await page.goto("/agents");
  await page.getByTestId("assistant-toggle").click();
  const panel = page.getByTestId("assistant-panel");
  await panel.getByTestId("assistant-input").fill("상담 에이전트에 환불 안내를 더하는 제안을 해 줘");
  await panel.getByTestId("assistant-send").click();
  const card = panel.getByTestId("approval-card");
  await expect(card.getByTestId("approval-proposal-0-attention")).toContainText("저장하기 전에 확인할 점");
  await expect(card.getByTestId("approval-proposal-0-text-agent-main-system_prompt")).toContainText("환불 요청은 상담원에게 넘긴다.");
  await expect(card.getByTestId("decision-approve")).toHaveText("제안대로 저장");
  await card.getByTestId("approval-proposal-0-open-canvas").click();

  await expect(page).toHaveURL(/\/agents\/ag-support\?tab=definition&proposal=p-1&session=/);
  const bar = page.getByTestId("canvas-proposal");
  await expect(bar).toHaveAttribute("data-state", "open");
  await expect(bar.getByTestId("canvas-proposal-removed-guardrail-input")).toBeVisible();
  await expect(page.getByTestId("node-guardrail-output")).toHaveAttribute("data-proposal", "added");
  await expect(page.getByTestId("node-agent-main")).toHaveAttribute("data-proposal", "changed");
  await expect(page.getByTestId("canvas-dirty")).toBeVisible();
  // 캔버스와 그 대화를 함께 본다
  await expect(page.getByTestId("assistant-panel")).toBeVisible();

  await bar.getByTestId("canvas-proposal-approve").click();
  await expect(page.getByTestId("canvas-notice")).toContainText("r3");
  await expect(page.getByTestId("canvas-proposal")).toHaveCount(0);
  await expect(page.getByTestId("assistant-panel").getByTestId("approval-card")).toHaveAttribute("data-resolved", "true");
  expect(decision).toEqual({ decisions: [{ id: "call-proposal-1", type: "approve", proposal_digest: "p48.1:digest-1" }] });
});
