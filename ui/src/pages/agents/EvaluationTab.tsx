/**
 * Agent 상세의 평가 탭 (P45 7절~12절, 14절, D109). 남의 대화를 읽는 승인자(`agent:approve`)만 탭을 본다(Detail 이 가른다).
 *
 * 위에서부터 평가 세트 목록, 고른 세트의 사례, 그 세트의 실행, Agent 의 배포 전 평가 카드다. 세트와 사례를 쓰는 단추는
 * `agent:write` 가, 실행의 시작과 취소, 게이트의 저장은 `agent:deploy` 가 있어야 선다. 서버가 같은 범위를 다시 본다.
 *
 * 배포 대화상자의 "배포된 리비전 평가 실행" 은 주소의 `set`·`revision` 으로 이 탭을 연다. 그 세트를 고르고 그 리비전으로 시작 창을
 * 연 뒤 두 쿼리를 주소에서 지운다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useLocation, useSearchParams } from "react-router";

import { createEvaluationSet, deleteEvaluationCase, deleteEvaluationSet, listEvaluationCases, listEvaluationSets, putEvaluationSet } from "@/api/agentEvaluation";
import { getPlatformSettings } from "@/api/resources";
import type { Agent, EvaluationCase, EvaluationSet, EvaluationSetBody } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { EVALUATION_MAX_CASE_RUNS, MAX_NAME_CHARS, MAX_REPEATS, caseLabel, graderChoices } from "@/lib/evaluation";
import { effectiveModel } from "@/lib/providers";
import { PLATFORM_SETTINGS_KEY, agentEvaluationKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { cn } from "@/lib/utils";

import { CaseEditorDialog, type CaseEditorTarget } from "./EvaluationCaseEditor";
import { EvaluationGateCard } from "./EvaluationGateCard";
import { RunsCard } from "./EvaluationRuns";

const SET_PARAM = "set";
const REVISION_PARAM = "revision";
const CASE_COLUMNS = 5;

export function EvaluationTab({ agent, canWrite, canDeploy }: { agent: Agent; canWrite: boolean; canDeploy: boolean }) {
  const t = useT();
  const text = t.agents.evaluation;
  const tenant = useTenant();
  const linked = useLinkedRun();
  const sets = useQuery({ queryKey: agentEvaluationKey(tenant, agent.id), queryFn: () => listEvaluationSets(agent.id) });
  const [selected, setSelected] = useState<string | null>(linked.set);
  const [editing, setEditing] = useState<EvaluationSet | "new" | null>(null);
  // 주소가 시작 창을 열어도 실행을 시작할 수 없는 사람(agent:deploy 가 없다)에게는 열지 않는다
  const [startRevision, setStartRevision] = useState<number | null>(canDeploy ? linked.revision : null);
  const gate = queryGate(sets, { testId: "evaluation-sets-error" });
  if (gate) return gate;
  const items = sets.data ?? [];
  const current = items.find((s) => s.id === selected) ?? items[0] ?? null;
  return (
    <div className="flex flex-col gap-4" data-testid="evaluation-tab">
      <Card data-testid="evaluation-sets">
        <CardHeader>
          <CardTitle>{text.sets.title}</CardTitle>
          {canWrite ? (
            <Button size="sm" className="ml-auto" data-testid="evaluation-set-create" onClick={() => setEditing("new")}>
              {text.sets.create}
            </Button>
          ) : null}
        </CardHeader>
        <CardContent>
          {items.length ? (
            <ul className="flex flex-wrap gap-2" data-testid="evaluation-set-list">
              {items.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    aria-pressed={s.id === current?.id}
                    data-testid={`evaluation-set-${s.id}`}
                    className={cn("flex flex-col items-start rounded-card border px-3 py-2 text-left text-sm", s.id === current?.id ? "border-accent-line bg-accent-soft" : "border-line hover:bg-hover")}
                    onClick={() => setSelected(s.id)}
                  >
                    <span className="font-medium text-heading">{s.name}</span>
                    <span className="text-xs text-muted">
                      {text.sets.caseCount(s.case_count)} · {text.sets.repeats(s.repeats)} · {s.grader ? `${s.grader.provider} / ${s.grader.model}` : text.sets.noGrader}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted" data-testid="evaluation-sets-empty">
              {text.setsEmpty}
            </p>
          )}
        </CardContent>
      </Card>
      {current ? (
        <SetView
          key={current.id}
          agent={agent}
          set={current}
          canWrite={canWrite}
          canDeploy={canDeploy}
          startRevision={current.id === linked.set ? startRevision : null}
          onStartClosed={() => setStartRevision(null)}
          onEdit={() => setEditing(current)}
        />
      ) : null}
      {items.length ? <EvaluationGateCard agent={agent} sets={items} canEdit={canDeploy} /> : null}
      {editing ? (
        <SetDialog
          agent={agent}
          set={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            setEditing(null);
            setSelected(saved.id);
          }}
        />
      ) : null}
    </div>
  );
}

/** 주소의 세트와 리비전을 처음 한 번 읽고 지운다. 지울 때는 탭 이동처럼 화면이 들고 있는 state 를 남긴다. */
function useLinkedRun(): { set: string | null; revision: number | null } {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const [linked] = useState(() => {
    const revision = Number(params.get(REVISION_PARAM));
    return { set: params.get(SET_PARAM), revision: Number.isInteger(revision) && revision > 0 ? revision : null };
  });
  useEffect(() => {
    if (!params.has(SET_PARAM) && !params.has(REVISION_PARAM)) return;
    const next = new URLSearchParams(params);
    next.delete(SET_PARAM);
    next.delete(REVISION_PARAM);
    setParams(next, { replace: true, state: location.state });
  }, [params, setParams, location.state]);
  return linked;
}

