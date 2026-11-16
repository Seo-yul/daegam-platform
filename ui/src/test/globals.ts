import { onTestFinished } from "vitest";

/**
 * target 의 key 를 부른 테스트 동안만 value 로 바꾼다. 테스트가 끝나면(실패해도) 원래 속성을 되돌리고, 원래 없던 속성이면
 * 지운다. jsdom 에 없는 브라우저 기능(matchMedia, 보안 컨텍스트, Clipboard API, scrollIntoView, createImageBitmap)을
 * 흉내 낼 때 쓴다.
 */
export function stubProperty<T extends object>(target: T, key: keyof T, value: unknown): void {
  const original = Object.getOwnPropertyDescriptor(target, key);
  Object.defineProperty(target, key, { value, configurable: true, writable: true });
  onTestFinished(() => {
    if (original) Object.defineProperty(target, key, original);
    else Reflect.deleteProperty(target, key);
  });
}
