import type { ko } from "./ko";

/**
 * 사전의 모양. 한국어 사전에서 끌어낸다. 객체 리터럴의 문자열 속성은 이미 `string` 으로 넓혀지므로 따로 넓히지 않는다.
 * 코드로 찾는 표(errors 등)는 `satisfies` 로 적어 키가 타입에 남게 한다. `Record<string, ...>` 로 적으면 영어 쪽 키를
 * 검사하지 못한다.
 */
export type Messages = typeof ko;
