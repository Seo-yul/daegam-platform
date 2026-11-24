/**
 * 자동 실행 (P41)의 화면 규칙. 상태와 까닭의 말, 배지 색, 누가 무엇을 할 수 있는지, 실행이 켜져 있는지다. 말은 사전의 `runs` 와
 * 다시 쓰는 다른 영역의 말이다(phase-41 "Screen text" 의 reused keys).
 *
 * 서버 규칙은 platform/daegam/api/agent_runs.py 다. 읽기는 agent:read, 일정·트리거·지금 실행은 agent:write 와 agent:chat,
 * 실행 멈춤은 그 실행의 묶인 사람과 agent:approve 다. 실행의 대화는 묶인 사람과 agent:approve 만 연다.
 */
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { listRunsAsMe } from "@/api/resources";
import type { AgentRun, AgentRunStatus, MeOut, RunSchedule, RunSourceKind, RunTrigger } from "@/api/types";
import type { BadgeProps } from "@/components/ui/badge";
import type { Messages } from "@/i18n";
import { type AuthMode, hasScope } from "@/lib/auth";
import { MY_RUNS_AS_ME_KEY } from "@/lib/queryKeys";
import { ownValue } from "@/lib/utils";

/** 묶인 사람이 접근을 잃어 멈춘 소스의 까닭과, oidc 모드의 긴 한도를 넘은 원인 (platform services/runs/person.py). */
const LOST_ACCESS = "acts_as_lost_access";
const CAUSE_UNCONFIRMED = "unconfirmed";

/** 아직 움직이는 실행. 멈출 수 있고, 목록이 이것이 있는 동안 다시 읽는다. */
export const OPEN_RUN_STATUSES: ReadonlySet<AgentRunStatus> = new Set<AgentRunStatus>(["starting", "running", "waiting_approval"]);

