/**
 * 평가 사례의 편집기와 대화에서 사례로 복사하기 (P45 7절, 8절, 10절, 11절, D109).
 *
 * 편집기는 질문(1~5개, 새 대화 하나에서 차례로 보낸다), 참조 답변, 채점 기준, 확인 항목, 쓰기 Tool 의 가상 결과를 받는다. 저장이 422
 * `evaluation_case_pii` 이면 서버가 짚은 칸을 사람이 읽는 이름과 종류로 늘어놓고 그 칸에 표시를 단다. 손으로 쓴 사례(`acknowledgeable: true`)는 경고 문장과 확인란이
 * 서고 확인란을 선택하면 `pii_acknowledged` 를 실어 저장한다. 복사한 사례는 확인으로 저장되지 않으므로 오류 문장(그 칸을 지우거나
 * 바꾸라는 말)만 선다.
 *
 * 복사 창은 동의한 평가의 턴을 받아 세트를 고르면 서버가 그 턴의 질문과 답을 자리표로 바꾼 복사본을 돌려준다(`from-turn`). 사람이
 * 그 내용을 고쳐 저장하면 출처(origin)와 함께 들어간다. 저장은 동의를 다시 본다(409 `evaluation_consent_missing`). 다시 생성으로
 * 걷혔거나 대화 기록의 보관 기간이 지난 턴(404 `turn_not_found`)은 복사할 수 없다고 말한다. 복사본은 감사에 남는 쓰기라 평가 탭의
 * 앞머리 밖의 키에 두어, 사례를 저장한 뒤의 무효화가 다시 받지 않게 한다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { copyRatedTurn, createEvaluationCase, listEvaluationSets, putEvaluationCase } from "@/api/agentEvaluation";
import { ApiError } from "@/api/client";
import type { EvaluationCase, EvaluationCaseContentIn, EvaluationCaseDraft, EvaluationCaseOrigin, EvaluationCheckKind } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { TextRows } from "@/components/TextRows";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { type Messages, useT } from "@/i18n";
import {
  CHECK_KINDS,
  EMPTY_CHECK,
  MAX_CHECKS,
  MAX_CRITERIA,
  MAX_MESSAGES,
  MAX_TOOL_RESULTS,
  PII_KINDS,
  caseBodyOf,
  caseDraftOf,
  draftFieldOf,
  emptyCase,
  parsePlace,
  toolCheck,
  type CaseDraft,
  type CheckDraft,
  type DraftField,
  type PiiMatch,
} from "@/lib/evaluation";
import { agentEvaluationKey, evaluationCopyKey } from "@/lib/queryKeys";
import { cn } from "@/lib/utils";
import { useTenant } from "@/lib/tenant";
import { ownValue } from "@/lib/utils";

const CASE_PII = "evaluation_case_pii";
const TURN_NOT_FOUND = "turn_not_found";
/** 개인정보 검사가 짚은 칸의 테두리. */
const MARKED = "border-err-fg";

/** 짚은 칸의 모음. 키는 칸의 종류와 줄 차례다. */
type Marks = ReadonlySet<string>;

function markKey(f: DraftField): string {
  return "index" in f ? `${f.field}:${f.index}` : f.field;
}

/** 서버가 짚은 자리를 사람이 읽는 칸 이름으로. 편집기에서 찾지 못하면 저장의 차례로, 그것도 모르면 자리 그대로다. */
function placeLabel(t: Messages, place: string, draft: CaseDraft): string {
  const names = t.agents.evaluation.case.places;
  const f = draftFieldOf(place, draft);
  const p = parsePlace(place);
  if (p?.field === "toolResult") return names.toolResult(p.name);
  if (!p) return place;
  if (p.field === "reference") return names.reference;
  const n = (f && "index" in f ? f.index : p.index) + 1;
  return names[p.field](n);
}

