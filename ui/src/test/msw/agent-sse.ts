/**
 * Agent 채팅 SSE 의 대본과 그 턴이 쌓는 런타임 세션 색인 (D26, D65, P36). 채팅·결정·다시 생성 핸들러(agent-chat.ts)가 여기
 * 대본으로 스트림을 짓는다. 세션 색인은 재배포 경고(agents.ts)와 세션 파일·피드백 목도 읽는다.
 */
import { DETACHED_TURN_HEADER, SESSION_ID_HEADER, TURN_ID_HEADER } from "@/api/agentChat";
import type { AgentDecision, AgentPending, AgentSession, AgentSessionDetail, AgentSessionMessage, AssistantTranscriptItem } from "@/api/types";
import type { DoneEvent, ErrorEvent } from "@/lib/chatStream";
import { titleFromMessage } from "@/lib/sessionTitle";

import { ATTACHMENTS_FIXTURE, CITATIONS_FIXTURE, FALLBACK_FIXTURE, TURN_FIXTURE } from "../runtimeFixtures";
import { NOW, apiError, docStore } from "./core";
import { heldSse, tokenUsage, type SseEvent } from "./sse";

// -- 런타임 세션 색인 -------------------------------------------------------------------------

export const agentPendingFixture: AgentPending = {
  session_id: "s-2",
  action_requests: [{ name: "Divide", args: { a: 8, b: 2 }, description: "8을 2로 나눕니다." }],
  review_configs: [{ action_name: "Divide", allowed_decisions: ["approve", "edit", "reject", "respond"] }],
};

function sessionFixtures(): AgentSession[] {
  return [
    // 색인 행의 usage 에는 total_tokens 가 없다. 합은 화면이 더한다 (lib/usage.tokensOf). 제목은 첫 메시지 첫 줄이고
    // s-3 은 제목이 없는 P36 앞 세션이다
    { session_id: "s-1", user_sub: "header-admin", title: "환불 기한 문의", created_at: NOW, last_turn_at: NOW, turns: 3, usage: { input_tokens: 1200, output_tokens: 300 }, status: "active" },
    { session_id: "s-2", user_sub: "dev@example.com", title: "8 나누기 2 는?", created_at: NOW, last_turn_at: NOW, turns: 1, usage: { input_tokens: 400, output_tokens: 80 }, status: "pending", pending_since: NOW },
    { session_id: "s-3", user_sub: "header-admin", created_at: NOW, last_turn_at: NOW, turns: 6, usage: { input_tokens: 5000, output_tokens: 900 }, status: "closed" },
  ];
}

export const agentSessionState: { items: AgentSession[]; pending: Record<string, AgentPending> } = { items: sessionFixtures(), pending: { "s-2": agentPendingFixture } };

/** 세션 파일·피드백 목도 이것으로 세션을 찾는다. 없으면 런타임처럼 404 session_not_found 다. */
export const agentSessionStore = docStore(agentSessionState, "session", { key: "session_id", code: "session_not_found" });

/**
 * 세션 상세. 런타임 `GET /sessions/{sid}`(agent-runtime/runtime/routes.py 의 get_session)가 실제로 내는 두 기록이다.
 * `messages` 는 체크포인트의 200자 요약이라 role 이 langchain 종류(human·ai·tool)이고 본문은 text 다. `transcript.items` 는
 * 스트림을 접은 원문 전사(runtime/transcript.py)이고 승인 카드의 resolved 는 읽을 때 정한다. 닫힌 세션은 체크포인트가 지워져
 * 요약이 비고 전사만 남는다 (D46).
 *
 * 전에는 요약을 화면이 믿던 `{role: "user", content}` 로 적어 두어 세션 탭이 빈 본문을 그려도 테스트가 통과했다. 이 모양을
 * 고칠 때는 런타임 쪽 응답을 먼저 보고 맞춘다. 대화는 나눗셈 한 번이다. 보류 중이면 승인 카드에서 멈추고 아니면 승인 뒤
 * 답과 done 까지 간다.
 */
