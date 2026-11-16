/**
 * 화면이 스스로 내는 채팅 안내 (P49 1절). 오류 자리에 문장 대신 사전(chat.notice)의 키를 두고, 그릴 때
 * components/chat/items.ts 의 describeChatError 가 문장을 만든다. 비서의 결정 본문(lib/assistant/decisions.ts)도 던지므로
 * 컴포넌트가 아니라 lib 에 둔다.
 */
import type { Messages } from "@/i18n";

/** 안내 하나. 던질 수도 있게 Error 를 잇고 message 는 개발자가 읽는 키다. */
export class ChatNotice extends Error {
  readonly code: keyof Messages["chat"]["notice"];

  constructor(code: keyof Messages["chat"]["notice"]) {
    super(code);
    this.name = "ChatNotice";
    this.code = code;
  }
}

// 아래 표식은 상태에 넣어 두고 던지지 않는다. 같은 값을 계속 쓰므로 한 번만 만든다

/** 보낸 첨부가 전달되지 않았다. `done.attachments_received` 가 보낸 수와 다를 때다(lib/attachments.ts attachmentsLost). */
export const ATTACHMENTS_NOT_DELIVERED = new ChatNotice("attachmentsNotDelivered");

/** 비서 패널의 중단. 비서의 턴은 요청과 떨어진 태스크라 끊겨도 이어지고, 패널은 그것에 기대 세션을 다시 읽는다. */
export const ASSISTANT_ABORTED = new ChatNotice("assistantAborted");

// 아래 넷은 연결과 떨어져 도는 에이전트의 턴을 따라가는 채팅 패널의 것이다 (P78 4절)

/** 되살린 대화나 스트림이 끊긴 패널에서 턴이 아직 돈다. 오류가 아니라 안내 줄이다. */
export const TURN_RUNNING = new ChatNotice("turnRunning");
/** 중지 요청이 5xx·네트워크 오류로 실패했다. 턴은 그대로 돈다. */
export const CANCEL_FAILED = new ChatNotice("cancelFailed");
/** 중지가 받아들여진 뒤에도 턴이 끝나지 않는다. 파드가 죽은 턴이 리스가 끝나기 전까지 이렇게 보인다. */
export const STOP_PENDING = new ChatNotice("stopPending");
/** 따라가던 대화를 더 읽을 수 없다. 공유 상태가 없는 파드가 바뀐 때다. */
export const SESSION_GONE = new ChatNotice("sessionGone");

// 아래 둘은 잠든 Agent 를 깨우는 동안 보낼 것을 쥐고 기다리는 줄이다 (P44 8절)

/** 메시지(다시 생성 포함)를 쥐고 파드가 뜨기를 기다린다. */
export const WAKING = new ChatNotice("waking");
/** 승인 결정을 쥐고 파드가 뜨기를 기다린다. */
export const WAKING_DECISION = new ChatNotice("wakingDecision");
