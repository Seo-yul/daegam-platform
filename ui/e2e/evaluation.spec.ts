import { expect, test, type Page } from "@playwright/test";

/**
 * P45 의 되돌리기와 평가(D109)를 목 API(mock-server.ts) 위의 실제 브라우저로 본다. 배포 탭에서 이전 리비전으로 되돌리고, 평가 탭에서
 * 세트를 고치고, 동의한 대화를 사례로 복사하고, 실행을 시작해 결과를 읽고, 배포 전 평가가 재배포를 막았다가 받아들인다. 단위
 * 테스트(agent-rollback.test.tsx·agent-evaluation.test.tsx)가 jsdom 에서 보는 것을 실제 레이아웃과 폴링으로 다시 본다.
 *
 * Agent 는 mock-server.ts 가 넣은 `ag-eval` 이다. r2 를 배포했고 r3 을 저장만 했다. 세트 `es-e2e` 에 다른 사람이 쓴 사례 하나가
 * 있고 평가 `fb-e2e` 가 평가 사례로 쓰는 데 동의했다. 목의 실행은 시작한 뒤 1.5초가 지나야 끝난다. 목 상태가 서버 프로세스
 * 전역이라 이 파일의 테스트는 차례로 돌고 앞 테스트가 남긴 상태를 잇는다.
 */

const AGENT = "ag-eval";
test.describe.configure({ mode: "serial" });

test("배포 탭에서 r1 로 되돌리면 새 리비전으로 배포되고 기록에 남는다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=deployment`);
  const revisions = page.getByTestId("revisions-table");
  // r2 는 배포된 내용이고 r3 은 지금 리비전이라 되돌릴 수 있는 것은 r1 뿐이다
  await expect(revisions.getByTestId("revision-rollback-1")).toBeVisible();
  await expect(revisions.getByTestId("revision-rollback-2")).toHaveCount(0);
  await revisions.getByTestId("revision-rollback-1").click();
  const dialog = page.getByTestId("rollback-dialog");
  await expect(dialog.getByTestId("rollback-body")).toContainText("r1의 정의를 새 리비전 r4로 저장하고 배포합니다.");
  const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/agents/${AGENT}/rollback`));
  await dialog.getByTestId("rollback-submit").click();
  expect(JSON.parse((await sent).postData() ?? "{}")).toEqual({ revision: 1 });
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("deploy-history-table").getByRole("row").nth(1)).toContainText("r1로 되돌림");
});

test("평가 탭에서 세트의 이름을 고친다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=evaluation`);
  await page.getByTestId("evaluation-set-edit").click();
  const dialog = page.getByTestId("evaluation-set-dialog");
  await dialog.getByTestId("evaluation-set-name").fill("환불 문의 세트");
  await dialog.getByTestId("evaluation-set-save").click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("evaluation-set-view")).toContainText("환불 문의 세트");
});

test("동의한 대화를 평가 사례로 복사하면 전화번호가 자리표로 바뀐 채 저장된다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=feedback`);
  await page.getByTestId("feedback-copy-fb-e2e").click();
  const dialog = page.getByTestId("copy-case-dialog");
  await expect(dialog.getByTestId("case-copy-replaced")).toHaveText("전화번호 1건을 바꿔 두었습니다.");
  await expect(dialog.getByTestId("case-message-0")).toHaveValue(/\[phone\]/);
  await dialog.getByTestId("case-save").click();
  await expect(dialog).toHaveCount(0);
  await page.goto(`/agents/${AGENT}?tab=evaluation`);
  await expect(page.getByTestId("evaluation-cases")).toContainText("대화에서 복사");
});

