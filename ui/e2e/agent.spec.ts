import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { expect, test, type APIRequestContext } from "@playwright/test";

import { HEADERS, SOAP_ENDPOINT, SPIKE_NS, WSDL_PATH, deleteFromDetail, deleteLeftovers, recordCreate, skipIfOidc } from "./cluster";
import { selectValue } from "./select";

/**
 * P12 F5 (D26): Agent 캔버스에서 검증하고 저장한 뒤 배포해 상세에서 채팅 한 턴을 나누는 흐름을 브라우저로 재현한다.
 * 대상: `make deploy-local` 클러스터의 플랫폼(ingress http://daegam.test, header 모드). `make e2e-ui-full` 이 full.spec.ts 와
 * 함께 한 worker 로 돌린다. Playwright 가 파일을 경로 순서로 돌리므로 이 스펙이 먼저이고, 둘은 서로의 리소스에 기대지 않는다.
 *
 * 화면이 대상인 것: 캔버스에 템플릿 불러오기(`?template=support-agent`), 인스펙터에서 provider·도구 소스·Database 고르기,
 * 검증 배지(클라이언트 → 서버), 저장(리비전 1, 문서는 아직 draft), 배포 탭의 배포 대화상자, 상세의 Ready 폴링과
 * 채팅 탭(SSE 도구 호출·결과·done), 홈 챗(D35)에서 같은 에이전트를 골라 한 턴과 새로고침 뒤 전사 복원, 화면보다 오래 사는
 * 턴(D92: 새로고침·끊긴 스트림·다른 대화로 갔다 돌아오기 뒤에도 답이 나타나고 중단 단추만 멈춘다), 상세에서 삭제.
 *
 * 정의와 배포가 한 리소스이므로(D42) 저장이 곧 만들기이고 배포는 상세에서 여는 대화상자다. 배포 본문에 이름이
 * 없고, 삭제 하나가 정의와 클러스터의 것을 함께 지운다. 그래서 지울 것도 에이전트 하나뿐이다.
 *
 * API 로 준비하는 것(이 시나리오의 대상이 아닌 화면): stub provider 등록(PATCH /settings. 배포본이 DAEGAM_ALLOW_STUB_PROVIDER 를
 * 켰을 때만 되므로 아니면 SKIP. 원래 없었으면 afterEach 가 다시 걷어 Platform Settings 에 실측 흔적을 남기지 않는다),
 * 도구 소스가 될 MCP Server(calculator.wsdl → mock-soap, tools [Add, Divide])를 저장한 뒤 배포해 Ready 까지,
 * lab Postgres 를 Database 로 등록(비밀번호는 kubectl 로 lab Secret 에서 읽는다. 없으면 SKIP).
 * 채팅은 스텁 모델이라 메시지 안 JSON 조각 `{"tool":"Add","args":{"a":3,"b":4}}` 을 그대로 도구 호출로 옮기고 결과 7 을 답한다.
 *
 * 규칙: 요소는 data-testid 로만 찾는다. 이름에 run id 를 붙여 재실행이 안전하다. 만든 리소스는 만들기 응답으로 기록해
 * afterEach 가 남은 것을 지운다. 스크린샷 두 장(캔버스·채팅)은 E2E_SHOTS_DIR(기본 test-results) 에 남긴다.
 */

const READY_TIMEOUT = 300_000;
const RID = Date.now().toString(36).slice(-6);
const BP_NAME = `uia-${RID}-calc`;
const MS_NAME = `uia-${RID}-mcp`;
const DS_NAME = `uia-${RID}-pg`;
const AG_NAME = `uia-${RID}-agent`;
const MS_DISPLAY = `계산기 ${RID}`;
const AG_DISPLAY = `상담 에이전트 ${RID}`;
const STUB_PROVIDER = "stub-e2e";
/** 이 스펙이 stub provider 를 새로 넣었으면 true. afterEach 가 그때만 걷는다 */
let stubAdded = false;
/** 이 실행이 만든 리소스의 API 경로(만든 순서). afterEach 가 남은 것을 지운다 */
const created: string[] = [];
const KUBE_CONTEXT = process.env.KUBE_CONTEXT ?? "orbstack";
const PG_HOST = `postgres.${SPIKE_NS}.svc`;
const SHOTS = process.env.E2E_SHOTS_DIR ?? "test-results";

