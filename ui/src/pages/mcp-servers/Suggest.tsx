/**
 * 도구 오버라이드 제안 (D37). /mcp-servers/:id/suggest
 *
 * 왼쪽 폼에서 provider·프리셋·지시를 고르고 job 을 만들면, 서버가 노출되는 operation 의 원본 이름·
 * 설명과 지금 걸린 오버라이드만 모델에 보내고 돌려받은 값을 현재 리비전 위에 투영한다. 오른쪽에서
 * 항목을 골라 "채택해 새 리비전 만들기" 를 누르면 리비전이 오른다.
 *
 * 변경 목록은 `OverrideChanges` 다. `SpecDiff` 는 OpenAPI 문서를 전제해서 여기서는 화면만 틀리고
 * `tsc` 가 잡아 주지 않는다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { applyMcpServerDraft, createOverridesJob, getMcpServer, getPlatformSettings, listBackendApis } from "@/api/resources";
import { OVERRIDES_PRESETS, type OverridesPreset } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { PageHeader } from "@/components/Layout";
import { OverrideChanges } from "@/components/OverrideChanges";
import { PageWidth } from "@/components/PageWidth";
import { CheckList, InstructionField, ProviderField, SpecJobProgress, useSpecJob, useSpecProvider, type CheckOption } from "@/components/SpecJobForm";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { displayName, secondaryName } from "@/lib/names";
import { PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** 프리셋 선택지. 이름은 Backend API 다듬기와 같이 쓰고 풀이는 오버라이드 제안의 것이다. */
function presetOptions(t: Messages): CheckOption<OverridesPreset>[] {
  return OVERRIDES_PRESETS.map((value) => ({ value, label: t.backendApis.specJob.preset[value], hint: t.mcpServers.suggest.presetHint[value] }));
}

export function McpServerSuggestPage() {
  const t = useT();
  const text = t.mcpServers.suggest;
  const specJob = t.backendApis.specJob;
  const { id = "" } = useParams();
  const tenant = useTenant();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const profile = useQuery({ queryKey: ["mcp-servers", tenant, id], queryFn: () => getMcpServer(id) });
  const backends = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const settings = useQuery({ queryKey: PLATFORM_SETTINGS_KEY, queryFn: getPlatformSettings });

  const { providers, defaultProvider, provider, setProvider } = useSpecProvider(settings.data);
  const [presets, setPresets] = useState<Set<OverridesPreset>>(() => new Set<OverridesPreset>(["enrich_descriptions"]));
  const [instruction, setInstruction] = useState("");

  const revision = profile.data?.revision ?? 0;
  const { jobId, createJob, job, changes, running } = useSpecJob(tenant, () =>
    createOverridesJob({
      mode: "overrides",
      source: { mcp_server_id: id, revision },
      presets: OVERRIDES_PRESETS.filter((p) => presets.has(p)),
      ...(instruction.trim() ? { instruction: instruction.trim() } : {}),
      ...(provider ? { provider } : {}),
    }),
  );

  const [accepted, setAccepted] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (changes) setAccepted(new Set(changes.map((c) => c.id)));
  }, [changes]);

  const apply = useMutation({
    mutationFn: () =>
      applyMcpServerDraft(id, {
        job_id: jobId as string,
        accepted_ids: changes ? changes.filter((c) => accepted.has(c.id)).map((c) => c.id) : [],
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-servers", tenant, id], refetchType: "all" });
      await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      navigate(`/mcp-servers/${encodeURIComponent(id)}`);
    },
  });

  const noProvider = settings.isSuccess && providers.length === 0;

  if (profile.isLoading) return <p className="text-sm text-muted">{t.common.loading}</p>;
  if (profile.error && !profile.data) return <ErrorMessage error={profile.error} />;
  const p = profile.data!;
  const backendLabel = (index: number) => {
    const sel = p.backends[index];
    return sel ? backends.data?.find((b) => b.id === sel.backend_profile_id)?.name : undefined;
  };

  return (
    <PageWidth data-testid="suggest-page" className="flex flex-col gap-4">
      <PageHeader
        title={`${displayName(p)} · r${p.revision}`}
        name={secondaryName(p)}
        subtitle={text.subtitle}
        testId="page-title"
        actions={
          <Link to={`/mcp-servers/${encodeURIComponent(id)}`} className={buttonVariants({ variant: "outline" })} data-testid="suggest-back">
            {specJob.back}
          </Link>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <Card data-testid="suggest-form">
          <CardHeader>
            <CardTitle>{text.title}</CardTitle>
            <Badge variant="muted">{text.llmJob}</Badge>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                createJob.mutate();
              }}
            >
              <ProviderField prefix="suggest" providers={providers} defaultProvider={defaultProvider} value={provider} onChange={setProvider} loading={settings.isLoading} noProvider={noProvider} />
              <CheckList legend={specJob.presets} options={presetOptions(t)} value={presets} onChange={setPresets} testIdPrefix="preset" showCode />
              <InstructionField prefix="suggest" value={instruction} onChange={setInstruction} placeholder={text.instructionPlaceholder} />

              <ErrorMessage error={createJob.error} testId="suggest-run-error" />

              <p className="text-xs text-muted" data-testid="suggest-llm-notice">
                {text.notice}
              </p>

              <div className="flex justify-end">
                <Button type="submit" data-testid="suggest-run" disabled={running || noProvider || !provider || (presets.size === 0 && !instruction.trim())}>
                  {running ? specJob.running : specJob.run}
                </Button>
              </div>
            </form>

            {/* 이 모드는 검사 파이프라인이 없어 반복 기록이 다듬기보다 단순하다. 진행 줄만 둔다 */}
            {jobId !== null ? <SpecJobProgress prefix="suggest" job={job.data} error={job.error} /> : null}
          </CardContent>
        </Card>

        <div className="flex min-w-0 flex-col gap-3">
          {changes ? (
            <>
              <OverrideChanges changes={changes} accepted={accepted} onAcceptedChange={setAccepted} backendLabel={backendLabel} />
              <Alert variant="info" data-testid="suggest-deploy-notice">
                {text.deployNotice}
              </Alert>
              <ErrorMessage error={apply.error} testId="suggest-apply-error" />
              <div className="flex items-center justify-end gap-3">
                <span className="text-xs text-muted">{text.unaccepted}</span>
                <Button data-testid="apply-suggest" disabled={apply.isPending || accepted.size === 0} onClick={() => apply.mutate()}>
                  {apply.isPending ? specJob.applying : text.apply(p.revision + 1)}
                </Button>
              </div>
            </>
          ) : (
            <Card>
              <CardContent className="text-sm text-muted" data-testid="suggest-empty">
                {running ? specJob.waiting : text.empty}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </PageWidth>
  );
}
