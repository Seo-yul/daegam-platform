import { screen, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/**
 * 열린 확인 대화상자에서 확인 버튼을 누른다. 이름 재입력이 필요한 대화상자면 name 을 채운다.
 * name 없이 이름 입력이 나타나면 테스트가 위험도 규칙을 잘못 알고 있는 것이므로 바로 실패로 끝낸다.
 */
export async function submitConfirm(user: UserEvent, name?: string): Promise<void> {
  const dialog = await screen.findByTestId("confirm-dialog");
  const input = within(dialog).queryByTestId("confirm-name");
  if (input) {
    if (!name) throw new Error("confirm dialog requires the resource name; pass it to submitConfirm");
    await user.clear(input);
    await user.type(input, name);
  }
  await user.click(within(dialog).getByTestId("confirm-submit"));
}
