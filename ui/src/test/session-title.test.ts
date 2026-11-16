import { describe, expect, it } from "vitest";

import { AUTO_TITLE_MAX_CHARS, titleFromMessage } from "@/lib/sessionTitle";

import { readRuntimeFixture } from "./runtimeFixtures";

/** 런타임 `title_from_message` 가 같은 사례로 맞대는 자동 제목 표본이다(P36 3절). */
interface TitleCase {
  message: string;
  title: string | null;
}
const CASES = readRuntimeFixture<{ cases: TitleCase[] }>("chat/titles.json").cases;

describe("titleFromMessage", () => {
  it.each(CASES.map((c) => [JSON.stringify(c.message), c] as const))("표본의 메시지 %s 에서 런타임과 같은 제목을 만든다", (_message, c) => {
    expect(titleFromMessage(c.message)).toBe(c.title);
  });

  // 자르는 길이는 UTF-16 단위가 아니라 글자(코드 포인트) 수다. 이모지 하나가 UTF-16 두 단위다
  const emoji = "🎉".repeat(AUTO_TITLE_MAX_CHARS);

  it.each([
    ["상한만큼의 이모지는 자르지 않는다", emoji, emoji],
    ["상한을 한 글자 넘긴 이모지는 잘라 … 를 붙인다", `${emoji}🎉`, `${emoji}…`],
    ["글이 있는 줄이 없으면 null 이다", " \n\t\u200b\n", null],
  ])("%s", (_case, message, title) => {
    expect(titleFromMessage(message)).toBe(title);
  });
});
