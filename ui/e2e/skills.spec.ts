import { expect, test, type Page } from "@playwright/test";

import { particle } from "../src/lib/particle";
import { selectValue } from "./select";

/**
 * Skills 화면의 브라우저 확인 (D64, P39). 목 API(mock-server.ts) 위에서 jsdom 이 못 보는 것만 본다. 단위 테스트의 Monaco 는
 * textarea 스텁이고 jsdom 은 레이아웃·실제 초점 이동을 돌리지 않는다.
 *
 *  - 파일 탭의 마크다운 미리 보기와 읽기 전용 Monaco, 리비전 탭의 Monaco diff
 *  - 직접 작성에서 입력이 멈춘 뒤 서버 검사가 파일 옆에 서는 것과 파일 삭제 뒤의 초점
 *  - 캔버스에서 키보드로 Skill 을 붙이고 빼는 동안 초점이 패널 안에 남는 것과 노드 카드의 칩
 *  - 새 리비전을 고치는 동안 상세 조회가 다시 와도 고친 글이 남고 저장이 409 를 받는 것
 *
 * 마지막 테스트는 목 상태의 sk-report 에 리비전을 하나 더하므로 맨 뒤에 둔다. 앞의 테스트는 리비전 번호를 고정해 보지 않는다.
 */

/** 새 리비전 화면의 부제 앞부분(`r2를 고쳐`). */
function editing(n: number): string {
  return `r${n}${particle(`r${n}`, "을/를")} 고쳐`;
}

/** 지금 초점을 가진 요소의 data-testid. */
function focusedTestId(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.getAttribute("data-testid") ?? null);
}

test("파일 탭: SKILL.md 는 머리말을 뗀 마크다운 미리 보기이고 스크립트는 읽기 전용 Monaco 다", async ({ page }) => {
  await page.goto("/skills/sk-report?tab=files");
  const preview = page.getByTestId("skill-files-preview").first();
  await expect(preview.getByRole("heading", { name: "보고서 문체" })).toBeVisible();
  await expect(preview.getByRole("listitem").first()).toHaveText("결론을 먼저 씁니다.");
  await expect(preview).not.toContainText("description:");

  await page.getByTestId("skill-files-file-scripts/collect.py").click();
  await expect(page.getByTestId("skill-files-script")).toHaveText("실행되지 않음");
  const viewer = page.getByTestId("skill-files-viewer");
  await expect(viewer.locator(".monaco-editor").first()).toBeVisible({ timeout: 20_000 });
  await expect(viewer).toContainText("print('collect')");
  // 읽기 전용이라 쳐도 글이 바뀌지 않는다
  await viewer.locator(".view-lines").click();
  await page.keyboard.type("zzz");
  await expect(viewer).not.toContainText("zzz");
});

test("리비전 탭: 고른 두 리비전 사이에 바뀐 파일만 목록에 서고 Monaco diff 가 그 차이를 보인다", async ({ page }) => {
  await page.goto("/skills/sk-report?tab=revisions");
  await expect(page.getByTestId("skill-revision-2")).toContainText("근거 표");
  await selectValue(page, "diff-from", "1");
  await selectValue(page, "diff-to", "2");
  const files = page.getByTestId("skill-diff-files");
  await expect(files.getByTestId("skill-diff-file-SKILL.md")).toContainText("변경");
  await expect(files.getByTestId("skill-diff-file-scripts/collect.py")).toContainText("추가");
  await expect(files.getByTestId("skill-diff-file-references/tone.md")).toHaveCount(0);
  const diff = page.getByTestId("skill-diff-editor");
  await expect(diff.locator(".monaco-diff-editor").first()).toBeVisible({ timeout: 20_000 });
  await expect(diff).toContainText("근거는 표로 정리합니다.");
});

test("직접 작성: 입력이 멈추면 서버 검사가 파일 옆에 서고, 키보드로 파일을 지우면 초점이 남은 파일로 간다", async ({ page }) => {
  await page.goto("/skills/new");
  const editor = page.getByTestId("skill-files-editor");
  await expect(editor.locator(".monaco-editor").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("skill-validation")).toHaveText("고치기 시작하면 서버가 형식을 검사합니다.");

  // 이름 칸은 머리말의 name 줄을 고치고, 실제 Monaco 에도 그 줄이 보인다
  await page.getByTestId("skill-display-name").fill("E2E Style");
  await expect(page.getByTestId("skill-name")).toHaveValue("e2e-style");
  await expect(editor).toContainText("name: e2e-style");
  await expect(page.getByTestId("skill-validation")).toHaveText("형식에 문제가 없습니다.");

  await page.getByTestId("skill-files-add-path").fill("scripts/run.sh");
  await page.getByTestId("skill-files-add-path").press("Enter");
  await expect(page.getByTestId("skill-files-warnings-scripts/run.sh")).toHaveText("1");
  await expect(page.getByTestId("skill-validation")).toHaveText("경고 1개가 있지만 저장할 수 있습니다.");

  // 머리말 name 을 규칙에 맞지 않게 고치면 SKILL.md 옆에 오류가 선다
  await page.getByTestId("skill-name").fill("Bad_Name");
  await expect(page.getByTestId("skill-files-errors-SKILL.md")).toHaveText("1");
  await expect(page.getByTestId("skill-validation")).toContainText("오류 1개");

  // 삭제 단추가 줄과 함께 사라져도 초점은 body 로 떨어지지 않는다
  await page.getByTestId("skill-files-remove-scripts/run.sh").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("skill-files-file-scripts/run.sh")).toHaveCount(0);
  expect(await focusedTestId(page)).toBe("skill-files-file-SKILL.md");
});

