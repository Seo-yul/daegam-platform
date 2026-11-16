/** 가드레일 노드 패널. 위치·규칙(종류별 입력)·위반 동작·안내문이다. injection_check 는 판정 모델 노드와 두 한도를 받는다(P43 7절). */
import { Button } from "@/components/ui/button";
import { Field, Input, Label, Select, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { GUARDRAIL_RULE_KINDS, INJECTION_CHECK, JUDGE_MAX_CALLS, JUDGE_MAX_CHARS, PII_TYPES, type GuardrailData, type GuardrailRule, type InjectionCheck, type PIIType } from "@/lib/agentGraph";

import { NumberField, PanelTitle } from "./fields";

/** 판정 모델로 고를 수 있는 모델 노드. 셀렉트의 줄은 모델 노드의 이름이다. */
export interface JudgeModelOption {
  id: string;
  name: string;
}

function ruleDefault(kind: GuardrailRule["kind"]): GuardrailRule {
  if (kind === "max_length") return { kind, value: 4000 };
  if (kind === "deny_pattern") return { kind, value: "" };
  if (kind === INJECTION_CHECK) return { kind, value: { model: "", max_chars: JUDGE_MAX_CHARS.default, max_calls: JUDGE_MAX_CALLS.default } };
  return { kind: "pii_mask", value: { types: ["email"] } };
}

export function GuardrailPanel({ data, update, models }: { data: GuardrailData; update: (patch: Partial<GuardrailData>, commit?: boolean) => void; models: readonly JudgeModelOption[] }) {
  const t = useT();
  const text = t.canvas.guardrail;
  const rules = data.rules ?? [];
  function setRule(i: number, r: GuardrailRule, commit = false) {
    update({ rules: rules.map((x, j) => (j === i ? r : x)) }, commit);
  }
  function setKind(i: number, kind: GuardrailRule["kind"]) {
    const next = rules.map((x, j) => (j === i ? ruleDefault(kind) : x));
    // 판정 모델은 Tool 결과에만 걸리므로 그 규칙을 고르면 위치도 Tool 로 옮긴다
    update(kind === INJECTION_CHECK ? { rules: next, position: "tool" } : { rules: next }, true);
  }
  function setJudge(i: number, patch: Partial<InjectionCheck>, commit = false) {
    const r = rules[i];
    if (r?.kind === INJECTION_CHECK) setRule(i, { kind: INJECTION_CHECK, value: { ...r.value, ...patch } }, commit);
  }
  return (
    <>
      <PanelTitle id="guardrail">{t.canvas.nodeLabels.guardrail}</PanelTitle>
      <div className="grid grid-cols-2 gap-3">
        <Field label={text.position} htmlFor="guardrail-position" hint={text.positionHint}>
          <Select id="guardrail-position" data-testid="guardrail-position" value={data.position} onChange={(e) => update({ position: e.target.value as GuardrailData["position"] }, true)}>
            <option value="input">{text.positions.input}</option>
            <option value="output">{text.positions.output}</option>
            <option value="tool">{text.positions.tool}</option>
          </Select>
        </Field>
        <Field label={text.onViolation} htmlFor="guardrail-on-violation" hint={text.onViolationHint}>
          <Select id="guardrail-on-violation" data-testid="guardrail-on-violation" value={data.on_violation} onChange={(e) => update({ on_violation: e.target.value as GuardrailData["on_violation"] }, true)}>
            <option value="block">{text.violations.block}</option>
            <option value="flag">{text.violations.flag}</option>
          </Select>
        </Field>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <Label>{text.rules}</Label>
          <Button size="sm" variant="outline" data-testid="guardrail-rule-add" onClick={() => update({ rules: [...rules, ruleDefault("deny_pattern")] }, true)}>
            {text.addRule}
          </Button>
        </div>
        {rules.map((r, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-item border border-line bg-surface-strong px-2.5 py-2" data-testid={`guardrail-rule-${i}`}>
            <div className="flex items-center gap-2">
              <Select aria-label={text.ruleKind} data-testid={`guardrail-rule-kind-${i}`} value={r.kind} onChange={(e) => setKind(i, e.target.value as GuardrailRule["kind"])}>
                {GUARDRAIL_RULE_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </Select>
              <Button size="sm" variant="ghost" data-testid={`guardrail-rule-remove-${i}`} disabled={rules.length <= 1} onClick={() => update({ rules: rules.filter((_, j) => j !== i) }, true)}>
                {t.common.delete}
              </Button>
            </div>
            {r.kind === "max_length" ? (
              <Input aria-label={text.maxLength} data-testid={`guardrail-rule-value-${i}`} type="number" min={1} className="h-8 text-xs" value={r.value} onChange={(e) => setRule(i, { kind: "max_length", value: Number(e.target.value) })} />
            ) : r.kind === "deny_pattern" ? (
              <Input aria-label={text.denyPattern} data-testid={`guardrail-rule-value-${i}`} className="h-8 font-mono text-xs" placeholder={text.regex} value={r.value} onChange={(e) => setRule(i, { kind: "deny_pattern", value: e.target.value })} />
            ) : r.kind === INJECTION_CHECK ? (
              <div className="flex flex-col gap-2">
                <p className="text-xs text-fg-soft">{text.injectionHint}</p>
                <Field label={text.judgeModel} htmlFor={`guardrail-judge-model-${i}`} hint={text.judgeModelHint}>
                  <Select id={`guardrail-judge-model-${i}`} data-testid={`guardrail-judge-model-${i}`} value={r.value.model} placeholder={t.shared.select} onChange={(e) => setJudge(i, { model: e.target.value }, true)}>
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <div className="grid grid-cols-2 gap-2">
                  <NumberField id={`guardrail-judge-max-chars-${i}`} label="max_chars" min={JUDGE_MAX_CHARS.min} value={r.value.max_chars} onChange={(v) => setJudge(i, { max_chars: v ?? JUDGE_MAX_CHARS.default })} />
                  <NumberField id={`guardrail-judge-max-calls-${i}`} label="max_calls" min={JUDGE_MAX_CALLS.min} value={r.value.max_calls} onChange={(v) => setJudge(i, { max_calls: v ?? JUDGE_MAX_CALLS.default })} />
                </div>
                <p className="text-xs text-fg-soft">{text.injectionLimitsHint}</p>
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                <div className="flex flex-wrap gap-2">
                  {PII_TYPES.map((type) => (
                    <label key={type} className="flex items-center gap-1 text-xs text-fg-soft">
                      <input
                        data-testid={`guardrail-rule-pii-${i}-${type}`}
                        type="checkbox"
                        checked={r.value.types.includes(type)}
                        onChange={(e) => {
                          const types = e.target.checked ? [...r.value.types, type] : r.value.types.filter((x) => x !== type);
                          setRule(i, { kind: "pii_mask", value: { ...r.value, types: types as PIIType[] } }, true);
                        }}
                      />
                      {type}
                    </label>
                  ))}
                </div>
                {r.value.types.includes("regex") ? <Input aria-label={text.piiRegex} data-testid={`guardrail-rule-value-${i}`} className="h-8 font-mono text-xs" placeholder={text.regex} value={r.value.regex ?? ""} onChange={(e) => setRule(i, { kind: "pii_mask", value: { ...r.value, regex: e.target.value } })} /> : null}
              </div>
            )}
          </div>
        ))}
      </div>
      <Field label={text.message} htmlFor="guardrail-message" hint={text.messageHint}>
        <Textarea id="guardrail-message" data-testid="guardrail-message" rows={2} className="font-sans" value={data.message ?? ""} onChange={(e) => update({ message: e.target.value || null })} />
      </Field>
    </>
  );
}
