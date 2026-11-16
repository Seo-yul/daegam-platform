/**
 * 배포 전 평가 카드 (P45 12절, 14절). Agent 의 배포 게이트를 켜고, 바꾸고, 끈다. 게이트는 리비전 밖의 설정이라 리비전이 끌 수 없고,
 * 켜면 지금까지 배포된 적 없는 내용을 핀하는 배포, 재배포, 되돌리기가 그 세트의 통과한 실행을 요구한다.
 *
 * 나빠진 사례를 막는 칸은 반복이 둘 이상인 세트에서만 켤 수 있다(한 번의 아쉬운 답이 배포를 막지 않게). 저장과 끄기는
 * `agent:deploy` 가 있는 사람만 하고, 다른 승인자는 지금의 설정을 읽기만 한다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { putEvaluationGate } from "@/api/agentEvaluation";
import type { Agent, EvaluationGateBody, EvaluationSet } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { BASELINE_REPEATS, DEFAULT_MAX_AGE_DAYS, MAX_AGE_DAYS } from "@/lib/evaluation";

interface GateForm {
  setId: string;
  /** 최소 통과율(%) 칸의 글. */
  minPassRate: string;
  noWorseCases: boolean;
  maxAgeDays: string;
}

function formOf(agent: Agent, sets: readonly EvaluationSet[]): GateForm {
  const gate = agent.evaluation_gate;
  if (gate) {
    return { setId: gate.set_id, minPassRate: String(Math.round(gate.min_pass_rate * 100)), noWorseCases: gate.no_worse_cases, maxAgeDays: String(gate.max_age_days) };
  }
  return { setId: sets[0]?.id ?? "", minPassRate: "80", noWorseCases: false, maxAgeDays: String(DEFAULT_MAX_AGE_DAYS) };
}

/** 칸의 글을 본문으로. 범위를 벗어나면 null 이고 저장 단추가 꺼진다. */
function bodyOf(form: GateForm, set: EvaluationSet | undefined): EvaluationGateBody | null {
  const rate = Number(form.minPassRate);
  const days = Number(form.maxAgeDays);
  if (!set || form.minPassRate.trim() === "" || !Number.isFinite(rate) || rate < 0 || rate > 100) return null;
  if (!Number.isInteger(days) || days < 1 || days > MAX_AGE_DAYS) return null;
  return { set_id: set.id, min_pass_rate: rate / 100, no_worse_cases: form.noWorseCases && set.repeats >= BASELINE_REPEATS, max_age_days: days };
}

export function EvaluationGateCard({ agent, sets, canEdit }: { agent: Agent; sets: readonly EvaluationSet[]; canEdit: boolean }) {
  const t = useT();
  const text = t.agents.evaluation.gate;
  const queryClient = useQueryClient();
  const gate = agent.evaluation_gate;
  const [form, setForm] = useState<GateForm>(() => formOf(agent, sets));
  // 저장된 게이트나 세트 목록이 바뀌면(다른 사람이 바꿨거나 방금 저장했다) 그 값으로 칸을 맞춘다. Agent 를 다시 읽기만 해서는
  // 고치던 칸을 덮지 않게 내용으로 견준다
  const saved = JSON.stringify([gate, sets.map((s) => s.id)]);
  useEffect(() => setForm(formOf(agent, sets)), [saved]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = useMutation({
    mutationFn: (body: EvaluationGateBody | null) => putEvaluationGate(agent.id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });
  const set = sets.find((s) => s.id === form.setId);
  const body = bodyOf(form, set);
  const baselineOk = (set?.repeats ?? 0) >= BASELINE_REPEATS;
  const days = Number(form.maxAgeDays) || DEFAULT_MAX_AGE_DAYS;
  const disabled = !canEdit || save.isPending;
  return (
    <Card data-testid="evaluation-gate">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <Badge variant={gate ? "ok" : "muted"} data-testid="evaluation-gate-state">
          {gate ? text.on : t.agents.deployment.off}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted" data-testid="evaluation-gate-hint">
          {text.hint(days)}
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t.agents.evaluation.copy.set} htmlFor="evaluation-gate-set">
            <Select id="evaluation-gate-set" data-testid="evaluation-gate-set" value={form.setId} disabled={disabled} onChange={(e) => setForm({ ...form, setId: e.target.value })}>
              {sets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={text.minPassRate} htmlFor="evaluation-gate-rate">
            <Input id="evaluation-gate-rate" data-testid="evaluation-gate-rate" type="number" min={0} max={100} value={form.minPassRate} disabled={disabled} onChange={(e) => setForm({ ...form, minPassRate: e.target.value })} />
          </Field>
          <Field label={text.maxAgeDays} htmlFor="evaluation-gate-days">
            <Input id="evaluation-gate-days" data-testid="evaluation-gate-days" type="number" min={1} max={MAX_AGE_DAYS} value={form.maxAgeDays} disabled={disabled} onChange={(e) => setForm({ ...form, maxAgeDays: e.target.value })} />
          </Field>
        </div>
        <div className="flex flex-col gap-1">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              data-testid="evaluation-gate-no-worse"
              checked={form.noWorseCases && baselineOk}
              disabled={disabled || !baselineOk}
              onChange={(e) => setForm({ ...form, noWorseCases: e.target.checked })}
            />
            {text.noWorseCases}
          </label>
          <p className="text-xs text-muted">{text.noWorseCasesHint(BASELINE_REPEATS)}</p>
        </div>
        <ErrorMessage error={save.error} testId="evaluation-gate-error" />
        {canEdit ? (
          <div className="flex justify-end gap-2">
            {gate ? (
              <Button variant="outline" data-testid="evaluation-gate-off" disabled={disabled} onClick={() => save.mutate(null)}>
                {text.turnOff}
              </Button>
            ) : null}
            <Button data-testid="evaluation-gate-save" disabled={disabled || body === null} onClick={() => body && save.mutate(body)}>
              {save.isPending ? t.shared.saving : t.shared.save}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
