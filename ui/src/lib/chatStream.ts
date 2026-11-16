/**
 * Agent 채팅 SSE 를 fetch + ReadableStream 으로 읽는다 (D26, agent-runtime/README.md SSE 이벤트 표).
 * 이벤트는 `event: <이름>` 한 줄과 `data: <JSON>` 한 줄이고 빈 줄로 끝난다. EventSource 는 POST 를 못 하므로
 * 직접 파싱한다. 플랫폼 프록시(`POST /api/agents/{id}/chat`, `/decisions`)도 같은 형식이다.
 */
import type { AgentPending, AgentSessionUsage } from "@/api/types";

/** 새 세션의 첫 이벤트. 입구 노드의 인사말이 비어 있지 않을 때만 오고 모델 메시지에는 들어가지 않는다 (D65). */
export interface GreetingEvent {
  session_id: string;
  text: string;
}
export interface TokenEvent {
  text: string;
  agent?: string;
}
export interface ToolCallEvent {
  id: string;
  name: string;
  args: Record<string, unknown>;
  agent?: string;
}
export interface ToolResultEvent {
  id: string;
  name: string;
  status: "success" | "error";
  content: string;
  agent?: string;
  /** 결과가 외부 내용이면 그 종류다(P39b 14절 3부, P43 2절). 웹 Tool 의 결과는 `web`, 그 밖의 외부 내용은 `outside` 이고
   * 런타임이 성공한 결과에만 싣는다. */
  untrusted?: "web" | "outside";
}
/** 도구 호출 카드의 상태. 결과가 오면 그 상태이고, 아직이면 승인 카드가 붙들었는지에 따라 기다리거나 도는 중이다. */
export type ToolCallStatus = ToolResultEvent["status"] | "waiting" | "running";
/**
 * 인용 출처 하나 (P36 7절). 번호 `n` 은 세션 안에서 이어지고 답의 `[n]` 이 이것을 가리킨다. 본문은 싣지 않으므로 발췌는
 * 같은 호출의 `tool_result` 본문에서 `cite` 로 찾는다(lib/citations.ts). 그 본문은 도구 가드레일의 가림을 지난 것이다.
 */
export interface CitationItem {
  n: number;
  kind: string;
  title: string | null;
  url: string | null;
  kb?: { id: string; name: string };
  doc_id?: string;
  chunk?: number;
  score?: number;
}
/** 막히지 않은 검색 결과 바로 뒤에 오는 출처 묶음. 항목이 아니라 `call_id` 가 같은 도구 호출 카드에 붙는다. */
export interface CitationsEvent {
  call_id: string;
  agent?: string;
  items: CitationItem[];
}
/** 재시도를 다 쓴 모델 호출이 예비 모델로 넘어갔다 (P36 11절). 모델 호출마다 오므로 한 턴에 여러 번 올 수 있다. */
export interface ModelFallbackEvent {
  from: string;
  to: string;
  agent?: string;
}
export interface DoneEvent {
  session_id: string;
  turn: number;
  /** 턴 시도의 id (P36). 결정 재개는 같은 id 를 이어 쓴다. 옛 런타임과 비서에는 없다 */
  turn_id?: string;
  /** 이 실행의 trace id (P37 1절). 재개는 같은 턴의 다음 실행이라 다른 값이다. 옛 런타임과 비서에는 없다 */
  trace_id?: string;
  usage: AgentSessionUsage;
  tool_calls: number;
  text: string;
  /** 응답 형식이 `json_schema` 일 때 출력 필터를 지난 구조화 결과. 런타임이 값이 있을 때만 싣는다(D65) */
  structured_response?: unknown;
  /** 런타임이 받아 저장한 첨부 수 (P36 9절). 첨부를 모르는 옛 런타임에는 없어 보낸 수와 견줘 버려졌는지 안다 */
  attachments_received?: number;
}
export interface ErrorEvent {
  session_id?: string;
  /** 오류로 끝난 턴 시도의 id (P36). 옛 런타임과 비서에는 없다 */
  turn_id?: string;
  /** 오류로 끝난 실행의 trace id (P37 1절) */
  trace_id?: string;
  code: string;
  message: string;
}
/** 가드레일 위반(F2c). 런타임 계약이 확정되기 전이라 필드는 넓게 받는다. */
export interface GuardrailEvent {
  position?: string;
  rule?: string;
  action?: "block" | "flag" | string;
  message?: string;
  [key: string]: unknown;
}

/** 한 실행의 Tool 호출 한도에 닿았다 (T82). 런타임이 처음 넘을 때 한 번 낸다. agent 는 서브에이전트의 한도일 때 그 이름이다. */
export interface ToolLimitEvent {
  limit: number;
  agent?: string;
}

export type ChatEvent =
  | { event: "greeting"; data: GreetingEvent }
  | { event: "token"; data: TokenEvent }
  | { event: "tool_call"; data: ToolCallEvent }
  | { event: "tool_result"; data: ToolResultEvent }
  | { event: "citations"; data: CitationsEvent }
  | { event: "interrupt"; data: AgentPending }
  | { event: "done"; data: DoneEvent }
  | { event: "error"; data: ErrorEvent }
  | { event: "guardrail"; data: GuardrailEvent }
  | { event: "model_fallback"; data: ModelFallbackEvent }
  | { event: "tool_limit"; data: ToolLimitEvent }
  | { event: string; data: unknown };

/** 텍스트 버퍼에서 완성된 이벤트를 꺼낸다. 남은 조각은 rest 로 돌려준다. */
export function parseSseChunk(buffer: string): { events: ChatEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: ChatEvent[] = [];
  for (const block of blocks) {
    const ev = parseSseBlock(block);
    if (ev) events.push(ev);
  }
  return { events, rest };
}

function parseSseBlock(block: string): ChatEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const idx = line.indexOf(":");
    const field = idx < 0 ? line : line.slice(0, idx);
    let value = idx < 0 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  let data: unknown = raw;
  try {
    data = JSON.parse(raw);
  } catch {
    data = { text: raw };
  }
  return { event, data } as ChatEvent;
}

/**
 * Response body 를 이벤트 단위로 내보낸다. 스트림이 끝나면 남은 조각도 한 번 더 파싱한다.
 * 중단은 signal 로 받아 reader 를 취소한다 (fetch 에 signal 을 넘기지 않는다. jsdom 의 AbortSignal 은 Node fetch 가
 * 거부하고, 브라우저에서는 reader.cancel 이 연결을 닫는다).
 */
export async function* readChatStream(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const onAbort = () => void reader.cancel().catch(() => undefined);
  signal?.addEventListener("abort", onAbort);
  try {
    for (;;) {
      if (signal?.aborted) {
        await reader.cancel().catch(() => undefined);
        throw new DOMException("aborted", "AbortError");
      }
      const { value, done } = await reader.read();
      if (done) {
        if (signal?.aborted) throw new DOMException("aborted", "AbortError");
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSseChunk(buffer);
      buffer = rest;
      for (const ev of events) yield ev;
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const { events } = parseSseChunk(buffer + "\n\n");
      for (const ev of events) yield ev;
    }
  } finally {
    signal?.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}
