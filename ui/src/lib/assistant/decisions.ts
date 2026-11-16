/**
 * 승인 결정 본문 (D31). 결정의 id 는 interrupt data 의 `action_requests[i].id` 가 있으면 그것, 없으면
 * `GET /sessions/{id}` 의 `pending.calls[i].id` 다 (계약: 둘은 같은 순서다).
 *
 * 사람에게 보이는 거절은 사전 키를 든 ChatNotice 로 던지고 패널이 그릴 때 문장을 만든다. 결정 수가 호출 수와 다른 것은 카드가
 * 호출마다 결정을 하나씩 만들어 생기지 않는 개발자의 잘못이라 영어 그대로 던진다.
 */
import { getAssistantSession } from "@/api/resources";
import type { AgentDecision, AssistantDecision, AssistantPending } from "@/api/types";
import { ChatNotice } from "@/lib/chatNotice";
import { isRecord } from "@/lib/json";

/**
 * 비서가 Agent 정의 제안(P48 6.1절)을 기다리게 할 때 그 호출의 인자에 넣는 예약 칸이다(`platform/daegam/services/assistant/proposals.py`
 * `PROPOSAL_KEY`). 카드는 이 칸을 인자로 보이지 않고, 결정에 그 지문(`digest`)을 `proposal_digest` 로 되돌려 보낸다.
 */
export const PROPOSAL_KEY = "_proposal";

/** 카드가 보이는 인자. 예약 칸을 뺀다. */
export function visibleArgs(args: Record<string, unknown>): Record<string, unknown> {
  // 런타임이 인자 없이 보낸 요청도 카드가 그리므로 객체가 아니면 그대로 둔다
  if (!isRecord(args) || !(PROPOSAL_KEY in args)) return args;
  const { [PROPOSAL_KEY]: _reserved, ...rest } = args;
  return rest;
}

/** 예약 칸의 글자 칸 하나. 제안이 아니거나 비었으면 undefined 다. */
function reservedString(args: Record<string, unknown> | undefined, key: "id" | "digest"): string | undefined {
  const reserved = args?.[PROPOSAL_KEY];
  const value = isRecord(reserved) ? reserved[key] : undefined;
  return typeof value === "string" && value ? value : undefined;
}

/** 제안 호출의 제안 id. 캔버스는 이것으로 주소의 제안을 기다리는 호출을 찾는다. 제안이 아니면 undefined 다. */
export function proposalIdOf(args: Record<string, unknown> | undefined): string | undefined {
  return reservedString(args, "id");
}

/** 제안 호출의 지문. 제안이 아니면 undefined 다. */
export function proposalDigest(args: Record<string, unknown> | undefined): string | undefined {
  return reservedString(args, "digest");
}

/**
 * ApprovalCard 의 위치 순서 결정(`{type}`)을 계약의 `{id, type, message?}` 로 바꾼다. 비서의 allowed_decisions 는 approve·reject 뿐이고,
 * respond 가 오면 그 메시지를 사유로 한 reject 로 본다. edit 는 계약에 없어 거부한다.
 *
 * requests 는 같은 순서의 action_requests 다. 제안 호출이면 그 지문을 승인이든 거절이든 결정에 싣는다. 싣지 않으면 서버가
 * 409 proposal_changed 로 막는다(P48 6.1절).
 */
export function buildDecisions(decisions: AgentDecision[], ids: string[], requests: AssistantPending["action_requests"]): AssistantDecision[] {
  if (ids.length !== decisions.length) throw new Error(`${decisions.length} decisions do not match ${ids.length} pending calls`);
  return decisions.map((d, i) => {
    const id = ids[i];
    const digest = proposalDigest(requests[i]?.args);
    const proposal = digest ? { proposal_digest: digest } : {};
    switch (d.type) {
      case "approve":
        return { id, type: "approve", ...proposal };
      case "reject":
        return d.message ? { id, type: "reject", message: d.message, ...proposal } : { id, type: "reject", ...proposal };
      case "respond":
        return { id, type: "reject", message: d.message, ...proposal };
      default:
        throw new ChatNotice("editUnsupported");
    }
  });
}

/**
 * 보류 호출의 id 목록. interrupt data 의 `action_requests[i].id` 가 전부 있으면 그대로, 하나라도 없으면 세션을 다시 읽어
 * `pending.calls[i].id` 를 쓴다(같은 순서 계약). 그 사이 보류가 바뀌었으면 오류다.
 */
export async function resolveDecisionIds(sessionId: string, pending: AssistantPending): Promise<string[]> {
  const inline = pending.action_requests.map((r) => r.id);
  if (inline.length && inline.every((x): x is string => typeof x === "string" && x.length > 0)) return inline;
  const detail = await getAssistantSession(sessionId);
  const calls = detail.pending?.calls ?? [];
  if (!detail.pending || calls.length !== pending.action_requests.length) {
    throw new ChatNotice("pendingChanged");
  }
  return calls.map((c) => c.id);
}
