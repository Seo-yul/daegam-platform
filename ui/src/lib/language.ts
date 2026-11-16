/**
 * 화면 언어 (P49, D67). 한국어가 기본이고 사용자가 고르기 전까지 한국어다. 브라우저 언어는 읽지 않는다. 지금 쓰는
 * 사람의 화면 언어가 판올림으로 바뀌지 않게 하려는 것이다. 선택은 localStorage(daegam.language)에 남고, 테마처럼
 * 번들이 뜬 뒤에 <html lang> 에 입힌다(index.html 은 lang="ko" 로 시작한다). 사전은 src/i18n.
 */
import { useSyncExternalStore } from "react";

import { STORAGE_KEYS, createStore, readItem, writeItem } from "@/lib/storage";

export type Language = "ko" | "en";

export const LANGUAGES: readonly Language[] = ["ko", "en"];

/** 언어마다 자기 말로 적은 이름. 읽지 못하는 화면에서도 자기 언어를 찾을 수 있게 사전 밖에 둔다. */
export const LANGUAGE_NAMES: Readonly<Record<Language, string>> = { ko: "한국어", en: "English" };

const DEFAULT_LANGUAGE: Language = "ko";
const KEY = STORAGE_KEYS.language;
const store = createStore();
let current: Language | null = null;

function isLanguage(v: string | null): v is Language {
  return (LANGUAGES as readonly (string | null)[]).includes(v);
}

function stored(): Language | null {
  const v = readItem(KEY);
  return isLanguage(v) ? v : null;
}

function apply(language: Language): void {
  document.documentElement.lang = language;
}

export function getLanguage(): Language {
  if (current === null) {
    current = stored() ?? DEFAULT_LANGUAGE;
    apply(current);
  }
  return current;
}

export function setLanguage(language: Language): void {
  current = language;
  apply(language);
  writeItem(KEY, language);
  store.emit();
}

/** 테스트용 초기화. 저장된 선택을 지우고 기본 언어로 돌린다. */
export function resetLanguage(): void {
  current = null;
  writeItem(KEY, null);
  apply(DEFAULT_LANGUAGE);
  store.emit();
}

export function useLanguage(): Language {
  return useSyncExternalStore(store.subscribe, getLanguage, () => DEFAULT_LANGUAGE);
}
