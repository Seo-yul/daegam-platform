/**
 * LLM 스펙 작업(job) 화면의 공용 조각. 스펙 다듬기(D25)와 툴 오버라이드 제안(D37)이 같이 쓴다.
 *
 * 제안 화면은 다듬기를 본떠 만들면서 provider 기본값, job id 를 URL 에 두는 규칙, 폴링, 체크 목록, 진행
 * 줄을 통째로 베꼈다. 그러다 진행 줄 한 벌만 고쳐져 제안 쪽은 폴링 실패를 삼키고 오류 코드를 날것으로
 * 보였다. 두 화면에서 다른 것은 testid 접두어와 프리셋 표, 다듬기의 반복 기록뿐이라 나머지는 여기 둔다.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { getSpecJob } from "@/api/resources";
import type { PlatformSettings, RefineJob } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Alert } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { describeCode } from "@/lib/errorText";
import { EMPTY_CHANGES, INSTRUCTION_MAX, MAX_ITERATIONS, POLL_MS, isActive } from "@/lib/specJobs";
import { tokensOf } from "@/lib/usage";
import { formatNumber, toggleIn } from "@/lib/utils";

// -- 훅 ------------------------------------------------------------------------------------

/** provider 고르기. 기본값은 Platform Settings 의 defaults.spec_generator 이고, 그것이 목록에 없으면 첫 provider 다. */
export function useSpecProvider(settings: PlatformSettings | undefined) {
  const providers = settings?.llm_providers ?? [];
  const defaultProvider = settings?.defaults.spec_generator ?? null;
  const [provider, setProvider] = useState("");
  useEffect(() => {
    if (!provider && providers.length) setProvider(defaultProvider && providers.some((p) => p.name === defaultProvider) ? defaultProvider : providers[0].name);
  }, [provider, providers, defaultProvider]);
  return { providers, defaultProvider, provider, setProvider };
}

/** 다시 물어도 소용없는 조회 실패인가. 4xx 는 job 이 만료됐거나 낡은 `?job=` 주소라는 뜻이다. */
function isLostJob(error: unknown): boolean {
  return error instanceof ApiError && error.status < 500;
}

/**
 * job 하나의 생애: 만들기, URL 의 job id, 폴링.
 *
 * job id 는 URL 에 둔다. 로컬 상태로만 들고 있으면 반복 5회짜리 job 이 도는 동안 새로고침하거나 상세에
 * 다녀오는 순간 화면이 job 을 잃는다. 서버에서는 계속 돌고 토큰도 계속 쓰는데 그 결과에 다시 붙을 길이 없다.
 *
 * 받은 적 없는 job 의 조회가 4xx 로 실패하면 폴링을 멈추고 실행 중으로 치지 않는다. 그러지 않으면 실행
 * 단추가 "실행 중…" 에 묶이고 없는 job 을 계속 다시 묻는다.
 */
export function useSpecJob(tenant: string, create: () => Promise<RefineJob>) {
  const [params, setSearchParams] = useSearchParams();
  const jobId = params.get("job");
  function setJobId(next: string | null) {
    const p = new URLSearchParams(params);
    if (next) p.set("job", next);
    else p.delete("job");
    setSearchParams(p, { replace: true });
  }
  const createJob = useMutation({ mutationFn: create, onSuccess: (made) => setJobId(made.id) });
  const job = useQuery({
    queryKey: ["spec-jobs", tenant, jobId],
    queryFn: () => getSpecJob(jobId as string),
    enabled: jobId !== null,
    refetchInterval: (q) => ((q.state.data ? !isActive(q.state.data.state) : isLostJob(q.state.error)) ? false : POLL_MS),
  });
  // 완료 전에는 null. 완료 뒤 참조가 고정돼야 받는 쪽 effect 가 한 번만 돈다 (?? [] 는 렌더마다 새 배열이다)
  const changes = job.data?.state === "done" ? (job.data.changes ?? EMPTY_CHANGES) : null;
  const lost = !job.data && isLostJob(job.error);
  const running = createJob.isPending || (jobId !== null && !lost && isActive(job.data?.state ?? "queued"));
  return { jobId, setJobId, createJob, job, changes, running };
}

// -- 폼 칸 ----------------------------------------------------------------------------------