function labSecret(key: string): string | null {
  try {
    const b64 = execFileSync("kubectl", ["--context", KUBE_CONTEXT, "-n", SPIKE_NS, "get", "secret", "postgres-credentials", "-o", `jsonpath={.data.${key}}`], { encoding: "utf8" });
    return b64 ? Buffer.from(b64, "base64").toString("utf8") : null;
  } catch {
    return null;
  }
}

/**
 * stub provider 를 뺀 llm_providers 목록. PATCH 는 목록 전체를 바꾸므로 나머지는 GET 이 준 그대로 돌려보낸다.
 * 필드를 골라 옮기면 고르지 않은 필드(embedding_model 등)가 다른 provider 에서 지워진다.
 */
async function providersWithout(request: APIRequestContext, base: string): Promise<Record<string, unknown>[]> {
  const settings = await (await request.get(`${base}/api/settings`, { headers: HEADERS })).json();
  return (settings.llm_providers as Record<string, unknown>[]).filter((p) => p.name !== STUB_PROVIDER);
}

/** kind stub provider 를 등록한다. 이미 있었으면 present, 새로 넣었으면 added, 배포본이 막으면 disallowed(422 stub_provider_disallowed). */
async function ensureStubProvider(request: APIRequestContext, base: string): Promise<"present" | "added" | "disallowed"> {
  const settings = await (await request.get(`${base}/api/settings`, { headers: HEADERS })).json();
  if ((settings.llm_providers as Record<string, unknown>[]).some((p) => p.name === STUB_PROVIDER && p.kind === "stub")) return "present";
  const providers = await providersWithout(request, base);
  providers.push({ name: STUB_PROVIDER, kind: "stub" });
  const res = await request.patch(`${base}/api/settings`, { headers: HEADERS, data: { llm_providers: providers } });
  if (res.status() === 422 && (await res.json()).detail?.code === "stub_provider_disallowed") return "disallowed";
  expect(res.status(), await res.text()).toBe(200);
  return "added";
}

async function waitReady(request: APIRequestContext, url: string): Promise<void> {
  await expect
    .poll(async () => ((await (await request.get(url, { headers: HEADERS })).json()).status ?? {}).phase, { timeout: READY_TIMEOUT, intervals: [5_000] })
    .toBe("Ready");
}

// 이 실행이 만든 리소스와 이 스펙이 넣은 stub provider 는 실패하거나 SKIP 으로 끝나도 걷는다. 클러스터와 Platform Settings 에
// 실측 흔적이 남지 않아야 한다. Agent 가 그 provider 를 쓰므로 리소스를 먼저 지운다. 남은 리소스는 soft 실패라 provider 도 걷는다
test.afterEach(async ({ request, baseURL }) => {
  const base = baseURL ?? "";
  await deleteLeftovers(request, base, created.splice(0));
  if (!stubAdded) return;
  stubAdded = false;
  const res = await request.patch(`${base}/api/settings`, { headers: HEADERS, data: { llm_providers: await providersWithout(request, base) } });
  expect(res.status(), `stub provider ${STUB_PROVIDER} was left in Platform Settings: ${await res.text()}`).toBe(200);
});