/** 고른 세트의 머리(편집과 삭제), 사례, 실행. */
function SetView({
  agent,
  set,
  canWrite,
  canDeploy,
  startRevision,
  onStartClosed,
  onEdit,
}: {
  agent: Agent;
  set: EvaluationSet;
  canWrite: boolean;
  canDeploy: boolean;
  startRevision: number | null;
  onStartClosed: () => void;
  onEdit: () => void;
}) {
  const t = useT();
  const text = t.agents.evaluation;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const cases = useQuery({ queryKey: agentEvaluationKey(tenant, agent.id, set.id, "cases"), queryFn: () => listEvaluationCases(agent.id, set.id) });
  const [deleting, setDeleting] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteEvaluationSet(agent.id, set.id),
    onSuccess: async () => {
      setDeleting(false);
      await queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agent.id) });
    },
  });
  const items = cases.data?.items ?? [];
  return (
    <>
      <Card data-testid="evaluation-set-view">
        <CardHeader>
          <CardTitle>{set.name}</CardTitle>
          {canWrite ? (
            <span className="ml-auto flex gap-2">
              <Button size="sm" variant="outline" data-testid="evaluation-set-edit" onClick={onEdit}>
                {text.sets.edit}
              </Button>
              <Button size="sm" variant="ghost" data-testid="evaluation-set-delete" onClick={() => setDeleting(true)}>
                {t.common.delete}
              </Button>
            </span>
          ) : null}
        </CardHeader>
        <CasesTable agentId={agent.id} setId={set.id} cases={items} loading={cases.isLoading} error={cases.error} canWrite={canWrite} />
      </Card>
      <RunsCard agent={agent} set={set} cases={items} canDeploy={canDeploy} startRevision={cases.data ? startRevision : null} onStartClosed={onStartClosed} />
      <ConfirmDialog
        open={deleting}
        title={text.set.deleteTitle}
        description={text.set.deleteBody(set.name)}
        confirmLabel={t.common.delete}
        pendingLabel={t.common.deleting}
        pending={remove.isPending}
        error={remove.error ? <ErrorMessage error={remove.error} testId="evaluation-set-delete-error" /> : null}
        onConfirm={() => remove.mutate()}
        onCancel={() => {
          remove.reset();
          setDeleting(false);
        }}
      />
    </>
  );
}

