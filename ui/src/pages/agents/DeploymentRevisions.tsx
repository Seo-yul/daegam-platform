/**
 * 배포 탭의 리비전 표, 되돌리기 대화상자, 배포 기록 카드 (P45 6절, 14절, D109).
 *
 * 리비전 표는 리비전 목록(`GET /agents/{id}/revisions`)의 내용 지문으로 배포된 내용과 견준다. 배포된 내용도 지금 리비전의 내용도
 * 아닌 리비전에 "이 리비전으로 되돌리기" 가 선다(`lib/evaluation.ts` `canRollBackTo`). 되돌리기는 그 정의를 새 리비전으로 저장하고
 * 재배포하므로 `agent:write` 와 `agent:deploy` 가 함께 있어야 하고, 배포됐거나 실패한 Agent 에서만 된다.
 *
 * 되돌리기 대화상자는 되돌릴 리비전과 새 리비전의 번호, 그 내용이 마지막으로 배포된 기록 줄의 시각과 이미지를 보이고, 배포가 오늘의
 * 런타임 이미지와 provider 설정, Tool 목록을 따른다고 말한다. 배포 게이트가 걸리는 내용이면 처음부터 평가 없이 배포하는 칸을 낸다.
 * 되돌릴 정의가 지금 플랫폼에서 검증되지 않는 422 `invalid_graph` 는 그 리비전을 정의 탭에서 고치라는 말로 보인다.
 *
 * 배포 기록은 새것부터 보인다. 게이트가 받아들인 배포의 통과율은 지금 게이트 세트의 실행 목록에서 찾고, 승인 권한이 없거나 그
 * 실행을 찾지 못하면 통과했다는 말만 한다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { listAgentRevisions, listEvaluationRuns, rollbackAgent } from "@/api/agentEvaluation";
import { ApiError } from "@/api/client";
import type { Agent, DeployRecord } from "@/api/types";
import { EvaluationSkip, NO_SKIP, skipBody, skipIncomplete, type SkipState } from "@/components/agents/EvaluationSkip";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Modal } from "@/components/ui/modal";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { WarningBody } from "@/components/WarningDetail";
import { type Messages, useT } from "@/i18n";
import { canRollBackTo, digestsOf, gateApplies, lastDeployedRow, passRate } from "@/lib/evaluation";
import { agentEvaluationKey, agentRevisionsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate, ownValue } from "@/lib/utils";
import { describeWarning } from "@/lib/warningText";

const REVISION_COLUMNS = 2;
const HISTORY_COLUMNS = 7;
/** 되돌릴 정의가 지금 플랫폼에서 검증되지 않는다 (6절). 아무것도 바꾸지 않았다. */
const INVALID_GRAPH = "invalid_graph";