export function sessionDetailOf(s: AgentSession, pending: AgentPending | undefined, greeting: string | null | undefined): AgentSessionDetail {
  const question = "8 나누기 2 는?";
  const ask = "나눗셈은 승인이 필요합니다.";
  const answer = "결과는 4 입니다.";
  const call = { id: "call-divide", name: "Divide", args: { a: 8, b: 2 } };
  const result = { id: call.id, name: call.name, status: "success", content: "4.0" };
  const asked: AgentSessionMessage[] = [
    { role: "human", text: question },
    { role: "ai", text: ask, tool_calls: [call.name] },
  ];
  const answered: AgentSessionMessage[] = [
    { role: "tool", text: result.content, tool: call.name, status: result.status },
    { role: "ai", text: answer },
  ];
  const items: AssistantTranscriptItem[] = [
    ...(greeting ? [{ kind: "assistant", text: greeting, streaming: false, greeting: true }] : []),
    { kind: "user", text: question },
    { kind: "assistant", text: ask, streaming: false },
    { kind: "tool_call", call, result: pending ? null : result },
    { kind: "interrupt", pending: pending ?? { ...agentPendingFixture, session_id: s.session_id }, resolved: !pending },
    ...(pending ? [] : [{ kind: "assistant", text: answer, streaming: false }, { kind: "done", usage: tokenUsage(90, 16), turn: 1, tool_calls: 1, turn_id: makeTurnId(s.session_id, 1) }]),
  ];
  const messages = s.status === "closed" ? [] : pending ? asked : [...asked, ...answered];
  return { session: s, messages, pending: Boolean(pending), transcript: { items, limit: 500 } };
}

// -- SSE ---------------------------------------------------------------------------------------

/** 런타임이 SSE 응답 머리로 알리는 세션 id, 시도 id, 연결과 떨어진 턴이라는 표시. 화면과 같은 이름이다(api/agentChat.ts). */
export { DETACHED_TURN_HEADER, SESSION_ID_HEADER, TURN_ID_HEADER };

/** 중단을 기다리는 턴이다. 세션 id 마다 그 시도의 id 와 그 턴을 `error{turn_cancelled}` 로 끝내는 함수를 든다. */
const runningTurns = new Map<string, { turnId: string; stop: () => void }>();

/**
 * 그 세션의 중단을 기다리는 턴을 멈춘다. 멈출 턴이 없거나 `turnId` 가 다른 시도를 가리키면 false 다. 런타임은 시도 id 를 표시와
 * 같은 문장에서 견줘 낡은 화면의 중단이 다음 턴을 멈추지 않게 한다 (P78 2절).
 */
export function cancelRunningTurn(sessionId: string, turnId?: string | null): boolean {
  const turn = runningTurns.get(sessionId);
  if (!turn || (turnId && turnId !== turn.turnId)) return false;
  turn.stop();
  return true;
}

/** 그 세션에 중단을 기다리는 턴이 있는가. */
export function turnInProgress(sessionId: string): boolean {
  return runningTurns.has(sessionId);
}

/** 세션 색인과 중단을 기다리는 턴을 기본값으로 되돌린다. resetAgentState 가 Agent 와 함께 부른다. */
export function resetAgentSessionState(): void {
  agentSessionState.items = sessionFixtures();
  agentSessionState.pending = { "s-2": agentPendingFixture };
  runningTurns.clear();
}

/** 이 말로 시작하는 메시지의 턴은 느린 턴(slowTurnResponse)이다. */
export const SLOW_TURN_PREFIX = "천천히";
/** 느린 턴이 열린 채 내는 토큰 하나다. */
export const SLOW_TURN_TOKEN = "천천히 생각하는 중입니다. ";

/** 중지 API 로 멈춘 턴의 마지막 이벤트다. 런타임의 문장과 같다. */
function cancelledEvent(sessionId: string, turnId: string): SseEvent {
  return errorEvent(sessionId, turnId, { code: "turn_cancelled", message: "요청으로 턴을 멈췄다" });
}