/** 세트의 사례 표. 줄마다 첫 질문, 확인 항목과 채점 기준의 수, 출처, 마지막으로 바꾼 사람과 편집·삭제다. */
function CasesTable({
  agentId,
  setId,
  cases,
  loading,
  error,
  canWrite,
}: {
  agentId: string;
  setId: string;
  cases: readonly EvaluationCase[];
  loading: boolean;
  error: unknown;
  canWrite: boolean;
}) {
  const t = useT();
  const text = t.agents.evaluation.case;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const userLabel = useUserLabel();
  const [target, setTarget] = useState<CaseEditorTarget | null>(null);
  const [deleting, setDeleting] = useState<EvaluationCase | null>(null);
  const remove = useMutation({
    mutationFn: (caseId: string) => deleteEvaluationCase(agentId, setId, caseId),
    onSuccess: async () => {
      setDeleting(null);
      await queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agentId) });
    },
  });
  return (
    <>
      <ErrorMessage error={error} testId="evaluation-cases-error" />
      <Table data-testid="evaluation-cases">
        <THead>
          <tr>
            <TH>{text.columns.question}</TH>
            <TH>{text.checks}</TH>
            <TH>{text.criteria}</TH>
            <TH>{text.columns.origin}</TH>
            <TH>{text.columns.updatedBy}</TH>
          </tr>
        </THead>
        <TBody>
          {loading ? (
            <EmptyRow colSpan={CASE_COLUMNS}>{t.common.loading}</EmptyRow>
          ) : cases.length ? (
            cases.map((c) => (
              <TR key={c.id} data-testid={`evaluation-case-${c.id}`}>
                <TD className="max-w-96 text-sm">
                  <span className="flex items-center gap-2">
                    <span className="truncate" title={caseLabel(c.content, c.id)}>
                      {caseLabel(c.content, c.id)}
                    </span>
                    {canWrite ? (
                      <span className="ml-auto flex shrink-0 gap-1">
                        <Button size="sm" variant="ghost" data-testid={`evaluation-case-${c.id}-edit`} onClick={() => setTarget({ existing: c })}>
                          {text.edit}
                        </Button>
                        <Button size="sm" variant="ghost" data-testid={`evaluation-case-${c.id}-delete`} onClick={() => setDeleting(c)}>
                          {t.common.delete}
                        </Button>
                      </span>
                    ) : null}
                  </span>
                </TD>
                <TD className="text-xs">{(c.content.checks?.length ?? 0) + (c.content.expect_done ? 1 : 0)}</TD>
                <TD className="text-xs">{c.content.criteria?.length ?? 0}</TD>
                <TD>
                  <Badge variant={c.origin ? "info" : "muted"}>{c.origin ? text.copied : text.handWritten}</Badge>
                </TD>
                <TD className="text-xs">{userLabel(c.updated_by)}</TD>
              </TR>
            ))
          ) : (
            <EmptyRow colSpan={CASE_COLUMNS}>{text.empty}</EmptyRow>
          )}
        </TBody>
      </Table>
      {canWrite ? (
        <div className="flex border-t border-line px-4 py-2">
          <Button size="sm" variant="outline" data-testid="evaluation-case-add" onClick={() => setTarget({})}>
            {text.add}
          </Button>
        </div>
      ) : null}
      {target ? <CaseEditorDialog agentId={agentId} setId={setId} target={target} onClose={() => setTarget(null)} /> : null}
      <ConfirmDialog
        open={deleting !== null}
        title={text.deleteTitle}
        description={deleting ? text.deleteBody : null}
        confirmLabel={t.common.delete}
        pendingLabel={t.common.deleting}
        pending={remove.isPending}
        error={remove.error ? <ErrorMessage error={remove.error} /> : null}
        onConfirm={() => deleting && remove.mutate(deleting.id)}
        onCancel={() => {
          remove.reset();
          setDeleting(null);
        }}
      />
    </>
  );
}

