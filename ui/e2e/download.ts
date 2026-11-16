import { readFile } from "node:fs/promises";

import type { Locator, Page } from "@playwright/test";

/**
 * 단추를 눌러 내려받은 파일의 이름과 글. 브라우저가 실제로 저장한 것을 읽는다. 스펙 파일이 아니므로 Playwright 가 테스트로 집어
 * 들지 않는다.
 */
export async function downloaded(page: Page, button: Locator): Promise<{ name: string; text: string }> {
  const download = page.waitForEvent("download");
  await button.click();
  const saved = await download;
  return { name: saved.suggestedFilename(), text: await readFile(await saved.path(), "utf8") };
}
