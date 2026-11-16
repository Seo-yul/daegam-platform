/**
 * Backend API 버전 다듬기 (D25). /backend-apis/:id/versions/:n/refine?mode=minify|llm
 *
 * 왼쪽 폼에서 규칙(minify, 즉시 응답) 또는 LLM(job 을 만들어 폴링) 으로 변경 목록을 받고, SpecDiff 에서 항목을 골라
 * "새 버전으로 적용"(apply-draft) 으로 v(n+1) 을 만든다. 성공하면 상세로 돌아가며 새 버전을 선택한다.
 * 원본은 GET .../versions/{n}/spec (JSON, 사용자 인증 경로) 이다. 422 는 필드 오류로 해당 칸 아래에 보인다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { applyDraft, createRefineJob, getBackendApi, getPlatformSettings, getVersionSpec, listMcpServersUsingBackend, minifyVersion } from "@/api/resources";
import { MINIFY_ENRICH, REFINE_PRESETS, type MinifyEnrich, type MinifyOptions, type RefineJob, type RefinePreset, type SpecChange } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import type { RefineMode } from "@/components/GenerateMenu";
import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { SpecDiff } from "@/components/SpecDiff";
import { CheckList, InstructionField, ProviderField, SpecJobProgress, useSpecJob, useSpecProvider, type CheckOption } from "@/components/SpecJobForm";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { type Messages, useT } from "@/i18n";
import { describeCode } from "@/lib/errorText";
import { displayName, secondaryName } from "@/lib/names";
import { PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { isActive } from "@/lib/specJobs";
import { tokensOf } from "@/lib/usage";
import { formatNumber } from "@/lib/utils";

/** 규칙 다듬기의 enrich 선택지. 순서는 서버 값 목록을 따른다. */
function enrichOptions(t: Messages): CheckOption<MinifyEnrich>[] {
  return MINIFY_ENRICH.map((value) => ({ value, ...t.backendApis.refine.enrich[value] }));
}

/** LLM 다듬기의 프리셋 선택지. 이름은 오버라이드 제안과 같이 쓰고 풀이는 다듬기의 것이다. */
function presetOptions(t: Messages): CheckOption<RefinePreset>[] {
  return REFINE_PRESETS.map((value) => ({ value, label: t.backendApis.specJob.preset[value], hint: t.backendApis.refine.presetHint[value] }));
}

/** 오류를 칸 아래로 보낼 수 있는 필드. 서버 요청 본문의 키 이름 그대로다. */
const FIELDS = ["provider", "instruction", "max_description", "accepted_ids"] as const;
type FieldName = (typeof FIELDS)[number];
const isFieldName = (v: unknown): v is FieldName => (FIELDS as readonly unknown[]).includes(v);

/** 422 코드와 pydantic loc 을 화면 필드로 잇는다. 없으면 null 이고 save-error 로 간다. */
function fieldOf(error: unknown): FieldName | null {
  if (!(error instanceof ApiError)) return null;
  const byCode: Record<string, FieldName> = {
    provider_no_key: "provider",
    no_secret_store: "provider",
    unknown_provider: "provider",
    no_provider: "provider",
    unknown_change_id: "accepted_ids",
    unknown_change: "accepted_ids",
  };
  if (error.code in byCode) return byCode[error.code];
  const field = error.extra<string>("field");
  if (isFieldName(field)) return field;
  if (error.code === "validation_error") return FIELDS.find((f) => error.detail.includes(f)) ?? null;
  return null;
}

function FieldError({ error, field, testId }: { error: unknown; field: FieldName; testId: string }) {
  const t = useT();
  if (fieldOf(error) !== field) return null;
  const e = error as ApiError;
  // 칸 옆의 한 줄도 정상 경로와 같은 표를 지난다. 표에 없으면 서버 원문 그대로다 (D43 첫째)
  return (
    <p className="text-xs text-err-fg" data-testid={testId} role="alert">
      {describeCode(t, e.code, e.detail).text}
    </p>
  );
}