/** 사례 편집기의 바탕. 고칠 사례(existing)나 복사본(copy)을 주면 그 내용에서 시작한다. 둘 다 없으면 새 사례다. */
export interface CaseEditorTarget {
  existing?: EvaluationCase;
  copy?: EvaluationCaseDraft;
}

/** 사례를 만들거나 고치는 창. 저장하면 평가 탭의 앞머리를 무효화하고 닫는다. */
export function CaseEditorDialog({ agentId, setId, target, onClose }: { agentId: string; setId: string; target: CaseEditorTarget; onClose: () => void }) {
  const t = useT();
  const text = t.agents.evaluation.case;
  const title = target.existing ? text.editTitle : target.copy ? t.agents.evaluation.copy.action : text.addTitle;
  return (
    <Modal open title={title} testId="case-editor" closeTestId="case-editor-close" onClose={onClose}>
      <CaseForm agentId={agentId} setId={setId} target={target} onDone={onClose} onCancel={onClose} />
    </Modal>
  );
}

/** 편집기의 몸. 복사 창도 세트를 고른 뒤 이것을 그린다. */
function CaseForm({ agentId, setId, target, onDone, onCancel }: { agentId: string; setId: string; target: CaseEditorTarget; onDone: () => void; onCancel: () => void }) {
  const t = useT();
  const text = t.agents.evaluation.case;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const origin: EvaluationCaseOrigin | null = target.existing?.origin ?? target.copy?.origin ?? null;
  const [draft, setDraft] = useState<CaseDraft>(() => {
    const content = target.existing?.content ?? target.copy?.content;
    return content ? caseDraftOf(content) : emptyCase();
  });
  const [acknowledged, setAcknowledged] = useState(false);
  const [problem, setProblem] = useState<number | null>(null);
  const save = useMutation({
    mutationFn: (content: EvaluationCaseContentIn) => {
      const body = { content, pii_acknowledged: acknowledged };
      if (target.existing) return putEvaluationCase(agentId, setId, target.existing.id, body);
      return createEvaluationCase(agentId, setId, { ...body, ...(origin ? { origin } : {}) });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: agentEvaluationKey(tenant, agentId) });
      onDone();
    },
  });
  const pii = save.error instanceof ApiError && save.error.code === CASE_PII ? save.error : null;
  const acknowledgeable = pii?.extra<boolean>("acknowledgeable") === true;
  const matches = pii?.extra<PiiMatch[]>("matches") ?? [];
  const marks: Marks = new Set(matches.flatMap((m) => {
    const f = draftFieldOf(m.place, draft);
    return f ? [markKey(f)] : [];
  }));
  const marked = (key: string) => marks.has(key);
  const pending = save.isPending;
  const set = (next: Partial<CaseDraft>) => setDraft((prev) => ({ ...prev, ...next }));
  return (
    <form
      className="flex max-h-[75vh] flex-col gap-3 overflow-y-auto px-5 py-4"
      data-testid="case-form"
      onSubmit={(e) => {
        e.preventDefault();
        const built = caseBodyOf(draft);
        if ("problem" in built) return setProblem(built.problem.index);
        setProblem(null);
        save.mutate(built.content);
      }}
    >
      {target.copy ? <CopyNotice draft={target.copy} /> : null}
      <TextRows
        label={text.messages}
        hint={text.messagesHint(MAX_MESSAGES)}
        values={draft.messages}
        max={MAX_MESSAGES}
        min={1}
        multiline
        inputClassName="min-h-12 font-sans text-sm"
        testId="case-message"
        invalid={(i) => marked(`message:${i}`)}
        onChange={(messages) => set({ messages })}
      />
      <Field label={text.reference} htmlFor="case-reference">
        <Textarea
          id="case-reference"
          data-testid="case-reference"
          className={cn("min-h-16 font-sans text-sm", marked("reference") && MARKED)}
          aria-invalid={marked("reference") || undefined}
          value={draft.reference}
          onChange={(e) => set({ reference: e.target.value })}
        />
      </Field>
      {draft.reference.trim() ? (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" data-testid="case-reference-good" checked={draft.referenceGood} onChange={(e) => set({ referenceGood: e.target.checked })} />
          {text.referenceGood}
        </label>
      ) : null}
      <TextRows
        label={text.criteria}
        hint={text.criteriaHint}
        values={draft.criteria}
        max={MAX_CRITERIA}
        multiline
        inputClassName="min-h-12 font-sans text-sm"
        testId="case-criterion"
        invalid={(i) => marked(`criterion:${i}`)}
        onChange={(criteria) => set({ criteria })}
      />
      <ChecksField checks={draft.checks} problem={problem} marked={marked} onChange={(checks) => set({ checks })} />
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" data-testid="case-expect-done" checked={draft.expectDone} onChange={(e) => set({ expectDone: e.target.checked })} />
        {text.expectDone}
      </label>
      <ToolResultsField draft={draft} marked={marked} onChange={(toolResults) => set({ toolResults })} />
      {pii ? (
        <div className="flex flex-col gap-2" data-testid="case-pii">
          {acknowledgeable ? (
            <Alert variant="warn" data-testid="case-pii-warning">
              {text.piiWarning}
            </Alert>
          ) : (
            <ErrorMessage error={pii} testId="case-pii-error" />
          )}
          <ul className="flex flex-col gap-0.5 text-xs" data-testid="case-pii-matches">
            {matches.map((m, i) => (
              <li key={`${m.place}-${i}`}>
                {placeLabel(t, m.place, draft)} · {(PII_KINDS as readonly string[]).includes(m.kind) ? ownValue(text.piiKinds, m.kind) : m.kind}
              </li>
            ))}
          </ul>
          {acknowledgeable ? (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" data-testid="case-pii-acknowledge" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
              {text.piiAcknowledge}
            </label>
          ) : null}
        </div>
      ) : (
        <ErrorMessage error={save.error} testId="case-error" />
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={pending} onClick={onCancel}>
          {t.common.cancel}
        </Button>
        <Button type="submit" data-testid="case-save" disabled={pending || !draft.messages.some((m) => m.trim()) || (acknowledgeable && !acknowledged)}>
          {pending ? t.shared.saving : t.shared.save}
        </Button>
      </div>
    </form>
  );
}

/** 복사본의 안내와 바꿔 둔 개인정보의 수. */
function CopyNotice({ draft }: { draft: EvaluationCaseDraft }) {
  const t = useT();
  const text = t.agents.evaluation.copy;
  const kinds = t.agents.evaluation.case.piiKinds;
  const counts = Object.entries(draft.replaced).filter(([, n]) => n > 0);
  return (
    <Alert variant="info" data-testid="case-copy-hint">
      <p>{text.hint}</p>
      {counts.length ? (
        <p className="mt-1" data-testid="case-copy-replaced">
          {text.replaced(counts.map(([kind, n]) => text.replacedCount(ownValue(kinds, kind) ?? kind, n)).join(", "))}
        </p>
      ) : null}
    </Alert>
  );
}

/** 확인 항목. 종류마다 Tool 이름(그리고 `tool_called` 의 인자 JSON)이나 글을 받는다. problem 은 인자를 풀지 못한 항목이다. */
function ChecksField({
  checks,
  problem,
  marked,
  onChange,
}: {
  checks: CheckDraft[];
  problem: number | null;
  /** 개인정보 검사가 짚은 칸인가. 키는 `checkTool:0` 꼴이다. */
  marked: (key: string) => boolean;
  onChange: (next: CheckDraft[]) => void;
}) {
  const t = useT();
  const text = t.agents.evaluation.case;
  const update = (i: number, next: Partial<CheckDraft>) => onChange(checks.map((c, j) => (j === i ? { ...c, ...next } : c)));
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium text-fg-soft">{text.checks}</legend>
      {checks.map((c, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-card border border-line px-3 py-2" data-testid={`case-check-${i}`}>
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Field label={text.checkKind} htmlFor={`case-check-${i}-kind`}>
                <Select id={`case-check-${i}-kind`} data-testid={`case-check-${i}-kind`} value={c.kind} onChange={(e) => update(i, { kind: e.target.value as EvaluationCheckKind })}>
                  {CHECK_KINDS.map((kind) => (
                    <option key={kind} value={kind}>
                      {text.checkKinds[kind]}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Button size="sm" variant="ghost" data-testid={`case-check-${i}-remove`} onClick={() => onChange(checks.filter((_, j) => j !== i))}>
              {t.shared.remove}
            </Button>
          </div>
          {toolCheck(c.kind) ? (
            <>
              <Field label={text.toolName} htmlFor={`case-check-${i}-tool`}>
                <Input
                  id={`case-check-${i}-tool`}
                  data-testid={`case-check-${i}-tool`}
                  className={cn("font-mono text-xs", marked(`checkTool:${i}`) && MARKED)}
                  aria-invalid={marked(`checkTool:${i}`) || undefined}
                  value={c.tool}
                  required
                  onChange={(e) => update(i, { tool: e.target.value })}
                />
              </Field>
              {c.kind === "tool_called" ? (
                <Field label={text.checkArgs} htmlFor={`case-check-${i}-args`} error={problem === i ? text.argsInvalid(i + 1) : null}>
                  <Input
                    id={`case-check-${i}-args`}
                    data-testid={`case-check-${i}-args`}
                    className={cn("font-mono text-xs", marked(`checkArgs:${i}`) && MARKED)}
                    aria-invalid={marked(`checkArgs:${i}`) || undefined}
                    value={c.args}
                    onChange={(e) => update(i, { args: e.target.value })}
                  />
                </Field>
              ) : null}
            </>
          ) : (
            <Field label={text.checkText} htmlFor={`case-check-${i}-text`}>
              <Input
                id={`case-check-${i}-text`}
                data-testid={`case-check-${i}-text`}
                className={cn(marked(`checkText:${i}`) && MARKED)}
                aria-invalid={marked(`checkText:${i}`) || undefined}
                value={c.text}
                required
                onChange={(e) => update(i, { text: e.target.value })}
              />
            </Field>
          )}
        </div>
      ))}
      {checks.length < MAX_CHECKS ? (
        <div>
          <Button size="sm" variant="outline" data-testid="case-check-add" onClick={() => onChange([...checks, { ...EMPTY_CHECK }])}>
            {t.shared.add}
          </Button>
        </div>
      ) : null}
    </fieldset>
  );
}

/** 쓰기 Tool 의 가상 결과. Tool 이름마다 돌려줄 글 하나다 (10절). */
function ToolResultsField({ draft, marked, onChange }: { draft: CaseDraft; marked: (key: string) => boolean; onChange: (next: CaseDraft["toolResults"]) => void }) {
  const t = useT();
  const text = t.agents.evaluation.case;
  const id = useId();
  const rows = draft.toolResults;
  const update = (i: number, next: Partial<CaseDraft["toolResults"][number]>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...next } : r)));
  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={`${id}-hint`}>
      <legend className="mb-1 text-sm font-medium text-fg-soft">{text.toolResults}</legend>
      <p id={`${id}-hint`} className="text-xs text-muted">
        {text.toolResultsHint}
      </p>
      {rows.map((r, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-card border border-line px-3 py-2" data-testid={`case-tool-result-${i}`}>
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Field label={text.toolName} htmlFor={`case-tool-result-${i}-tool`}>
                <Input
                  id={`case-tool-result-${i}-tool`}
                  data-testid={`case-tool-result-${i}-tool`}
                  className={cn("font-mono text-xs", marked(`toolResultName:${i}`) && MARKED)}
                  aria-invalid={marked(`toolResultName:${i}`) || undefined}
                  value={r.tool}
                  onChange={(e) => update(i, { tool: e.target.value })}
                />
              </Field>
            </div>
            <Button size="sm" variant="ghost" data-testid={`case-tool-result-${i}-remove`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              {t.shared.remove}
            </Button>
          </div>
          <Field label={text.toolResult} htmlFor={`case-tool-result-${i}-result`}>
            <Textarea
              id={`case-tool-result-${i}-result`}
              data-testid={`case-tool-result-${i}-result`}
              className={cn("min-h-12", marked(`toolResult:${i}`) && MARKED)}
              aria-invalid={marked(`toolResult:${i}`) || undefined}
              value={r.result}
              onChange={(e) => update(i, { result: e.target.value })}
            />
          </Field>
        </div>
      ))}
      {rows.length < MAX_TOOL_RESULTS ? (
        <div>
          <Button size="sm" variant="outline" data-testid="case-tool-result-add" onClick={() => onChange([...rows, { tool: "", result: "" }])}>
            {t.shared.add}
          </Button>
        </div>
      ) : null}
    </fieldset>
  );
}

