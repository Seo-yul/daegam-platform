/**
 * 미디어 쿼리 구독 (useSyncExternalStore). 좁은 폭에서 껍데기를 접는 데 쓴다.
 *
 * 사이드바는 펼친 상태가 240px 라, 420px 안팎의 창에서는 본문에 165px 밖에 안 남아 폼이 못 쓰게 된다.
 * 그 폭에서는 저장된 취향과 무관하게 아이콘 레일로 접는다(사용자가 고른 값은 건드리지 않으므로 창을
 * 넓히면 원래대로 돌아온다).
 */
import { useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";

/** 이 폭 미만이면 사이드바를 레일로 접는다. tailwind 의 md 와 같다. */
export const NARROW_QUERY = "(max-width: 767px)";

function subscribe(query: string) {
  return (onChange: () => void) => {
    if (typeof window.matchMedia !== "function") return () => undefined;
    const mql = window.matchMedia(query);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  };
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    subscribe(query),
    () => (typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false),
    () => false,
  );
}

/** 사이드바를 펼쳐 둘 자리가 없는 폭인가. */
export function useNarrowViewport(): boolean {
  return useMediaQuery(NARROW_QUERY);
}

/** 이보다 좁은 칸에서는 diff 를 좌우로 나누지 않는다. 한쪽에 YAML 한 줄이 들어갈 만한 폭이다. */
export const DIFF_SIDE_BY_SIDE_MIN = 700;

/**
 * 요소 하나의 폭이 min 보다 좁은가. 화면이 아니라 그 칸을 잰다. 좁은 칸에서 Monaco 는 좌우 diff 를 고집해 원본 쪽을 몇
 * 픽셀로 찌그러뜨리므로 스펙 다듬기와 Skill 리비전 차이가 이것으로 정한다. 요소는 처음 그릴 때부터 있어야 한다.
 */
export function useNarrowerThan<T extends HTMLElement>(min: number): [RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < min));
    ro.observe(el);
    return () => ro.disconnect();
  }, [min]);
  return [ref, narrow];
}
