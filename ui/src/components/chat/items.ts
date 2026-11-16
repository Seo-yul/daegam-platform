/**
 * 채팅 전사의 항목 모델과 SSE 이벤트를 항목으로 접는 규칙 (D26·D31). Agent 채팅(ChatPanel)과 플랫폼 비서(AssistantPanel)가
 * 같은 어휘를 쓴다. 이 파일은 JSX 가 없는 순수 함수만 두어 msw 목(테스트·스모크 목 서버)도 같은 접기 규칙으로 전사를 만든다.
 *
 * 비서가 더한 것(phase-14 계약 절): `resource` 이벤트는 링크 카드 항목이고, `interrupt` 의 data 에 `kind: confirm|question` 과
 * `question` 이 실려 올 수 있으며, `ui{type: navigate}` 는 항목이 아니라 콜백(hooks.onNavigate)이다.
 */
import { ApiError } from "@/api/client";
import { normalizePending } from "@/api/runtimeShapes";
import type { AgentPending, AgentSessionUsage, AssistantPending, AssistantResource } from "@/api/types";
import type { Messages } from "@/i18n";
import { AttachmentRefused, refusalText, type AttachmentFacts } from "@/lib/attachments";
import type { ChatEvent, CitationItem, CitationsEvent, DoneEvent, ErrorEvent, GreetingEvent, GuardrailEvent, ModelFallbackEvent, ToolCallEvent, ToolLimitEvent, ToolResultEvent } from "@/lib/chatStream";
import { ChatNotice } from "@/lib/chatNotice";
import { describeCode } from "@/lib/errorText";
import { isAbortError } from "@/lib/utils";

/** 입력 가드레일이 스트림을 열기 전에 막을 때의 코드다(422, 런타임 routes). detail 이 운영자가 정한 안내문이다. */
export const GUARDRAIL_BLOCKED = "guardrail_blocked";

/** 입구의 가드레일이 막은 거절인가. */
export function guardrailBlocked(e: unknown): e is ApiError {
  return e instanceof ApiError && e.code === GUARDRAIL_BLOCKED;
}

export type ChatItem =
  // attachments: 이 말과 함께 보낸 첨부다(P36 9절). 보낸 말풍선 아래 칩이 되고 전사도 같은 칸을 든다
  | { kind: "user"; text: string; attachments?: AttachmentFacts[] }
  // greeting: 새 세션의 인사말 말풍선이다. 런타임의 `greeting` 이벤트로 오고 전사도 같은 표시를 든다(D65). "인사말" 라벨을 단다
  | { kind: "assistant"; text: string; agent?: string; streaming: boolean; greeting?: boolean }
  // waiting: 승인 카드가 이 호출을 붙들고 있다. 결과가 오거나 재개가 같은 호출을 다시 알리면 풀린다
  // citations: 이 호출의 결과가 낸 인용 출처다(P36 7절). 답의 [n] 을 번호 칩으로 바꾸는 표가 이것들로 만들어진다
  | { kind: "tool_call"; call: ToolCallEvent; result: ToolResultEvent | null; waiting?: boolean; citations?: CitationItem[] }
  | { kind: "interrupt"; pending: AssistantPending; resolved: boolean }
  | { kind: "resource"; resource: AssistantResource }
  // structured_response: 응답 형식이 json_schema 일 때의 구조화 결과. 런타임이 값이 있을 때만 싣고 전사도 같은 칸을 든다(D65).
  // turn_id: 그 턴 시도의 id 다(P36). 피드백·다시 생성이 이것으로 턴을 가리키고, 옛 런타임과 비서에는 없다.
  // trace_id: 그 실행의 id 다(P37). 턴 마감 줄의 트레이스 단추가 turn_id 가 없을 때 이것으로 실행 하나를 연다
  | { kind: "done"; usage: AgentSessionUsage; turn: number; tool_calls: number; structured_response?: unknown; turn_id?: string; trace_id?: string }
  | { kind: "error"; code: string; message: string; turn_id?: string; trace_id?: string }
  // message: 운영자가 정한 안내문이다. 스트림이 싣지 않았으면 없고, 그릴 때 사전의 기본 문장을 쓴다
  | { kind: "guardrail"; action: string; message?: string; position?: string; rule?: string }
  // 예비 모델이 대신 답했다는 알림이다(P36 11절). `model_fallback` 이벤트를 접은 것이고 전사도 같은 모양이다
  | { kind: "fallback"; from: string; to: string; agent?: string }
  // 한 실행의 Tool 호출 한도에 닿았다는 알림이다(T82). `tool_limit` 이벤트를 접은 것이고 전사도 같은 모양이다
  | { kind: "tool_limit"; limit: number; agent?: string }
  // 홈 챗에서 청지기의 `ui.navigate` 를 대신한다. 거기서 이동하면 청지기가 자기가 사는 화면을 닫는다(D35)
  | { kind: "navigate"; to: string };

