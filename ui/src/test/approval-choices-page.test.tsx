/**
 * 계정 화면 "기억한 선택" (P43 5절). 목록의 Agent·Tool·선택·만료와 지우기이고, 지운 Agent 의 선택은 이름 대신 id 를 보인다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";

import { approvalChoiceState, lastRequest } from "./msw";

describe("기억한 선택", () => {
  it("내 선택을 보이고 하나를 지운다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/approval-choices" />);
    const row = await screen.findByTestId("approval-choice-ac-divide");
    expect(screen.getByTestId("page-title")).toHaveTextContent("기억한 선택");
    expect(row).toHaveTextContent("support");
    expect(row).toHaveTextContent("Divide");
    expect(row).toHaveTextContent("항상 승인");
    const gone = screen.getByTestId("approval-choice-ac-gone");
    expect(gone).toHaveTextContent("ag-gone");
    expect(gone).toHaveTextContent("항상 거부");
    await user.click(within(row).getByTestId("approval-choice-delete-ac-divide"));
    await waitFor(() => expect(screen.queryByTestId("approval-choice-ac-divide")).not.toBeInTheDocument());
    expect(lastRequest("DELETE", "/me/approval-choices/ac-divide")).toBeDefined();
  });

  it("선택이 없으면 빈 줄의 문장이다", async () => {
    approvalChoiceState.mine = [];
    render(<TestApp initialPath="/account/approval-choices" />);
    expect(await screen.findByText("기억한 선택이 없습니다.")).toBeInTheDocument();
  });
});
