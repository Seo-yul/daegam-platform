/**
 * 영어로 그린 Gateway 폼의 언어 전환 (P49 F6). 리소스 오류는 상태에 문장 대신 문제를 두므로 언어를 바꾸면 다시 그려진다.
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { setLanguage } from "@/lib/language";
import { leftKorean } from "./leftKorean";

describe("gateway screens in English", () => {
  beforeEach(() => setLanguage("en"));

  it("redraws the resources error in the new language after a switch", async () => {
    setLanguage("ko");
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/new" />);
    await user.type(await screen.findByTestId("gp-name"), "helper");
    await user.click(await screen.findByTestId("backend-calc"));
    await user.type(screen.getByTestId("gp-req-mem"), "500mi");
    await user.click(screen.getByTestId("save-button"));
    const error = await screen.findByTestId("form-error");
    expect(error).toHaveTextContent(/^resources: requests\.memory: .*[가-힣]/);
    act(() => setLanguage("en"));
    expect(error).toHaveTextContent(/^resources: requests\.memory: /);
    expect(leftKorean(error)).toEqual([]);
  });
});