/**
 * 항목이 아닌 이벤트를 받는 콜백. `ui.navigate` 는 패널의 "따라가기" 토글이 켜졌을 때만 화면을 옮기므로
 * 콜백에 맡긴다. 홈 챗은 같은 콜백에서 옮기는 대신 `navigate` 항목을 전사에 남긴다(D35).
 */
export interface ChatEventHooks {
  onNavigate?: (to: string) => void;
}

/** 항목이 아닌 이벤트(`ui{type: navigate, to}`)를 콜백으로 넘긴다. 처리했으면 true. 상태 갱신 함수 밖에서 부른다. */
export function applyUiEvent(ev: ChatEvent, hooks: ChatEventHooks): boolean {
  if (ev.event !== "ui") return false;
  const d = ev.data as { type?: string; to?: string };
  if (d.type === "navigate" && typeof d.to === "string") {
    hooks.onNavigate?.(d.to);
    return true;
  }
  return false;
}

/** 스트림 이벤트 하나를 대화 목록에 접는다. 순수 함수라 테스트하기 쉽다. */
export function applyChatEvent(items: ChatItem[], ev: ChatEvent, hooks: ChatEventHooks = {}): ChatItem[] {
  switch (ev.event) {
    case "greeting":
      return insertGreeting(items, (ev.data as GreetingEvent).text);
    case "token": {
      const d = ev.data as { text: string; agent?: string };
      const last = items[items.length - 1];
      if (last && last.kind === "assistant" && last.streaming && last.agent === d.agent) {
        return [...items.slice(0, -1), { ...last, text: last.text + d.text }];
      }
      // 라벨이 다른 말풍선은 앞 말풍선을 닫고 연다(런타임 전사와 같은 규칙)
      return [...closeStreaming(items), { kind: "assistant", text: d.text, agent: d.agent, streaming: true }];
    }
    case "tool_call": {
      const d = ev.data as ToolCallEvent;
      // 승인 뒤 재개하면 런타임이 같은 id 의 호출을 다시 알린다(턴마다 새로 센다, runtime/stream.py). 결과를 아직 못 받은
      // 같은 호출이 있으면 그 줄을 이어 쓴다. 새 줄을 달면 결과는 앞 줄에 붙고 뒤 줄은 영영 "실행 중" 에 남는다
      const idx = d.id ? items.findIndex((it) => it.kind === "tool_call" && it.call.id === d.id && !it.result) : -1;
      if (idx >= 0) return closeStreaming(items).map((it, i) => (i === idx && it.kind === "tool_call" ? { ...it, call: { ...it.call, ...d }, waiting: false } : it));
      return [...closeStreaming(items), { kind: "tool_call", call: d, result: null }];
    }
    case "tool_result": {
      const d = ev.data as ToolResultEvent;
      const idx = items.findIndex((it) => it.kind === "tool_call" && it.call.id === d.id && !it.result);
      if (idx < 0) return [...items, { kind: "tool_call", call: { id: d.id, name: d.name, args: {}, agent: d.agent }, result: d }];
      return items.map((it, i) => (i === idx && it.kind === "tool_call" ? { ...it, result: d, waiting: false } : it));
    }
    case "citations": {
      // 결과 바로 뒤에 오므로 카드는 이미 결과를 받았다. 카드가 없으면 버리는 것은 runtime/transcript.py 와 같다. 런타임은 토큰이
      // 아닌 이벤트마다 흐르던 말풍선을 닫지만 이 갈래는 말풍선을 건드리지 않는다
      const d = ev.data as CitationsEvent;
      const idx = items.findLastIndex((it) => it.kind === "tool_call" && it.call.id === d.call_id);
      if (idx < 0) return items;
      return items.map((it, i) => (i === idx && it.kind === "tool_call" ? { ...it, citations: d.items } : it));
    }
    case "interrupt": {
      const d = normalizePending(ev.data);
      // 카드가 붙든 호출은 도는 중이 아니라 사람의 결정을 기다린다. 이번 턴(마지막 사용자 말 뒤)의 결과 없는 호출 중
      // 카드에 이름이 있는 것만 표시한다
      const names = new Set(d.action_requests.map((r) => r.name));
      const start = Math.max(0, lastUserIndex(items));
      const marked = closeStreaming(items).map((it, i) => (i >= start && it.kind === "tool_call" && !it.result && names.has(it.call.name) ? { ...it, waiting: true } : it));
      return [...marked, { kind: "interrupt", pending: d, resolved: false }];
    }
    case "resource": {
      const d = ev.data as AssistantResource;
      return [...closeStreaming(items), { kind: "resource", resource: d }];
    }
    case "ui":
      applyUiEvent(ev, hooks);
      return items;
    case "done":
      return [...closeStreaming(items), doneItem(ev.data as DoneEvent)];
    case "error":
      return [...closeStreaming(items), errorItem(ev.data as ErrorEvent)];
    case "guardrail": {
      // 런타임은 규칙 이름을 rule 로 싣는다(runtime/guards.py)
      const d = ev.data as GuardrailEvent;
      return [...closeStreaming(items), { kind: "guardrail", action: d.action ?? "block", message: d.message, position: d.position, rule: d.rule }];
    }
    case "model_fallback":
      return addFallback(closeStreaming(items), fallbackItem(ev.data as ModelFallbackEvent));
    case "tool_limit":
      return [...closeStreaming(items), toolLimitItem(ev.data as ToolLimitEvent)];
    default:
      return items;
  }
}

