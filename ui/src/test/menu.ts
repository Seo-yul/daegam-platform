import { screen, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/**
 * 목록 카드의 더보기 메뉴 (D47).
 *
 * 카드는 동작을 바닥에 늘어놓지 않고 오른쪽 위 `···` 하나에 모은다. 그래서 카드 모양에서 삭제·배포를
 * 보는 테스트는 그 카드의 더보기를 먼저 연다. 표 모양에는 이 버튼이 없고 행에 버튼이 그대로 있다.
 */
export async function openCardMenu(user: UserEvent, name: string): Promise<HTMLElement> {
  await user.click(await screen.findByTestId(`card-menu-${name}`));
  return screen.findByTestId("action-menu-panel");
}

/** 카드의 더보기를 열고 항목 하나를 누른다. */
export async function clickCardMenuItem(user: UserEvent, name: string, testId: string): Promise<void> {
  const panel = await openCardMenu(user, name);
  await user.click(within(panel).getByTestId(testId));
}
