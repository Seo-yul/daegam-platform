/**
 * 화면 문구 사전 (P49, D67). 글자를 그리는 컴포넌트와 훅은 `useT()` 로 지금 언어의 사전을 받는다. 언어 저장소를 직접
 * 구독하므로 provider 가 없고, 언어를 바꾸면 `useT()` 를 부른 컴포넌트가 다시 그려진다.
 *
 * - 컴포넌트 밖의 도우미는 사전을 인자로 받는다(`formatDate(t, iso)`). 모듈 맨 위에서 사전을 읽지 않는다.
 * - 상태에는 문장 대신 코드와 인자를 둔다. 문장은 그릴 때 만든다.
 * - `messages()` 는 컴포넌트 밖에서 일이 벌어지는 순간(핸들러 안의 window.confirm 등)에만 쓰고 결과를 남기지 않는다.
 */
import { type Language, getLanguage, useLanguage } from "@/lib/language";

import { en } from "./en";
import { ko } from "./ko";
import type { Messages } from "./types";

export type { Messages };

const DICTIONARIES: Readonly<Record<Language, Messages>> = { ko, en };

/** 지금 언어의 사전. 글자를 그리는 컴포넌트와 훅이 부른다. */
export function useT(): Messages {
  return DICTIONARIES[useLanguage()];
}

/** 지금 언어의 사전. 컴포넌트 밖에서 일이 벌어지는 순간에만 쓰고, 결과를 상태나 모듈 변수에 남기지 않는다. */
export function messages(): Messages {
  return DICTIONARIES[getLanguage()];
}

/** 언어를 정해 받는 사전. 두 언어를 나란히 견주는 테스트가 쓴다. */
export function dictionary(language: Language): Messages {
  return DICTIONARIES[language];
}