/** provider 칸. 등록된 것이 없으면 Platform Settings 로 가는 길을 보인다. `children` 은 칸 아래의 오류 줄이다. */
export function ProviderField({
  prefix,
  providers,
  defaultProvider,
  value,
  onChange,
  loading,
  noProvider,
  children,
}: {
  prefix: string;
  providers: PlatformSettings["llm_providers"];
  defaultProvider: string | null;
  value: string;
  onChange: (next: string) => void;
  loading: boolean;
  noProvider: boolean;
  children?: ReactNode;
}) {
  const t = useT();
  const text = t.detail.specJobForm;
  return (
    <Field label={text.labels.provider} htmlFor={`${prefix}-provider`} hint={defaultProvider ? text.defaultProvider(defaultProvider) : undefined}>
      <Select id={`${prefix}-provider`} data-testid={`${prefix}-provider`} value={value} disabled={providers.length === 0} placeholder={loading ? text.loadingProviders : text.noProviderOption} onChange={(e) => onChange(e.target.value)}>
        {providers.map((pv) => (
          <option key={pv.name} value={pv.name}>
            {text.providerOption(pv.name, pv.name === defaultProvider, Boolean(pv.api_key_secret))}
          </option>
        ))}
      </Select>
      {children}
      {noProvider ? (
        <Alert variant="warn" data-testid={`${prefix}-no-provider`}>
          {text.noProvider(
            <Link to="/admin/settings" className="underline underline-offset-2" data-testid={`${prefix}-settings-link`}>
              {text.settingsLink}
            </Link>,
          )}
        </Alert>
      ) : null}
    </Field>
  );
}

export interface CheckOption<T extends string> {
  value: T;
  label: string;
  hint: string;
}

/** 체크박스 목록. `showCode` 면 이름 옆에 서버 값을 고정폭으로 적는다 (프리셋). */
export function CheckList<T extends string>({
  legend,
  options,
  value,
  onChange,
  testIdPrefix,
  showCode = false,
}: {
  legend: string;
  options: readonly CheckOption<T>[];
  value: ReadonlySet<T>;
  onChange: (next: Set<T>) => void;
  testIdPrefix: string;
  showCode?: boolean;
}) {
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="mb-1 text-sm font-medium text-fg-soft">{legend}</legend>
      {options.map((o) => (
        <label key={o.value} className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            data-testid={`${testIdPrefix}-${o.value}`}
            checked={value.has(o.value)}
            onChange={(e) => onChange(toggleIn(value, o.value, e.target.checked))}
          />
          <span className="flex flex-col">
            <span>
              {o.label}
              {showCode ? (
                <>
                  {" "}
                  <code className="font-mono text-[11px] text-muted">{o.value}</code>
                </>
              ) : null}
            </span>
            <span className="text-xs text-muted">{o.hint}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/** 자유 지시 한 줄. 줄바꿈은 공백으로 바꾼다. `children` 은 칸 아래의 오류 줄이다. */
export function InstructionField({ prefix, value, onChange, placeholder, children }: { prefix: string; value: string; onChange: (next: string) => void; placeholder: string; children?: ReactNode }) {
  const t = useT();
  const text = t.detail.specJobForm;
  return (
    <Field label={text.instruction} htmlFor={`${prefix}-instruction`} hint={text.instructionHint(value.length, INSTRUCTION_MAX)}>
      <Input id={`${prefix}-instruction`} data-testid={`${prefix}-instruction`} maxLength={INSTRUCTION_MAX} placeholder={placeholder} value={value} onChange={(e) => onChange(e.target.value.replace(/[\r\n]+/g, " "))} />
      {children}
    </Field>
  );
}

// -- 진행 ------------------------------------------------------------------------------------

/**
 * job 진행 줄: 상태 배지, 반복 n/5, 토큰. 실패하면 오류 코드를 한국어 처방으로 풀어 보인다 (D43 첫째).
 *
 * job 조회 자체가 실패하면 진행 대신 그 오류를 보인다. `children` 은 줄 아래의 반복 기록이다.
 */
export function SpecJobProgress({ prefix, job, error, children }: { prefix: string; job: RefineJob | undefined; error: unknown; children?: ReactNode }) {
  const t = useT();
  const text = t.detail.specJobForm;
  if (error) return <ErrorMessage error={error} testId={`${prefix}-job-error`} />;
  const state = job?.state ?? "queued";
  const err = (job?.error ?? null) as { code?: string; detail?: string } | null;
  return (
    <div className="mt-3 flex flex-col gap-2 border-t border-line-soft pt-3 text-xs" data-testid={`${prefix}-progress`} data-state={state}>
      <div className="flex items-center gap-2">
        <Badge variant={state === "done" ? "ok" : state === "failed" ? "error" : "info"}>{state}</Badge>
        <span data-testid={`${prefix}-iterations`}>{text.iterations(job?.iterations.length ?? 0, MAX_ITERATIONS)}</span>
        <span className="ml-auto text-muted" data-testid={`${prefix}-tokens`}>
          {text.tokens(formatNumber(t, tokensOf(job?.usage_total)))}
        </span>
      </div>
      {children}
      {state === "failed" ? (
        <Alert variant="error" data-testid={`${prefix}-job-failed`}>
          <div>{describeCode(t, err?.code, err?.detail).text}</div>
          {err?.code ? <div className="mt-1 text-right font-mono text-[10px] opacity-60">{err.code}</div> : null}
        </Alert>
      ) : null}
    </div>
  );
}