/**
 * 세트를 만들거나 고치는 창. 이름, 채점 모델(없음도 된다), 반복 횟수다. 채점 모델은 provider 와 모델 이름 둘이다. provider 는 이
 * Agent 의 지금 리비전이 문서에 싣는 모델 자리의 것만 고른다(`lib/evaluation.ts` `graderChoices`). 모델 이름은 캔버스의 모델 칸처럼
 * 적고, 그 자리의 모델 노드가 정한 모델로 채워 두며, 비워 두면 provider 의 기본 모델이다(Platform Settings 를 읽을 수 있을 때).
 */
function SetDialog({ agent, set, onClose, onSaved }: { agent: Agent; set: EvaluationSet | null; onClose: () => void; onSaved: (saved: EvaluationSet) => void }) {
  const t = useT();
  const text = t.agents.evaluation.set;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: PLATFORM_SETTINGS_KEY, queryFn: getPlatformSettings });
  const choices = graderChoices(agent.graph);
  // 저장된 채점 모델의 provider 가 지금 그래프에 없어도 선택지에 남긴다. 실행의 시작이 provider 를 다시 본다
  if (set?.grader && !choices.some((c) => c.provider === set.grader?.provider)) choices.push({ provider: set.grader.provider, model: set.grader.model });
  const [name, setName] = useState(set?.name ?? "");
  const [provider, setProvider] = useState(set?.grader?.provider ?? "");
  const [model, setModel] = useState(set?.grader?.model ?? "");
  const [repeats, setRepeats] = useState(set?.repeats ?? 1);
  const fallbackModel = effectiveModel(settings.data?.llm_providers.find((p) => p.name === provider)) ?? "";
  const graderModel = model.trim() || fallbackModel;
  const save = useMutation({
    mutationFn: (body: EvaluationSetBody) => (set ? putEvaluationSet(agent.id, set.id, body) : createEvaluationSet(agent.id, body)),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agent.id) });
      onSaved(saved);
    },
  });
  const pending = save.isPending;
  return (
    <Modal open title={set ? text.editTitle : text.createTitle} testId="evaluation-set-dialog" closeTestId="evaluation-set-close" onClose={() => (pending ? undefined : onClose())}>
      <form
        className="flex flex-col gap-3 px-5 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate({ name: name.trim(), grader: provider ? { provider, model: graderModel } : null, repeats });
        }}
      >
        <Field label={text.name} htmlFor="evaluation-set-name" required>
          <Input id="evaluation-set-name" data-testid="evaluation-set-name" value={name} maxLength={MAX_NAME_CHARS} required onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={text.grader} htmlFor="evaluation-set-grader" hint={text.graderHint}>
          <Select
            id="evaluation-set-grader"
            data-testid="evaluation-set-grader"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              // provider 를 바꾸면 그 자리의 모델 노드가 정한 모델로 채운다. 없으면 비워 기본 모델을 쓰게 한다
              setModel(choices.find((c) => c.provider === e.target.value)?.model ?? "");
            }}
          >
            <option value="">{t.agents.evaluation.sets.noGrader}</option>
            {choices.map((c) => (
              <option key={c.provider} value={c.provider}>
                {c.provider}
              </option>
            ))}
          </Select>
        </Field>
        {provider ? (
          <Field label={t.canvas.nodeLabels.model} htmlFor="evaluation-set-model" hint={t.canvas.model.modelHint}>
            <Input id="evaluation-set-model" data-testid="evaluation-set-model" className="font-mono" placeholder={fallbackModel} value={model} onChange={(e) => setModel(e.target.value)} />
          </Field>
        ) : null}
        <Field label={text.repeats} htmlFor="evaluation-set-repeats" hint={text.repeatsHint(EVALUATION_MAX_CASE_RUNS)}>
          <Select id="evaluation-set-repeats" data-testid="evaluation-set-repeats" value={String(repeats)} onChange={(e) => setRepeats(Number(e.target.value))}>
            {Array.from({ length: MAX_REPEATS }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </Field>
        <ErrorMessage error={save.error} testId="evaluation-set-error" />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={pending} onClick={onClose}>
            {t.common.cancel}
          </Button>
          <Button type="submit" data-testid="evaluation-set-save" disabled={pending || !name.trim() || Boolean(provider && !graderModel)}>
            {pending ? t.shared.saving : set ? t.shared.save : t.shared.create}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
