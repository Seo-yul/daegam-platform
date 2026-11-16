/**
 * 서버가 "값 없음" 자리에 적어 보내는 한국어 세 개 (P49 6절). 서버는 바꾸지 않는다. 옛 DCR client_id 와 연결(OAuthGrant),
 * 비서 세션 문서와 저장된 대화 항목이 이 값을 이미 갖고 있고, 서버도 이 값으로 제목이 없는 세션을 알아본다. 화면은 이 값을
 * "값 없음" 으로 읽고 사전의 말을 그린다. 이 파일은 한글이 늘 남는 파일이다(test/i18n-pending/permanent.ts).
 */
import type { Messages } from "@/i18n";

/** platform/daegam/services/oauth/clients.py `UNNAMED_CLIENT`. 이름을 밝히지 않은 MCP 클라이언트에 서버가 붙인다. */
const UNNAMED_CLIENT = "이름 없는 클라이언트";
/** platform/daegam/store/assistant_sessions.py `DEFAULT_TITLE`. 아직 제목이 없는 비서 세션의 제목이다. */
const UNTITLED_ASSISTANT_SESSION = "새 대화";
/**
 * agent-runtime/runtime/transcript.py `GUARDRAIL_FALLBACK_MESSAGE`. 문구 없는 가드레일 위반을 저장할 때 런타임이 채우므로,
 * 다시 연 대화의 항목이 이 값을 갖고 있다.
 */
const GUARDRAIL_FALLBACK_MESSAGE = "가드레일에 걸렸습니다.";

/** MCP 클라이언트의 보이는 이름. 연결 허용 화면과 연결 목록이 쓴다. */
export function clientName(t: Messages, name: string): string {
  return name === UNNAMED_CLIENT ? t.auth.unnamedClient : name;
}

/** 비서 세션의 보이는 제목. 제목이 없으면 빈 문자열이고, 호출부가 세션 id 로 대신한다. */
export function assistantSessionTitle(t: Messages, title: string | null | undefined): string {
  return title === UNTITLED_ASSISTANT_SESSION ? t.shared.chatPanel.newChat : (title ?? "");
}

/** 가드레일 항목의 보이는 안내문. 런타임이 채운 기본 문장은 사전의 기본 문장으로 바꾼다. 안내문이 없으면 빈 문자열이고, 호출부가 사전의 기본 문장으로 대신한다. */
export function guardrailMessage(t: Messages, message: string | null | undefined): string {
  return message === GUARDRAIL_FALLBACK_MESSAGE ? t.chat.item.guardrailDefault : (message ?? "");
}