/** 리비전 표. canRollBack 은 되돌릴 권한(`agent:write`·`agent:deploy`)과 상태(배포됐거나 실패)가 모두 맞는가다. */
export function RevisionsCard({ agent, canRollBack }: { agent: Agent; canRollBack: boolean }) {
  const t = useT();
  const text = t.agents.deployment;
  const tenant = useTenant();
  const revisions = useQuery({ queryKey: agentRevisionsKey(tenant, agent.id), queryFn: () => listAgentRevisions(agent.id) });
  const digests = useMemo(() => digestsOf(revisions.data), [revisions.data]);
  const [target, setTarget] = useState<number | null>(null);
  // 되돌린 응답의 경고(배포 게이트가 꺼졌다 등)는 다음 상태 읽기에 없으므로 여기서 보인다
  const [warnings, setWarnings] = useState<string[]>([]);
  const numbers = [...agent.revisions].sort((a, b) => b - a);
  return (
    <Card data-testid="revisions-card">
      <CardHeader>
        <CardTitle>{t.agents.revision}</CardTitle>
      </CardHeader>
      <ErrorMessage error={revisions.error} testId="revisions-error" />
      {warnings.map((w) => (
        <Alert key={w} variant="warn" className="mx-4 mb-2" data-testid="rollback-warning">
          <WarningBody d={describeWarning(t, w)} />
        </Alert>
      ))}
      <Table data-testid="revisions-table">
        <THead>
          <tr>
            <TH>{t.agents.revision}</TH>
            <TH>
              <span className="sr-only">{text.rollback.action}</span>
            </TH>
          </tr>
        </THead>
        <TBody>
          {numbers.length ? (
            numbers.map((n) => (
              <TR key={n} data-testid={`revision-row-${n}`}>
                <TD>
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-xs">r{n}</span>
                    {n === agent.revision ? <Badge variant="muted">{text.revisions.current}</Badge> : null}
                    {n === agent.deployed_revision ? <Badge variant="ok">{text.revisions.deployed}</Badge> : null}
                  </span>
                </TD>
                <TD className="text-right">
                  {canRollBack && canRollBackTo(agent, digests, n) ? (
                    <Button size="sm" variant="outline" data-testid={`revision-rollback-${n}`} onClick={() => setTarget(n)}>
                      {text.rollback.action}
                    </Button>
                  ) : null}
                </TD>
              </TR>
            ))
          ) : (
            <EmptyRow colSpan={REVISION_COLUMNS}>{t.common.loading}</EmptyRow>
          )}
        </TBody>
      </Table>
      {target !== null ? (
        <RollbackDialog
          agent={agent}
          revision={target}
          digests={digests}
          onClose={() => setTarget(null)}
          onDone={(next) => {
            setTarget(null);
            setWarnings(next);
          }}
        />
      ) : null}
    </Card>
  );
}