/**
 * 토큰 하나(`SLOW_TURN_TOKEN`)를 낸 채 열려 있는 턴. 중지 API 가 오면 런타임처럼 `error{code: turn_cancelled}` 를 마지막 이벤트로
 * 내고 닫는다. `detached` 면 런타임이 턴을 연결과 떼어 돌린다고 알리는 머리를 싣고, 화면이 읽기를 닫아도 턴은 돌아 세션 줄이
 * `running` 과 시도 id 를 든다 (P78 1·2절). 아니면 옛 이미지라 읽기가 닫히면 턴도 멈춘다 (D65).
 */
function heldTurnResponse(sessionId: string, turnId: string, prelude: SseEvent[], { detached }: { detached: boolean }): Response {
  const s = detached ? agentSessionStore.find(sessionId) : null;
  if (s) Object.assign(s, { running: true, turn_id: turnId });
  const turn = heldSse([...prelude, { event: "token", data: { text: SLOW_TURN_TOKEN } }], {
    headers: detached ? { [SESSION_ID_HEADER]: sessionId, [TURN_ID_HEADER]: turnId, [DETACHED_TURN_HEADER]: "1" } : { [SESSION_ID_HEADER]: sessionId },
    onCancel: detached ? undefined : () => runningTurns.delete(sessionId),
  });
  runningTurns.set(sessionId, {
    turnId,
    stop: () => {
      runningTurns.delete(sessionId);
      if (s) Object.assign(s, { running: false, turn_id: null });
      // 연결과 떨어진 턴은 읽기가 이미 닫혔을 수 있다. 보낼 곳이 없어도 턴은 멈춘다
      try {
        turn.push(cancelledEvent(sessionId, turnId));
        turn.close();
      } catch {
        // 닫힌 읽기
      }
    },
  });
  return turn.response;
}

/** `SLOW_TURN_PREFIX` 로 시작하는 말의 턴. 옛 이미지처럼 읽기가 닫히면 멈추는 느린 턴이다 (D65). */
export function slowTurnResponse(sessionId: string, turnId: string, prelude: SseEvent[]): Response {
  return heldTurnResponse(sessionId, turnId, prelude, { detached: false });
}

/** 이 말로 시작하는 메시지의 턴은 연결과 떨어져 도는 느린 턴(detachedTurnResponse)이다 (P78). */
export const DETACHED_TURN_PREFIX = "끝까지";

/** `DETACHED_TURN_PREFIX` 로 시작하는 말의 턴. 연결과 떨어져 도는 느린 턴이다 (P78 1·2절). */
export function detachedTurnResponse(sessionId: string, turnId: string, prelude: SseEvent[]): Response {
  return heldTurnResponse(sessionId, turnId, prelude, { detached: true });
}

/** 그 세션의 턴 시도 id 가 모두 이것으로 시작한다. 피드백 목이 턴이 그 세션의 것인지 이것으로 본다. */
export function turnIdPrefix(sessionId: string): string {
  return `turn-${sessionId}-`;
}

/** 턴 시도의 id 를 낸다(P36). 런타임은 uuid4 hex 를 내지만 목은 세션과 턴 번호로 정해 재개가 같은 id 를 이어 쓰게 한다. */
export function makeTurnId(sessionId: string, turn: number): string {
  return `${turnIdPrefix(sessionId)}${turn}`;
}

/*
 * 턴의 마지막 이벤트. 런타임의 표본(agent-runtime/schema/fixtures/chat/turn.json)을 펼치고 이 턴의 값만 덮어 칸이 런타임과 같다.
 * 런타임 test_transcript.py 가 실제 `/chat` 이 낸 칸을 같은 표본과 견주므로 목이 서버와 함께 틀리지 않는다.
 */
function doneEvent(sessionId: string, turn: number, turnId: string, d: Pick<DoneEvent, "usage" | "tool_calls" | "text">): SseEvent {
  return { event: "done", data: { ...TURN_FIXTURE.events.done, session_id: sessionId, turn, turn_id: turnId, ...d } };
}

function errorEvent(sessionId: string, turnId: string, d: Pick<ErrorEvent, "code" | "message">): SseEvent {
  return { event: "error", data: { ...TURN_FIXTURE.events.error, session_id: sessionId, turn_id: turnId, ...d } };
}

