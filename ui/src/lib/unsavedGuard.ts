/**
 * 저장하지 않은 편집을 두고 화면을 떠나려는 것을 잡는다 (D26 캔버스, D43 다섯째). 캔버스와 편집 폼
 * 여럿(Backend API·MCP Server·Gateway·Skill)이 함께 쓰므로 lib 에 둔다.
 *
 * 두 자리를 막는다. 새로고침·탭 닫기는 `beforeunload` 로 브라우저 기본 경고에 태우고, 앱 안의 이동은 링크 클릭을
 * 잡아 확인을 받는다. 링크를 직접 잡는 것은 앱 라우터가 선언형 `<BrowserRouter>` 라서다. react-router 의
 * `useBlocker` 는 데이터 라우터(`createBrowserRouter`)에서만 돌고, 라우터를 바꾸는 것은 `src/App.tsx` 의 일이다.
 * 뒤로 가기(popstate)는 이 방법으로 잡지 못한다.
 *
 * 취소 버튼처럼 링크가 아닌 이탈은 클릭 가드에 걸리지 않으므로, 훅이 돌려주는 함수를 눌렀을 때 부른다.
 * `data-unsaved-guard="same-path"` 를 단 링크는 경로가 지금과 같으면 묻지 않는다. 그 화면이 머문 채 스스로 묻는 링크다.
 */
import { useCallback, useEffect, useRef } from "react";
import { useLocation } from "react-router";

import { messages } from "@/i18n";

/** 떠나기 전에 물을 말. 주지 않았으면 편집 폼이 함께 쓰는 문구이고, 묻는 순간의 언어로 사전을 읽는다. */
function leaveMessage(message: string | undefined): string {
  return message ?? messages().ui.unsavedGuard.confirm;
}

/**
 * @param message 이탈 확인 문구. 캔버스처럼 잃는 것이 다른 화면만 준다. 없으면 편집 폼이 함께 쓰는 문구다.
 * @returns 떠나도 되는지 묻는 함수. 지킬 것이 없으면 묻지 않고 true 다.
 */
export function useUnsavedGuard(when: boolean, message?: string): () => boolean {
  // 지금 화면의 경로. 앱이 그리는 경로라 라우터에서 읽는다
  const location = useLocation();
  const pathRef = useRef(location.pathname);
  pathRef.current = location.pathname;
  useEffect(() => {
    if (!when) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // 문구는 어느 브라우저도 보이지 않는다. 옛 브라우저가 경고를 내려면 returnValue 가 비어 있지 않아야 한다
      e.returnValue = leaveMessage(message);
    };
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const target = e.target instanceof Element ? e.target : null;
      const link = target?.closest("a[href]") as HTMLAnchorElement | null;
      // 새 탭·내려받기·다른 사이트는 앱 안의 이동이 아니다 (다른 사이트는 beforeunload 가 받는다)
      if (!link || link.target === "_blank" || link.hasAttribute("download") || link.origin !== window.location.origin) return;
      if (`${link.pathname}${link.search}` === `${window.location.pathname}${window.location.search}`) return;
      // 같은 화면에 머물며 그 화면이 스스로 묻는 링크다(청지기 카드가 연 캔버스의 다른 제안, P48 6.5절). 두 번 묻지 않는다
      if (link.dataset.unsavedGuard === "same-path" && link.pathname === pathRef.current) return;
      if (!window.confirm(leaveMessage(message))) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    // 캡처 단계다. react-router 의 Link 가 먼저 이동하지 못하게 한다
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClick, true);
    };
  }, [when, message]);

  return useCallback(() => !when || window.confirm(leaveMessage(message)), [when, message]);
}

/**
 * 폼이 처음 상태에서 달라졌는지.
 *
 * `ready` 가 참이 된 첫 순간의 `draft` 를 기준으로 삼고 그 뒤로는 그것과 견준다. 그래서 고쳤다가 되돌리면
 * 다시 깨끗해진다. 편집 폼은 서버 값을 다 채운 뒤가 기준이라 그 시점을 `ready` 로 알려 준다.
 */
export function useDirty(draft: string, ready = true): boolean {
  const baseline = useRef<string | null>(null);
  useEffect(() => {
    if (ready && baseline.current === null) baseline.current = draft;
  }, [ready, draft]);
  return ready && baseline.current !== null && baseline.current !== draft;
}