/** 되돌리기 대화상자. 성공하면 Agent 를 다시 읽고 응답의 경고를 넘긴다. */
function RollbackDialog({
  agent,
  revision,
  digests,
  onClose,
  onDone,
}: {
  agent: Agent;
  revision: number;
  digests: ReadonlyMap<number, string>;
  onClose: () => void;
  onDone: (warnings: string[]) => void;
}) {
  const t = useT();
  const text = t.agents.deployment.rollback;
  const queryClient = useQueryClient();
  const [skip, setSkip] = useState<SkipState>(NO_SKIP);
  useEffect(() => setSkip(NO_SKIP), [revision]);
  const digest = digests.get(revision);
  const last = lastDeployedRow(agent.deploy_history, digest);
  const applies = gateApplies(agent, digests, digest);
  const rollback = useMutation({
    mutationFn: () => rollbackAgent(agent.id, { revision, ...skipBody(skip) }),
    onSuccess: async (done) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      onDone(done.warnings ?? []);
    },
  });
  const pending = rollback.isPending;
  return (
    <Modal open title={text.title(revision)} testId="rollback-dialog" closeTestId="rollback-close" onClose={() => (pending ? undefined : onClose())}>
      <div className="flex flex-col gap-3 px-5 py-4">
        <p className="text-sm text-muted" data-testid="rollback-body">
          {text.body(revision, agent.revision + 1)}
        </p>
        {last ? (
          <p className="text-sm" data-testid="rollback-last-deployed">
            {text.lastDeployed(formatDate(t, last.at), last.image ?? "-")}
          </p>
        ) : null}
        <EvaluationSkip agent={agent} error={rollback.error} applies={applies} value={skip} onChange={setSkip} onLeave={onClose} testId="rollback-skip" />
        {rollback.error instanceof ApiError && rollback.error.code === INVALID_GRAPH ? (
          <Alert variant="error" data-testid="rollback-error">
            {text.invalidGraph(revision)}
          </Alert>
        ) : (
          <ErrorMessage error={rollback.error} testId="rollback-error" />
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" data-testid="rollback-cancel" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button data-testid="rollback-submit" disabled={pending || skipIncomplete(skip)} onClick={() => rollback.mutate()}>
            {pending ? text.submitting : text.submit}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/** 배포 기록 카드. canReadRuns 는 실행 목록을 읽을 권한(`agent:approve`)이다. */
export function DeployHistoryCard({ agent, canReadRuns }: { agent: Agent; canReadRuns: boolean }) {
  const t = useT();
  const text = t.agents.deployment.history;
  const tenant = useTenant();
  const userLabel = useUserLabel();
  const gateSet = agent.evaluation_gate?.set_id ?? null;
  const admitted = agent.deploy_history.some((r) => r.evaluation_run);
  // 게이트가 없으면 읽지 않는다. 그때의 키는 세트 목록의 앞머리와 겹치지 않는 자리다
  const runs = useQuery({
    queryKey: gateSet ? agentEvaluationKey(tenant, agent.id, gateSet, "runs") : [...agentEvaluationKey(tenant, agent.id), "no-gate"],
    queryFn: () => listEvaluationRuns(agent.id, gateSet as string),
    enabled: canReadRuns && admitted && gateSet !== null,
  });
  const rates = new Map((runs.data?.items ?? []).map((r) => [r.id, passRate(r)]));
  const rows = [...agent.deploy_history].reverse();
  return (
    <Card data-testid="deploy-history">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <Table data-testid="deploy-history-table">
        <THead>
          <tr>
            <TH>{text.columns.time}</TH>
            <TH>{text.columns.action}</TH>
            <TH>{t.agents.revision}</TH>
            <TH>{text.columns.image}</TH>
            <TH>{text.columns.by}</TH>
            <TH>{text.columns.outcome}</TH>
            <TH>{text.columns.evaluation}</TH>
          </tr>
        </THead>
        <TBody>
          {rows.length ? (
            rows.map((r) => (
              <TR key={r.id} data-testid={`deploy-history-${r.id}`}>
                <TD className="text-xs text-muted">{formatDate(t, r.at)}</TD>
                <TD>{actionLabel(t.agents.deployment.history.actions, r)}</TD>
                <TD className="font-mono text-xs">r{r.revision}</TD>
                <TD className="max-w-56 truncate font-mono text-xs" title={r.image ?? undefined}>
                  {r.image ?? "-"}
                </TD>
                <TD className="text-xs">{userLabel(r.by) || "-"}</TD>
                <TD>
                  <Badge variant={r.outcome === "deployed" ? "ok" : r.outcome === "failed" ? "error" : "muted"}>{ownValue(text.outcomes, r.outcome) ?? r.outcome}</Badge>
                </TD>
                <TD className="max-w-72 text-xs break-words" data-testid={`deploy-history-${r.id}-evaluation`}>
                  {evaluationLabel(text, r, rates)}
                </TD>
              </TR>
            ))
          ) : (
            <EmptyRow colSpan={HISTORY_COLUMNS}>{text.empty}</EmptyRow>
          )}
        </TBody>
      </Table>
    </Card>
  );
}

type HistoryText = Messages["agents"]["deployment"]["history"];

/** 동작의 이름. 되돌리기는 내용을 가져온 리비전을 말한다. 모르는 동작은 그대로다. */
function actionLabel(actions: HistoryText["actions"], r: DeployRecord): string {
  if (r.action === "rollback") return actions.rollback(r.restored_from ?? r.revision);
  if (r.action === "deploy" || r.action === "redeploy") return actions[r.action];
  return r.action;
}

/** 평가 칸. 게이트가 받아들였으면 그 실행의 통과율, 건너뛰었으면 그 까닭이다. */
function evaluationLabel(text: HistoryText, r: DeployRecord, rates: ReadonlyMap<string, number | null>): string {
  if (r.skipped_reason) return text.skipped(r.skipped_reason);
  if (!r.evaluation_run) return "-";
  const rate = rates.get(r.evaluation_run);
  return rate === undefined || rate === null ? text.passedUnknown : text.passed(rate);
}
