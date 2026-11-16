/**
 * 영어로 그린 머리 메뉴의 접근 이름 (P49 F4). 머리의 더보기 메뉴는 제목, K8s 이름, "이 리소스" 순으로 이름을 받는다.
 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PageHeader } from "@/components/Layout";
import { setLanguage } from "@/lib/language";

import { renderWithProviders } from "./render";

describe("page header in English", () => {
  beforeEach(() => setLanguage("en"));

  it("names the page header menu after the title, the K8s name or this resource", async () => {
    const user = userEvent.setup();
    const menu = [{ label: "Delete", onClick: () => undefined }];
    const view = renderWithProviders(<PageHeader title="Orders" menu={menu} />);
    await user.click(screen.getByTestId("header-menu"));
    expect(screen.getByTestId("action-menu-panel")).toHaveAttribute("aria-label", "Actions for Orders");
    view.unmount();

    // 제자리에서 이름을 고치는 화면은 입력칸을 제목으로 넘긴다. 그때는 name 이고, 그것도 없으면 이 리소스다
    renderWithProviders(<PageHeader title={<input aria-label="name" />} menu={menu} />);
    await user.click(screen.getByTestId("header-menu"));
    expect(screen.getByTestId("action-menu-panel")).toHaveAttribute("aria-label", "Actions for this resource");
  });
});
