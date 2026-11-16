/**
 * 라이트·다크 테마 전환. 라이트가 기본이다. CSS 는 :root 가 다크 토큰이고 <html data-theme="light"> 가 라이트
 * 토큰으로 덮는다. index.html 이 data-theme="light" 로 시작하므로 첫 그림부터 라이트다(인라인 스크립트는 CSP 가
 * 막아 저장된 선택은 번들이 뜬 뒤에 입힌다). 선택은 localStorage(daegam.theme)에 남는다. 토큰 정의는 src/index.css.
 */
import { useSyncExternalStore } from "react";

import { STORAGE_KEYS, createStore, readItem, writeItem } from "@/lib/storage";

export type Theme = "dark" | "light";

const KEY = STORAGE_KEYS.theme;
const store = createStore();
let current: Theme | null = null;

function stored(): Theme | null {
  const v = readItem(KEY);
  return v === "light" || v === "dark" ? v : null;
}

function apply(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function getTheme(): Theme {
  if (current === null) {
    current = stored() ?? "light";
    apply(current);
  }
  return current;
}

export function setTheme(theme: Theme): void {
  current = theme;
  apply(theme);
  writeItem(KEY, theme);
  store.emit();
}

export function toggleTheme(): void {
  setTheme(getTheme() === "dark" ? "light" : "dark");
}

/**
 * 라이트 테마에서 편집기가 쓰는 Monaco 테마 이름. `setupMonaco`(components/monacoSetup.ts)가 이 이름으로
 * 등록한다. 모르는 이름을 주면 Monaco 가 말없이 `vs` 로 떨어뜨리므로 등록과 사용이 이 상수 하나를 본다.
 * 상수가 여기 있는 것은 이 모듈이 Monaco 를 끌어오지 않아서다. 반대로 두면 메인 번들에 Monaco 가 들어온다.
 */
export const MONACO_LIGHT = "daegam-light";

/**
 * Monaco 에 넘길 테마 이름. 편집기 넷이 같이 쓴다.
 *
 * 다크는 내장 `vs-dark` 이고 라이트는 `MONACO_LIGHT`(vs 에서 커서 줄 테두리만 뺀 것)다.
 */
export function monacoTheme(theme: Theme): typeof MONACO_LIGHT | "vs-dark" {
  return theme === "dark" ? "vs-dark" : MONACO_LIGHT;
}

/** 테스트용 초기화 */
export function resetTheme(): void {
  current = null;
  writeItem(KEY, null);
  delete document.documentElement.dataset.theme;
  store.emit();
}

export function useTheme(): Theme {
  return useSyncExternalStore(store.subscribe, getTheme, () => "light" as Theme);
}