/**
 * 동의한 평가의 턴을 평가 사례로 복사하는 창 (8절). 세트를 고르면 그 세트로 복사본을 받고, 사람이 고쳐 저장한다. 세트가 없으면 평가
 * 탭에서 먼저 만들라고 말한다.
 */
export function CopyCaseDialog({ agentId, sessionId, turnId, onClose }: { agentId: string; sessionId: string; turnId: string; onClose: () => void }) {
  const t = useT();
  const text = t.agents.evaluation.copy;
  const tenant = useTenant();
  const sets = useQuery({ queryKey: agentEvaluationKey(tenant, agentId), queryFn: () => listEvaluationSets(agentId) });
  const [setId, setSetId] = useState("");
  useEffect(() => {
    if (!setId && sets.data?.length) setSetId(sets.data[0].id);
  }, [setId, sets.data]);
  const draft = useQuery({
    queryKey: evaluationCopyKey(tenant, agentId, setId, sessionId, turnId),
    queryFn: () => copyRatedTurn(agentId, setId, sessionId, turnId),
    enabled: Boolean(setId),
    // 복사본은 저장하기 전의 것이고 다시 받으면 같은 글이다. 창이 열린 동안 다시 읽지 않는다
    staleTime: Infinity,
    retry: false,
  });
  return (
    <Modal open title={text.action} testId="copy-case-dialog" closeTestId="copy-case-close" onClose={onClose}>
      <div className="flex flex-col gap-3 px-5 pt-4">
        <ErrorMessage error={sets.error} testId="copy-case-sets-error" />
        {sets.data && !sets.data.length ? (
          <p className="text-sm text-muted" data-testid="copy-case-no-sets">
            {text.noSets}
          </p>
        ) : null}
        {sets.data?.length ? (
          <Field label={text.set} htmlFor="copy-case-set">
            <Select id="copy-case-set" data-testid="copy-case-set" value={setId} onChange={(e) => setSetId(e.target.value)}>
              {sets.data.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
        {draft.isLoading ? <p className="text-sm text-muted">{t.common.loading}</p> : null}
        {draft.error instanceof ApiError && draft.error.code === TURN_NOT_FOUND ? (
          <Alert variant="error" data-testid="copy-case-error">
            {text.turnGone}
          </Alert>
        ) : (
          <ErrorMessage error={draft.error} testId="copy-case-error" />
        )}
      </div>
      {draft.data && setId ? <CaseForm key={setId} agentId={agentId} setId={setId} target={{ copy: draft.data }} onDone={onClose} onCancel={onClose} /> : null}
      {!draft.data ? (
        <div className="flex justify-end px-5 py-4">
          <Button variant="ghost" onClick={onClose}>
            {t.common.cancel}
          </Button>
        </div>
      ) : null}
    </Modal>
  );
}