test("agent canvas to chat through the browser (stub provider, header auth mode)", async ({ page, request, baseURL }) => {
  test.setTimeout(900_000);
  const base = baseURL ?? "";

  // ---- 0. header 모드, stub provider, lab Postgres. 없으면 명시적으로 SKIP ----
  await skipIfOidc(request, base, "SKIP: run in header mode");
  const stub = await ensureStubProvider(request, base);
  stubAdded = stub === "added"; // test.skip 보다 먼저 세워야 afterEach 가 걷는다
  test.skip(stub === "disallowed", "SKIP: the deployment does not allow kind stub providers (DAEGAM_ALLOW_STUB_PROVIDER=true in platform.extraConfig of deploy/dev/values-local.yaml)");
  const pgPassword = labSecret("password");
  const pgUser = labSecret("username");
  const pgDb = labSecret("database");
  test.skip(!pgPassword || !pgUser || !pgDb, `SKIP: lab Postgres Secret postgres-credentials missing in ${SPIKE_NS} (make lab-up)`);

  // ---- 1. API 로 준비: calculator MCP Server 를 저장하고 배포해 Ready, Postgres Database ----
  const bp = await request.post(`${base}/api/backend-apis`, {
    headers: HEADERS,
    multipart: { name: BP_NAME, file: { name: "calculator.wsdl", mimeType: "application/xml", buffer: readFileSync(WSDL_PATH) } },
  });
  expect(bp.status(), await bp.text()).toBe(201);
  const bpDoc = await bp.json();
  const bpPath = `/api/backend-apis/${bpDoc.id}`;
  created.push(bpPath);
  expect((await request.patch(`${base}${bpPath}`, { headers: HEADERS, data: { endpoint: SOAP_ENDPOINT } })).status()).toBe(200);
  // 만들기는 저장이다. 클러스터에는 아직 아무것도 없다 (D42)
  const ms = await request.post(`${base}/api/mcp-servers`, {
    headers: HEADERS,
    data: { name: MS_NAME, display_name: MS_DISPLAY, backends: [{ backend_profile_id: bpDoc.id, spec_version: bpDoc.version, tools: ["Add", "Divide"] }] },
  });
  expect(ms.status(), await ms.text()).toBe(201);
  const msDoc = await ms.json();
  const msPath = `/api/mcp-servers/${msDoc.id}`;
  created.push(msPath);
  // 배포 본문은 이름을 받지 않는다. 이름은 저장할 때 이미 정해졌다 (D42)
  const deployed = await request.post(`${base}${msPath}/deploy`, { headers: HEADERS, data: {} });
  expect(deployed.status(), await deployed.text()).toBe(201);
  const ds = await request.post(`${base}/api/data-services`, {
    headers: HEADERS,
    data: { name: DS_NAME, display_name: `에이전트 DB ${RID}`, kind: "postgres", host: PG_HOST, port: 5432, database: pgDb, username: pgUser, password: pgPassword },
  });
  expect(ds.status(), await ds.text()).toBe(201);
  const dsDoc = await ds.json();
  created.push(`/api/data-services/${dsDoc.id}`);
  await waitReady(request, `${base}${msPath}`);

  // ---- 2. 캔버스: 템플릿 불러오기 → 빈 자리 셋이 오류 표식 → 인스펙터로 채운다 ----
  await page.goto("/agents/new?template=support-agent");
  await expect(page.getByTestId("agent-canvas")).toBeVisible();
  await expect(page.getByTestId("node-agent-main")).toBeVisible();
  // 모델은 노드다 (D28). 서버 템플릿의 모델 노드 id 는 정하지 않으므로 종류로 찾는다
  const modelNode = page.locator('[data-testid^="node-"][data-node-type="model"]').first();
  await expect(modelNode).toBeVisible();
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "error");
  await expect(modelNode.locator('[data-testid^="node-mark-"]')).toBeVisible();
  await expect(page.getByTestId("canvas-save")).toBeDisabled();
  await page.getByTestId("ap-display-name").fill(AG_DISPLAY);
  await page.getByTestId("ap-name").fill(AG_NAME);
  // 메인 에이전트에 붙은 모델 노드: stub provider
  await page.getByTestId("node-agent-main").click();
  await expect(page.getByTestId("inspector-node-id")).toHaveText("agent-main");
  await expect(page.getByTestId("agent-model-missing")).toHaveCount(0);
  await modelNode.click();
  await selectValue(page, "model-provider", STUB_PROVIDER);
  await expect(page.getByTestId("node-model-agent-main")).toContainText(STUB_PROVIDER);
  // 도구 소스: MCP Server 로 바꾸고 방금 배포한 것을 고른다. 정의의 도구 [Add, Divide] 가 목록에 나온다.
  // 그래프에 저장되는 값은 그대로 mcp_instance 다. 바뀐 것은 화면 글자뿐이다
  await page.getByTestId("node-tools-calculator").click();
  await selectValue(page, "tools-source-kind", "mcp_instance");
  await selectValue(page, "tools-source", msDoc.id);
  await expect(page.getByTestId("tool-allow-Add")).toBeVisible();
  await expect(page.getByTestId("tool-allow-Divide")).toBeVisible();
  // 메모리: Postgres Database
  await page.getByTestId("node-memory").click();
  await selectValue(page, "memory-data-service", dsDoc.id);
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok");
  await expect(page.getByTestId("node-mark-agent-main")).toHaveCount(0);

  // ---- 3. 서버 검증 → 저장(리비전 1). 저장은 draft 다 ----
  await page.getByTestId("canvas-validate").click();
  await expect(page.getByTestId("validation-badge")).toContainText("서버");
  await expect(page.getByTestId("validation-badge")).toHaveAttribute("data-level", "ok");
  await page.screenshot({ path: `${SHOTS}/agent-canvas-${RID}.png`, fullPage: true });
  await recordCreate(page, "/api/agents", created, () => page.getByTestId("canvas-save").click());
  await expect(page).toHaveURL(/\/agents\/[^/?]+$/, { timeout: 60_000 });
  const agUrl = page.url();
  await expect(page.getByTestId("state-badge")).toHaveText("미배포");
  await expect(page.getByTestId("page-title")).toContainText(AG_DISPLAY);
  // 캔버스는 상세의 정의 탭 안이다. 저장된 리비전은 1 이다
  await page.getByTestId("tab-definition").click();
  await expect(page.getByTestId("canvas-revision")).toHaveAttribute("value", "1");

  // ---- 4. 배포 탭의 대화상자 → Ready 폴링 ----
  await page.getByTestId("tab-deployment").click();
  await expect(page.getByTestId("deployment-empty")).toContainText("아직 배포하지 않았습니다");
  await page.getByTestId("deployment-deploy").click();
  const deployDialog = page.getByTestId("deploy-dialog");
  await expect(deployDialog).toBeVisible();
  // 이름 칸이 없다. CR 이름은 문서 이름에서 온다 (D42)
  await expect(deployDialog.getByTestId("ag-preview")).toContainText(`agent-${AG_NAME}`);
  // Agent 노출은 클러스터 설정과 무관하게 oidc 전용이다 (D27)
  await expect(deployDialog.getByTestId("ai-exposure-oidc-only")).toContainText("외부 노출은 oidc 모드에서만 켤 수 있습니다.");
  await deployDialog.getByTestId("deploy-submit").click();
  await expect(deployDialog).toBeHidden({ timeout: 120_000 });
  await expect(page.getByTestId("state-badge")).toHaveText("배포됨", { timeout: 120_000 });
  await expect(page.getByTestId("status-phase")).toHaveText("Ready", { timeout: READY_TIMEOUT });
  await expect(page.getByTestId("status-chat-url")).toContainText("/api/agents/");

  // ---- 5. 채팅 한 턴: 스텁이 Add(3,4) 를 부르고 결과 7 을 답한다 ----
  await page.getByTestId("tab-chat").click();
  await expect(page.getByTestId("chat-card")).toBeVisible();
  await page.getByTestId("chat-input").fill('{"tool":"Add","args":{"a":3,"b":4}}');
  await page.getByTestId("chat-send").click();
  const toolCall = page.locator('[data-testid^="chat-message-"][data-role="tool_call"]').first();
  await expect(toolCall).toBeVisible({ timeout: 60_000 });
  await expect(toolCall).toContainText("Add");
  await expect(toolCall).toHaveAttribute("data-status", "success", { timeout: 60_000 });
  await expect(page.locator('[data-testid^="chat-message-"][data-role="done"]').first()).toBeVisible({ timeout: 60_000 });
  const answer = page.locator('[data-testid^="chat-message-"][data-role="assistant"]').last();
  await expect(answer).toContainText("결과:");
  await expect(answer).toContainText("7");
  await expect(page.getByTestId("chat-session")).not.toHaveText(/첫 메시지/);
  await toolCall.getByTestId(/chat-message-\d+-toggle/).click();
  await expect(toolCall.getByTestId(/chat-message-\d+-detail/)).toContainText("7");
  await page.screenshot({ path: `${SHOTS}/agent-chat-${RID}.png`, fullPage: true });

  // ---- 5b. 홈 챗 (D35·D45): 시작 화면에서 상대를 골라 첫 메시지를 보내면 대화 화면으로 넘어간다 ----
  await page.goto("/");
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByTestId("home-chat-page")).toBeVisible();
  // 홈 챗에서는 청지기가 화면 안에 있으므로 오른쪽 패널 토글이 없다 (F5)
  await expect(page.getByTestId("assistant-toggle")).toHaveCount(0);
  // 대화가 없으니 인사와 입력칸 하나다 (D45 첫째)
  await expect(page.getByTestId("home-start")).toBeVisible();
  await expect(page.getByTestId("home-greeting")).toBeVisible();
  await selectValue(page, "home-agent-select", { label: AG_DISPLAY });
  await expect(page.getByTestId("home-actor-note")).toContainText("자기 자격으로");
  // 상대가 Agent 면 모델은 정의가 정한 값이고 빠른 시작 칩은 사라진다 (D45 둘째·넷째)
  await expect(page.getByTestId("home-model-readonly")).toBeVisible();
  await expect(page.getByTestId("home-quick-starts")).toHaveCount(0);
  // 스텁은 Tool 결과를 받은 뒤 "after" 초 기다렸다 답한다(runtime/stub.py). 그동안이 진행 중인 턴이다
  const homeMessage = '{"tool":"Add","args":{"a":10,"b":20},"after":3}';
  await page.getByTestId("home-start-input").fill(homeMessage);
  await page.getByTestId("home-start-send").click();
  // 첫 메시지가 대화 화면으로 이어지고 그 말이 첫 턴이 된다 (D45 다섯째)
  await expect(page.getByTestId("chat-panel")).toBeVisible({ timeout: 60_000 });
  // 세션 고르개는 왼쪽 목록 하나뿐이다
  await expect(page.getByTestId("chat-session")).toHaveCount(0);
  // 연결과 떨어진 턴은 스트림을 여는 순간 세션을 정한다(D92). 그때 홈 챗이 패널을 비우면 답 전에 받은 Tool 카드가 사라진다
  const toolCards = page.locator('[data-testid^="chat-message-"][data-role="tool_call"]');
  await expect(toolCards.first()).toHaveAttribute("data-status", "success", { timeout: 60_000 });
  await expect(page.getByTestId("chat-panel")).toHaveAttribute("data-session-id", /.+/);
  const homeAnswer = page.locator('[data-testid^="chat-message-"][data-role="assistant"]').last();
  await expect(homeAnswer).toContainText("30", { timeout: 60_000 });
  await expect(toolCards).toHaveCount(1);
  // 첫 턴이 만든 세션이 왼쪽 목록에 바로 보인다
  // data-selected 는 모든 행에 "true"/"false" 로 붙으므로 값까지 봐야 고른 세션이다. 속성 유무만 보면 첫 행,
  // 즉 상세 화면에서 먼저 만든 세션을 집어 새로고침 뒤 다른 전사를 연다 (실측)
  const homeSession = page.locator('[data-testid^="home-session-"][data-selected="true"]').first();
  await expect(homeSession).toBeVisible({ timeout: 60_000 });
  const homeSessionId = (await homeSession.getAttribute("data-testid"))!.replace("home-session-", "");

  // 새로고침: 고른 상대가 기억되고 시작 화면으로 돌아온다. 그 대화를 고르면 전사가 원문 그대로 돌아온다 (F1)
  await page.reload();
  await expect(page.getByTestId("home-start")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("home-actor-note")).toContainText("자기 자격으로");
  await page.getByTestId(`home-session-${homeSessionId}`).click();
  await expect(page.getByTestId("chat-panel")).toBeVisible({ timeout: 60_000 });
  const homeTranscript = page.getByTestId("chat-transcript");
  await expect(homeTranscript).toContainText(homeMessage, { timeout: 60_000 });
  await expect(homeTranscript).toContainText("30");
  await page.screenshot({ path: `${SHOTS}/home-chat-${RID}.png`, fullPage: true });

  // ---- 5c. 화면보다 오래 사는 턴 (D92, P78): 화면을 떠나도 턴은 끝까지 가고, 돌아오면 진행 중임과 그 뒤의 답이 보인다 ----
  const answers = page.locator('[data-testid^="chat-message-"][data-role="assistant"]');
  /** 지금 대화에 Tool 결과 뒤 after 초 기다리는 턴을 보내고 그 결과가 나올 때까지 기다린다. 대화의 세션 id 를 돌려준다 */
  async function startSlowTurn(a: number, b: number, after: number): Promise<string> {
    const before = await toolCards.count();
    await page.getByTestId("chat-input").fill(JSON.stringify({ tool: "Add", args: { a, b }, after }));
    await page.getByTestId("chat-send").click();
    await expect(toolCards).toHaveCount(before + 1, { timeout: 60_000 });
    await expect(toolCards.nth(before)).toHaveAttribute("data-status", "success", { timeout: 60_000 });
    const sid = await page.getByTestId("chat-panel").getAttribute("data-session-id");
    expect(sid, "the panel names its session once the stream opens").toBeTruthy();
    return sid!;
  }
  /**
   * 이 페이지의 다음 채팅 요청 하나를 Tool 결과까지만 받고 끊는다. Chromium 의 오프라인 흉내는 열린 fetch 스트림을 끊지 않으므로
   * (실측) 시험 프로세스가 같은 요청을 보내 턴을 시작하고, 패널에는 Tool 결과까지의 이벤트만 담은 응답을 준다. 그래서 패널의
   * 스트림은 마감 없이 끝나고, 그 뒤 첫 세션 전체 읽기도 503 으로 실패하게 한다. 시험 프로세스의 연결도 닫지만 턴은 연결과
   * 떨어져 끝까지 돈다(D92)
   */
  async function cutNextChatAfterToolResult(): Promise<void> {
    await page.route(
      "**/api/agents/*/chat",
      async (route) => {
        const request = route.request();
        // 압축되지 않은 SSE 를 받고, 몸 길이와 연결은 fetch 가 다시 정한다
        const headers = Object.fromEntries(
          Object.entries(await request.allHeaders()).filter(([name]) => !["host", "content-length", "connection", "accept-encoding"].includes(name)),
        );
        const res = await fetch(request.url(), { method: "POST", headers, body: request.postData() ?? undefined });
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let text = "";
        let cut = -1;
        while (cut < 0) {
          const { value, done } = await reader.read();
          if (done) throw new Error(`the turn ended before its tool_result: ${text}`);
          text += decoder.decode(value, { stream: true });
          const at = text.indexOf("event: tool_result");
          const end = at < 0 ? -1 : text.indexOf("\n\n", at);
          if (end >= 0) cut = end + 2;
        }
        await reader.cancel();
        await page.route(
          /\/api\/agents\/[^/]+\/sessions\/[^/?]+$/,
          (read) => read.fulfill({ status: 503, json: { detail: { code: "runtime_unreachable", detail: "e2e: the first read after the cut fails" } } }),
          { times: 1 },
        );
        const answer = Object.fromEntries(
          [...res.headers.entries()].filter(([name]) => !["content-length", "transfer-encoding", "connection", "content-encoding"].includes(name)),
        );
        await route.fulfill({ status: res.status, headers: answer, body: text.slice(0, cut) });
      },
      { times: 1 },
    );
  }
  /** 왼쪽 목록에서 그 대화를 연다 */
  async function openSession(sid: string): Promise<void> {
    await page.getByTestId(`home-session-${sid}`).click();
    await expect(page.getByTestId("chat-panel")).toHaveAttribute("data-session-id", sid, { timeout: 60_000 });
  }
  /** 돌던 턴을 되살린 화면: 진행 중 안내가 보이고, 다시 보내지 않아도 그 턴의 답(합 sum)이 나타난 뒤 안내가 사라진다 */
  async function followsToAnswer(sum: number): Promise<void> {
    await expect(page.getByTestId("chat-notice")).toContainText("진행 중", { timeout: 30_000 });
    await expect(answers.filter({ hasText: String(sum) })).toHaveCount(1, { timeout: 90_000 });
    await expect(page.getByTestId("chat-notice")).toHaveCount(0, { timeout: 30_000 });
  }

  // 새로고침: 시작 화면으로 돌아오지만 턴은 돈다. 그 대화를 열면 목록의 배지와 안내가 보이고 답이 뒤에 나타난다. 새로고침과
  // 목록 읽기, 대화 열기가 턴이 끝나기 전에 끝나도록 30초를 둔다
  const slowSid = await startSlowTurn(20, 21, 30);
  await page.reload();
  await expect(page.getByTestId("home-start")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId(`home-session-running-${slowSid}`)).toBeVisible({ timeout: 30_000 });
  await openSession(slowSid);
  await followsToAnswer(41);
  // 끊긴 스트림: 패널의 스트림이 Tool 결과 뒤 마감 없이 끝나고 그 뒤 첫 세션 읽기도 실패해도, 오류 대신 진행 중 안내를 두고
  // 턴을 따라가 답을 보인다. 끊긴 것을 안내로 확인하므로 스트림이 끊기지 않고 답이 오면 이 단계가 실패한다
  await cutNextChatAfterToolResult();
  await startSlowTurn(35, 39, 30);
  await expect(page.getByTestId("chat-notice")).toContainText("진행 중", { timeout: 15_000 });
  await expect(page.getByTestId("chat-error")).toHaveCount(0);
  await expect(answers.filter({ hasText: "74" })).toHaveCount(1, { timeout: 90_000 });
  await expect(page.getByTestId("chat-error")).toHaveCount(0);
  // 다른 화면(새 대화)으로 갔다 돌아오면 도는 턴을 되살린다
  await startSlowTurn(25, 27, 15);
  await page.getByTestId("home-new-session").click();
  await expect(page.getByTestId("home-start")).toBeVisible({ timeout: 30_000 });
  await openSession(slowSid);
  await followsToAnswer(52);
  // 중단 단추만 턴을 멈춘다. 되살린 대화의 도는 턴을 멈추면 그 턴은 중단한 말로 끝난다
  await startSlowTurn(30, 33, 45);
  await page.reload();
  await expect(page.getByTestId("home-start")).toBeVisible({ timeout: 60_000 });
  await openSession(slowSid);
  await expect(page.getByTestId("chat-notice")).toContainText("진행 중", { timeout: 30_000 });
  await page.getByTestId("chat-abort").click();
  await expect(page.locator('[data-testid^="chat-message-"][data-role="error"]').last()).toContainText("중단했습니다", { timeout: 30_000 });
  await expect(page.getByTestId("chat-notice")).toHaveCount(0, { timeout: 30_000 });
  await expect(answers.filter({ hasText: "63" })).toHaveCount(0);

  // 다른 화면으로 나가면 청지기 패널 토글이 돌아온다 (F5). 토글은 비서가 켜진 배포에만 있으므로(Layout), 꺼진 배포에서는
  // 허브 화면에도 없는지 본다
  await page.getByTestId("nav-mcp-hub").click();
  await expect(page.getByTestId("hub-page")).toBeVisible();
  const assistant = await (await request.get(`${base}/api/assistant/config`, { headers: HEADERS })).json();
  if (assistant.enabled) await expect(page.getByTestId("assistant-toggle")).toBeVisible();
  else await expect(page.getByTestId("assistant-toggle")).toHaveCount(0);

  // ---- 6. 화면에서 삭제. 삭제 하나가 정의와 클러스터의 것을 함께 지운다 (D42). API 로 만든 준비물은 afterEach 가 지운다 ----
  await deleteFromDetail(page, agUrl, "agents-page", AG_NAME);
  await expect(page.getByTestId(`agent-row-${AG_NAME}`)).toHaveCount(0, { timeout: 60_000 });
});
