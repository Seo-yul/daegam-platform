/**
 * 턴의 트레이스 창 (P37 12절). 턴 시도(`turn_id`) 하나의 실행들을 차례로 세우고 실행 사이의 틈을 적는다. 승인 뒤 재개는
 * 같은 턴의 다음 실행이라 그 틈이 "승인 대기 3분 12초" 다. turn_id 가 없으면 실행(`trace_id`) 하나만 그린다. 둘 다 없거나
 * 이벤트가 없는 턴(옛 런타임)은 "이 턴은 트레이스가 없습니다" 다.
 *
 * 채팅의 턴 마감 줄과 세션 탭의 턴 표가 연다. 소유자가 아니면 서버가 403 이다. 턴이 막 끝나면 이벤트가 아직 저장소에 없어
 * 404 일 수 있다. 그때는 "모으는 중" 을 보이며 몇 번 다시 읽고, 그래도 없으면 트레이스가 없다고 한다.
 */
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import { getAgentTrace, listAgentTurns } from "@/api/resources";
import type { AgentTurnRun } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { Badge } from "@/components/ui/badge";
import { Modal } from "@/components/ui/modal";
import { useT, type Messages } from "@/i18n";
import { runStatusName } from "@/lib/agentEvents";
import { agentTurnsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { formatDate, formatDuration } from "@/lib/utils";

import { TraceTimeline } from "./TraceTimeline";

/** 무엇의 트레이스인가. turn_id 가 있으면 세션 id 와 함께 그 턴의 실행 전부, 없으면 trace_id 하나다. */
export interface TraceTarget {
  sessionId?: string | null;
  turnId?: string | null;
  traceId?: string | null;
}

/** 404 를 다시 읽는 횟수와 간격. 런타임은 2초마다 보내고 수집은 곧바로 넣으므로 10초 안에 나타난다. */
const COLLECT_RETRIES = 5;
const COLLECT_DELAY_MS = 2_000;

function notFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** 실행 사이의 틈. 한 시간 아래는 초까지 적는다. 승인을 기다린 시간은 초가 뜻이 있다. */
export function gapText(t: Messages, ms: number): string {
  return formatDuration(t, Math.max(0, Math.round(ms / 1000)), { exact: true });
}

function endOf(run: AgentTurnRun): number {
  return new Date(run.start).getTime() + (run.duration_ms ?? 0);
}

export function TraceDrawer({ agentId, target, onClose }: { agentId: string; target: TraceTarget | null; onClose: () => void }) {
  const t = useT();
  return (
    <Modal open={target !== null} title={t.agents.trace.title} size="lg" testId="trace-drawer" closeTestId="trace-drawer-close" onClose={onClose}>
      <div className="min-h-0 flex-1 overflow-y-auto">{target ? <TraceRuns agentId={agentId} target={target} /> : null}</div>
    </Modal>
  );
}

function TraceRuns({ agentId, target }: { agentId: string; target: TraceTarget }) {
  const t = useT();
  const text = t.agents.trace;
  const tenant = useTenant();
  const byTurn = Boolean(target.turnId && target.sessionId);
  const turns = useQuery({
    queryKey: agentTurnsKey(tenant, agentId, target.sessionId),
    queryFn: () => listAgentTurns(agentId, target.sessionId ?? ""),
    enabled: byTurn,
  });
  // 턴 기록의 404 는 소유자를 아직 정할 수 없는 것(turn_start 가 오기 전)이라 실행 id 로 그린다
  const gate = byTurn && !notFound(turns.error) ? queryGate(turns, { testId: "trace-error" }) : null;
  if (gate) return gate;
  const turn = byTurn ? turns.data?.find((x) => x.turn_id === target.turnId) : undefined;
  const runs = (turn?.runs ?? []).filter((r) => r.trace_id);
  const traceIds = runs.length ? runs.map((r) => r.trace_id as string) : target.traceId ? [target.traceId] : [];
  if (!traceIds.length) {
    return (
      <p className="text-sm text-muted" data-testid="trace-none">
        {text.none}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4" data-testid="trace-runs">
      {traceIds.map((traceId, i) => (
        <div key={traceId} className="flex flex-col gap-2">
          {i > 0 && runs[i - 1] && runs[i] ? (
            <p className="text-xs text-muted" data-testid="trace-gap">
              {text.approvalWait(gapText(t, new Date(runs[i].start).getTime() - endOf(runs[i - 1])))}
            </p>
          ) : null}
          <RunSection agentId={agentId} traceId={traceId} run={runs[i]} index={i} />
        </div>
      ))}
    </div>
  );
}

function RunSection({ agentId, traceId, run, index }: { agentId: string; traceId: string; run: AgentTurnRun | undefined; index: number }) {
  const t = useT();
  const text = t.agents.trace;
  const tenant = useTenant();
  const q = useQuery({
    queryKey: ["agents", tenant, agentId, "trace", traceId],
    queryFn: () => getAgentTrace(agentId, traceId),
    // 막 끝난 턴은 이벤트가 아직 없어 404 다. 그것만 몇 번 다시 읽는다
    retry: (count, error) => notFound(error) && count < COLLECT_RETRIES,
    retryDelay: COLLECT_DELAY_MS,
  });
  const collecting = q.isPending && notFound(q.failureReason);
  return (
    <section className="flex flex-col gap-2 rounded-tile border border-line-soft p-3" data-testid={`trace-run-${index}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium">{text.run(index + 1)}</span>
        {run ? <Badge variant="muted">{runStatusName(t, run.status)}</Badge> : null}
        {run?.resumed ? <Badge variant="muted">{text.resumed}</Badge> : null}
        {run ? <span className="text-muted">{formatDate(t, run.start)}</span> : null}
        <span className="break-all font-mono text-muted">{traceId}</span>
      </div>
      {collecting ? (
        <p className="text-sm text-muted" data-testid={`trace-run-${index}-collecting`}>
          {text.collecting}
        </p>
      ) : q.isLoading ? (
        <p className="text-sm text-muted">{t.common.loading}</p>
      ) : null}
      {notFound(q.error) ? (
        <p className="text-sm text-muted" data-testid={`trace-run-${index}-none`}>
          {text.none}
        </p>
      ) : (
        <ErrorMessage error={q.error} testId={`trace-run-${index}-error`} />
      )}
      {q.data ? <TraceTimeline trace={q.data} testId={`trace-run-${index}-timeline`} /> : null}
    </section>
  );
}