test("배포 전 평가를 켜면 평가하지 않은 새 리비전의 재배포가 막히고, 그 리비전을 평가해 통과하면 받아들인다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=evaluation`);
  const gate = page.getByTestId("evaluation-gate");
  await gate.getByTestId("evaluation-gate-rate").fill("50");
  await gate.getByTestId("evaluation-gate-save").click();
  await expect(gate.getByTestId("evaluation-gate-state")).toHaveText("켜짐");

  const revision = await saveNewRevision(page, 150_000);
  await page.goto(`/agents/${AGENT}`);
  await page.getByTestId("redeploy-button").click();
  const dialog = page.getByTestId("redeploy-dialog");
  await dialog.getByTestId("redeploy-submit").click();
  await expect(dialog.getByTestId("redeploy-error")).toContainText("이 Agent 는 배포 전에 평가를 통과해야 합니다.");
  await expect(dialog.getByTestId("evaluation-refusal-reason")).toHaveText("이 정의를 평가한 결과가 없습니다.");
  await dialog.getByTestId("redeploy-close").click();

  await page.goto(`/agents/${AGENT}?tab=evaluation`);
  await page.getByTestId("evaluation-run-start").click();
  const start = page.getByTestId("evaluation-start-dialog");
  await expect(start).toContainText(`r${revision} 평가 실행`);
  // 다른 사람이 쓴 사례는 내 권한으로 돌므로 시작 전에 보인다
  await expect(start.getByTestId("evaluation-start-review-ec-e2e")).toContainText("환불 기한이 언제인가요?");
  await start.getByTestId("evaluation-start-submit").click();
  await expect(start).toHaveCount(0);
  const row = page.getByTestId("evaluation-runs-table").getByRole("row").nth(1);
  await expect(row).toContainText("실행 중");
  await expect(row).toContainText("통과 (100%)", { timeout: 10_000 });
  await row.getByRole("button", { name: "결과 보기" }).click();
  const detail = page.getByTestId("evaluation-run-dialog");
  await expect(detail.getByTestId("evaluation-run-summary")).toContainText("통과 (100%)");
  // 시작한 사람은 답변을 본다
  await expect(detail.getByTestId("evaluation-results")).toBeVisible();
  await detail.getByTestId("evaluation-run-close").click();

  await page.goto(`/agents/${AGENT}`);
  await page.getByTestId("redeploy-button").click();
  await page.getByTestId("redeploy-dialog").getByTestId("redeploy-submit").click();
  await expect(page.getByTestId("redeploy-dialog")).toHaveCount(0);
  await page.goto(`/agents/${AGENT}?tab=deployment`);
  await expect(page.getByTestId("deploy-history-table").getByRole("row").nth(1)).toContainText("평가를 통과함 (100%)");
});

test("평가하지 않은 새 리비전은 까닭을 적어 평가 없이 배포하고, 기록에 그 까닭이 남는다", async ({ page }) => {
  await saveNewRevision(page, 120_000);
  await page.goto(`/agents/${AGENT}`);
  await page.getByTestId("redeploy-button").click();
  const dialog = page.getByTestId("redeploy-dialog");
  await dialog.getByTestId("redeploy-submit").click();
  await expect(dialog.getByTestId("evaluation-refusal-reason")).toHaveText("이 정의를 평가한 결과가 없습니다.");
  await dialog.getByTestId("redeploy-skip-toggle").check();
  await expect(dialog.getByTestId("redeploy-submit")).toBeDisabled();
  await dialog.getByTestId("redeploy-skip-reason").fill("장애 대응");
  await dialog.getByTestId("redeploy-submit").click();
  await expect(dialog).toHaveCount(0);

  await page.goto(`/agents/${AGENT}?tab=deployment`);
  await expect(page.getByTestId("deploy-history-table").getByRole("row").nth(1)).toContainText("평가 없이 배포함: 장애 대응");
});

/**
 * 새 리비전을 저장하고 그 번호를 돌려준다. 화면에서는 캔버스의 저장이 하는 일이라 여기서는 같은 PUT 을 API 로 보낸다. 세션 토큰
 * 예산을 바꿔 내용이 달라지게 한다.
 */
async function saveNewRevision(page: Page, sessionTokens: number): Promise<number> {
  const agent = await (await page.request.get(`/api/agents/${AGENT}`)).json();
  const saved = await page.request.put(`/api/agents/${AGENT}`, {
    data: {
      name: agent.name,
      display_name: agent.display_name,
      graph: agent.graph,
      layout: agent.layout,
      service: { ...agent.service, budget: { ...agent.service.budget, session_tokens: sessionTokens } },
    },
  });
  expect(saved.ok()).toBe(true);
  const body = await saved.json();
  expect(body.revision_created).toBe(true);
  return body.revision as number;
}
