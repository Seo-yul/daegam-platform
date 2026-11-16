/**
 * Agent 의 런타임 이벤트와 실행의 이름 (P37 12절). 이벤트 탭, 트레이스 그림의 점 이벤트, 세션 탭과 트레이스 창의 실행 결과가
 * 같은 사전을 읽는다. 사전에 없는 이름은 서버의 이름 그대로다.
 */
import type { Messages } from "@/i18n";
import { ownValue } from "@/lib/utils";

/** turn_end 가 아직 오지 않은 실행의 결과다(서버 traces.RUNNING). */
export const RUNNING = "running";

/** 이벤트 이름. 사전의 agents.events.names 에 있으면 그 이름, 아니면 서버의 이름 그대로다. */
export function eventName(t: Messages, name: string): string {
  return ownValue(t.agents.events.names, name) ?? name;
}

/** 실행(턴)의 결과 이름. 지표 탭의 턴 결과와 같은 사전이고 도는 중이면 "실행 중" 이다. 없으면 "-" 다. */
export function runStatusName(t: Messages, status: string | null | undefined): string {
  if (!status) return "-";
  if (status === RUNNING) return t.agents.trace.running;
  return ownValue(t.agents.metrics.turnResult, status) ?? status;
}
