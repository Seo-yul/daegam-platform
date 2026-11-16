/**
 * 빈 상태의 그림 슬롯 (D44 다섯째, P24 F4).
 *
 * 그림은 빈 자리에만 붙는 장식이라 로딩·오류에는 없어야 하고, 문장과 다음 행동을 밀어내서도 안 된다.
 */
import { render, screen, within } from "@testing-library/react";

import { StateBlock } from "@/components/StateBlock";
import { AgentIllustration } from "@/components/illustrations";

describe("빈 상태 일러스트", () => {
  it("빈 상태에만 그림이 붙고 문장과 다음 행동은 그대로다", () => {
    render(
      <StateBlock empty="Agent 가 없습니다." illustration={<AgentIllustration />} emptyAction={<button type="button">만들기</button>} />,
    );
    const empty = screen.getByTestId("state-empty");
    expect(within(empty).getByRole("img", { name: "작은 그래프가 담긴 말풍선 선 그림" })).toBeInTheDocument();
    expect(empty).toHaveTextContent("Agent 가 없습니다.");
    expect(within(empty).getByRole("button", { name: "만들기" })).toBeInTheDocument();
  });

  it("로딩과 오류에는 그림이 없다", () => {
    const { rerender } = render(<StateBlock loading illustration={<AgentIllustration />} empty="없습니다." />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    rerender(<StateBlock error={new Error("boom")} illustration={<AgentIllustration />} empty="없습니다." />);
    expect(screen.getByTestId("state-error")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
