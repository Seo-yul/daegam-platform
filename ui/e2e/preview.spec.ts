import { expect, test } from "@playwright/test";

/**
 * P44b 의 미리 보기(D107)를 목 API(mock-server.ts) 위의 실제 브라우저로 본다. 테스트 채팅 서랍에서 draft Agent 의 미리 보기를
 * 띄우고, 파드가 뜨는 동안의 진행을 보고, 미리 보기 아래 라우트로 대화하고, 끝낸다. 개요에서는 다른 사람이 연 미리 보기를 보고
 * 끝낸다. 단위 테스트(agent-preview.test.tsx)가 jsdom 에서 보는 것을 실제 레이아웃과 SSE 스트림으로 다시 본다.
 *
 * Agent 는 mock-server.ts 가 넣은 draft `ag-preview` 이고 다른 사람(dev@example.com)의 미리 보기 `pv-other` 가 열려 있다. 목은
 * 시작한 뒤 1.5초가 지나야 파드가 Ready 다. 목 상태가 서버 프로세스 전역이라 직렬로 돈다.
 */

const AGENT = "ag-preview";

test("서랍에서 draft 의 미리 보기를 띄워 대화하고 끝낸다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}?tab=definition`);
  const drawer = page.getByTestId("test-chat-drawer");
  await expect(drawer.getByTestId("test-chat-empty")).toHaveText("미리 보기를 시작하거나 배포 탭에서 배포하면 여기서 대화할 수 있습니다.");

  const started = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/agents/${AGENT}/preview`));
  await drawer.getByTestId("preview-start").click();
  await started;
  const status = drawer.getByTestId("preview-status");
  await expect(status).toHaveText("미리 보기를 준비하는 중입니다. 파드가 뜨면 대화할 수 있습니다.");
  await expect(drawer.getByTestId("preview-tools")).toHaveText("미리 보기도 배포된 Agent 처럼 실제 Tool 을 부르므로 쓰기 Tool 은 실제 시스템을 바꿉니다.");
  await expect(status).toHaveText("미리 보기 r1과 대화합니다. 장기 기억을 사용하지 않고, 미리 보기를 끝내면 이 대화도 사라집니다.", { timeout: 10_000 });

  const input = drawer.getByTestId("chat-input");
  await input.fill("3 더하기 4");
  const sent = page.waitForRequest((r) => r.url().endsWith(`/agents/${AGENT}/preview/chat`));
  await drawer.getByTestId("chat-send").click();
  expect(JSON.parse((await sent).postData() ?? "{}")).toMatchObject({ message: "3 더하기 4", ephemeral: true });
  await expect(drawer.getByTestId("chat-transcript")).toContainText("결과는 7 입니다.");
  await expect(drawer.getByTestId("chat-error")).toHaveCount(0);

  const ended = page.waitForRequest((r) => r.method() === "DELETE" && r.url().endsWith(`/agents/${AGENT}/preview`));
  await drawer.getByTestId("preview-end").click();
  await ended;
  await expect(input).toHaveCount(0);
  await expect(drawer.getByTestId("preview-start")).toHaveText("r1 미리 보기");
});

test("개요에서 다른 사람이 연 미리 보기를 보고 끝낸다", async ({ page }) => {
  await page.goto(`/agents/${AGENT}`);
  const card = page.getByTestId("open-previews");
  await expect(card).toContainText("열린 미리 보기");
  await expect(card.getByTestId("open-preview-row-pv-other")).toHaveText(/^dev@example\.com 님의 r1 미리 보기, \d{2}:\d{2}에 끝납니다$/);

  // 그 사람의 대화가 사라지므로 먼저 묻는다
  await card.getByTestId("open-preview-end-pv-other").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toContainText("미리 보기 끝내기");
  const ended = page.waitForRequest((r) => r.method() === "DELETE" && r.url().endsWith(`/agents/${AGENT}/previews/pv-other`));
  await dialog.getByTestId("confirm-submit").click();
  await ended;
  // 열린 것이 없으면 카드가 없다
  await expect(card).toHaveCount(0);
});
