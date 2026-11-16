/**
 * 열린 채팅 SSE(Response)를 끝까지 읽어 전사 항목으로 접는 공용 루프 (D26·D31). Agent 채팅(ChatPanel)과 플랫폼 비서
 * (AssistantPanel)가 같이 쓴다. 항목 갱신은 함수형(`update(prev => next)`)이라 setState 를 그대로 넘겨도 되고, `ui.navigate`
 * 같은 항목이 아닌 이벤트는 갱신 함수 밖에서 hooks 로 처리해 갱신 함수 안에 부수 효과가 들어가지 않는다.
 * 스트림이 끝나거나 끊기면 흘러 들던 말풍선을 닫고 done·aborted·error 중 하나를 돌려준다. 열기(fetch) 자체의 실패는 부르는 쪽이 다룬다.
 * 끊긴 까닭은 문장이 아니라 잡은 값(error)으로 돌려주고, 부르는 쪽이 그릴 때 describeChatError 로 문장을 만든다.
 */
import { applyChatEvent, applyUiEvent, closeStreaming, type ChatEventHooks, type ChatItem } from "@/components/chat/items";
import { readChatStream, type ChatEvent } from "@/lib/chatStream";
import { isAbortError } from "@/lib/utils";

export interface ChatStreamHandlers {
  /** 전사 갱신. React 의 setState(함수형)를 그대로 넘겨도 된다. */
  update: (fn: (prev: ChatItem[]) => ChatItem[]) => void;
  /** 항목이 아닌 이벤트(`ui.navigate`)의 처리. */
  hooks?: ChatEventHooks;
  /** 항목을 접은 뒤 이벤트마다 부른다(세션 id 잡기, 쿼리 무효화 같은 부수 효과). */
  onEvent?: (ev: ChatEvent) => void;
  /** 중단. reader 를 취소한다(lib/chatStream.ts). */
  signal?: AbortSignal;
}

export type ChatStreamResult = { status: "done" } | { status: "aborted" | "error"; error: unknown };

export async function consumeChatStream(res: Response, h: ChatStreamHandlers): Promise<ChatStreamResult> {
  try {
    // 스트림을 연 응답에는 늘 본문이 있다. 없으면 개발자의 잘못이라 영어 그대로 던진다
    if (!res.body) throw new Error("The chat response has no body.");
    for await (const ev of readChatStream(res.body, h.signal)) {
      if (ev.event === "ui") applyUiEvent(ev, h.hooks ?? {});
      else h.update((prev) => applyChatEvent(prev, ev));
      h.onEvent?.(ev);
    }
    h.update(closeStreaming);
    return { status: "done" };
  } catch (e) {
    h.update(closeStreaming);
    if (isAbortError(e)) return { status: "aborted", error: e };
    return { status: "error", error: e };
  }
}
