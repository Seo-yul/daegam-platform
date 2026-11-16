import { stubProperty } from "./globals";

/**
 * 미디어 쿼리 (lib/media.ts). jsdom 에는 matchMedia 가 없어 화면은 늘 넓은 창으로 보인다. 부른 테스트 동안만 matching
 * 쿼리 하나만 참인 구현을 꽂는다.
 */
export function stubMatchMedia(matching: string): void {
  stubProperty(window, "matchMedia", (query: string) => ({
    matches: query === matching,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}
