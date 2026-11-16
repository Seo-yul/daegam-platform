/**
 * 코드 실행(P40)의 수다. 캔버스 검증(agentGraph.ts)과 화면 문장(i18n)이 같이 읽으므로 아무것도 import 하지 않는다.
 */

/** 명령 하나가 받을 수 있는 가장 긴 시간(초)이다(agent-runtime `sandbox.py` MAX_COMMAND_SECONDS, 서버 `SANDBOX_COMMAND_MAX_SECONDS`). */
export const SANDBOX_COMMAND_MAX_SECONDS = 300;

/** 코드 실행 게이트가 닫힌 까닭이다(서버 `services/sandbox_gate.py` GateReason). 고칠 차례대로다. */
export type GateReason = "rights" | "unchecked" | "unaccepted" | "capacity";
export const GATE_REASONS: readonly GateReason[] = ["rights", "unchecked", "unaccepted", "capacity"];

/** 서버 문장이나 오류의 까닭 글자가 아는 까닭이면 그것이다. 모르면 null 이다. */
export function gateReason(value: unknown): GateReason | null {
  return GATE_REASONS.find((r) => r === value) ?? null;
}

/** 명령 실행 전 승인을 켤 때의 결정이다. 승인과 거부만 묻는다. */
export const CODE_EXECUTION_APPROVAL = ["approve", "reject"] as const;
