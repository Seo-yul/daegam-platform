/**
 * 저장된 전사(`GET /sessions/{id}`)를 채팅 항목으로 되살린다 (D31). 새로고침하거나 다른 세션을 골랐을 때
 * 패널이 이것으로 화면을 다시 그린다. SSE 로 들어오는 실시간 항목은 `components/chat/items.ts` 가 만든다.
 */
import type {
  AgentPending,
  AgentSessionUsage,
  AssistantPending,
  AssistantQuestion,
  AssistantResource,
  AssistantSessionDetail,
  AssistantTranscriptItem,
} from "@/api/types";
import { normalizePending, transcriptItems } from "@/api/runtimeShapes";
import { applyChatEvent, doneItem, errorItem, fallbackItem, sameRequests, toolLimitItem, type ChatItem } from "@/components/chat/items";
import { transcriptAttachments } from "@/lib/attachments";
import { finiteNumber, isRecord } from "@/lib/json";
import type { CitationItem, ToolCallEvent, ToolResultEvent } from "@/lib/chatStream";

function str(v: unknown): string {
  return typeof v === "string" ? v : v === null || v === undefined ? "" : String(v);
}

function num(v: unknown): number {
  return finiteNumber(v) ?? 0;
}

function usageOf(v: unknown): AgentSessionUsage {
  const u = isRecord(v) ? v : {};
  const input = num(u.input_tokens ?? u.input);
  const output = num(u.output_tokens ?? u.output);
  const cached = num(u.cached_input_tokens);
  return { input_tokens: input, output_tokens: output, total_tokens: num(u.total_tokens) || input + output, ...(cached ? { cached_input_tokens: cached } : {}) };
}

