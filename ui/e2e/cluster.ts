import { expect, test, type APIRequestContext, type APIResponse, type Page } from "@playwright/test";

/**
 * 실제 클러스터를 향하는 스펙(full.spec.ts·agent.spec.ts)이 함께 쓰는 환경 값과 도우미. 스펙 파일이 아니므로
 * Playwright 가 테스트로 집어 들지 않는다.
 */

export const SPIKE_NS = process.env.SPIKE_NS ?? "daegam-spike";
export const SOAP_ENDPOINT = process.env.E2E_SOAP_ENDPOINT ?? `http://mock-soap.${SPIKE_NS}.svc:18080/calc`;
export const WSDL_PATH = new URL("../../platform/tests/fixtures/calculator.wsdl", import.meta.url).pathname;
/** 시나리오가 API 를 직접 부를 때 싣는 테넌트. 화면은 기본 테넌트 `default` 를 고른다(ui/src/lib/tenant.ts) */
export const HEADERS = { "X-Daegam-Tenant": process.env.E2E_TENANT ?? "default" };

/** 배포본이 header 모드가 아니면(oidc) 이 테스트를 건너뛴다. 까닭은 스펙마다 달라 reason 으로 받는다. */
export async function skipIfOidc(request: APIRequestContext, base: string, reason: string): Promise<void> {
  const cfg = await (await request.get(`${base}/api/auth/config`)).json();
  test.skip(cfg.auth_mode === "oidc", reason);
}

/** 미룬 삭제가 끝나기를 기다리는 상한 */
const GONE_TIMEOUT = 180_000;

/**
 * 화면의 저장 버튼(save)이 보내는 만들기 POST 를 기다려, 만들어졌으면 그 문서의 API 경로를 created 에 쌓는다. 상세 화면을
 * 확인하기 전에 기록하므로 상세가 깨진 실행이 만든 것도 afterEach 가 지운다. collection 은 `/api/gateways` 처럼 받는다.
 */
export async function recordCreate(page: Page, collection: string, created: string[], save: () => Promise<void>): Promise<void> {
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === "POST" && new URL(r.url()).pathname === collection, { timeout: 120_000 }),
    save(),
  ]);
  if (res.ok()) created.push(`${collection}/${(await res.json()).id}`);
}

/**
 * 시나리오가 만든 리소스를 API 로 지운다. `afterEach` 에서 불러 실패하거나 건너뛴 실행의 것도 클러스터에 남기지 않는다.
 * 남은 Gateway·MCP Server 는 뒤의 e2e 가 인증 모드를 바꿀 때마다 다시 렌더되어 롤아웃한다. paths 는 만든 순서대로
 * (`/api/gateways/<id>` 처럼) 받아 거꾸로 지우므로 참조하는 쪽이 먼저 지워진다.
 *
 * 이미 지웠으면 404 라 넘어간다. 게시된 Gateway 는 409 published 가 알려 주는 허브 항목을 먼저 내린다. 남이 쥔 클레임 뒤로
 * 미룬 삭제(`deleted: false`)는 문서가 deleting 으로 남아 다음 경로를 409 in_use 로 막으므로 404 가 될 때까지 기다린다.
 * 지우지 못한 것은 모아 soft 실패로 알려, 부르는 쪽의 다른 정리도 돈다.
 */
export async function deleteLeftovers(request: APIRequestContext, base: string, paths: string[]): Promise<void> {
  const left: string[] = [];
  const refused = async (path: string, res: APIResponse) => left.push(`${path}: ${res.status()} ${await res.text()}`);
  const body = async (res: APIResponse) => res.json().catch(() => ({}));
  for (const path of [...paths].reverse()) {
    let res = await request.delete(`${base}${path}`, { headers: HEADERS });
    const detail = res.status() === 409 ? (await body(res)).detail : undefined;
    if (detail?.code === "published") {
      for (const item of detail.hub_item_ids as string[]) {
        const hub = await request.delete(`${base}/api/hub/${item}`, { headers: HEADERS });
        if (!hub.ok() && hub.status() !== 404) await refused(`/api/hub/${item}`, hub);
      }
      res = await request.delete(`${base}${path}`, { headers: HEADERS });
    }
    if (res.status() === 404) continue;
    if (!res.ok()) {
      await refused(path, res);
    } else if ((await body(res)).deleted === false) {
      await expect
        .poll(async () => (await request.get(`${base}${path}`, { headers: HEADERS })).status(), { timeout: GONE_TIMEOUT, intervals: [2_000] })
        .toBe(404)
        .catch(() => left.push(`${path}: still deleting after ${GONE_TIMEOUT / 1000} s`));
    }
  }
  expect.soft(left, "resources this run created were left on the cluster").toEqual([]);
}

/**
 * 상세 화면의 삭제 버튼을 누르고 확인 대화상자를 통과해 목록으로 돌아올 때까지 기다린다.
 * MCP Server·Gateway·Agent 처럼 배포된 워크로드가 사라지는 리소스는 이름을 다시 입력해야 한다
 * (lib/deletion.ts 의 requireName). Backend API 는 단순 확인이라 칸이 없다.
 */
export async function deleteFromDetail(page: Page, detailUrl: string, listTestId: string, name: string): Promise<void> {
  await page.goto(detailUrl);
  await page.getByTestId("delete-button").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  const nameInput = dialog.getByTestId("confirm-name");
  if (await nameInput.count()) await nameInput.fill(name);
  await dialog.getByTestId("confirm-submit").click();
  await expect(page.getByTestId(listTestId)).toBeVisible({ timeout: 60_000 });
}
