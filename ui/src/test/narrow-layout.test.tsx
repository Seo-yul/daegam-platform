import { render, screen } from "@testing-library/react";

import { TestApp } from "@/App";
import { NARROW_QUERY } from "@/lib/media";

import { stubMatchMedia } from "./media";

/**
 * 좁은 창의 껍데기. 펼친 사이드바는 240px 라 420px 창에서는 본문에 165px 밖에 안 남아
 * 폼이 못 쓰게 된다. 그 폭에서는 저장된 취향과 무관하게 아이콘 레일로 접고, 아무 일도 못 하는
 * 펼치기 버튼은 두지 않는다.
 */

describe("narrow viewport shell", () => {
  it("collapses the sidebar to the icon rail and drops the toggle", async () => {
    stubMatchMedia(NARROW_QUERY);
    render(<TestApp initialPath="/hub" />);
    const sidebar = await screen.findByTestId("sidebar");
    expect(sidebar).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByTestId("sidebar-toggle")).not.toBeInTheDocument();
    // 레일이어도 항목은 이름을 갖고 있어 닿을 수 있다
    expect(screen.getByTestId("nav-mcp-hub")).toHaveAttribute("aria-label", "MCP Hub");
  });
});
