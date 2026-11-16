/**
 * 파일 이름과 크기의 화면 규칙. 여러 화면이 같은 판정과 같은 문장을 쓰도록 한 곳에 둔다.
 *
 * 마크다운 판정은 Skill 파일 화면과 채팅의 기억·세션 파일 서랍이, 상한을 넘은 파일의 안내는 Knowledge 문서 업로드와 Skill
 * 가져오기가 쓴다.
 */
import type { Messages } from "@/i18n";

import { formatBytes } from "./utils";

/** 마크다운으로 그리는 파일인가. 끝의 `.md` 를 대소문자 없이 본다. */
export function isMarkdownPath(path: string): boolean {
  return path.toLowerCase().endsWith(".md");
}

/** 파일 선택 창의 accept 꼴 확장자 목록(".zip,.md")에 드는 이름인가. 끌어 놓은 파일은 선택 창을 거치지 않으므로 이것으로 거른다. */
export function matchesAccept(name: string, accept: string): boolean {
  const lower = name.toLowerCase();
  return accept.split(",").some((ext) => lower.endsWith(ext.trim().toLowerCase()));
}

/**
 * 상한을 넘어 올리지 않은 파일의 안내. 올린 뒤 413 을 받는 대신 고른 자리에서 되돌릴 때 쓴다. `limit` 은 "문서 하나는 10MB" 처럼
 * 상한을 말하는 앞부분이고 `advice` 는 덧붙일 처방이다. 파일 크기는 상한과 같은 단위로 적도록 `format` 을 받는다. 문장은
 * 사전(ui.files.tooLarge)에 있다.
 */
export function fileTooLargeText(
  t: Messages,
  file: { name: string; size: number },
  limit: string,
  { advice, format = formatBytes }: { advice?: string; format?: (n: number) => string } = {},
): string {
  const head = t.ui.files.tooLarge(file.name, format(file.size), limit);
  return advice ? `${head} ${advice}` : head;
}