/** 전사의 turn_id (P36)와 trace_id (P37). 옛 런타임과 비서의 전사에는 없다. */
function idOf(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

function questionOf(args: Record<string, unknown> | undefined): AssistantQuestion {
  const options = Array.isArray(args?.options) ? args.options.filter((o): o is string => typeof o === "string") : null;
  return { text: str(args?.question ?? args?.text), options, field: typeof args?.field === "string" ? args.field : null };
}

/** 살아 있는 보류(`pending{kind, calls[]}`)를 승인·질문 카드가 읽는 interrupt data 로 만든다. id 가 실려 있어 결정 때 다시 읽지 않는다. */
export function pendingFromState(detail: AssistantSessionDetail): AssistantPending | null {
  const p = detail.pending;
  if (!p) return null;
  const first = p.calls[0];
  return normalizePending({
    session_id: detail.id,
    kind: p.kind,
    question: p.kind === "question" ? questionOf(first?.args) : null,
    action_requests: p.calls.map((c) => ({ id: c.id, name: c.name, args: c.args ?? {} })),
  });
}

/** 서버 전사 항목 하나 → ChatItem. 계약은 ChatItem 과 같은 모양이지만 다른 개발자가 만드는 것이라 평평한 꼴도 받는다. */
export function transcriptItem(raw: AssistantTranscriptItem): ChatItem | null {
  switch (raw.kind) {
    case "user": {
      // 함께 보낸 첨부(P36 9절)를 옮겨야 새로고침한 대화에도 칩이 남는다. 전사에는 이름과 크기뿐이다
      const attachments = transcriptAttachments(raw.attachments);
      return { kind: "user", text: str(raw.text ?? raw.content), ...(attachments.length ? { attachments } : {}) };
    }
    case "assistant":
      // 런타임 전사의 인사말 표시(greeting)를 그대로 들어 "인사말" 라벨이 새로고침 뒤에도 남는다 (D65)
      return { kind: "assistant", text: str(raw.text ?? raw.content), streaming: false, ...(raw.greeting === true ? { greeting: true } : {}) };
    case "tool_call": {
      const call = (raw.call as ToolCallEvent | undefined) ?? { id: str(raw.id), name: str(raw.name), args: (raw.args as Record<string, unknown> | undefined) ?? {} };
      const flatStatus = raw.status === "success" || raw.status === "error" ? raw.status : null;
      const result = (raw.result as ToolResultEvent | null | undefined) ?? (flatStatus ? { id: call.id, name: call.name, status: flatStatus, content: str(raw.content) } : null);
      // 인용 출처(P36 7절)를 옮겨야 새로고침한 대화에도 번호 칩이 남는다
      const citations = Array.isArray(raw.citations) ? (raw.citations as CitationItem[]) : null;
      return { kind: "tool_call", call, result, ...(citations ? { citations } : {}) };
    }
    case "interrupt": {
      // 옛 전사는 보류를 `pending` 안에 싣고, 평평한 꼴은 항목 자체가 보류다
      const source = isRecord(raw.pending) ? raw.pending : raw;
      // 전사에 남은 보류는 끝난 것으로 본다. 살아 있는 보류는 transcriptToItems 가 state·pending 으로 다시 연다
      return { kind: "interrupt", pending: normalizePending(source), resolved: raw.resolved !== false };
    }
    case "resource": {
      const resource = (raw.resource as AssistantResource | undefined) ?? { kind: str(raw.resource_kind), id: str(raw.id), name: str(raw.name), url: str(raw.url) };
      return { kind: "resource", resource };
    }
    case "done":
      return doneItem({ usage: usageOf(raw.usage), turn: num(raw.turn), tool_calls: num(raw.tool_calls), structured_response: raw.structured_response, turn_id: idOf(raw.turn_id), trace_id: idOf(raw.trace_id) });
    case "error":
      return errorItem({ code: str(raw.code), message: str(raw.message), turn_id: idOf(raw.turn_id), trace_id: idOf(raw.trace_id) });
    case "guardrail":
      // 전사 항목의 kind 는 항목 종류("guardrail")라 규칙 이름은 rule 만 읽는다(runtime/transcript.py 가 rule 로 옮겨 둔다)
      return { kind: "guardrail", action: str(raw.action) || "block", message: str(raw.message), position: str(raw.position) || undefined, rule: str(raw.rule) || undefined };
    case "fallback":
      // 예비 모델 알림(P36 11절)을 되살려야 새로고침한 대화에서도 어느 모델이 답했는지 남는다
      return fallbackItem({ from: str(raw.from), to: str(raw.to), agent: str(raw.agent) || undefined });
    case "tool_limit":
      // Tool 호출 한도 알림(T82)을 되살려야 새로고침한 대화에서도 왜 Tool 을 더 부르지 않았는지 남는다
      return toolLimitItem({ limit: num(raw.limit), agent: str(raw.agent) || undefined });
    default:
      return null;
  }
}

/**
 * 저장된 전사 항목 배열을 ChatItem 배열로. 비서와 에이전트(D35 F1)가 같은 어휘라 둘이 같이 쓴다.
 * 봉투가 다르므로(비서는 `transcript[]`, 에이전트는 `transcript{items[], limit}`) 푸는 것은 부르는 쪽이다.
 */
export function itemsFromTranscript(raw: unknown): ChatItem[] {
  const items: ChatItem[] = [];
  for (const r of transcriptItems(raw)) {
    const it = transcriptItem(r);
    if (it) items.push(it);
  }
  return items;
}

/**
 * 되살린 Agent 전사에 살아 있는 보류(`GET /sessions/{sid}/pending`)를 다시 세운다. 마지막 항목이 승인 카드면 그 자리를
 * 대신하고 아니면 끝에 붙는다. 스트림의 `interrupt` 와 같은 접기라 카드가 붙든 호출은 스트림으로 본 화면처럼 "승인 대기" 다.
 * Agent 채팅의 복원(ChatPanel)과 읽기 전용 세션 탭(pages/agents/SessionsTab.tsx)이 같이 쓴다.
 */
export function reopenPending(items: ChatItem[], pending: AgentPending): ChatItem[] {
  const base = items[items.length - 1]?.kind === "interrupt" ? items.slice(0, -1) : items;
  return applyChatEvent(base, { event: "interrupt", data: pending });
}

/** `GET /sessions/{id}` → 화면 항목. `state=pending` 이면 마지막 보류 카드를 연 채로 두고 id 를 실어 둔다. */
export function transcriptToItems(detail: AssistantSessionDetail): ChatItem[] {
  const items = itemsFromTranscript(detail.transcript);
  const live = detail.state === "pending" ? pendingFromState(detail) : null;
  if (live) {
    const idx = items.findLastIndex((it) => it.kind === "interrupt");
    if (idx >= 0) {
      const prev = items[idx] as Extract<ChatItem, { kind: "interrupt" }>;
      // 개수뿐 아니라 도구 이름까지 같아야 옛 카드에 새 id 를 입힌다. 하나라도 다르면 살아 있는 보류를 그대로 쓴다
      const sameShape = sameRequests(prev.pending, live);
      items[idx] = {
        ...prev,
        resolved: false,
        pending: sameShape
          ? { ...prev.pending, kind: live.kind, question: prev.pending.question ?? live.question, action_requests: prev.pending.action_requests.map((r, i) => ({ ...r, id: live.action_requests[i].id })) }
          : live,
      };
    } else {
      items.push({ kind: "interrupt", pending: live, resolved: false });
    }
  }
  return items;
}
