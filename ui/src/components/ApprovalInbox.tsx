/**
 * 승인함. pending 세션 목록에서 세션을 고르면 `GET /pending` 의 요청을 승인 카드로 보이고, 결정을 보내면 재개
 * 스트림을 그 자리에 이어 보인다. 결정 권한은 세션 소유자(sub 일치) 또는 agent:approve scope 다.
 *
 * agent:approve 가 있으면 모든 사람의 자동 실행 가운데 승인을 기다리는 것도 함께 보인다(P41 7절, `runs=only` 와 `all_users`).
 * 그 줄은 자동 실행 표시와 마감을 단다. 실행이 결정이 아니라 사람이 결정한다(R9). 마감이 지났거나 이미 결정된 실행은 서버가 409
 * run_closed 로 거절하고 그 문장은 오류 표에 있다.
 *
 * 잠든 Agent 에 보낸 결정은 플랫폼이 깨우는 동안 503 agent_waking 이다(P44 8절). 결정을 쥔 채 기다림 줄과 중단 단추를 세우고
 * Retry-After 마다 다시 보낸다. 5분을 넘기면 마지막 오류를 보이고, 중단하면 결과 자리를 걷어 카드로 돌아간다.
 * 상태가 Sleeping·Waking 인 동안에는 두 목록과 보류 읽기를 보내지 않는다. 목록 자리에 잠들 때 남긴 승인 대기 수와 깨우기 단추가
 * 서고, 상태가 그 둘을 떠나면 다시 읽는다. 읽기는 Agent 를 깨우지 않으므로 이 탭을 열어 두어도 Agent 는 잠든 채다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { chatBase } from "@/api/client";
import { getAgentPending, listAgentSessions, openAgentDecisions } from "@/api/resources";
import type { AgentDecision } from "@/api/types";
import { SleepingNotice } from "@/components/agents/AgentSleep";
import { ApprovalCard } from "@/components/ApprovalCard";
import { WAKING_DECISION, describeChatError, type ChatItem } from "@/components/chat";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Markdown } from "@/components/markdown/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";
import { pausesRuntimeReads, untilAwake } from "@/lib/agentSleep";
import { hasScope, useAuthState } from "@/lib/auth";
import { consumeChatStream } from "@/lib/chatTurn";
import { describeCode } from "@/lib/errorText";
import { pendingRunSessionsKey } from "@/lib/queryKeys";
import { useRunsAvailability } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";
import { tokensOf } from "@/lib/usage";
import { cn, formatDate, isAbortError } from "@/lib/utils";

export const APPROVAL_POLL_MS = 5000;

export function ApprovalInbox({
  instanceId,
  phase = null,
  savedPending = null,
}: {
  instanceId: string;
  /** 상태의 phase. Sleeping·Waking 이면 런타임을 읽지 않는다 (P44 8절). */
  phase?: string | null;
  /** 잠들 때 남긴 승인 대기 수. 상태의 pending_approvals 다. */
  savedPending?: number | null;
}) {
  const t = useT();
  const text = t.shared.approvalInbox;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const { me } = useAuthState();
  const canApprove = hasScope(me, tenant, "agent:approve");
  const [selected, setSelected] = useState<string | null>(null);
  const paused = pausesRuntimeReads(phase);
  // error 는 잡은 오류이고 문장은 그릴 때 describeChatError 로 만든다. waking 은 깨우는 동안 결정을 쥐고 기다리는 중이다
  const [outcome, setOutcome] = useState<{ sessionId: string; items: ChatItem[]; error: unknown; busy: boolean; waking: boolean } | null>(null);
  // 깨우는 동안의 기다림을 멈추는 신호다. 스트림이 열리면 쓸 일이 없다. 탭을 떠나면 기다림도 멈춰 보이지 않는 결정을 보내지 않는다
  const waitRef = useRef<AbortController | null>(null);
  useEffect(() => () => waitRef.current?.abort(), []);

  const list = useQuery({
    queryKey: ["agents", tenant, instanceId, "sessions", "pending"],
    queryFn: () => listAgentSessions(instanceId, "pending"),
    refetchInterval: APPROVAL_POLL_MS,
    enabled: !paused,
  });
  // 다른 사람의 실행 줄은 자동 실행이 켜진 것을 확인한 뒤에만 묻고 쓴다
  const runsOn = useRunsAvailability() === "on";
  const withRuns = canApprove && runsOn;
  const runRows = useQuery({
    queryKey: pendingRunSessionsKey(tenant, instanceId),
    queryFn: () => listAgentSessions(instanceId, "pending", undefined, { runs: "only", allUsers: true }),
    refetchInterval: APPROVAL_POLL_MS,
    enabled: withRuns && !paused,
  });
  const pending = useQuery({
    queryKey: ["agents", tenant, instanceId, "pending", selected],
    queryFn: () => getAgentPending(chatBase(instanceId), selected as string),
    enabled: Boolean(selected) && !paused,
  });

  async function decide(sessionId: string, decisions: AgentDecision[]) {
    const wait = new AbortController();
    waitRef.current = wait;
    setOutcome({ sessionId, items: [], error: null, busy: true, waking: false });
    try {
      const res = await untilAwake(() => openAgentDecisions(chatBase(instanceId), sessionId, { decisions }), {
        signal: wait.signal,
        onWaiting: () => setOutcome((prev) => prev && { ...prev, waking: true }),
      }).finally(() => {
        waitRef.current = null;
        setOutcome((prev) => prev && { ...prev, waking: false });
      });
      // 재개 스트림은 채팅과 같은 루프로 읽는다. 끝나면 흘러 들던 말풍선도 닫힌다
      const r = await consumeChatStream(res, { update: (fn) => setOutcome((prev) => prev && { ...prev, items: fn(prev.items) }) });
      setOutcome((prev) => prev && { ...prev, busy: false, error: r.status === "done" ? null : r.error });
    } catch (e) {
      // 깨우는 동안의 기다림을 멈췄다. 결정은 가지 않았으므로 카드로 돌아간다
      if (isAbortError(e)) setOutcome(null);
      // 스트림을 열지도 못했다
      else setOutcome((prev) => prev && { ...prev, busy: false, error: e });
    } finally {
      // ["agents", tenant] 가 접두어라 이 Agent 의 세션·보류 쿼리까지 함께 무효화된다
      await queryClient.invalidateQueries({ queryKey: ["agents", tenant] });
    }
  }

  // 자기 세션 다음에 다른 사람의 실행 줄이다. 같은 세션이 두 목록에 오면 실행 표시가 있는 줄을 쓴다
  const runs = withRuns ? (runRows.data ?? []) : [];
  const rows = [...(list.data ?? []).filter((s) => !runs.some((r) => r.session_id === s.session_id)), ...runs];
  const current = selected ? rows.find((s) => s.session_id === selected) : null;
  const owner = current ? current.user_sub === me?.sub : false;

  return (
    <Card data-testid="approval-inbox">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {/* 잠든 동안에는 다시 읽지 않으므로 갱신 주기를 말하지 않는다. 수는 아래 휴면 줄이 말한다 */}
        {!paused ? (
          <span className="text-xs text-muted" data-testid="approval-inbox-count">
            {text.count(rows.length, APPROVAL_POLL_MS / 1000)}
          </span>
        ) : null}
      </CardHeader>
      <CardContent className="grid grid-cols-[16rem_1fr] gap-4">
        {paused ? (
          // 잠든 동안에는 목록이 없으므로 휴면 줄이 한 줄을 다 쓴다. 이미 보낸 결정의 결과 자리는 그 아래에 남는다
          // 잠들 때 수를 읽지 못했으면(null) 수를 말하지 않는 목록의 휴면 줄이다
          <SleepingNotice
            agentId={instanceId}
            text={savedPending === null || savedPending === undefined ? t.agents.sleepingList : text.sleeping(savedPending)}
            phase={phase}
            className="col-span-2"
            testId="approval-sleeping"
          />
        ) : (
          <div className="flex flex-col gap-1">
            <ErrorMessage error={list.error ?? (withRuns ? runRows.error : null)} />
            {list.isLoading ? <p className="text-xs text-muted">{t.common.loading}</p> : null}
            {!list.isLoading && !rows.length ? <p className="text-xs text-muted">{text.empty}</p> : null}
            {rows.map((s) => (
              <button
                key={s.session_id}
                type="button"
                data-testid={`pending-session-${s.session_id}`}
                aria-pressed={selected === s.session_id}
                onClick={() => {
                  setSelected(s.session_id);
                  setOutcome(null);
                }}
                className={cn("flex flex-col gap-0.5 rounded-item border px-3 py-2 text-left text-xs hover:bg-hover", selected === s.session_id ? "border-accent-line bg-accent-soft" : "border-line bg-surface-strong")}
              >
                <span className="flex items-center gap-2">
                  <span className="font-mono text-fg">{s.session_id}</span>
                  <Badge variant="warn">{text.pending}</Badge>
                  {s.run ? (
                    <Badge variant="info" data-testid={`pending-session-${s.session_id}-run`}>
                      {t.agents.tabs.runs}
                    </Badge>
                  ) : null}
                </span>
                <span className="text-muted">
                  {s.user_sub} · {formatDate(t, s.pending_since ?? s.last_turn_at)}
                </span>
                {s.run?.deadline ? (
                  <span className="text-warn-fg" data-testid={`pending-session-${s.session_id}-deadline`}>
                    {text.runDeadline(formatDate(t, s.run.deadline))}
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        )}
        <div className={cn("flex min-w-0 flex-col gap-3", paused && "col-span-2")}>
          {!selected && !paused ? <p className="text-sm text-muted">{text.pick}</p> : null}
          {selected && pending.isLoading ? <p className="text-sm text-muted">{t.common.loading}</p> : null}
          <ErrorMessage error={pending.error} testId="pending-error" />
          {selected && pending.data && !outcome ? (
            <>
              {!canApprove && !owner ? (
                <Alert variant="warn" data-testid="approval-forbidden">
                  {text.forbidden}
                </Alert>
              ) : null}
              <ApprovalCard pending={pending.data} disabled={!canApprove && !owner} onSubmit={(decisions) => void decide(selected, decisions)} />
            </>
          ) : null}
          {outcome ? (
            <div className="flex flex-col gap-2" data-testid="approval-outcome">
              <div className="flex items-center gap-2 text-xs text-muted">
                <Badge variant={outcome.busy ? "info" : outcome.error !== null ? "error" : "ok"}>{outcome.busy ? text.outcome.busy : outcome.error !== null ? text.outcome.failed : text.outcome.done}</Badge>
                <span className="font-mono">{outcome.sessionId}</span>
                {!outcome.busy ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-auto"
                    data-testid="approval-outcome-close"
                    onClick={() => {
                      setOutcome(null);
                      setSelected(null);
                    }}
                  >
                    {t.ui.modal.close}
                  </Button>
                ) : null}
              </div>
              {outcome.waking ? (
                <Alert variant="info" className="flex flex-wrap items-center gap-3" data-testid="approval-outcome-waking">
                  <span className="min-w-0 flex-1">{describeChatError(t, WAKING_DECISION)}</span>
                  <Button size="sm" variant="ghost" data-testid="approval-outcome-stop" onClick={() => waitRef.current?.abort()}>
                    {t.shared.chatPanel.stop}
                  </Button>
                </Alert>
              ) : null}
              {outcome.items.map((it, i) => (
                <OutcomeLine key={i} item={it} index={i} />
              ))}
              {outcome.error !== null ? (
                <Alert variant="error" data-testid="approval-outcome-error">
                  {describeChatError(t, outcome.error)}
                </Alert>
              ) : null}
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

function OutcomeLine({ item, index }: { item: ChatItem; index: number }) {
  const t = useT();
  const text = t.shared.approvalInbox;
  const testId = `approval-outcome-${index}`;
  if (item.kind === "assistant")
    return (
      // 재개한 답도 채팅 말풍선처럼 마크다운으로 그린다 (D61)
      <div className="rounded-card border border-line bg-surface-strong px-3 py-2 text-sm" data-testid={testId}>
        <Markdown text={item.text} streaming={item.streaming} testId={testId} />
      </div>
    );
  if (item.kind === "tool_call")
    return (
      <p className="text-xs text-muted" data-testid={testId}>
        {text.tool} <span className="font-mono text-fg">{item.call.name}</span> {item.result ? `→ ${item.result.status}: ${item.result.content}` : text.running}
      </p>
    );
  if (item.kind === "done")
    return (
      <p className="text-xs text-muted" data-testid={testId}>
        {text.turnDone(item.turn, tokensOf(item.usage))}
      </p>
    );
  if (item.kind === "error")
    return (
      // 스트림이 실은 코드도 errorText 표를 지난다. 채팅 전사(ChatItemView)와 같은 말이다 (D43 첫째)
      <Alert variant="error" data-testid={testId}>
        {describeCode(t, item.code, item.message).text}
      </Alert>
    );
  if (item.kind === "interrupt")
    return (
      <Alert variant="warn" data-testid={testId}>
        {text.interrupted}
      </Alert>
    );
  return null;
}
