import { expect, test } from "@playwright/test";

import { HEADERS, SOAP_ENDPOINT, WSDL_PATH, deleteFromDetail, deleteLeftovers, recordCreate, skipIfOidc } from "./cluster";
import { downloaded } from "./download";
import { selectValue } from "./select";

/**
 * P7 단위 9 / P8 F6: 허브 → Gateway 시나리오를 브라우저로 1회 재현한다.
 * 대상: `make deploy-local` 클러스터의 플랫폼 (make e2e-ui-full 이 :19100 으로 port-forward).
 *
 * 흐름: Backend API 업로드(calculator.wsdl) → 접속 정보 폼으로 endpoint 를 mock-soap 으로
 * → MCP Server 저장(tools [Add]) → 배포 대화상자로 클러스터에 올림 → Ready 폴링(5분 상한)
 * → 상세의 Publish 로 허브에 게시(D23, 수동) → 허브 행 확인 → Gateway 저장(허브 항목 + tools [Add])
 * → 배포 대화상자로 외부 노출(gw-<name>.e2e.local)과 함께 올림 → Ready → 허브 게시(entry)
 * → 게시 항목의 Refresh tools 로 status.url 에 tools/list → [Add] → 허브 목록의 클라이언트 설정 내보내기(P55a F7)
 * → 게시 해제 → 화면에서 역순 삭제.
 *
 * 리소스 넷이 정의와 배포를 함께 가지므로(D42) 만들기는 저장이고 화면은 `draft` 로 앉는다. 배포는
 * 화면 이동이 아니라 상세에서 여는 대화상자이고, 정하는 것은 클러스터와 네임스페이스·노출뿐이다.
 * 이름은 저장할 때 이미 정해져 배포 본문에 없다. 지울 것도 셋으로 줄었다.
 *
 * 규칙: 요소는 data-testid 로만 찾는다. 이름에 run id 를 붙여 재실행이 안전하다(이전 잔여물과
 * 충돌하지 않는다). 직렬 1 worker. 만든 리소스는 저장 POST 의 응답으로 기록해, 중간에 실패해도 afterEach 가 남은 것을 지운다.
 *
 * 이름 두 칸(D24): 폼마다 한글 Display name(`<prefix>-display-name`)과 K8s 이름 Name(`<prefix>-name`)을 따로
 * 친다. Display name 을 먼저 치면 Name 이 슬러그로 따라오므로 그 뒤 Name 을 덮어쓴다. 목록·상세 헤더는
 * 표시 이름을 보이고 `data-testid` 는 name 그대로다.
 *
 * 인증 scope 결정(주석으로 명시): 실행 모드는 header 다. 브라우저 PKCE 로그인은 클러스터 내부
 * issuer 에 브라우저가 닿아야 해 로컬 실증이 과하게 복잡하므로, OIDC 로그인 UI 는 smoke 의
 * oidc-client-ts 모킹 검증으로, MCP 계층 OIDC 는 `make e2e-mcp-auth` 로 각각 갈음한다
 * (docs/archive/plan/phase-7-ui.md 단위 9). auth_mode 가 oidc 면 명시적으로 SKIP 한다.
 *
 * tools/list 경로: status.url 은 클러스터 안 Service 주소라 브라우저가 직접 닿지 못한다. 게시된
 * 허브 항목의 Refresh tools 가 플랫폼에서 그 주소로 tools/list 를 보내므로(9절) 그 결과를 화면에서
 * 읽는다. 브라우저는 플랫폼만 알면 된다.
 */

const READY_TIMEOUT = 300_000; // Ready 폴링 상한 (스펙: 5분)
const RID = Date.now().toString(36).slice(-6);
const BP_NAME = `uif-${RID}-calc`;
const MS_NAME = `uif-${RID}-mcp`;
const GW_NAME = `uif-${RID}-gw`;
// 표시 이름은 한글이라 슬러그가 비고, 사용자가 Name 을 직접 친다 (D24 의 "한글만 친 경우")
const BP_DISPLAY = `계산기 SOAP ${RID}`;
const MS_DISPLAY = `계산기 서버 ${RID}`;
const GW_DISPLAY = `게이트웨이 ${RID}`;
/** 이 실행이 만든 리소스의 API 경로(만든 순서). 저장 POST 의 응답에서 쌓는다. 끝 단계가 화면에서 지웠으면 afterEach 에서 404 로 넘어간다 */
const created: string[] = [];

