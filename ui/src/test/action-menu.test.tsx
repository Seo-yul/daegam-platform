import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ActionMenu, type MenuItem } from "@/components/ui/action-menu";

import { focusLikeBrowser } from "./browserFocus";
import { renderWithProviders } from "./render";

/**
 * 더보기 메뉴 (D44 둘째). 머리의 동작 대부분이 이 안으로 들어갔으므로 열림·고름·닫힘과 파괴적 항목의
 * 자리를 여기서 못 박는다. 포털·자리 잡기는 `usePopover` 쪽 규칙이라 여기서 다시 재지 않는다.
 */
function renderMenu(items: MenuItem[]) {
  return renderWithProviders(<ActionMenu items={items} label="테스트 동작" />);
}

describe("ActionMenu", () => {
  // 열면 첫 항목이 포커스를 받는 것을 숨은 패널에 포커스를 주지 않는 브라우저 규칙으로 본다
  beforeEach(() => focusLikeBrowser());

  const items = (onClick: () => void): MenuItem[] => [
    { label: "복제", testId: "clone-button", onClick },
    { label: "오버라이드 제안", testId: "suggest-button", to: "/mcp-servers/ms-calc/suggest" },
    { label: "삭제", testId: "delete-button", danger: true, onClick: () => {} },
  ];

  it("동그란 트리거가 메뉴를 열고 항목을 누르면 그 일을 하고 닫힌다", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    renderMenu(items(onClick));

    const trigger = screen.getByTestId("header-menu");
    expect(trigger).toHaveAttribute("aria-label", "더보기");
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("action-menu-panel")).not.toBeInTheDocument();

    await user.click(trigger);
    const panel = screen.getByTestId("action-menu-panel");
    expect(panel).toHaveAttribute("role", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    // 링크 항목도 같은 role 이라야 키보드가 한 줄로 읽는다
    expect(within(panel).getByTestId("suggest-button")).toHaveAttribute("href", "/mcp-servers/ms-calc/suggest");

    await user.click(within(panel).getByTestId("clone-button"));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("action-menu-panel")).not.toBeInTheDocument();
  });

  it("Escape 는 메뉴를 닫고 포커스를 트리거로 돌린다", async () => {
    const user = userEvent.setup();
    renderMenu(items(() => {}));
    const trigger = screen.getByTestId("header-menu");
    await user.click(trigger);
    // 열면 첫 항목에 포커스가 간다. 그래야 화살표로 바로 고른다
    expect(screen.getByTestId("clone-button")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("suggest-button")).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("action-menu-panel")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("파괴적 항목은 구분선 아래에 붉은 글자로 모인다", async () => {
    const user = userEvent.setup();
    renderMenu(items(() => {}));
    await user.click(screen.getByTestId("header-menu"));
    const panel = screen.getByTestId("action-menu-panel");
    const del = within(panel).getByTestId("delete-button");
    expect(del.className).toContain("text-err-fg");
    expect(within(panel).getByTestId("clone-button").className).not.toContain("text-err-fg");
    // 준 순서와 상관없이 파괴적 항목은 맨 아래다
    const order = within(panel)
      .getAllByRole("menuitem")
      .map((el) => el.getAttribute("data-testid"));
    expect(order).toEqual(["clone-button", "suggest-button", "delete-button"]);
  });

  it("disabled 항목은 눌리지 않고 포커스도 받지 않는다", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    renderMenu([
      { label: "복제", testId: "clone-button", disabled: true, onClick },
      { label: "삭제", testId: "delete-button", danger: true, onClick: () => {} },
    ]);
    await user.click(screen.getByTestId("header-menu"));
    const panel = screen.getByTestId("action-menu-panel");
    expect(within(panel).getByTestId("clone-button")).toBeDisabled();
    expect(within(panel).getByTestId("delete-button")).toHaveFocus();
    await user.click(within(panel).getByTestId("clone-button"));
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByTestId("action-menu-panel")).toBeInTheDocument();
  });
});
