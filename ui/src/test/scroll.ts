import { stubProperty } from "./globals";

/**
 * 바닥 따라가기 (components/chat/useStickToBottom.ts). jsdom 의 요소에는 scrollTo 가 없다. 부른 테스트 동안만 호출을
 * 세는 구현을 꽂고, 센 수로 바닥을 따라갔는지 본다. scrollIntoView 도 세어 두어, 전사 밖(껍데기의 main)을 움직이는
 * 호출이 없는지 본다.
 */
export function spyScrollTo(): { count: () => number; intoView: () => number } {
  let calls = 0;
  let intoView = 0;
  stubProperty(HTMLElement.prototype, "scrollTo", () => {
    calls += 1;
  });
  stubProperty(HTMLElement.prototype, "scrollIntoView", () => {
    intoView += 1;
  });
  return { count: () => calls, intoView: () => intoView };
}