/** Claude Code 의 `serverUrl` 패턴이 주소에 맞는가. `*` 는 아무 글자나 몇 개든이고 나머지는 글자 그대로다. */
function patternMatches(pattern: string, url: string): boolean {
  const body = pattern
    .split("*")
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${body}$`, "i").test(url);
}

test.afterEach(async ({ request, baseURL }) => {
  await deleteLeftovers(request, baseURL ?? "", created.splice(0));
});

test("hub to gateway scenario end to end through the browser (header auth mode)", async ({ page, request, baseURL }) => {
  test.setTimeout(900_000);

  // ---- 0. header 모드 확인. oidc 면 명시적으로 건너뛴다 (위 주석의 scope 결정) ----
  await skipIfOidc(request, baseURL ?? "", "SKIP: run in header mode (OIDC login UI is covered by smoke mocks, MCP-layer OIDC by make e2e-mcp-auth)");

  // ---- 1. Backend API: calculator.wsdl 업로드 ----
  await page.goto("/backend-apis/new");
  await page.getByTestId("spec-file").setInputFiles(WSDL_PATH);
  await page.getByTestId("bp-display-name").fill(BP_DISPLAY);
  await page.getByTestId("bp-name").fill(BP_NAME);
  // 업로드 전에 Verify 로 판정 확인 (P2 화면 흐름)
  await page.getByTestId("verify-button").click();
  await expect(page.getByTestId("verdict-level")).toHaveText(/ok|warn/, { timeout: 60_000 });
  await recordCreate(page, "/api/backend-apis", created, () => page.getByTestId("save-button").click());
  await expect(page.getByTestId("backend-api-detail-page")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("page-title")).toContainText(BP_DISPLAY);
  await expect(page.getByTestId("page-name")).toHaveText(BP_NAME);
  const bpUrl = page.url();

  // ---- 2. 접속 정보 폼에서 endpoint 를 mock-soap 으로 (API PATCH 가 아니라 화면으로) ----
  await page.getByTestId("conn-endpoint").fill(SOAP_ENDPOINT);
  await page.getByTestId("connection-save").click();
  await expect(page.getByTestId("connection-saved")).toBeVisible();

  // ---- 3. MCP Server 저장: tools [Add]. 만들기는 저장이라 아직 클러스터에 없다 (D42) ----
  await page.goto("/mcp-servers/new");
  await page.getByTestId("mp-display-name").fill(MS_DISPLAY);
  await page.getByTestId("mp-name").fill(MS_NAME);
  await selectValue(page, "mp-backend", { label: new RegExp(BP_NAME) });
  // calculator.wsdl 은 v1 하나다 (index 0 은 "(select)")
  await selectValue(page, "mp-version", { index: 1 });
  await page.getByTestId("tool-Add").check();
  await recordCreate(page, "/api/mcp-servers", created, () => page.getByTestId("save-button").click());
  await expect(page.getByTestId("mcp-server-detail-page")).toBeVisible({ timeout: 60_000 });
  const msUrl = page.url();
  await expect(page.getByTestId("state-badge")).toHaveText("미배포");
  await expect(page.getByTestId("overview-empty")).toContainText("아직 배포하지 않았습니다");

  // 목록은 표시 이름을 보이고 검색은 표시 이름으로도 된다. 행의 testid 는 name 그대로다
  await page.goto("/mcp-servers");
  await page.getByTestId("list-search").fill(MS_DISPLAY);
  const msRow = page.getByTestId(`mcp-server-row-${MS_NAME}`);
  await expect(msRow).toBeVisible();
  await expect(msRow.getByRole("link").first()).toHaveText(MS_DISPLAY);
  await expect(msRow).toContainText(MS_NAME);
  await page.goto(msUrl);

  // ---- 4. 배포 대화상자로 클러스터에 올린다 → Ready 폴링 (5분 상한, 화면 폴링 5초) ----
  // 대화상자가 정하는 것은 클러스터와 네임스페이스·노출뿐이다. 이름은 저장할 때 이미 정해졌다 (D42)
  await page.getByTestId("deploy-button").click();
  const deployDialog = page.getByTestId("deploy-dialog");
  await expect(deployDialog).toBeVisible();
  await deployDialog.getByTestId("deploy-submit").click();
  await expect(deployDialog).toBeHidden({ timeout: 120_000 });
  await expect(page.getByTestId("state-badge")).toHaveText("배포됨", { timeout: 120_000 });
  await expect(page.getByTestId("page-title")).toContainText(MS_DISPLAY);
  await expect(page.getByTestId("page-name")).toHaveText(MS_NAME);
  await expect(page.getByTestId("status-phase")).toHaveText("Ready", { timeout: READY_TIMEOUT });
  await expect(page.getByTestId("status-url")).not.toHaveText("-");

  // ---- 5. 허브 게시는 수동이다 (D23). 상세의 Publish 로 게시(이름 기본값 = 문서 이름)한 뒤 허브 행을 확인한다 ----
  await expect(page.getByTestId("published-badge")).toHaveCount(0);
  await page.getByTestId("publish-button").click();
  const publishDialog = page.getByTestId("publish-dialog");
  await expect(publishDialog).toBeVisible();
  await expect(publishDialog.getByTestId("publish-display-name")).toHaveValue(MS_DISPLAY);
  await expect(publishDialog.getByTestId("publish-name")).toHaveValue(MS_NAME);
  await publishDialog.getByTestId("publish-submit").click();
  await expect(page.getByTestId("published-badge")).toHaveText("Published", { timeout: 60_000 });
  await expect(page.getByTestId("hub-link")).toHaveText(MS_NAME);
  await expect(page.getByTestId("unpublish-button")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.goto("/hub");
  await expect(page.getByTestId(`hub-row-${MS_NAME}`)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId(`hub-row-${MS_NAME}`)).toContainText(MS_DISPLAY);
  await page.getByTestId(`hub-row-${MS_NAME}`).getByRole("link").first().click();
  await expect(page.getByTestId("hub-detail-page")).toBeVisible();
  // internal 의 도구 이름은 우리 리비전에서 계산하므로 새로고침 버튼이 서지 않는다 (D49).
  // 탭 이름표의 수도 저장된 사본이 아니라 그 계산값이다
  await expect(page.getByTestId("refresh-tools-button")).toHaveCount(0);
  await expect(page.getByTestId("tab-tools")).toContainText("1");
  // 도구 탭이 보이는 것은 그와 별개로 그때 서버에 직접 물어본 스키마다 (D48).
  // 실제 MCP 서버를 향해 그 경로가 도는 것을 여기서 밟는다
  await page.getByTestId("tab-tools").click();
  const addTool = page.getByTestId("tool-detail-Add");
  await expect(addTool).toBeVisible({ timeout: 60_000 });
  // 기본은 접힘이다. 펴면 입력 인자가 서고, 다시 그려도 접히지 않는다
  await expect(addTool.getByTestId("tool-args-Add")).toBeHidden();
  await addTool.locator("summary").click();
  await expect(addTool.getByTestId("tool-args-Add")).toBeVisible();
  await addTool.locator("summary").click();
  await expect(addTool.getByTestId("tool-args-Add")).toBeHidden();
  // 저장된 이름과 방금 본 것이 같으면 어긋남 안내가 서지 않는다
  await expect(page.getByTestId("tools-stale")).toHaveCount(0);

  // ---- 6. Gateway 저장: 허브 internal 항목(MCP Server 이름) + tools [Add] ----
  await page.goto("/gateways/new");
  await page.getByTestId("gp-display-name").fill(GW_DISPLAY);
  await page.getByTestId("gp-name").fill(GW_NAME);
  await page.getByTestId(`backend-${MS_NAME}`).check();
  await page.getByTestId(`backend-${MS_NAME}-tool-Add`).check();
  await recordCreate(page, "/api/gateways", created, () => page.getByTestId("save-button").click());
  await expect(page.getByTestId("gateway-detail-page")).toBeVisible({ timeout: 60_000 });
  const gwUrl = page.url();
  await expect(page.getByTestId("state-badge")).toHaveText("미배포");
  await expect(page.getByTestId("overview-empty")).toContainText("아직 배포하지 않았습니다");

  // ---- 7. 배포 대화상자로 vMCP 를 올린다 (노출: host gw-<name>.e2e.local, IngressClass traefik, TLS 없음) → Ready ----
  // home 은 deploy-local 의 Traefik 이고 allow_unauthenticated 다 (P8 F4·F7). 여기서는 화면이 보내는 본문과
  // 서버가 돌려주는 external_url 만 단언한다. 그 주소로 실제 tools/list 를 부르는 실증은 F7 의
  // e2e_gateway.sh 가 `curl --resolve` 로 맡는다 (브라우저는 e2e.local 을 풀지 못한다).
  const GW_HOST = `gw-${GW_NAME}.e2e.local`;
  await page.getByTestId("deploy-button").click();
  const gwDeployDialog = page.getByTestId("deploy-dialog");
  await expect(gwDeployDialog).toBeVisible();
  await gwDeployDialog.getByTestId("exp-enabled").check();
  await expect(gwDeployDialog.getByTestId("exp-warning")).toBeVisible(); // header 모드의 무인증 노출 경고
  await selectValue(page, "exp-class", "traefik");
  await gwDeployDialog.getByTestId("exp-host").fill(GW_HOST);
  await selectValue(page, "exp-tls-mode", "none");
  await expect(gwDeployDialog.getByTestId("exp-preview")).toHaveText(`http://${GW_HOST}/mcp`);
  await gwDeployDialog.getByTestId("deploy-submit").click();
  await expect(gwDeployDialog).toBeHidden({ timeout: 120_000 });
  await expect(page.getByTestId("state-badge")).toHaveText("배포됨", { timeout: 120_000 });
  await expect(page.getByTestId("page-title")).toContainText(GW_DISPLAY);
  await expect(page.getByTestId("page-name")).toHaveText(GW_NAME);
  await expect(page.getByTestId("status-external-url")).toHaveText(`http://${GW_HOST}/mcp`);
  await expect(page.getByTestId("copy-external-url")).toBeVisible();
  await expect(page.getByTestId("status-phase")).toHaveText("Ready", { timeout: READY_TIMEOUT });
  // 주소 할당 배지는 Traefik 이 Ingress status 를 채우는지에 달려 있어 여기서 단언하지 않는다 (F7 몫)
  await expect(page.getByTestId("exp-badge-unauth")).toBeVisible();
  await expect(page.getByTestId(`backend-state-${MS_NAME}`)).toHaveText("ready", { timeout: 120_000 });
  // 내부 URL 은 개요 탭에 있다. 탭은 배타 렌더라 배포 탭으로 옮긴 뒤에는 못 본다
  await expect(page.getByTestId("status-url")).toHaveText(/^http:\/\/vmcp-/);

  // 광고 도구와 허브 게시는 배포 탭에 있다. 스냅샷의 필터대로 Add 만 광고한다
  await page.getByTestId("tab-deployment").click();
  await expect(page.getByTestId("tools-card")).toBeVisible();
  await expect(page.getByTestId("tool-Add")).toBeVisible();
  await expect(page.getByTestId("tool-Divide")).toHaveCount(0);

  // ---- 8. 허브 게시(entry) → 게시 항목의 Refresh tools 가 status.url 로 tools/list → [Add] ----
  await expect(page.getByTestId("published-empty")).toBeVisible();
  // 게시 이름의 기본값은 문서 name 그대로, 표시 이름은 문서 표시 이름이다 (D24, services/gateways.py publish)
  const PUB_NAME = GW_NAME;
  await expect(page.getByTestId("publish-display-name")).toHaveValue(GW_DISPLAY);
  await expect(page.getByTestId("publish-name")).toHaveValue(PUB_NAME);
  await expect(page.getByTestId("publish-name-preview")).toContainText(`hub-${PUB_NAME}`);
  await page.getByTestId("publish-button").click();
  await expect(page.getByTestId(`published-link-${PUB_NAME}`)).toBeVisible({ timeout: 60_000 });
  await page.getByTestId(`published-link-${PUB_NAME}`).click();
  await expect(page.getByTestId("hub-detail-page")).toBeVisible();
  await expect(page.getByTestId("origin-gateway-link")).toHaveAttribute("href", gwUrl.slice(gwUrl.indexOf("/gateways/")));
  await expect(page.getByTestId("hub-backend-url")).toHaveText(/^http:\/\/vmcp-/);
  const pubUrl = page.url();
  // published 의 도구 이름은 Gateway 의 배포 스냅샷에서 계산한다 (D49). 새로고침 버튼도 없고
  // 기다릴 것도 없다. 전에는 여기서 vMCP 가 세션을 받을 때까지 새로고침을 재시도했다
  await expect(page.getByTestId("refresh-tools-button")).toHaveCount(0);
  await expect(page.getByTestId("tab-tools")).toHaveText("Tools 1");

  // ---- 8b. 클라이언트 설정 내보내기 (P55a F7): 게시한 Gateway 의 노출 주소가 내려받은 설정 파일에 든다 ----
  // header 모드라 인증이 없어 토큰 env 도 없다. 노출하지 않은 MCP Server 의 게시 항목은 외부 주소가 없어 고를 수 없다
  await page.goto("/hub");
  await expect(page.getByTestId(`hub-row-${PUB_NAME}`)).toBeVisible({ timeout: 60_000 });
  await page.getByTestId("hub-export-clients").click();
  const exportDialog = page.getByTestId("client-export-dialog");
  await expect(exportDialog).toBeVisible();
  await expect(exportDialog.getByTestId(`export-item-${PUB_NAME}`)).toBeChecked();
  await expect(exportDialog.getByTestId(`export-item-${MS_NAME}`)).toBeDisabled();
  await expect(exportDialog.getByTestId("client-export-anonymous")).toBeVisible();
  const claudeFile = await downloaded(page, exportDialog.getByTestId("client-export-download"));
  expect(claudeFile.name).toBe("mcp.json");
  expect(JSON.parse(claudeFile.text).mcpServers[PUB_NAME]).toEqual({ type: "http", url: `http://${GW_HOST}/mcp` });
  expect(claudeFile.text).not.toMatch(/Authorization|DAEGAM_PAT/);
  await exportDialog.getByTestId("tab-codex").click();
  const codexFile = await downloaded(page, exportDialog.getByTestId("client-export-download"));
  expect(codexFile.name).toBe("config.toml");
  expect(codexFile.text).toContain(`[mcp_servers."${PUB_NAME}"]\nurl = "http://${GW_HOST}/mcp"\n`);
  // header 모드의 주체는 platform-admin 이라 회사 전체 허용 목록도 받는다. 항목마다 serverUrl 하나뿐이다
  const allowlistButton = exportDialog.getByTestId("client-export-allowlist-download");
  await expect(allowlistButton).toBeEnabled({ timeout: 30_000 });
  const allowlist = await downloaded(page, allowlistButton);
  expect(allowlist.name).toBe("allowed-mcp-servers.json");
  const allowed = JSON.parse(allowlist.text) as { allowedMcpServers: Record<string, string>[] };
  expect(Object.keys(allowed)).toEqual(["allowedMcpServers"]);
  for (const entry of allowed.allowedMcpServers) {
    expect(Object.keys(entry)).toEqual(["serverUrl"]);
    expect(entry.serverUrl).not.toMatch(/@|\?/);
  }
  // 게시한 Gateway 의 노출 주소는 그 호스트가 노출을 허용한 클러스터의 도메인 아래일 때 꼭 그때만 패턴에 맞는다(목표 2).
  // deploy-local 의 home 은 도메인이 없어 맞지 않는 쪽을 밟는다
  const clusters = (await (await request.get(`${baseURL}/api/clusters`, { headers: HEADERS })).json()) as { ingress: { enabled: boolean; domain?: string | null } }[];
  const underDomain = clusters.some((c) => c.ingress.enabled && Boolean(c.ingress.domain) && GW_HOST.endsWith(`.${c.ingress.domain!.toLowerCase().replace(/^\.+|\.+$/g, "")}`));
  expect(allowed.allowedMcpServers.some((e) => patternMatches(e.serverUrl, `http://${GW_HOST}/mcp`))).toBe(underDomain);
  await exportDialog.getByTestId("client-export-close").click();
  await expect(exportDialog).toBeHidden();

  // ---- 9. 게시 해제 (허브 상세에서) → Gateway 는 그대로 배포된 채로 남는다 ----
  await page.goto(pubUrl);
  await page.getByTestId("delete-button").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toContainText("Hub item 게시 해제");
  await dialog.getByTestId("confirm-submit").click();
  await expect(page.getByTestId("hub-page")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId(`hub-row-${PUB_NAME}`)).toHaveCount(0);
  await page.goto(gwUrl);
  await expect(page.getByTestId("state-badge")).toHaveText("배포됨");
  await page.getByTestId("tab-deployment").click();
  await expect(page.getByTestId("published-empty")).toBeVisible();

  // ---- 10. 화면에서 역순 삭제 (Gateway → MCP Server → Backend API). 삭제 하나가 정의와 배포를 함께 지운다 (D42) ----
  await deleteFromDetail(page, gwUrl, "gateways-page", GW_NAME);
  await deleteFromDetail(page, msUrl, "mcp-servers-page", MS_NAME);
  await deleteFromDetail(page, bpUrl, "backend-apis-page", BP_NAME);
  await expect(page.getByTestId(`backend-api-row-${BP_NAME}`)).toHaveCount(0);
});
