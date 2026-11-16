import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AgentStatus } from "@/api/types";
import { RUNTIME_CANCEL, chatFeatures, useChatFeatures } from "@/lib/runtimeFeatures";

/** 런타임 `/metrics` 의 `chat` 이 상태로 넘어온 모양이다(D61, agent-runtime/runtime/attachments.py). */
const CHAT: NonNullable<AgentStatus["chat"]> = {
  documents: true,
  images: false,
  attachments_limit_fetch: false,
  limits: {
    max_attachments_per_turn: 5,
    max_images_per_turn: 4,
    max_session_images: 8,
    max_image_bytes: 2 * 1024 * 1024,
    max_session_bytes: 8 * 1024 * 1024,
  },
};
const RUNTIME = { image: "daegam-agent-runtime:0.2.0", features: [RUNTIME_CANCEL] };

describe("chatFeatures", () => {
  it("기록의 이름과 받는 첨부를 함께 읽는다", () => {
    const f = chatFeatures({ runtime: RUNTIME, chat: CHAT });
    expect(f.has(RUNTIME_CANCEL)).toBe(true);
    expect(f.has("regenerate")).toBe(false);
    expect([f.documents, f.images, f.limits]).toEqual([true, false, CHAT.limits]);
  });

  it("옛 런타임(chat 없음)과 상태가 없는 때는 첨부를 받지 않는 것으로 본다", () => {
    const old = chatFeatures({ runtime: RUNTIME, chat: null });
    expect(old.has(RUNTIME_CANCEL)).toBe(true);
    expect([old.documents, old.images, old.limits]).toEqual([false, false, null]);
    const none = chatFeatures(undefined);
    expect(none.has(RUNTIME_CANCEL)).toBe(false);
    expect([none.documents, none.images, none.limits]).toEqual([false, false, null]);
  });
});

describe("useChatFeatures", () => {
  it("runtime·chat 이 같으면 같은 값이고 바뀌면 새로 만든다", () => {
    const { result, rerender } = renderHook(({ status }) => useChatFeatures(status), {
      initialProps: { status: { runtime: RUNTIME, chat: CHAT } as Partial<AgentStatus> },
    });
    const first = result.current;
    // 상태를 다시 읽어 다른 칸(phase)만 바뀌었다
    rerender({ status: { runtime: RUNTIME, chat: CHAT, phase: "Ready" } });
    expect(result.current).toBe(first);
    rerender({ status: { runtime: RUNTIME, chat: { ...CHAT, images: true } } });
    expect(result.current).not.toBe(first);
    expect(result.current.images).toBe(true);
  });
});
