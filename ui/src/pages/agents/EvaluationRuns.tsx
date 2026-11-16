/**
 * 평가 세트 하나의 실행 (P45 9절, 11절, 14절, D109). 실행 목록, 시작 창, 실행 하나의 결과 창이다.
 *
 * 시작 창은 실행이 무엇을 하는지(미리 보기 파드, 읽기 Tool 은 시작한 사람의 권한, 쓰기 Tool 은 부르지 않는다)를 말하고, 시작한
 * 사람이 마지막으로 시작한 뒤에 다른 사람이 쓰거나 고친 사례를 늘어놓는다. 그 사례는 시작한 사람의 권한으로 돌기 때문이다. 시작은
 * 그 사람이 본 세트의 지문을 싣고, 그 사이 세트가 바뀌었으면 서버가 409 `evaluation_set_changed` 로 막는다.
 *
 * 결과 창은 판정(사례와 반복마다 확인 항목과 채점 기준의 통과)과 통과율, 배포된 내용의 기준선보다 나빠진 사례를 모두에게 보인다.
 * 실행 하나의 읽기는 감사에 남으므로(`audit/middleware.py` `AUDITED_READ_PATHS`) 결과 창은 그것을 되풀이해 읽지 않는다. 감사에
 * 남지 않는 목록이 도는 동안 짧게 다시 읽고, 결과 창은 목록에서 그 실행의 상태나 진행이 바뀔 때만 다시 읽는다.
 * 답변과 Tool 호출, 기준의 모자란 점은 시작한 사람만 받는다. 다른 사람에게는 그 사람만 볼 수 있다는 말이 선다. 게이트가 채점 기준을
 * 세지 않는 동안 기준의 판단은 참고용이라고 적는다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { cancelEvaluationRun, getEvaluationRun, listEvaluationRuns, startEvaluationRun } from "@/api/agentEvaluation";
import { ApiError } from "@/api/client";
import type { Agent, EvaluationCase, EvaluationResult, EvaluationReview, EvaluationRun, EvaluationRunDetail, EvaluationSet, EvaluationVerdict } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import { useAuthState } from "@/lib/auth";
import { errorText } from "@/lib/errorText";
import { RUN_POLL_MS, activeRun, caseLabel, checksPassed, criteriaPassed, passRate } from "@/lib/evaluation";
import { agentEvaluationKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate, ownValue } from "@/lib/utils";

const RUN_COLUMNS = 6;
const VERDICT_COLUMNS = 4;

/** 시작이 본 세트의 지문이 지금 세트와 다르다(9절). */
const SET_CHANGED = "evaluation_set_changed";

/** 실행의 상태 이름. 모르는 값은 그대로다. */
function stateName(t: Messages, state: string): string {
  return ownValue(t.agents.evaluation.run.states, state) ?? state;
}

/** 실행 상태의 배지. 목록과 결과 창이 같은 색을 쓴다. */
function RunStateBadge({ state, testId }: { state: string; testId?: string }) {
  const t = useT();
  const variant = state === "finished" ? "ok" : state === "error" ? "error" : activeRun(state) ? "info" : "muted";
  return (
    <Badge variant={variant} data-testid={testId}>
      {stateName(t, state)}
    </Badge>
  );
}

/** 도는 실행의 진행. 채점 중이면 채점을 마친 판정의 수이고, 그 전에는 돈 사례와 반복의 수다. */
function progressLine(t: Messages, run: EvaluationRun): string {
  const text = t.agents.evaluation.run;
  if (run.state === "grading") return text.gradingProgress(run.graded_steps, run.verdicts.filter((v) => v.criteria > 0 && v.state !== "changed").length);
  return text.progress(run.done_steps, run.cases * run.repeats);
}

/** 실행이 오류나 취소로 끝난 까닭의 문장. 실행의 표에 없으면 오류 사전에서, 거기도 없으면 코드 그대로다. */
export function runReason(t: Messages, reason: string): string {
  return ownValue(t.agents.evaluation.run.errors, reason) ?? errorText(t, reason) ?? reason;
}

/**
 * 세트의 실행 목록과 시작 단추. canDeploy 는 시작과 취소의 권한(`agent:deploy`)이다. startRevision 을 주면(배포 대화상자의
 * "배포된 리비전 평가 실행") 그 리비전으로 시작 창을 연 채 그린다.
 */
