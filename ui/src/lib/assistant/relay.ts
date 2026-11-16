/**
 * 패널 밖에서 연 비서 스트림을 패널이 이어 그리게 하는 다리 (P48 6.5절). 캔버스의 제안 보기는 "제안대로 저장" 과 사람이
 * 직접 저장한 뒤의 거절을 스스로 보내는데, 그 결정이 다시 연 턴은 옆에 열린 패널의 대화다. 그래서 캔버스는 연 SSE 를
 * 패널에 넘기고, 패널이 없거나 다른 세션을 보고 있으면 직접 끝까지 읽는다.
 *
 * 스트림의 이벤트는 누가 읽든 듣는 쪽 모두에게 간다. 캔버스는 이것으로 제 호출의 결과(저장된 리비전, 새 Agent 의 id)와
 * 턴이 끝난 것을 안다. 읽는 쪽은 스트림을 그만 읽을 때마다 null 을 건넨다. 턴의 끝을 받지 못했을 수 있고(중단, 세션 바꾸기,
 * 끊김, 끝 이벤트 없이 닫힘) 서버의 턴은 이어지므로 듣는 쪽은 그때 다시 읽는다. 패널의 폴링이 턴의 끝을 보았을 때도 null 이다.
 * 상태는 모듈 안에만 있고 저장하지 않는다.
 */
import type { QueryClient } from "@tanstack/react-query";

import { openAssistantDecisions } from "@/api/resources";
import type { AssistantDecisionsBody } from "@/api/types";
import type { ChatEvent } from "@/lib/chatStream";
import { consumeChatStream } from "@/lib/chatTurn";

import { invalidateForEvent } from "./invalidate";

/** 넘겨받은 스트림을 그 세션의 것으로 읽으면 true 다. 다른 세션을 보고 있으면 false 를 돌려 넘기는 쪽이 읽게 한다. */
export type StreamReceiver = (sessionId: string, res: Response) => boolean;
/** ev 가 null 이면 읽는 쪽이 그 스트림을 그만 읽었거나 폴링이 턴의 끝을 보았다는 뜻이다. */
export type StreamListener = (sessionId: string, ev: ChatEvent | null) => void;

const receivers = new Set<StreamReceiver>();
const listeners = new Set<StreamListener>();

/** 패널이 마운트될 때 건다. 돌려준 함수로 푼다. */
export function registerStreamReceiver(receiver: StreamReceiver): () => void {
  receivers.add(receiver);
  return () => {
    receivers.delete(receiver);
  };
}

/** 연 스트림을 패널에 넘긴다. 받은 패널이 없으면 false 이고 부른 쪽이 읽는다. */
export function handOffStream(sessionId: string, res: Response): boolean {
  for (const receive of receivers) if (receive(sessionId, res)) return true;
  return false;
}

export function subscribeAssistantEvents(listener: StreamListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 스트림을 읽는 쪽(패널 또는 캔버스)이 이벤트마다, 그리고 그만 읽을 때 null 로 부른다. */
export function emitAssistantEvent(sessionId: string, ev: ChatEvent | null): void {
  for (const listen of listeners) listen(sessionId, ev);
}

/** 읽는 쪽이 이벤트마다 하는 일. 쓰기 결과면 그 리소스의 쿼리를 무효화하고 듣는 쪽에 건넨다. 패널과 캔버스가 같이 쓴다. */
export function relayEvent(queryClient: QueryClient, sessionId: string, ev: ChatEvent): void {
  void invalidateForEvent(queryClient, ev);
  emitAssistantEvent(sessionId, ev);
}

/**
 * 패널 밖에서 결정을 보낸다. 연 스트림은 그 세션을 보고 있는 패널에 넘기고, 받는 패널이 없으면 여기서 끝까지 읽으며
 * 패널처럼 쓰기 결과의 쿼리를 무효화하고 이벤트를 건넨다. 결정 요청이 거절되면(409 proposal_changed·turn_in_progress 등)
 * ApiError 를 그대로 던진다.
 */
export async function sendAssistantDecisions(queryClient: QueryClient, sessionId: string, body: AssistantDecisionsBody): Promise<void> {
  const res = await openAssistantDecisions(sessionId, body);
  if (handOffStream(sessionId, res)) return;
  void consumeChatStream(res, { update: () => {}, onEvent: (ev) => relayEvent(queryClient, sessionId, ev) }).then(() => emitAssistantEvent(sessionId, null));
}