/** 자동 실행이 꺼진 플랫폼의 답인가. 꺼지면 실행 라우트가 모두 404 다(P41 12절). */
export function runsOff(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** 나에게 묶인 일정과 트리거. 계정 화면이 보이고, 실행이 켜져 있는지도 이것의 404 로 안다. */
export function useRunsAsMe() {
  return useQuery({ queryKey: MY_RUNS_AS_ME_KEY, queryFn: listRunsAsMe });
}

/** 자동 실행이 켜졌는지. `unknown` 은 아직 모르는 것(불러오는 중, 404 가 아닌 오류)이다. */
export type RunsAvailability = "on" | "off" | "unknown";

export function useRunsAvailability(): RunsAvailability {
  const q = useRunsAsMe();
  if (q.isSuccess) return "on";
  return runsOff(q.error) ? "off" : "unknown";
}

/** 자동 실행의 탭과 링크를 세울지. 꺼진 것을 확인하기 전에는 세운다. 주소의 ?tab=runs 가 사라지지 않게 한다. */
export function useRunsEnabled(): boolean {
  return useRunsAvailability() !== "off";
}

/**
 * 일정과 트리거를 만들고 바꾸며 지금 실행할 수 있는가. 서버는 묶을 때 그 테넌트의 소속 줄을 본다(`rbac.holds_tenant`). 그래서
 * platform-admin 의 `*` 줄만 가진 사람은 403 이라 단추를 세우지 않는다. header 모드는 아무도 확인하지 않는다.
 */
export function canWriteRuns(me: MeOut | null, tenant: string, mode: AuthMode): boolean {
  if (!hasScope(me, tenant, "agent:write") || !hasScope(me, tenant, "agent:chat")) return false;
  return mode === "header" || (me?.memberships.some((m) => m.tenant === tenant) ?? false);
}

/** 실행의 대화를 열고 실행을 멈출 수 있는가. 그 실행의 묶인 사람이거나 agent:approve 다. */
export function canFollowRun(run: Pick<AgentRun, "acts_as">, me: MeOut | null, tenant: string): boolean {
  return (me !== null && run.acts_as.sub === me.sub) || hasScope(me, tenant, "agent:approve");
}

export function runStatusText(t: Messages, run: Pick<AgentRun, "status" | "count">): string {
  const turn = t.agents.metrics.turnResult;
  switch (run.status) {
    case "starting":
      return t.runs.status.starting;
    case "running":
      return t.chat.runningBadge;
    case "waiting_approval":
      return turn.pending;
    case "succeeded":
      return turn.done;
    case "failed":
      return turn.turn_failed;
    case "cancelled":
      return turn.cancelled;
    case "refused":
      return t.runs.status.refused;
    case "skipped":
      return t.runs.status.skipped(run.count ?? 1);
  }
}

export function runStatusVariant(status: AgentRunStatus): BadgeProps["variant"] {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "error";
  if (status === "waiting_approval") return "warn";
  if (status === "starting" || status === "running") return "info";
  return "muted";
}

/** 상태가 이미 말하는 까닭. 까닭 칸을 비운다. */
const SAID_BY_STATUS: ReadonlySet<string> = new Set(["stopped", "approval_deadline", "source_deleted", "agent_deleted", "session_closed"]);

/** 실행의 까닭 코드마다 사전의 말. 같은 말을 여러 코드가 나눠 쓴다. days 는 oidc 모드의 긴 한도(날)다. */
function reasonTable(t: Messages, days: number): Record<string, string> {
  const r = t.runs.reason;
  const prerequisites = r.prerequisites;
  return {
    previous_running: r.previousRunning,
    agent_run_limit: r.agentRunLimit,
    missed: r.missed,
    run_lost: r.runLost,
    acts_as_lost_access: r.actsAsLostAccess,
    acts_as_unconfirmed: r.actsAsUnconfirmed(days),
    agent_changed: r.agentChanged,
    prerequisites_failed: prerequisites,
    runs_need_memory: prerequisites,
    run_budget_required: prerequisites,
    runs_need_events: prerequisites,
    trigger_needs_approval: prerequisites,
    max_input_chars: r.inputTooLong,
    rate_limited: r.rateOrSessions,
    max_concurrent_sessions: r.rateOrSessions,
    daily_budget_exceeded: r.dailyBudget,
    guardrail_blocked: r.guardrail,
    not_deployed: r.unavailable,
    tools_unavailable: r.unavailable,
    runtime_unreachable: r.unavailable,
    turn_aborted: r.aborted,
    turn_access_lost: r.accessLost,
    caller_ended: r.callerEnded,
    caller_timeout: r.callerTimeout,
    caller_cancelled: r.callerCancelled,
    caller_updated: r.callerUpdated,
    turn_timeout: t.agents.metrics.turnResult.turn_timeout,
    budget_exceeded: t.agents.metrics.turnResult.budget_exceeded,
    agent_undeployed: t.runs.pausedReason.agentUndeployed,
    expired: t.runs.pausedReason.expired,
    paused: t.runs.state.paused,
  };
}

/**
 * 실행이 건너뛰거나 실패하거나 멈춘 까닭. 사람이 멈춘 것(`stopped`), 마감의 거절(`approval_deadline`), 지운 소스와 Agent
 * (`source_deleted`, `agent_deleted`), 닫힌 세션의 결정(`session_closed`)은 상태가 이미 말하므로 null 이다. 런타임의 5xx 는 다시 시도한 뒤의 것이라 응답하지 않은 것과 같은 말이다. 표에 없는 코드는 오류 표의 문장이고, 거기도
 * 없으면 코드 그대로다(`code` 가 참). confirmDays 는 oidc 모드의 긴 한도(날)이고 그 한도를 넘은 사람의 실행(`acts_as_unconfirmed`)이
 * 말한다.
 */
export function runReasonText(t: Messages, reason: string | null | undefined, confirmDays: number): { text: string; code: boolean } | null {
  if (!reason || SAID_BY_STATUS.has(reason)) return null;
  const known = ownValue(reasonTable(t, confirmDays), reason) ?? (/^http_5\d\d$/.test(reason) ? t.runs.reason.unavailable : ownValue(t.errors.codes, reason));
  return known ? { text: known, code: false } : { text: reason, code: true };
}

/** 실행 기록의 출처. 일정은 일정 카드의 제목과 같은 말이다. 사전에 없는 출처(뒤 판이 더한 것)는 코드 그대로다 (P46 A0). */
export function runSourceText(t: Messages, source: RunSourceKind): string {
  return source === "schedule" ? t.runs.schedules.title : (ownValue(t.runs.source, source) ?? source);
}

/** 다른 Agent 가 Tool 로 부른 실행의 부른 쪽 줄 (P46 6.5절). name 은 부른 Agent 의 화면 이름이다. 부른 쪽이 없는 실행은 null 이다. */
export function calledByText(t: Messages, run: Pick<AgentRun, "caller">, name: (agentId: string) => string): string | null {
  return run.caller ? t.runs.calledBy(name(run.caller.agent_id)) : null;
}

/** 누구의 이름으로 도는지. 사람이면 이름(없으면 sub), 아니면 Agent 다. */
export function actsAsText(t: Messages, actsAs: AgentRun["acts_as"]): string {
  return actsAs.kind === "person" ? t.runs.history.actsAs.person(actsAs.username ?? actsAs.sub) : t.runs.history.actsAs.agent;
}

/**
 * 일정이나 트리거가 멈춘 까닭의 문장. 손으로 멈춘 것은 상태만 보이므로 null 이다. 만료는 멈춤 표시 없이도 지났으면 만료다. 묶인
 * 사람이 oidc 모드의 긴 한도(confirmDays 날)를 넘어 멈췄으면(`paused_cause` 가 unconfirmed) 그것을 말한다(P53b 6.3절).
 */
export function pausedReasonText(
  t: Messages,
  source: Pick<RunSchedule | RunTrigger, "paused" | "paused_reason" | "paused_cause" | "expires_at">,
  confirmDays: number,
  now: number = Date.now(),
): string | null {
  const r = t.runs.pausedReason;
  if (!source.paused) return Date.parse(source.expires_at) <= now ? r.expired : null;
  if (source.paused_reason === LOST_ACCESS && source.paused_cause === CAUSE_UNCONFIRMED) return r.actsAsUnconfirmed(confirmDays);
  const table: Record<string, string> = {
    acts_as_lost_access: r.actsAsLostAccess,
    agent_changed: r.agentChanged,
    agent_undeployed: r.agentUndeployed,
    expired: r.expired,
    prerequisites_failed: r.prerequisites,
  };
  return source.paused_reason ? (ownValue(table, source.paused_reason) ?? null) : null;
}

/** 일정이나 트리거가 지금 돌 수 있는가. 멈췄거나 만료가 지났으면 멈춘 것이다. */
export function sourceActive(source: Pick<RunSchedule | RunTrigger, "paused" | "expires_at">, now: number = Date.now()): boolean {
  return !source.paused && Date.parse(source.expires_at) > now;
}

/** 실행이 걸린 초. 아직 끝나지 않았거나 시작하지 않았으면 null 이다. */
export function runSeconds(run: Pick<AgentRun, "started_at" | "ended_at">): number | null {
  if (!run.started_at || !run.ended_at) return null;
  return Math.max(0, (Date.parse(run.ended_at) - Date.parse(run.started_at)) / 1000);
}
