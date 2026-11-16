/**
 * 코드 편집기(`components/SpecEditor`)의 입력 칸.
 *
 * 편집기는 Monaco 라 lazy 로 들어온다. 그래서 첫 렌더에는 자리만 있고 조금 뒤에 뜬다. 테스트에서는
 * `stubs/monaco-react` 가 그것을 textarea 로 바꾸므로, testid 가 붙은 껍데기 안의 textbox 를 집는다.
 */
import { screen, within } from "@testing-library/react";

export async function codeEditor(testId: string): Promise<HTMLTextAreaElement> {
  const host = await screen.findByTestId(testId);
  return within(host).getByRole("textbox") as HTMLTextAreaElement;
}
