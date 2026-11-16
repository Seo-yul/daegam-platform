/**
 * 영어로 그린 MCP Server 만들기 폼의 언어 전환 (P49 F5). 폼의 오류는 상태에 문장 대신 문제를 두므로, 언어를 바꾸면 입력은
 * 그대로이고 오류 문장만 바뀐다. 폼의 글자와 낭독기 이름에 한글이 남지 않는다.
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { setLanguage } from "@/lib/language";

import { leftKorean } from "./leftKorean";
import { selectValue } from "./select";

describe("MCP Servers in English", () => {
  beforeEach(() => setLanguage("en"));

  it("draws the form in English and redraws its error in the new language without losing input", async () => {
    const user = userEvent.setup();
    setLanguage("ko");
    render(<TestApp initialPath="/mcp-servers/new" />);
    const page = await screen.findByTestId("mcp-server-form-page");
    await user.type(screen.getByTestId("mp-name"), "calc2");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("form-error")).toHaveTextContent("백엔드마다 Backend API 와 spec version 을 선택해 주세요.");

    act(() => setLanguage("en"));
    expect(screen.getByTestId("form-error")).toHaveTextContent("Select a Backend API and a spec version for each backend.");
    expect(screen.getByTestId("mp-name")).toHaveValue("calc2");
    expect(screen.getByTestId("mp-display-name")).toHaveAttribute("placeholder", "Order lookup Tools");
    expect(screen.getByTestId("backend-add")).toHaveTextContent("Add backend");
    expect(page).toHaveTextContent("After overrides, Tool names must be unique within this MCP Server.");
    expect(leftKorean(page)).toEqual([]);

    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    expect(await screen.findByTestId("tools-all-warning")).toHaveTextContent("(No Tools selected, so all");
    expect(screen.getByTestId("tools-all")).toHaveTextContent("Select all");
    expect(leftKorean(page)).toEqual([]);
  });

  it("redraws the resources error in the new language after a switch", async () => {
    const user = userEvent.setup();
    setLanguage("ko");
    render(<TestApp initialPath="/mcp-servers/new" />);
    await user.type(await screen.findByTestId("mp-name"), "calc2");
    await user.type(screen.getByTestId("mp-req-mem"), "500mi");
    await user.click(screen.getByTestId("save-button"));
    const error = await screen.findByTestId("form-error");
    expect(error).toHaveTextContent(/^resources: requests\.memory: .*[가-힣]/);

    act(() => setLanguage("en"));
    expect(error).toHaveTextContent("resources: requests.memory: Enter memory in bytes or with Ki/Mi/Gi/Ti or k/M/G/T units (e.g. 256Mi, 1Gi).");
    expect(screen.getByTestId("mp-req-mem")).toHaveValue("500mi");
    expect(leftKorean(error)).toEqual([]);
  });
});