export function RunsCard({
  agent,
  set,
  cases,
  canDeploy,
  startRevision,
  onStartClosed,
}: {
  agent: Agent;
  set: EvaluationSet;
  cases: readonly EvaluationCase[];
  canDeploy: boolean;
  startRevision?: number | null;
  onStartClosed?: () => void;
}) {
  const t = useT();
  const text = t.agents.evaluation.run;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: agentEvaluationKey(tenant, agent.id, set.id, "runs"),
    queryFn: () => listEvaluationRuns(agent.id, set.id),
    refetchInterval: (q) => (q.state.data?.items.some((r) => activeRun(r.state)) ? RUN_POLL_MS : false),
  });
  const [starting, setStarting] = useState<number | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const cancel = useMutation({
    mutationFn: (runId: string) => cancelEvaluationRun(agent.id, set.id, runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agent.id, set.id) }),
  });
  const startAt = starting ?? startRevision ?? null;
  const items = runs.data?.items ?? [];
  const busy = items.some((r) => activeRun(r.state));
  return (
    <Card data-testid="evaluation-runs">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {canDeploy ? (
          <Button size="sm" className="ml-auto" data-testid="evaluation-run-start" disabled={!runs.data || busy || !cases.length} onClick={() => setStarting(agent.revision)}>
            {text.start(agent.revision)}
          </Button>
        ) : null}
      </CardHeader>
      <ErrorMessage error={runs.error ?? cancel.error} testId="evaluation-runs-error" />
      <Table data-testid="evaluation-runs-table">
        <THead>
          <tr>
            <TH>{text.columns.started}</TH>
            <TH>{t.agents.revision}</TH>
            <TH>{text.columns.by}</TH>
            <TH>{text.columns.state}</TH>
            <TH>{text.columns.result}</TH>
            <TH>
              <span className="sr-only">{text.open}</span>
            </TH>
          </tr>
        </THead>
        <TBody>
          {runs.isLoading ? (
            <EmptyRow colSpan={RUN_COLUMNS}>{t.common.loading}</EmptyRow>
          ) : items.length ? (
            items.map((r) => (
              <TR key={r.id} data-testid={`evaluation-run-${r.id}`}>
                <TD className="text-xs text-muted">{formatDate(t, r.created_at)}</TD>
                <TD className="font-mono text-xs">r{r.revision}</TD>
                <TD className="text-xs">{r.started_by_name}</TD>
                <TD>
                  <RunStateBadge state={r.state} />
                </TD>
                <TD className="max-w-96 text-xs break-words" data-testid={`evaluation-run-${r.id}-result`}>
                  <RunResultLine run={r} />
                </TD>
                <TD className="text-right whitespace-nowrap">
                  <Button size="sm" variant="ghost" data-testid={`evaluation-run-${r.id}-open`} onClick={() => setOpen(r.id)}>
                    {text.open}
                  </Button>
                  {canDeploy && activeRun(r.state) ? (
                    <Button size="sm" variant="outline" data-testid={`evaluation-run-${r.id}-cancel`} disabled={cancel.isPending} onClick={() => cancel.mutate(r.id)}>
                      {text.cancel}
                    </Button>
                  ) : null}
                </TD>
              </TR>
            ))
          ) : (
            <EmptyRow colSpan={RUN_COLUMNS}>{text.empty}</EmptyRow>
          )}
        </TBody>
      </Table>
      {startAt !== null && runs.data ? (
        <StartRunDialog
          agent={agent}
          set={set}
          cases={cases}
          review={runs.data.review}
          initialRevision={startAt}
          onClose={() => {
            setStarting(null);
            onStartClosed?.();
          }}
        />
      ) : null}
      {open ? <RunDetailDialog agentId={agent.id} set={set} runId={open} row={items.find((r) => r.id === open)} cases={cases} onClose={() => setOpen(null)} /> : null}
    </Card>
  );
}

