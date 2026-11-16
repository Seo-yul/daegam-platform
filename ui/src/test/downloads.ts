import { vi } from "vitest";

import { stubProperty } from "./globals";

/** 가로챈 내려받기가 쓰는 object URL 이다. */
export const STUB_OBJECT_URL = "blob:test/1";

/**
 * 내려받기(lib/download.ts)를 부른 테스트 동안만 가로챈다. jsdom 에는 object URL 이 없고 링크를 눌러도 아무것도 저장되지
 * 않으므로, object URL 을 채우고 링크 클릭을 막아 저장 이름을 모은다. 돌려준 목록에 저장 이름이 쌓인다. object URL 은
 * stubProperty 가, 클릭 spy 는 setup.ts 의 resetMockState 가 되돌린다.
 */
export function stubDownloads(): string[] {
  const saved: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
  stubProperty(URL, "createObjectURL", vi.fn(() => STUB_OBJECT_URL));
  stubProperty(URL, "revokeObjectURL", vi.fn());
  return saved;
}