/**
 * 새 세션의 인사말을 이번 턴 사용자 말 앞에 세운다 (D65). 사용자 말풍선은 보낼 때 먼저 붙고 이벤트는 그 뒤에 오므로 끝에
 * 덧붙이지 않고 끼운다. 이미 인사말이 있으면 그대로다. 런타임의 transcript.py 가 전사를 같은 규칙으로 접는다.
 */
export function insertGreeting(items: ChatItem[], text: string): ChatItem[] {
  if (items.some((it) => it.kind === "assistant" && it.greeting)) return items;
  const item: ChatItem = { kind: "assistant", text, streaming: false, greeting: true };
  const at = lastUserIndex(items);
  return at < 0 ? [...items, item] : [...items.slice(0, at), item, ...items.slice(at)];
}

/**
 * 턴 마감 항목. 구조화 결과와 turn_id 는 값이 있을 때만 칸을 두어 스트림으로 접은 항목과 전사에서 되살린 항목
 * (lib/assistant/transcript.ts)이 같은 모양이다. 런타임의 전사 접기(agent-runtime/runtime/transcript.py)도 같은 규칙이다.
 */
export function doneItem(d: Pick<DoneEvent, "usage" | "turn" | "tool_calls" | "structured_response" | "turn_id" | "trace_id">): ChatItem {
  const item: ChatItem = { kind: "done", usage: d.usage, turn: d.turn, tool_calls: d.tool_calls, ...withIds(d) };
  return d.structured_response == null ? item : { ...item, structured_response: d.structured_response };
}

