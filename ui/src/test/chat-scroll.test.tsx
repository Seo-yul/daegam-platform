import { render } from "@testing-library/react";
import { act } from "react";

import { useStickToBottom } from "@/components/chat";
import { spyScrollTo } from "./scroll";

/**
 * 채팅 전사의 바닥 따라가기 (components/chat/useStickToBottom.ts). 말풍선 하나에 토큰이 이어 붙는
 * 동안에도 따라가야 하고, 사용자가 위로 올려 읽는 중에는 따라가면 안 된다.
 */

/** 컨테이너의 스크롤 위치를 꾸며 낸다. jsdom 은 레이아웃이 없어 셋 다 0 이다. */
function place(el: HTMLElement, { scrollHeight, clientHeight, scrollTop }: { scrollHeight: number; clientHeight: number; scrollTop: number }): void {
  for (const [k, value] of Object.entries({ scrollHeight, clientHeight, scrollTop })) {
    Object.defineProperty(el, k, { configurable: true, value });
  }
}

function Transcript({ text }: { text: string }) {
  const { scrollerRef, onScroll } = useStickToBottom();
  return (
    <div ref={scrollerRef} onScroll={onScroll} data-testid="scroller">
      <p>{text}</p>
    </div>
  );
}

describe("useStickToBottom", () => {
  it("follows the bottom when the same bubble grows and no item is added", () => {
    const scroll = spyScrollTo();
    const { rerender, getByTestId } = render(<Transcript text="안녕" />);
    getByTestId("scroller");
    const first = scroll.count();
    rerender(<Transcript text="안녕하세요 " />);
    rerender(<Transcript text="안녕하세요 반갑" />);
    rerender(<Transcript text="안녕하세요 반갑습니다" />);
    expect(scroll.count()).toBe(first + 3);
    // 전사 컨테이너만 움직인다. scrollIntoView 는 껍데기의 main 까지 끌어내린다
    expect(scroll.intoView()).toBe(0);
  });

  it("stops following once the reader scrolls up, and resumes at the bottom", () => {
    const scroll = spyScrollTo();
    const { rerender, getByTestId } = render(<Transcript text="한 줄" />);
    const scroller = getByTestId("scroller");

    // 위로 올려 읽는 중: 바닥까지 800px 남았다
    place(scroller, { scrollHeight: 1000, clientHeight: 200, scrollTop: 0 });
    act(() => scroller.dispatchEvent(new Event("scroll", { bubbles: true })));
    const before = scroll.count();
    rerender(<Transcript text="한 줄 더" />);
    rerender(<Transcript text="한 줄 더 더" />);
    expect(scroll.count()).toBe(before);

    // 다시 바닥으로 내리면 따라간다
    place(scroller, { scrollHeight: 1000, clientHeight: 200, scrollTop: 800 });
    act(() => scroller.dispatchEvent(new Event("scroll", { bubbles: true })));
    rerender(<Transcript text="한 줄 더 더 더" />);
    expect(scroll.count()).toBeGreaterThan(before);
  });
});
