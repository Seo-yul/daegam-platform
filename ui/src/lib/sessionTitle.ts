/**
 * 대화 목록과 채팅 패널이 보이는 세션 이름 (P36 3절). 제목은 런타임이 새 세션의 첫 메시지로 채우고
 * (agent-runtime/runtime/turn_record.py 의 title_from_message) 소유자가 바꾼다. 제목이 없는 옛 세션은 지금처럼 id 앞 8자를 쓴다.
 * 이 화면에서 연 새 세션은 런타임이 채운 제목을 다시 읽지 않고 같은 규칙의 `titleFromMessage` 로 만든 제목을 보인다. 두 규칙이
 * 같은지는 런타임 표본(agent-runtime/schema/fixtures/chat/titles.json)으로 맞댄다.
 */
import { clipChars } from "@/lib/utils";

/** 이름 바꾸기가 받는 제목 길이의 상한이다. 오류 사전도 읽어 lib/limits.ts 에 둔다. */
export { SESSION_TITLE_MAX_CHARS } from "@/lib/limits";

/** 제목이 없을 때 보이는 id 앞자리 길이다. */
const SESSION_ID_LABEL_CHARS = 8;
/** 첫 메시지로 채우는 자동 제목의 상한이다. 넘으면 잘라 "…" 를 붙인다. 런타임 turn_record.py 의 같은 이름 상수와 같은 값이다. */
export const AUTO_TITLE_MAX_CHARS = 60;

// 런타임(파이썬)의 str.splitlines 와 str.split() 이 가르는 문자다. JS 의 \s 와는 \x1c~\x1f, \x85, \ufeff 가 달라 따로 적는다
const LINE_BREAK = /\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/;
const SPACE_RUN = /[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;
// 공백이 아닌 제어·서식 문자(Cc·Cf)다. 줄바꿈이나 방향을 뒤집는 문자가 목록 한 줄을 흐트러뜨리지 않게 뺀다. 탭·줄바꿈처럼
// 공백인 제어 문자는 남겨 한 칸으로 줄인다
const DROPPED = /(?![\t\n\v\f\r\x1c-\x1f\x85])[\p{Cc}\p{Cf}]/gu;

/** 세션의 보이는 이름이다. */
export function sessionLabel(id: string, title?: string | null): string {
  return title || id.slice(0, SESSION_ID_LABEL_CHARS);
}

/**
 * 새 세션의 자동 제목이다. 첫 메시지에서 글이 있는 첫 줄을 골라 `cleanTitle` 로 정리하고, `AUTO_TITLE_MAX_CHARS` 자(코드
 * 포인트)를 넘으면 자르고 "…" 를 붙인다. 글이 있는 줄이 없으면 null 이다.
 */
export function titleFromMessage(message: string): string | null {
  for (const line of message.split(LINE_BREAK)) {
    const title = cleanTitle(line);
    if (title) return clipChars(title, AUTO_TITLE_MAX_CHARS);
  }
  return null;
}

/**
 * 런타임 clean_title 과 같은 정리다. 공백이 아닌 제어·서식 문자를 먼저 빼고, 공백을 한 칸으로 줄여 앞뒤를 뗀 뒤 NFC 로 맞춘다.
 * 빼는 것을 먼저 해야 공백 사이에 낀 문자가 빠진 자리에 공백 두 칸이 남지 않는다.
 */
function cleanTitle(text: string): string {
  return text.replace(DROPPED, "").split(SPACE_RUN).filter(Boolean).join(" ").normalize("NFC");
}