export function touchSession(sessionId: string, status: AgentSession["status"], sub: string, message?: string): AgentSession {
  let s = agentSessionStore.find(sessionId);
  if (!s) {
    // 새 세션의 제목은 런타임(agent-runtime/runtime/turn_record.py)과 같은 규칙으로 첫 메시지에서 만든다
    const title = message ? titleFromMessage(message) : null;
    s = { session_id: sessionId, user_sub: sub, title, created_at: NOW, last_turn_at: NOW, turns: 0, usage: tokenUsage(0, 0), status };
    agentSessionState.items = [s, ...agentSessionState.items];
  }
  s.status = status;
  s.last_turn_at = NOW;
  if (status !== "pending") s.turns += 1;
  return s;
}

/**
 * 예비 모델이 대신 답한 턴이다(P36 11절). 런타임 표본(chat/fallback.json)의 이벤트를 펼친다. 기본 모델이 계속 실패하면 모델
 * 호출마다 전환이 오므로 도구 호출 앞과 답 앞에 같은 전환을 두 번 내고, 마지막 done 은 구조화된 결과를 싣는다.
 */
function fallbackScript(sessionId: string, turn: number): SseEvent[] {
  const { events } = FALLBACK_FIXTURE;
  const fallback: SseEvent = { event: "model_fallback", data: events.model_fallback };
  return [
    fallback,
    { event: "tool_call", data: { id: "call-1", name: "Add", args: { a: 3, b: 4 } } },
    { event: "tool_result", data: { id: "call-1", name: "Add", status: "success", content: "7" } },
    fallback,
    { event: "token", data: { text: events.done.text } },
    { event: "done", data: { ...events.done, session_id: sessionId, turn, turn_id: makeTurnId(sessionId, turn), tool_calls: 1 } },
  ];
}

/**
 * 지식 검색으로 답한 턴이다(P36 7절). 런타임 표본(chat/citations.json)의 검색 호출·결과·출처 이벤트를 펼치고 표본 전사의 답으로
 * 답한다. 답의 [1]·[2] 가 번호 칩이 된다.
 */
function citationsScript(sessionId: string, turn: number): SseEvent[] {
  const { events, transcript } = CITATIONS_FIXTURE;
  const answer = String(transcript.find((it) => it.kind === "assistant")?.text ?? "");
  return [
    { event: "tool_call", data: events.tool_call },
    { event: "tool_result", data: events.tool_result },
    { event: "citations", data: events.citations },
    { event: "token", data: { text: answer } },
    doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(410, 52), tool_calls: 1, text: answer }),
  ];
}

/**
 * 한 턴의 대본. "나누"·divide 는 승인 인터럽트, 4111 은 가드레일 차단, "예비 모델" 은 예비 모델의 답, "환불 기한" 은 출처를 인용한
 * 답, 그 밖은 Add 도구 호출 뒤 답한다.
 */
export function chatScript(sessionId: string, message: string, turn: number): SseEvent[] {
  if (/예비 모델/.test(message)) return fallbackScript(sessionId, turn);
  if (/환불 기한/.test(message)) return citationsScript(sessionId, turn);
  if (/나누|divide/i.test(message)) {
    agentSessionState.pending[sessionId] = { ...agentPendingFixture, session_id: sessionId };
    return [
      { event: "token", data: { text: "나눗셈은 승인이 필요합니다. " } },
      { event: "interrupt", data: { ...agentPendingFixture, session_id: sessionId } },
    ];
  }
  // 출력 가드레일은 스트림 안의 guardrail 이벤트다. 입력 쪽 block 은 핸들러가 먼저 422 로 막는다
  if (/출력 차단/.test(message)) {
    return [
      { event: "guardrail", data: { position: "output", rule: "deny_pattern", action: "block", message: "답에 내보낼 수 없는 내용이 있어 막았다." } },
      doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(0, 0), tool_calls: 0, text: "" }),
    ];
  }
  return [
    { event: "token", data: { text: "3 더하기 4를 " } },
    { event: "token", data: { text: "계산하겠습니다. " } },
    { event: "tool_call", data: { id: "call-1", name: "Add", args: { a: 3, b: 4 } } },
    { event: "tool_result", data: { id: "call-1", name: "Add", status: "success", content: "7" } },
    { event: "token", data: { text: "결과는 7 입니다." } },
    doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(120, 24, 96), tool_calls: 1, text: "3 더하기 4를 계산하겠습니다. 결과는 7 입니다." }),
  ];
}