/** 오류로 끝난 턴의 항목. done 처럼 그 시도의 turn_id 와 실행의 trace_id 를 든다. */
export function errorItem(d: Pick<ErrorEvent, "code" | "message" | "turn_id" | "trace_id">): ChatItem {
  return { kind: "error", code: d.code, message: d.message, ...withIds(d) };
}

/** 값이 있는 id 만 칸으로 둔다. 스트림으로 접은 항목과 전사에서 되살린 항목이 같은 모양이어야 한다. */
function withIds(d: { turn_id?: string; trace_id?: string }): { turn_id?: string; trace_id?: string } {
  return { ...(d.turn_id ? { turn_id: d.turn_id } : {}), ...(d.trace_id ? { trace_id: d.trace_id } : {}) };
}

type FallbackItem = Extract<ChatItem, { kind: "fallback" }>;

/** 예비 모델 알림 항목. 서브에이전트의 전환일 때만 agent 칸을 두어 스트림으로 접은 항목과 되살린 항목이 같은 모양이다. */
export function fallbackItem(d: ModelFallbackEvent): FallbackItem {
  return { kind: "fallback", from: d.from, to: d.to, ...(d.agent ? { agent: d.agent } : {}) };
}

/**
 * 예비 모델 알림을 붙인다. 기본 모델이 계속 실패하면 모델 호출마다 전환이 와서 도구를 부른 턴에 알림이 쌓이므로, 같은 턴에
 * 같은 전환이 이미 있으면 그대로 둔다. 턴은 마지막 사용자 말이나 승인 카드 뒤에서 시작한다. 런타임의 전사 접기
 * (runtime/transcript.py 의 _add_fallback)와 같은 규칙이다.
 */
function addFallback(items: ChatItem[], item: FallbackItem): ChatItem[] {
  const start = items.findLastIndex((it) => it.kind === "user" || it.kind === "interrupt") + 1;
  const seen = items.slice(start).some((it) => it.kind === "fallback" && it.from === item.from && it.to === item.to && it.agent === item.agent);
  return seen ? items : [...items, item];
}

/** 예비 모델 알림의 문장. 서브에이전트의 전환이면 누구의 모델인지 앞에 밝힌다. */
export function fallbackNotice(t: Messages, item: FallbackItem): string {
  return t.chat.item.fallback(item.to, item.agent);
}

type ToolLimitItem = Extract<ChatItem, { kind: "tool_limit" }>;

/** Tool 호출 한도 알림 항목. 서브에이전트의 한도일 때만 agent 칸을 두어 스트림으로 접은 항목과 되살린 항목이 같은 모양이다. */
export function toolLimitItem(d: ToolLimitEvent): ToolLimitItem {
  return { kind: "tool_limit", limit: d.limit, ...(d.agent ? { agent: d.agent } : {}) };
}

/** Tool 호출 한도 알림의 문장이다. */
export function toolLimitNotice(t: Messages, item: ToolLimitItem): string {
  return t.chat.item.toolLimit(item.limit, item.agent);
}

/**
 * 턴이 열린 동안 "생각하는 중" 을 보일 때인가 (P36 11절). 이번 턴(마지막 사용자 말 뒤)에 흐르는 말풍선도 도는 도구 카드도
 * 없고 턴을 마감한 항목도 아직 없으면 모델이 다음 말을 고르는 중이다. 이벤트 없이 스트림 상태에서 정하고, 턴이 열렸는지는
 * 부르는 쪽이 본다. 승인 뒤 재개가 다시 알린 호출은 앞의 카드를 이어 쓰므로 승인 카드 뒤가 아니라 사용자 말 뒤를 본다.
 * 끝에 결정을 기다리는 승인 카드가 섰으면 모델이 아니라 사람의 차례다. 카드가 선 뒤 스트림이 닫히기까지 잠깐 다시 서지 않는다.
 */
