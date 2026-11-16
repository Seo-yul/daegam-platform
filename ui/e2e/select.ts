import { expect, type Locator, type Page } from "@playwright/test";

/**
 * 커스텀 Select(src/components/ui/select.tsx) 를 브라우저에서 조작한다. 네이티브 selectOption 대신 쓴다.
 * 트리거는 data-testid, 패널은 `${testid}-listbox`, 항목은 role=option 이다.
 */
export type SelectTarget = string | { value: string } | { label: string | RegExp } | { index: number };

export async function openSelect(page: Page, testId: string): Promise<Locator> {
  const trigger = page.getByTestId(testId);
  if ((await trigger.getAttribute("aria-expanded")) !== "true") await trigger.click();
  const listbox = page.getByTestId(`${testId}-listbox`);
  await expect(listbox).toBeVisible();
  return listbox;
}

/** 열린 패널 안의 항목. 문자열은 value 로 본다. 라벨이 문자열이면 정확히 같은 것만, 정규식이면 맞는 것을 찾는다. */
export function optionLocator(listbox: Locator, target: SelectTarget): Locator {
  if (typeof target === "string" || "value" in target) {
    const value = typeof target === "string" ? target : target.value;
    return listbox.locator(`[role="option"][data-value="${value.replace(/"/g, '\\"')}"]`);
  }
  if ("label" in target) return listbox.getByRole("option", { name: target.label, exact: typeof target.label === "string" });
  return listbox.getByRole("option").nth(target.index);
}

/** 항목이 비동기로 채워지면 나타날 때까지 기다린다. */
export async function selectValue(page: Page, testId: string, target: SelectTarget): Promise<void> {
  const listbox = await openSelect(page, testId);
  await optionLocator(listbox, target).click();
  await expect(listbox).toBeHidden();
}
