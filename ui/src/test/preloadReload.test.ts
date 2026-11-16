import { describe, expect, it, vi } from "vitest";

import { reloadOnceOnPreloadError } from "@/lib/preloadReload";

function fakeWindow(storage: Pick<Storage, "getItem" | "setItem">) {
  const events = new EventTarget();
  const reload = vi.fn();
  const win = {
    addEventListener: events.addEventListener.bind(events),
    sessionStorage: storage as Storage,
    location: { reload },
  };
  const fire = () => {
    const e = new Event("vite:preloadError", { cancelable: true });
    events.dispatchEvent(e);
    return e;
  };
  return { win, reload, fire };
}

function memoryStorage() {
  const map = new Map<string, string>();
  return { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v) };
}

describe("reloadOnceOnPreloadError (D52)", () => {
  it("reloads once, then lets the error through when it comes back within 10s", () => {
    let t = 1_000_000;
    const { win, reload, fire } = fakeWindow(memoryStorage());
    reloadOnceOnPreloadError(win, () => t);
    const first = fire();
    expect(reload).toHaveBeenCalledTimes(1);
    // 막지 않는다. 막으면 Vite 도우미가 오류를 삼켜 React.lazy 가 엉뚱한 오류를 낸다
    expect(first.defaultPrevented).toBe(false);
    t += 5_000;
    fire();
    expect(reload).toHaveBeenCalledTimes(1);
    t += 10_000;
    fire();
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("does not reload when session storage is blocked", () => {
    const blocked = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => undefined,
    };
    const { win, reload, fire } = fakeWindow(blocked);
    reloadOnceOnPreloadError(win);
    fire();
    expect(reload).not.toHaveBeenCalled();
  });
});
