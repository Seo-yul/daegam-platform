import { vi } from "vitest";

/**
 * 부른 테스트 동안 브라우저처럼 `visibility: hidden` 인 요소에는 포커스를 주지 않는다. spy 는 setup.ts 의 resetMockState 가
 * 되돌린다.
 *
 * jsdom 은 visibility 를 보지 않고 포커스를 준다. 그래서 열자마자 포커스를 주는 떠 있는 패널(인용 작은 창, 동작 메뉴)이 아직
 * 숨은 커밋에서 focus() 를 불러도 테스트가 통과하고, 실제 브라우저에서는 포커스가 가지 않아 Escape 로 닫히지 않던 결함을
 * 놓쳤다(P36).
 */
export function focusLikeBrowser(): void {
  const focus = HTMLElement.prototype.focus;
  vi.spyOn(HTMLElement.prototype, "focus").mockImplementation(function (this: HTMLElement, options?: FocusOptions) {
    if (getComputedStyle(this).visibility === "hidden") return;
    focus.call(this, options);
  });
}
