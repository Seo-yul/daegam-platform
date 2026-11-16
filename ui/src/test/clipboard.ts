import { vi, type Mock } from "vitest";

import { stubProperty } from "./globals";

/**
 * 복사(lib/clipboard.ts 의 `copyText`)는 두 길로 간다. jsdom 에는 보안 컨텍스트도 Clipboard API 도 execCommand 도
 * 없으므로, 부른 테스트 동안만 한쪽 길을 흉내 낸다.
 *
 * userEvent.setup() 은 navigator.clipboard 를 자기 것으로 갈아 끼우므로 그 뒤에 부른다.
 */

/** https 처럼 보안 컨텍스트의 Clipboard API 로 복사한다. 돌려준 writeText 목으로 복사한 글을 본다. */
export function stubClipboard(): Mock<(text: string) => Promise<void>> {
  const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined);
  stubProperty(window, "isSecureContext", true);
  stubProperty(navigator, "clipboard", { writeText });
  return writeText;
}

/** 평문 http 라 Clipboard API 가 없고 execCommand("copy") 폴백만 남는다. copied 는 폴백이 돌려줄 결과다. */
export function stubPlainHttpCopy(copied: boolean): Mock<(command: string) => boolean> {
  const execCommand = vi.fn<(command: string) => boolean>().mockReturnValue(copied);
  stubProperty(window, "isSecureContext", false);
  stubProperty(document, "execCommand", execCommand);
  return execCommand;
}