export function showsThinking(items: ChatItem[]): boolean {
  const last = items[items.length - 1];
  if (last?.kind === "done" || last?.kind === "error" || (last?.kind === "interrupt" && !last.resolved)) return false;
  return !items.slice(lastUserIndex(items) + 1).some((it) => (it.kind === "assistant" && it.streaming) || (it.kind === "tool_call" && !it.result && !it.waiting));
}

/** 흘러 들어오던 assistant 말풍선을 닫는다. 스트림이 끝나거나 다른 종류의 항목이 올 때 부른다. */
export function closeStreaming(items: ChatItem[]): ChatItem[] {
  const last = items[items.length - 1];
  if (last && last.kind === "assistant" && last.streaming) return [...items.slice(0, -1), { ...last, streaming: false }];
  return items;
}

/** 마지막 사용자 말의 자리다. 없으면 -1 이다. */
function lastUserIndex(items: ChatItem[]): number {
  return items.findLastIndex((it) => it.kind === "user");
}

/**
 * 마지막 턴이 마감 항목 없이 끝났는가 (P78 4절). 파드가 죽어 리스가 끝난 턴은 done·error·승인 카드가 없으므로 화면이 그 아래에
 * turn_aborted 를 스스로 붙인다. 턴은 마지막 사용자 말에서 시작한다. 승인 카드 뒤의 재개는 시작한 자리를 가를 수 없어 보지 않는다.
 * 입력 가드레일의 차단은 done 없이 끝나지만 마감된 턴이다.
 */
export function turnUnended(items: ChatItem[]): boolean {
  const start = lastUserIndex(items);
  if (start < 0 || items.findLastIndex((it) => it.kind === "interrupt") > start) return false;
  return !items
    .slice(start + 1)
    .some((it) => it.kind === "done" || it.kind === "error" || (it.kind === "guardrail" && it.action === "block"));
}

/** 화면이 스스로 붙이는 마감 줄이다. 파드가 죽어 끝나지 못한 턴 아래에 서고 저장되지 않는다 (P78 4절). */
export const TURN_ABORTED_ITEM: ChatItem = { kind: "error", code: "turn_aborted", message: "" };

/** 다시 생성·메시지 수정이 가리키는 마지막 턴 (P36 5절). 마지막 사용자 말의 자리와 글, 그 턴이 도구를 불렀는지다. */
export interface LastTurn {
  userAt: number;
  text: string;
  calledTools: boolean;
}

/**
 * 끝난 마지막 턴. 마지막 사용자 말 뒤가 done·error 로 마감됐을 때만 있다. 승인을 기다리는 턴, 아직 아무것도 받지 못한 말,
 * 입구에서 거절돼 가드레일 항목만 남은 말은 런타임의 마지막 턴이 아니므로 null 이다.
 */
export function lastTurn(items: ChatItem[]): LastTurn | null {
  const userAt = lastUserIndex(items);
  const user = items[userAt];
  const end = items[items.length - 1];
  if (user?.kind !== "user" || (end?.kind !== "done" && end?.kind !== "error")) return null;
  return { userAt, text: user.text, calledTools: items.slice(userAt + 1).some((it) => it.kind === "tool_call") };
}

/**
 * 다시 생성을 보내며 먼저 보이는 화면. 마지막 사용자 말 뒤를 걷어 내고, 메시지를 수정해 보내면 그 말을 새 글로 바꾼다. 그 말의
 * 첨부는 런타임이 새 갈래로 옮기므로 칩도 그대로 남는다.
 */
export function rewindLastTurn(items: ChatItem[], turn: LastTurn, text?: string): ChatItem[] {
  const user = items[turn.userAt] as Extract<ChatItem, { kind: "user" }>;
  return [...items.slice(0, turn.userAt), { ...user, text: text ?? turn.text }];
}

