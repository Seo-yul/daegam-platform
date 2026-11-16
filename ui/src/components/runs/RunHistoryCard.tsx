/**
 * 자동 실행 탭의 실행 기록 카드 (P41 1·11절). 출처, 상태, 까닭, 시작, 걸린 시간, 원장의 토큰, 실행 이름을 보인다. 대화 보기와
 * 중단은 그 실행의 묶인 사람과 agent:approve 만이고, 대화는 세션 탭의 상세(읽기 전용)로 연다. 세션 탭은 턴을 받는 Agent 에만
 * 있어 그때만 대화 보기가 선다. 지금 실행은 agent:write 와 agent:chat 이 있고 Agent 가 턴을 받을 때 메시지 상자를 연다. 움직이는
 * 실행이 있는 동안 승인함과 같은 간격으로 목록을 다시 읽는다.
 */
import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router";

import { ApiError } from "@/api/client";
import { APPROVAL_POLL_MS } from "@/components/ApprovalInbox";
import { cancelAgentRun, listAgentRuns, runAgentNow } from "@/api/resources";
import type { AgentRun } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { StateRow } from "@/components/StateBlock";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { idpConfirmDays, useAuthState } from "@/lib/auth";
import { agentRunsKey } from "@/lib/queryKeys";
import { OPEN_RUN_STATUSES, actsAsText, canFollowRun, runReasonText, runSeconds, runSourceText, runStatusText, runStatusVariant } from "@/lib/runs";
import { useTenant } from "@/lib/tenant";
import { tokensOf } from "@/lib/usage";
import { formatDate, formatDuration, formatNumber } from "@/lib/utils";

import { type RunsCardProps, useRunAction } from "./shared";

const COLUMNS = 8;

/** 실행의 대화를 세션 탭의 상세로 연다(SessionsTab 의 ?session=). */
export function runConversationSearch(sessionId: string): string {
  return new URLSearchParams({ tab: "sessions", session: sessionId }).toString();
}

export function RunHistoryCard({ agentId, canWrite, live }: RunsCardProps) {
  const t = useT();
  const text = t.runs.history;
  const tenant = useTenant();
  const { me } = useAuthState();
  const [composing, setComposing] = useState(false);
  const q = useQuery({
    queryKey: agentRunsKey(tenant, agentId, "history"),
    queryFn: () => listAgentRuns(agentId),
    refetchInterval: (query) => (query.state.data?.some((r) => OPEN_RUN_STATUSES.has(r.status)) ? APPROVAL_POLL_MS : false),
  });
  const stop = useRunAction(agentId, (runId: string) => cancelAgentRun(agentId, runId));
  const rows = q.data ?? [];

  return (
    <Card data-testid="run-history">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {canWrite && live ? (
          <Button size="sm" className="ml-auto" data-testid="run-now" onClick={() => setComposing(true)}>
            {text.runNow}
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ErrorMessage error={stop.error} testId="run-stop-error" />
        <Table data-testid="runs-table">
          <THead>
            <tr>
              <TH>{text.columns.source}</TH>
              <TH>{t.account.tokenTable.columns.status}</TH>
              <TH>{text.columns.reason}</TH>
              <TH>{text.columns.started}</TH>
              <TH>{text.columns.duration}</TH>
              <TH>{text.columns.tokens}</TH>
              <TH>{text.columns.actsAs}</TH>
              <TH />
            </tr>
          </THead>
          <TBody>
            {rows.map((run) => (
              <RunRow key={run.id} run={run} follow={canFollowRun(run, me, tenant)} live={live} stopping={stop.isPending} onStop={() => stop.mutate(run.id)} />
            ))}
            {!rows.length ? <StateRow colSpan={COLUMNS} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} /> : null}
          </TBody>
        </Table>
      </CardContent>
      {composing ? <RunNowDialog agentId={agentId} onClose={() => setComposing(false)} /> : null}
    </Card>
  );
}

function RunRow({ run, follow, live, stopping, onStop }: { run: AgentRun; follow: boolean; live: boolean; stopping: boolean; onStop: () => void }) {
  const t = useT();
  const text = t.runs.history;
  const auth = useAuthState();
  const reason = runReasonText(t, run.reason, idpConfirmDays(auth.config));
  const seconds = runSeconds(run);
  // 건너뛴 줄은 시작이 없다. 마지막으로 건너뛴 때를 보인다
  const started = run.status === "skipped" ? run.last_at : run.started_at;
  return (
    <TR data-testid={`run-${run.id}`}>
      <TD className="text-xs">{runSourceText(t, run.source)}</TD>
      <TD>
        <Badge variant={runStatusVariant(run.status)} data-testid={`run-${run.id}-status`}>
          {runStatusText(t, run)}
        </Badge>
      </TD>
      <TD className="max-w-64 text-xs" data-testid={`run-${run.id}-reason`}>
        {reason ? <span className={reason.code ? "font-mono" : undefined}>{reason.text}</span> : null}
      </TD>
      <TD className="text-xs whitespace-nowrap">{started ? formatDate(t, started) : "-"}</TD>
      <TD className="text-xs whitespace-nowrap">{seconds === null ? "-" : formatDuration(t, seconds)}</TD>
      <TD className="text-xs" data-testid={`run-${run.id}-tokens`}>
        {run.usage ? formatNumber(t, tokensOf(run.usage)) : "-"}
      </TD>
      <TD className="text-xs">{actsAsText(t, run.acts_as)}</TD>
      <TD className="text-right whitespace-nowrap">
        <span className="inline-flex gap-1">
          {follow && live && run.session_id ? (
            <Link to={{ search: runConversationSearch(run.session_id) }} className={buttonVariants({ variant: "ghost", size: "sm" })} data-testid={`run-${run.id}-conversation`}>
              {text.openConversation}
            </Link>
          ) : null}
          {follow && OPEN_RUN_STATUSES.has(run.status) ? (
            <Button size="sm" variant="outline" data-testid={`run-${run.id}-stop`} disabled={stopping} onClick={onStop}>
              {t.shared.chatPanel.stop}
            </Button>
          ) : null}
        </span>
      </TD>
    </TR>
  );
}

/** 지금 실행의 메시지 상자. 상한의 429 는 실행 한도 문장으로 보인다. */
function RunNowDialog({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const t = useT();
  const text = t.runs.history;
  const [message, setMessage] = useState("");
  const send = useRunAction(agentId, (body: string) => runAgentNow(agentId, body), onClose);
  const limited = send.error instanceof ApiError && send.error.code === "agent_run_limit";

  function submit(e: FormEvent) {
    e.preventDefault();
    if (message.trim() && !send.isPending) send.mutate(message);
  }

  return (
    <Modal open title={text.runNow} testId="run-now-dialog" onClose={onClose}>
      <form className="flex flex-col gap-3 p-5" onSubmit={submit}>
        <Textarea aria-label={text.runNow} data-testid="run-now-message" rows={5} placeholder={text.runNowPlaceholder} value={message} onChange={(e) => setMessage(e.target.value)} />
        {limited ? (
          <Alert variant="error" data-testid="run-now-error">
            {t.runs.reason.agentRunLimit}
          </Alert>
        ) : (
          <ErrorMessage error={send.error} testId="run-now-error" />
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="run-now-send" disabled={!message.trim() || send.isPending}>
            {t.chat.composer.send}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