export function BackendApiRefinePage() {
  const t = useT();
  const text = t.backendApis.refine;
  const specJob = t.backendApis.specJob;
  const { id = "", n = "" } = useParams();
  const version = Number(n);
  const [params] = useSearchParams();
  const mode: RefineMode = params.get("mode") === "llm" ? "llm" : "minify";
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const profile = useQuery({ queryKey: ["backend-apis", tenant, id], queryFn: () => getBackendApi(id) });
  const spec = useQuery({
    queryKey: ["backend-apis", tenant, id, "versions", version, "spec"],
    queryFn: () => getVersionSpec(id, version),
    enabled: Number.isInteger(version),
  });
  const settings = useQuery({
    queryKey: PLATFORM_SETTINGS_KEY,
    queryFn: getPlatformSettings,
    enabled: mode === "llm",
  });

  // 규칙 폼
  const [enrich, setEnrich] = useState<Set<MinifyEnrich>>(() => new Set());
  const [maxDescription, setMaxDescription] = useState("");
  const [dropValueExamples, setDropValueExamples] = useState(false);
  const minifyOptions = useMemo<MinifyOptions>(() => {
    const out: MinifyOptions = { enrich: MINIFY_ENRICH.filter((v) => enrich.has(v)), drop_value_examples: dropValueExamples };
    const max = Number(maxDescription);
    if (maxDescription.trim() && Number.isInteger(max) && max > 0) out.max_description = max;
    return out;
  }, [enrich, maxDescription, dropValueExamples]);

  // LLM 폼. provider 기본값은 Platform Settings defaults.spec_generator 다.
  const { providers, defaultProvider, provider, setProvider } = useSpecProvider(settings.data);
  const [presets, setPresets] = useState<Set<RefinePreset>>(() => new Set<RefinePreset>(["enrich_descriptions"]));
  const [instruction, setInstruction] = useState("");

  // 실행 결과. 규칙은 mutation 응답이 곧 결과이고, LLM 은 job id 를 들고 폴링한다.
  const minify = useMutation({
    mutationFn: () => minifyVersion(id, version, minifyOptions),
    onSuccess: (r) => resetAccepted(r.changes),
  });
  const { jobId, setJobId, createJob, job, changes: jobChanges, running: jobRunning } = useSpecJob(tenant, () =>
    createRefineJob({
      mode: "refine",
      source: { backend_profile_id: id, version },
      presets: REFINE_PRESETS.filter((p) => presets.has(p)),
      ...(instruction.trim() ? { instruction: instruction.trim() } : {}),
      ...(provider ? { provider } : {}),
    }),
  );
  useEffect(() => {
    if (jobChanges) resetAccepted(jobChanges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobChanges]);

  const changes: SpecChange[] | null = mode === "minify" ? (minify.data?.changes ?? null) : jobChanges;
  const renamed = mode === "llm" ? (job.data?.renamed ?? null) : null;
  const [accepted, setAccepted] = useState<Set<string>>(() => new Set());
  function resetAccepted(list: SpecChange[]) {
    setAccepted(new Set(list.map((c) => c.id)));
  }

  const referencing = useQuery({
    queryKey: ["mcp-servers", tenant, "using", id],
    queryFn: () => listMcpServersUsingBackend(id),
    enabled: Boolean(renamed?.length),
  });

  const apply = useMutation({
    mutationFn: () =>
      applyDraft(id, version, {
        ...(mode === "minify" ? { minify: minifyOptions } : { job_id: jobId as string }),
        accepted_ids: changes ? changes.filter((c) => accepted.has(c.id)).map((c) => c.id) : [],
      }),
    onSuccess: async (r) => {
      // 상세의 프로필·버전 목록은 지금 화면에 없어(inactive) 기본 invalidate 로는 다시 받지 않는다. 돌아가기 전에 받아 둔다.
      await queryClient.invalidateQueries({ queryKey: ["backend-apis", tenant, id], refetchType: "all" });
      navigate(`/backend-apis/${encodeURIComponent(id)}?version=${r.version}`);
    },
  });

  const runError = mode === "minify" ? minify.error : createJob.error;
  const running = mode === "minify" ? minify.isPending : jobRunning;
  const noProvider = mode === "llm" && settings.isSuccess && providers.length === 0;

  if (profile.isLoading) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (profile.error && !profile.data) return <ErrorMessage error={profile.error} />;
  const p = profile.data!;

  function run() {
    setJobId(null);
    if (mode === "minify") minify.mutate();
    else createJob.mutate();
  }

  // 폭 상한을 끈다. 이 화면의 본체는 diff 이고 넓을수록 잘 보인다 (D44 첫째의 full 용례). 상한이
  // 있으면 폼·변경 목록이 먹고 남는 diff 칸이 어느 화면에서나 384px 로 고정돼 좌우로 보기가 서지 못했다
  return (
    <PageWidth full data-testid="refine-page" data-mode={mode} className="flex flex-col gap-4">
      <PageHeader
        title={`${displayName(p)} · v${n}`}
        name={secondaryName(p)}
        subtitle={text.subtitle(t.shared.generateMenu.mode[mode], Number.isInteger(version) ? version + 1 : null)}
        testId="page-title"
        actions={
          <Link to={`/backend-apis/${encodeURIComponent(id)}`} className={buttonVariants({ variant: "outline" })} data-testid="refine-back">
            {specJob.back}
          </Link>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <Card data-testid="refine-form">
          <CardHeader>
            <CardTitle>{t.shared.generateMenu.mode[mode]}</CardTitle>
            <Badge variant="muted">{mode === "minify" ? text.instant : text.llmJob}</Badge>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                run();
              }}
            >
              {mode === "minify" ? (
                <>
                  <CheckList legend={text.enrichLegend} options={enrichOptions(t)} value={enrich} onChange={setEnrich} testIdPrefix="enrich" />
                  <Field label={text.maxDescription} htmlFor="max-description" hint={text.maxDescriptionHint}>
                    <Input id="max-description" data-testid="max-description" type="number" min={1} step={1} placeholder={text.maxDescriptionPlaceholder} value={maxDescription} onChange={(e) => setMaxDescription(e.target.value)} />
                    <FieldError error={runError} field="max_description" testId="refine-error-max_description" />
                  </Field>
                  <label className="flex items-start gap-2 text-sm">
                    <input type="checkbox" className="mt-0.5" data-testid="drop-value-examples" checked={dropValueExamples} onChange={(e) => setDropValueExamples(e.target.checked)} />
                    <span className="flex flex-col">
                      <span>{text.dropExamples}</span>
                      <span className="text-xs text-muted">{text.dropExamplesHint}</span>
                    </span>
                  </label>
                </>
              ) : (
                <>
                  <ProviderField prefix="refine" providers={providers} defaultProvider={defaultProvider} value={provider} onChange={setProvider} loading={settings.isLoading} noProvider={noProvider}>
                    <FieldError error={runError} field="provider" testId="refine-error-provider" />
                    {settings.error ? <ErrorMessage error={settings.error} testId="refine-settings-error" /> : null}
                  </ProviderField>
                  <CheckList legend={specJob.presets} options={presetOptions(t)} value={presets} onChange={setPresets} testIdPrefix="preset" showCode />
                  <InstructionField prefix="refine" value={instruction} onChange={setInstruction} placeholder={text.instructionPlaceholder}>
                    <FieldError error={runError} field="instruction" testId="refine-error-instruction" />
                  </InstructionField>
                </>
              )}

              {fieldOf(runError) === null ? <ErrorMessage error={runError} testId="refine-run-error" /> : null}
              {spec.error ? <ErrorMessage error={spec.error} testId="refine-spec-error" /> : null}

              {mode === "llm" ? (
                <p className="text-xs text-muted" data-testid="refine-llm-notice">
                  {text.llmNotice}
                </p>
              ) : null}

              <div className="flex justify-end">
                <Button type="submit" data-testid="refine-run" disabled={running || spec.isLoading || Boolean(spec.error) || noProvider || (mode === "llm" && !provider)}>
                  {running ? specJob.running : specJob.run}
                </Button>
              </div>
            </form>

            {mode === "llm" && jobId !== null ? <JobProgress job={job.data} error={job.error} /> : null}
          </CardContent>
        </Card>

        <div className="flex min-w-0 flex-col gap-3">
          {changes && spec.data ? (
            <>
              <SpecDiff
                original={spec.data}
                changes={changes}
                accepted={accepted}
                onAcceptedChange={setAccepted}
                renamed={renamed}
                referencingProfiles={renamed?.length ? referencing.data : undefined}
              />
              <FieldError error={apply.error} field="accepted_ids" testId="refine-error-accepted_ids" />
              {fieldOf(apply.error) === null ? <ErrorMessage error={apply.error} testId="save-error" /> : null}
              <div className="flex items-center justify-end gap-3">
                <span className="text-xs text-muted">{text.unaccepted}</span>
                <Button data-testid="apply-draft" disabled={apply.isPending || accepted.size === 0} onClick={() => apply.mutate()}>
                  {apply.isPending ? specJob.applying : text.apply}
                </Button>
              </div>
            </>
          ) : (
            <Card>
              <CardContent className="text-sm text-muted" data-testid="refine-empty">
                {spec.isLoading ? text.readingSpec : running ? specJob.waiting : text.idle}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </PageWidth>
  );
}

/** LLM job 진행: 상태, 반복 n/5, 반복마다 실패 항목 요약과 토큰, 합계. 실패면 error 를 보인다. */
function JobProgress({ job, error }: { job: RefineJob | undefined; error: unknown }) {
  const t = useT();
  const text = t.backendApis.refine.job;
  if (!error && !job) {
    return (
      <p className="mt-3 text-xs text-muted" data-testid="refine-progress">
        {text.creating}
      </p>
    );
  }
  return (
    <SpecJobProgress prefix="refine" job={job} error={error}>
      {job?.iterations.length ? (
        <ol className="flex flex-col gap-1">
          {job.iterations.map((it) => (
            <li key={it.n} data-testid={`refine-iteration-${it.n}`} className="flex flex-col gap-0.5 rounded-item bg-surface px-2 py-1">
              <div className="flex items-center gap-2">
                <span className="font-medium">#{it.n}</span>
                {it.batch && it.batch_total && it.batch_total > 1 ? (
                  <span className="text-muted" data-testid={`refine-iteration-${it.n}-batch`}>
                    {text.batch(it.batch, it.batch_total)}
                  </span>
                ) : null}
                <span className={it.failed_ids.length ? "text-warn-fg" : "text-ok-fg"}>{it.failed_ids.length ? text.failed(it.failed_ids.length) : text.passed}</span>
                <span className="ml-auto text-muted">{text.tokens(formatNumber(t, tokensOf(it.usage)))}</span>
              </div>
              {it.failed_ids.length ? (
                <div className="truncate font-mono text-[11px] text-muted" title={it.failed_ids.join("\n")}>
                  {it.failed_ids.slice(0, 3).join(", ")}
                  {it.failed_ids.length > 3 ? ` ${text.more(it.failed_ids.length - 3)}` : ""}
                </div>
              ) : null}
              {it.note ? <div className="text-muted">{it.note}</div> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-muted">{isActive(job?.state) ? text.waitingFirst : text.noIterations}</p>
      )}
    </SpecJobProgress>
  );
}