/** index 자리의 승인·질문 카드 하나를 닫는다. 결정을 보낸 카드다. resolved 가 거짓이면 다시 연다(보내지 못한 결정, P44 8절). */
export function resolveInterruptAt(items: ChatItem[], index: number, resolved = true): ChatItem[] {
  return items.map((it, i) => (i === index && it.kind === "interrupt" ? { ...it, resolved } : it));
}

/** 아직 결정하지 않은 승인·질문 카드를 전부 닫는다. 결정 없이 새 메시지를 보내면 서버가 pending 을 superseded 로 닫는 것과 짝이다. */
export function resolveInterrupts(items: ChatItem[]): ChatItem[] {
  return items.map((it) => (it.kind === "interrupt" && !it.resolved ? { ...it, resolved: true } : it));
}

/** 두 보류의 요청이 개수와 Tool 이름까지 같은가. 옛 카드를 살아 있는 보류로 고치거나 덧입힐 때 같은 카드인지 본다. */
export function sameRequests(a: AgentPending, b: AgentPending): boolean {
  return a.action_requests.length === b.action_requests.length && a.action_requests.every((r, i) => r.name === b.action_requests[i].name);
}

/**
 * 마지막 열린 승인 카드에 플랫폼이 알린 기억할 수 있는 선택을 입힌다(P43 5절). 스트림의 `interrupt` 에는 없고 `GET /pending` 이
 * 세션 소유자에게만 싣는다. 요청의 이름과 이유까지 같은 카드에만 입히고, 다르면(그사이 다른 보류가 섰다) 그대로 둔다.
 */
export function withRememberOptions(items: ChatItem[], pending: AgentPending): ChatItem[] {
  const index = items.findLastIndex((it) => it.kind === "interrupt" && !it.resolved);
  const card = items[index];
  if (card?.kind !== "interrupt" || !pending.remember_options) return items;
  const same = sameRequests(card.pending, pending) && card.pending.action_requests.every((r, i) => r.reason === pending.action_requests[i].reason);
  if (!same) return items;
  const remember = { remember_options: pending.remember_options, remember_days: pending.remember_days };
  return items.map((it, i) => (i === index ? { ...card, pending: { ...card.pending, ...remember } } : it));
}

/**
 * 오류 응답을 한 줄로. 429 는 Retry-After 를 덧붙인다.
 *
 * 코드는 `errorText` 표를 지난다. 정상 경로가 200여 개의 한국어 처방을 쓰면서 채팅만 `auth: invalid
 * x-api-key` 를 그대로 보이던 자리다 (D43 첫째).
 *
 * 중단은 화면의 reader 를 끊는다. 에이전트 채팅(상세, 테스트 채팅, 홈 챗)은 연결이 끊기면 런타임이 진행 중이던 모델 호출에서
 * 턴을 멈추고 `turn_cancelled` 로 마감하므로(D65) 그 코드의 문장이다. 스트림이 그 코드를 실어 끝나는 길과 같은 문장이라 어느
 * 길로 끝나든 같은 글을 본다. 비서 패널은 턴이 요청과 떨어진 태스크라 이어지므로 ASSISTANT_ABORTED 를 따로 쓴다.
 *
 * 입력 가드레일의 차단(`GUARDRAIL_BLOCKED`)은 운영자가 정한 안내문이 detail 이라 코드를 붙이지 않고 그대로 보인다. 화면이 낸
 * 안내(ChatNotice)와 첨부 거절(AttachmentRefused)은 사전의 문장이다.
 */
export function describeChatError(t: Messages, e: unknown): string {
  if (e instanceof ApiError) {
    if (guardrailBlocked(e) && e.detail) return e.detail;
    const text = describeCode(t, e.code, e.detail).text;
    const retry = e.extra<string>("retry_after");
    return retry ? t.chat.retryAfter(text, retry) : text;
  }
  if (e instanceof ChatNotice) return t.chat.notice[e.code];
  if (e instanceof AttachmentRefused) return refusalText(t, e.refusal);
  if (isAbortError(e)) return t.errors.codes.turn_cancelled;
  return e instanceof Error ? e.message : String(e);
}
