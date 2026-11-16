/**
 * 모달 대화상자의 공통 동작. Modal 과 ConfirmDialog 가 같이 쓴다.
 *
 * - 열릴 때 열었던 요소를 기억하고 첫 포커스를 준다. body 스크롤을 막는다.
 * - 닫힐 때 스크롤을 되돌리고 열었던 요소로 포커스를 돌린다(사라졌으면 페이지 제목, focus.ts).
 * - Esc 와 배경 클릭으로 닫고, Tab 은 패널 안에서 맴돈다.
 *
 * 포커스나 스크롤 잠금을 고칠 일이 생기면 여기 한 곳만 고친다. 두 대화상자에 따로 있으면 한쪽만 고쳐진다.
 */
import { useEffect, useRef, type KeyboardEvent, type MouseEvent, type RefObject } from "react";

import { restoreFocus, trapTab } from "./focus";

export interface DialogShell {
  /** role="dialog" 패널에 단다. Tab 을 가둘 범위다. */
  panelRef: RefObject<HTMLDivElement | null>;
  /** 패널의 onKeyDown. Esc 로 닫고 Tab 을 가둔다. */
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void;
  /** 배경의 onMouseDown. 배경 자체를 눌렀을 때만 닫는다. */
  onBackdropMouseDown: (e: MouseEvent<HTMLDivElement>) => void;
}

export function useDialogShell({
  open,
  onClose,
  canClose = true,
  initialFocus,
}: {
  open: boolean;
  onClose: () => void;
  /** 거짓이면 Esc·배경 클릭으로 닫지 않는다(진행 중인 확인 대화상자). */
  canClose?: boolean;
  /** 열릴 때 포커스를 줄 요소. 열린 뒤에 부르므로 패널 안의 ref 를 읽어도 된다. */
  initialFocus: () => HTMLElement | null;
}): DialogShell {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    initialFocus()?.focus();
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prevOverflow;
      restoreFocus(opener);
    };
    // 열고 닫힐 때만 돈다. initialFocus 는 렌더마다 새 함수라 넣으면 렌더마다 포커스를 빼앗는다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") {
      e.stopPropagation();
      if (canClose) onClose();
      return;
    }
    if (e.key !== "Tab") return;
    const panel = panelRef.current;
    if (panel) trapTab(panel, e);
  }

  function onBackdropMouseDown(e: MouseEvent<HTMLDivElement>) {
    // 바깥(배경) 을 눌렀을 때만 닫는다. 패널 안에서 시작한 드래그는 무시한다.
    if (e.target === e.currentTarget && canClose) onClose();
  }

  return { panelRef, onKeyDown, onBackdropMouseDown };
}