/** 목록의 결과 칸. 끝난 실행은 통과율, 도는 실행은 진행, 오류는 그 까닭, 취소는 취소한 사람이다. */
function RunResultLine({ run }: { run: EvaluationRun }) {
  const t = useT();
  const text = t.agents.evaluation.run;
  const userLabel = useUserLabel();
  if (activeRun(run.state)) return <>{progressLine(t, run)}</>;
  if (run.state === "finished") {
    const rate = passRate(run);
    return rate === null ? <>-</> : <>{text.passRate(run.totals.counted, run.totals.passed, rate)}</>;
  }
  if (run.state === "cancelled" && run.cancelled_by) return <>{text.cancelledBy(userLabel(run.cancelled_by))}</>;
  return run.reason ? <>{runReason(t, run.reason)}</> : <>-</>;
}

/**
 * 시작 창. 리비전(기본은 저장한 최신 것)을 고르고, 다른 사람이 쓰거나 고친 사례를 확인한 뒤 시작한다. header 모드면 턴이 Agent 의
 * 권한으로 돈다고 말한다.
 */
function StartRunDialog({
  agent,
  set,
  cases,
  review,
  initialRevision,
  onClose,
}: {
  agent: Agent;
  set: EvaluationSet;
  cases: readonly EvaluationCase[];
  review: EvaluationReview;
  initialRevision: number;
  onClose: () => void;
}) {
  const t = useT();
  const text = t.agents.evaluation.run;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const headerMode = useAuthState().mode === "header";
  const [revision, setRevision] = useState(agent.revisions.includes(initialRevision) ? initialRevision : agent.revision);
  const start = useMutation({
    mutationFn: () => startEvaluationRun(agent.id, set.id, review.set_digest, revision),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agent.id, set.id) });
      onClose();
    },
    // 세트가 그 사이 바뀌었으면 사례와 확인할 사례, 지문을 다시 읽는다. 그대로 두면 다시 눌러도 옛 지문으로 거절된다
    onError: (e) => {
      if (e instanceof ApiError && e.code === SET_CHANGED) void queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agent.id, set.id) });
    },
  });
  const changed = cases.filter((c) => review.cases.includes(c.id));
  const pending = start.isPending;
  return (
    <Modal open title={text.start(revision)} testId="evaluation-start-dialog" closeTestId="evaluation-start-close" onClose={() => (pending ? undefined : onClose())}>
      <div className="flex flex-col gap-3 px-5 py-4">
        <p className="text-sm text-muted">{text.note}</p>
        {headerMode ? (
          <Alert variant="info" data-testid="evaluation-start-header-mode">
            {text.headerMode}
          </Alert>
        ) : null}
        <Field label={t.agents.revision} htmlFor="evaluation-start-revision">
          <Select id="evaluation-start-revision" data-testid="evaluation-start-revision" value={String(revision)} onChange={(e) => setRevision(Number(e.target.value))}>
            {[...agent.revisions].reverse().map((n) => (
              <option key={n} value={n}>
                {t.agents.canvasHeader.revisionOption(n, n === agent.revision)}
              </option>
            ))}
          </Select>
        </Field>
        {changed.length ? (
          <div className="flex flex-col gap-1" data-testid="evaluation-start-review">
            <Alert variant="warn">{text.reviewChanged(changed.length)}</Alert>
            <ul className="flex flex-col gap-0.5 text-sm">
              {changed.map((c) => (
                <li key={c.id} className="truncate" data-testid={`evaluation-start-review-${c.id}`}>
                  {caseLabel(c.content, c.id)}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <ErrorMessage error={start.error} testId="evaluation-start-error" />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button data-testid="evaluation-start-submit" disabled={pending} onClick={() => start.mutate()}>
            {text.start(revision)}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * 실행 하나의 결과 창. 실행 하나의 읽기는 감사에 남으므로 스스로 되풀이해 읽지 않고, 목록의 줄(row)에서 상태나 진행이 바뀔 때만
 * 다시 읽는다. 목록은 도는 실행이 있는 동안 `RUN_POLL_MS` 마다 다시 읽힌다.
 */
function RunDetailDialog({
  agentId,
  set,
  runId,
  row,
  cases,
  onClose,
}: {
  agentId: string;
  set: EvaluationSet;
  runId: string;
  row: EvaluationRun | undefined;
  cases: readonly EvaluationCase[];
  onClose: () => void;
}) {
  const t = useT();
  const text = t.agents.evaluation.run;
  const tenant = useTenant();
  const detail = useQuery({
    queryKey: agentEvaluationKey(tenant, agentId, set.id, "run", runId),
    queryFn: () => getEvaluationRun(agentId, set.id, runId),
  });
  const seen = row ? `${row.state}:${row.done_steps}:${row.graded_steps}` : "";
  const first = useRef(seen);
  const { refetch } = detail;
  useEffect(() => {
    if (seen === first.current) return;
    first.current = seen;
    void refetch();
  }, [seen, refetch]);
  const gate = queryGate(detail, { testId: "evaluation-run-error" });
  const run = detail.data;
  return (
    <Modal open size="lg" title={run ? text.detailTitle(run.revision) : text.open} testId="evaluation-run-dialog" closeTestId="evaluation-run-close" onClose={onClose}>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">{gate ?? (run ? <RunDetailBody run={run} cases={cases} /> : null)}</div>
    </Modal>
  );
}

function RunDetailBody({ run, cases }: { run: EvaluationRunDetail; cases: readonly EvaluationCase[] }) {
  const t = useT();
  const text = t.agents.evaluation;
  const userLabel = useUserLabel();
  const byId = new Map(cases.map((c) => [c.id, c]));
  const label = (id: string) => caseLabel(byId.get(id)?.content, text.run.deletedCase);
  const worse = new Set(run.baseline?.worse ?? []);
  const rate = passRate(run);
  const graded = run.verdicts.some((v) => v.criteria > 0);
  const ran = run.verdicts.some((v) => v.state !== "changed");
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <RunStateBadge state={run.state} testId="evaluation-run-state" />
        <span className="text-muted">{run.started_by_name}</span>
        <span className="text-xs text-muted">{formatDate(t, run.created_at)}</span>
      </div>
      {run.header_mode ? (
        <Alert variant="info" data-testid="evaluation-run-header-mode">
          {text.run.headerMode}
        </Alert>
      ) : null}
      {run.state === "cancelled" && run.cancelled_by ? <p className="text-sm">{text.run.cancelledBy(userLabel(run.cancelled_by))}</p> : null}
      {run.reason && run.state !== "finished" ? (
        <Alert variant={run.state === "error" ? "error" : "warn"} data-testid="evaluation-run-reason">
          {runReason(t, run.reason)}
        </Alert>
      ) : null}
      {activeRun(run.state) ? <p className="text-sm text-muted">{progressLine(t, run)}</p> : null}
      {run.state === "finished" && rate !== null ? (
        <p className="text-sm font-medium text-heading" data-testid="evaluation-run-summary">
          {text.run.summary(run.totals.counted, run.totals.passed, rate, worse.size)}
        </p>
      ) : null}
      {run.baseline?.missing.length ? (
        <p className="text-xs text-muted" data-testid="evaluation-run-no-baseline">
          {text.run.noBaseline(run.baseline.missing.length)}
        </p>
      ) : null}
      {graded ? (
        <p className="text-xs text-muted" data-testid="evaluation-criteria-advisory">
          {text.criteriaAdvisory}
        </p>
      ) : null}
      <Table data-testid="evaluation-verdicts">
        <THead>
          <tr>
            <TH>{text.case.columns.question}</TH>
            <TH>{text.run.repeat}</TH>
            <TH>{text.case.checks}</TH>
            <TH>{text.case.criteria}</TH>
          </tr>
        </THead>
        <TBody>
          {run.verdicts.length ? (
            run.verdicts.map((v) => <VerdictRow key={`${v.case_id}-${v.repeat}`} verdict={v} label={label(v.case_id)} worse={worse.has(v.case_id)} />)
          ) : (
            <EmptyRow colSpan={VERDICT_COLUMNS}>-</EmptyRow>
          )}
        </TBody>
      </Table>
      {run.content_hidden && ran ? (
        <p className="text-sm text-muted" data-testid="evaluation-content-hidden">
          {text.run.contentHidden(run.started_by_name)}
        </p>
      ) : null}
      {!run.content_hidden ? (
        <div className="flex flex-col gap-2" data-testid="evaluation-results">
          {run.results.map((r) => (
            <ResultDetails key={r.id} result={r} label={label(r.case_id)} />
          ))}
        </div>
      ) : null}
    </>
  );
}

/** 판정 한 줄. 도는 동안 바뀐 사례는 그 까닭이 칸을 채운다. */
function VerdictRow({ verdict: v, label, worse }: { verdict: EvaluationVerdict; label: string; worse: boolean }) {
  const t = useT();
  const text = t.agents.evaluation;
  return (
    <TR data-testid={`evaluation-verdict-${v.case_id}-${v.repeat}`}>
      <TD className="max-w-80 truncate text-sm" title={label}>
        <span className="flex items-center gap-1.5">
          <span className="truncate">{label}</span>
          {worse ? (
            <Badge variant="warn" data-testid={`evaluation-worse-${v.case_id}`}>
              {text.run.worse}
            </Badge>
          ) : null}
        </span>
      </TD>
      <TD className="text-xs">{v.repeat + 1}</TD>
      {v.state === "changed" ? (
        <TD colSpan={2} className="text-xs text-muted">
          {text.case.changedDuringRun}
        </TD>
      ) : (
        <>
          <TD>
            <PassBadge passed={checksPassed(v)} />
          </TD>
          <TD>{v.grader_error ? <Badge variant="error">{text.run.graderError}</Badge> : <PassBadge passed={criteriaPassed(v)} />}</TD>
        </>
      )}
    </TR>
  );
}

function PassBadge({ passed }: { passed: boolean | null }) {
  const t = useT();
  const text = t.agents.evaluation.run;
  if (passed === null) return <span className="text-xs text-muted">-</span>;
  return <Badge variant={passed ? "ok" : "error"}>{passed ? text.passed : text.failed}</Badge>;
}

/** 시작한 사람이 보는 결과 하나. 답변, Tool 호출과 그 답한 꼴, 확인 항목과 채점 기준의 판단과 모자란 점이다. */
function ResultDetails({ result: r, label }: { result: EvaluationResult; label: string }) {
  const t = useT();
  const text = t.agents.evaluation;
  const kinds: Readonly<Record<string, string>> = { ...text.case.checkKinds, done: text.case.doneKind };
  return (
    <details className="rounded-card border border-line px-4 py-2" data-testid={`evaluation-result-${r.case_id}-${r.repeat}`}>
      <summary className="cursor-pointer text-sm font-medium text-heading">
        {label} · {text.run.repeat} {r.repeat + 1}
      </summary>
      <div className="mt-2 flex flex-col gap-2 text-sm">
        <div>
          <p className="text-xs font-medium text-muted">{text.run.answer}</p>
          <p className="break-words whitespace-pre-wrap" data-testid={`evaluation-result-${r.case_id}-${r.repeat}-answer`}>
            {r.answer || "-"}
          </p>
        </div>
        {r.tool_calls.length ? (
          <div>
            <p className="text-xs font-medium text-muted">{text.run.toolCalls}</p>
            <ul className="flex flex-col gap-1">
              {r.tool_calls.map((c, i) => (
                <li key={c.id ?? i} className="flex flex-col gap-0.5" data-testid={`evaluation-call-${r.case_id}-${r.repeat}-${i}`}>
                  <span className="flex flex-wrap items-center gap-1.5">
                    <code className="font-mono text-xs">{c.name}</code>
                    <Badge variant={c.answer === "ran" ? "info" : c.answer === "simulated" ? "accent" : "muted"}>{ownValue(text.call, c.answer) ?? (c.answer || "-")}</Badge>
                  </span>
                  {c.args && Object.keys(c.args).length ? <code className="font-mono text-xs break-all text-muted">{JSON.stringify(c.args)}</code> : null}
                  {c.result ? <span className="text-xs break-words whitespace-pre-wrap text-muted">{c.result}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {r.checks.length ? (
          <div>
            <p className="text-xs font-medium text-muted">{text.case.checks}</p>
            <ul className="flex flex-col gap-0.5">
              {r.checks.map((c, i) => (
                <li key={i} className="flex flex-wrap items-center gap-1.5">
                  <PassBadge passed={c.passed} />
                  <span>{ownValue(kinds, c.kind) ?? c.kind}</span>
                  {c.gap ? <span className="text-xs text-muted">{c.gap}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {r.criteria.length ? (
          <div>
            <p className="text-xs font-medium text-muted">{text.case.criteria}</p>
            <ul className="flex flex-col gap-0.5">
              {r.criteria.map((c, i) => (
                <li key={i} className="flex flex-wrap items-center gap-1.5">
                  <PassBadge passed={c.passed} />
                  <span>{c.criterion}</span>
                  {c.gap ? <span className="text-xs text-muted">{c.gap}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </details>
  );
}
