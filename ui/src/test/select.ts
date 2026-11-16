import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/**
 * 커스텀 Select(components/ui/select.tsx) 조작 헬퍼. 네이티브 <select> 의 user.selectOptions 대신 쓴다.
 * 트리거는 data-testid, 패널은 `${testid}-listbox`, 항목은 role="option" + data-value 로 찾는다.
 */

function trigger(target: string | HTMLElement): HTMLElement {
  return typeof target === "string" ? screen.getByTestId(target) : target;
}

/** 셀렉트를 열고 listbox 를 돌려준다. 이미 열려 있으면 그대로 둔다. */
export async function openSelect(user: UserEvent, target: string | HTMLElement): Promise<HTMLElement> {
  const el = trigger(target);
  if (el.getAttribute("aria-expanded") !== "true") await user.click(el);
  const listboxId = el.getAttribute("aria-controls");
  if (!listboxId) throw new Error("select did not open (aria-controls missing)");
  return waitFor(() => {
    const listbox = document.getElementById(listboxId);
    if (!listbox) throw new Error("listbox not rendered");
    return listbox;
  });
}

/**
 * 셀렉트에서 value 가 일치하는 항목을 고른다(문자열은 data-value, 없으면 라벨과 비교. RegExp 는 라벨).
 * 항목이 비동기로 채워지는 화면이면 나타날 때까지 기다린다.
 */
export async function selectValue(user: UserEvent, target: string | HTMLElement, value: string | RegExp): Promise<void> {
  const listbox = await openSelect(user, target);
  const option = await waitFor(() => {
    const options = within(listbox).getAllByRole("option");
    const found = options.find((o) => {
      const label = (o.textContent ?? "").trim();
      if (value instanceof RegExp) return value.test(label);
      return o.getAttribute("data-value") === value || label === value;
    });
    if (!found) throw new Error(`option ${String(value)} not found in listbox`);
    return found;
  });
  await user.click(option);
}