test("캔버스: 키보드로 Skill 을 붙이고 빼는 동안 초점이 Skills 절에 남고 노드 카드에 칩이 선다", async ({ page }) => {
  await page.goto("/agents/ag-support?tab=definition");
  await page.getByTestId("node-agent-main").click();
  await page.getByTestId("inspector-tabs-tools").click();
  const picker = page.getByTestId("agent-skill-add");
  await expect(page.getByTestId("agent-skills")).toContainText("붙인 Skill 이 없습니다.");

  // 고르개를 키보드로 열어 하나씩 붙인다. 고른 뒤에도 초점은 고르개에 있다
  for (const count of [1, 2]) {
    await picker.focus();
    await page.keyboard.press("ArrowDown");
    await expect(page.getByTestId("agent-skill-add-listbox")).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("node-skills-agent-main")).toHaveText(`Skills ${count}`);
    expect(await focusedTestId(page)).toBe("agent-skill-add");
  }
  // 후보가 다 떨어져도 disabled 가 아니라 aria-disabled 라 초점을 잃지 않고, 눌러도 열리지 않는다
  await expect(picker).toHaveAttribute("aria-disabled", "true");
  expect(await focusedTestId(page)).toBe("agent-skill-add");
  await page.keyboard.press("ArrowDown");
  await expect(page.getByTestId("agent-skill-add-listbox")).toHaveCount(0);

  // 빼기 단추의 이름에 Skill 이름이 있고, 누르면 줄과 함께 사라진 단추 대신 고르개가 초점을 받는다
  const detach = page.getByRole("button", { name: "보고서 문체 빼기" });
  await detach.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("node-skills-agent-main")).toHaveText("Skills 1");
  expect(await focusedTestId(page)).toBe("agent-skill-add");
  await expect(picker).not.toHaveAttribute("aria-disabled", "true");
});

test("새 리비전을 고치는 동안 다른 사람이 리비전을 넣고 상세 조회가 다시 와도 고친 글이 남고, 저장은 409 를 받는다", async ({ page, context }) => {
  await page.goto("/skills/sk-report/edit");
  const editor = page.getByTestId("skill-files-editor");
  await expect(editor.locator(".monaco-editor").first()).toBeVisible({ timeout: 20_000 });
  const opened = (await (await page.request.get("/api/skills/sk-report")).json()) as { revision: number };
  await expect(page.getByTestId("skill-form-page")).toContainText(editing(opened.revision));

  await editor.locator(".view-lines").click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("3. 브라우저에서 고친 줄");
  await expect(editor).toContainText("3. 브라우저에서 고친 줄");

  // 다른 사람이 새 리비전을 넣는다
  const latest = (await (await page.request.get(`/api/skills/sk-report/revisions/${opened.revision}`)).json()) as { files: { path: string; content: string }[] };
  const files = latest.files.map(({ path, content }) => ({ path, content: path === "SKILL.md" ? `${content}다른 사람이 더한 줄\n` : content }));
  const saved = await page.request.post("/api/skills/sk-report/revisions", { data: { files, expect_revision: opened.revision } });
  expect(saved.status()).toBe(201);

  // 조회가 낡은 뒤(staleTime 5초) 연결이 끊겼다 돌아오면 상세 조회가 다시 온다
  await page.waitForTimeout(5_500);
  const refetched = page.waitForResponse((r) => r.url().endsWith("/api/skills/sk-report") && r.request().method() === "GET");
  await context.setOffline(true);
  await context.setOffline(false);
  expect(((await (await refetched).json()) as { revision: number }).revision).toBe(opened.revision + 1);

  await expect(editor).toContainText("3. 브라우저에서 고친 줄");
  await expect(editor).not.toContainText("다른 사람이 더한 줄");
  await expect(page.getByTestId("skill-form-page")).toContainText(editing(opened.revision));

  await page.getByTestId("skill-save").click();
  await expect(page.getByTestId("skill-save-error")).toContainText("그 사이에 이 항목이 바뀌었습니다");
  await expect(editor).toContainText("3. 브라우저에서 고친 줄");
});