/**
 * 첨부 턴의 done 은 런타임의 첨부 턴 표본(chat/attachments.json)의 done 을 펴고 이 턴의 칸을 얹는다(P36 9절). 받은 첨부 수는 이번에
 * 보낸 수다. 그 칸 이름은 attachments-lib.test 가 같은 표본으로 화면의 판정(attachmentsLost)과 맞춰 본다. "옛 런타임" 이 든 말은 첨부를
 * 모르고 버리는 옛 파드처럼 받은 수가 보낸 수와 다르다.
 */
export function withReceived(events: SseEvent[], body: { message: string; attachments?: string[] }): SseEvent[] {
  const sent = body.attachments?.length ?? 0;
  if (!sent || /옛 런타임/.test(body.message)) return events;
  return events.map((ev) => {
    if (ev.event !== "done") return ev;
    const { session_id, turn, turn_id, usage, tool_calls, text } = ev.data as DoneEvent;
    return { ...ev, data: { ...ATTACHMENTS_FIXTURE.events.done, session_id, turn, turn_id, usage, tool_calls, text, attachments_received: sent } };
  });
}

export function decisionScript(sessionId: string, decisions: AgentDecision[], turn: number): SseEvent[] {
  const d = decisions[0];
  if (d.type === "reject") {
    return [
      { event: "token", data: { text: "요청을 취소했습니다." } },
      doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(60, 8), tool_calls: 0, text: "요청을 취소했습니다." }),
    ];
  }
  if (d.type === "respond") {
    return [
      { event: "token", data: { text: `메시지를 받았습니다: ${d.message}` } },
      doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(60, 12), tool_calls: 0, text: `메시지를 받았습니다: ${d.message}` }),
    ];
  }
  const args = d.type === "edit" ? d.edited_action.args : agentPendingFixture.action_requests[0].args;
  const a = Number(args.a);
  const b = Number(args.b);
  const result = b === 0 ? "error: division by zero" : String(a / b);
  return [
    { event: "tool_call", data: { id: "call-2", name: "Divide", args } },
    { event: "tool_result", data: { id: "call-2", name: "Divide", status: b === 0 ? "error" : "success", content: result } },
    { event: "token", data: { text: `결과는 ${result} 입니다.` } },
    doneEvent(sessionId, turn, makeTurnId(sessionId, turn), { usage: tokenUsage(90, 16), tool_calls: 1, text: `결과는 ${result} 입니다.` }),
  ];
}

/**
 * 다시 생성의 대본(P36). 턴 번호는 바꾸는 턴의 것이고 turn_id 는 새로 낸다. 수정한 메시지가 있으면 그 글에 답하고, 없으면
 * 도구 없이 다시 생성한 답변을 낸다.
 */
export function regenerateScript(sessionId: string, turn: number, message: string | null | undefined): SseEvent[] {
  const text = message ? `고친 질문에 답합니다: ${message}` : "다시 생성한 답변입니다.";
  return [
    { event: "token", data: { text } },
    doneEvent(sessionId, turn, `${makeTurnId(sessionId, turn)}-again`, { usage: tokenUsage(60, 10), tool_calls: 0, text }),
  ];
}

/** 입력 가드레일의 block 이다. 4111 이 든 말은 스트림을 열기 전에 422 로 막는다(런타임 routes 의 guardrail_blocked). 아니면 null. */
export function inputGuardrailBlock(message: string | null | undefined): Response | null {
  if (!message || !/4111/.test(message)) return null;
  // 플랫폼이 {code, detail, ...extra} 로 옮긴다
  return apiError(422, "guardrail_blocked", "카드 번호 같은 민감 정보는 보낼 수 없다. 지우고 다시 보내 달라.", { rule: "pii_mask" });
}
