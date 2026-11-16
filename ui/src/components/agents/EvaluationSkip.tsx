/**
 * 배포 게이트의 거절과 평가 없이 배포하기 (P45 12절, 14절). 배포, 재배포, 되돌리기 대화상자가 같이 쓴다.
 *
 * 서버가 409 `evaluation_required` 로 막으면 대화상자의 오류 줄(errors.codes)이 무엇을 하면 되는지 말하고, 이 부품이 그 아래에
 * 까닭과 판정한 실행의 수를 덧붙인다. 까닭이 `baseline_missing` 이면 배포된 리비전을 이 세트로 평가하는 길(평가 탭의 시작 창)을
 * 낸다. 그 길은 평가 탭을 볼 수 있는 사람(`agent:approve`)에게만 선다. 거절을 받았거나 게이트가 걸린다고 미리 안 대화상자(되돌리기)에는 평가 없이 배포하는 확인란과 까닭 칸이 선다. 까닭은 1~500자이고
 * 배포 기록이 사람과 함께 남긴다. 확인란을 켜고 까닭을 비워 둔 동안은 보내지 않는다.
 */
import { useId } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "@/api/client";
import type { Agent, EvaluationRun } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Field, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { hasScope, useAuthState } from "@/lib/auth";
import { passRate } from "@/lib/evaluation";
import { useTenant } from "@/lib/tenant";
import { ownValue } from "@/lib/utils";

/** 건너뛰기의 까닭 상한. 서버 `api/schemas/agents.py` 의 `SkipEvaluation` 과 같다. */
export const SKIP_REASON_MAX_CHARS = 500;
const EVALUATION_REQUIRED = "evaluation_required";

export interface SkipState {
  on: boolean;
  reason: string;
}

export const NO_SKIP: SkipState = { on: false, reason: "" };

/** 본문에 실을 `skip_evaluation`. 켜고 까닭을 적었을 때만 있다. */
export function skipBody(skip: SkipState): { skip_evaluation?: { reason: string } } {
  const reason = skip.reason.trim();
  return skip.on && reason ? { skip_evaluation: { reason } } : {};
}

/** 확인란을 켜고 까닭을 비워 둔 동안은 보내지 않는다. */
export function skipIncomplete(skip: SkipState): boolean {
  return skip.on && !skip.reason.trim();
}

/** 게이트가 막은 답인가. */
export function gateRefused(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === EVALUATION_REQUIRED;
}

/** 거절에 실린 실행의 요약 (`services/evaluation/gate.py` `run_summary`). */
type RunSummary = Pick<EvaluationRun, "id" | "state" | "totals">;

export function EvaluationSkip({
  agent,
  error,
  applies = false,
  value,
  onChange,
  onLeave,
  testId = "evaluation-skip",
}: {
  agent: Pick<Agent, "evaluation_gate">;
  error: unknown;
  /** 게이트가 걸린다는 것을 대화상자가 미리 안다. 거절 전에도 확인란이 선다. */
  applies?: boolean;
  value: SkipState;
  onChange: (next: SkipState) => void;
  /** 평가 탭으로 옮기기 전에 대화상자를 닫는다. */
  onLeave?: () => void;
  testId?: string;
}) {
  const t = useT();
  const text = t.agents.evaluation.gate;
  const reasonId = useId();
  const refused = gateRefused(error);
  if (!refused && !applies) return null;
  return (
    <div className="flex flex-col gap-2 rounded-card border border-line px-4 py-3" data-testid={testId}>
      {refused ? <GateRefusal agent={agent} error={error} onLeave={onLeave} /> : null}
      <label className="flex items-center gap-2 text-sm font-medium text-heading">
        <input type="checkbox" data-testid={`${testId}-toggle`} checked={value.on} onChange={(e) => onChange({ ...value, on: e.target.checked })} />
        {text.skip}
      </label>
      {value.on ? (
        <Field label={text.skipReason} htmlFor={reasonId} hint={text.skipHint} required>
          <Textarea
            id={reasonId}
            data-testid={`${testId}-reason`}
            className="min-h-16 font-sans text-sm"
            maxLength={SKIP_REASON_MAX_CHARS}
            required
            value={value.reason}
            onChange={(e) => onChange({ ...value, reason: e.target.value })}
          />
        </Field>
      ) : null}
    </div>
  );
}

/** 거절의 까닭, 판정한 실행(없으면 그 정의의 가장 새 실행)의 수, `baseline_missing` 이면 배포된 리비전을 평가하는 길. */
function GateRefusal({ agent, error, onLeave }: { agent: Pick<Agent, "evaluation_gate">; error: ApiError; onLeave?: () => void }) {
  const t = useT();
  const text = t.agents.evaluation;
  const navigate = useNavigate();
  const tenant = useTenant();
  const canApprove = hasScope(useAuthState().me, tenant, "agent:approve");
  const reason = error.extra<string>("reason") ?? "";
  const cases = error.extra<string[]>("cases") ?? [];
  const judged = error.extra<RunSummary | null>("judged") ?? null;
  const latest = error.extra<RunSummary | null>("run") ?? null;
  const offer = error.extra<{ revision?: number } | null>("offer") ?? null;
  const gate = agent.evaluation_gate;
  const reasons = text.gate.reasons;
  const line =
    reason === "stale"
      ? reasons.stale(gate?.max_age_days ?? 0)
      : reason === "pass_rate"
        ? reasons.pass_rate(percent(error.extra<number>("pass_rate")), percent(error.extra<number>("min_pass_rate")))
        : reason === "worse_cases"
          ? reasons.worse_cases(cases.length)
          : reason === "baseline_missing"
            ? reasons.baseline_missing(cases.length)
            : reason === "no_run" || reason === "nothing_counted"
              ? reasons[reason]
              : null;
  const rate = judged ? passRate(judged) : null;
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="evaluation-refusal">
      {line ? <p data-testid="evaluation-refusal-reason">{line}</p> : null}
      {judged && rate !== null ? (
        <p className="text-xs text-muted" data-testid="evaluation-refusal-run">
          {/* 나빠진 사례의 수는 worse_cases 의 거절만 싣는다. 다른 까닭은 기준선과 견주지 않았으므로 그 수를 말하지 않는다 */}
          {reason === "worse_cases"
            ? text.run.summary(judged.totals.counted, judged.totals.passed, rate, cases.length)
            : text.run.passRate(judged.totals.counted, judged.totals.passed, rate)}
        </p>
      ) : latest ? (
        <p className="text-xs text-muted" data-testid="evaluation-refusal-run">
          {text.gate.latestRun(ownValue(text.run.states, latest.state) ?? latest.state)}
        </p>
      ) : null}
      {reason === "baseline_missing" && gate && offer?.revision && canApprove ? (
        <div>
          <Button
            size="sm"
            variant="outline"
            data-testid="evaluation-run-deployed"
            onClick={() => {
              onLeave?.();
              navigate({ search: new URLSearchParams({ tab: "evaluation", set: gate.set_id, revision: String(offer.revision) }).toString() });
            }}
          >
            {text.gate.runDeployed}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** 0~1 의 비율을 정수 %로. 거절에 실린 통과율과 기준이다. */
function percent(rate: number | undefined): number {
  return Math.round((rate ?? 0) * 100);
}
