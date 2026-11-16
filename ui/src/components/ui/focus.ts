/**
 * 대화상자나 그 자리 입력칸이 닫힐 때 포커스가 갈 곳.
 *
 * 열었던 요소로 돌아가는 것이 원칙이지만, 그 버튼이 사라지는 일이 잦다. 배포를 누르면 "배포" 버튼이
 * "재배포" 로 바뀌고, 삭제를 누르면 그 행 자체가 없어진다. 그러면 포커스가 body 로 떨어져 낭독기는
 * 문서 처음으로 돌아가고 키보드는 Tab 을 처음부터 다시 밟는다. 그때는 페이지 제목으로 보낸다 (D43).
 */
import { useCallback, useRef } from "react";

/** 페이지 제목(PageHeader 의 h1). 없으면 null 이다. */
export function pageHeading(): HTMLElement | null {
  return document.querySelector<HTMLElement>("[data-page-heading]") ?? document.querySelector<HTMLElement>("h1");
}

/** 열었던 요소로 되돌린다. 그것이 사라졌으면 페이지 제목으로 간다. */
export function restoreFocus(opener: HTMLElement | null): void {
  if (opener?.isConnected) {
    opener.focus();
    return;
  }
  const heading = pageHeading();
  if (!heading) return;
  // 제목은 원래 포커스를 받지 않는 요소라 받을 수 있게 만들어 준다. Tab 순서에는 넣지 않는다
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  heading.focus();
}

/** 대화상자 안에서 Tab 이 맴돌게 하는 선택자. 감춰진 것과 disabled 는 뺀다. */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Tab 을 패널 안에 가둔다. `keydown` 에서 Tab 일 때만 부른다.
 *
 * 마지막에서 Tab 이면 처음으로, 처음에서 Shift+Tab 이면 마지막으로 보낸다. 가둘 것이 하나도 없으면
 * 패널 자신이 받는다. 그러지 않으면 Tab 한 번에 대화상자 뒤의 화면으로 빠져나가, 낭독기는 가려진
 * 것을 읽고 키보드는 돌아올 길을 잃는다.
 */
export function trapTab(panel: HTMLElement, e: { shiftKey: boolean; preventDefault: () => void }): void {
  const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
  if (items.length === 0) {
    e.preventDefault();
    panel.focus();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || active === panel)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

/**
 * 그 자리 입력칸이 닫힌 뒤 다시 서는 단추로 포커스를 돌려준다. 대화 이름 칸(SessionList)과 메시지 수정 칸(TurnActions)이 쓴다.
 * 입력칸이 열린 동안에는 연 단추가 사라지므로 그 요소를 쥐어 둘 수 없다. 닫기 전에 `arm` 을 부르면 그 뒤 처음 다시 붙는
 * 단추(`ref`)가 포커스를 받는다. 이미 붙어 있는 단추에는 ref 가 다시 불리지 않으므로 다른 행이 가져가지 않는다. 바깥을 눌러
 * 닫혀 포커스가 이미 다른 요소로 갔으면 빼앗지 않는다. body 로 떨어졌을 때만 돌려준다.
 */
export function useFocusOnReturn<T extends HTMLElement>(): { ref: (el: T | null) => void; arm: () => void } {
  const armed = useRef(false);
  const ref = useCallback((el: T | null) => {
    if (!el || !armed.current) return;
    armed.current = false;
    const active = document.activeElement;
    if (active && active !== document.body) return;
    el.focus();
  }, []);
  const arm = useCallback(() => {
    armed.current = true;
  }, []);
  return { ref, arm };
}
