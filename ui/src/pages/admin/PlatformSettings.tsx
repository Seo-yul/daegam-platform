import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError } from "@/api/client";
import { checkLlmProvider, getLlmGateway, getPlatformSettings, listClusters, patchPlatformSettings, setLlmProviderKey } from "@/api/resources";
import {
  ASSISTANT_EFFORTS,
  ASSISTANT_MAX_TOOL_CALLS,
  type AssistantEffort,
  type AssistantSettings,
  type LLMProvider,
  type LLMProviderCheck,
  type LlmGatewayProviderState,
  type PlatformSettings,
  type PlatformSettingsPatch,
  type ProviderKind,
} from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Link } from "react-router";

import { PageHeader } from "@/components/Layout";
import { useAdminAccess } from "./permission";
import { WebToolsSection } from "./WebToolsSettings";
import { type Messages, useT } from "@/i18n";
import { ASSISTANT_CONFIG_KEY, CLUSTERS_KEY, LLM_GATEWAY_KEY, PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";
import { DEFAULT_EMBEDDING_MODELS, supportsEmbedding } from "@/lib/knowledge";
import { DEFAULT_BASE_URLS, DEFAULT_MODELS, routingForm, routingFromForm, sdkKind, type RoutingForm } from "@/lib/providers";
import { ProviderRoutingFields } from "@/components/ProviderRoutingFields";
import { PageWidth } from "@/components/PageWidth";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HubImagesCard } from "@/components/settings/HubImagesCard";
import { IssuerSwitchCard } from "@/components/settings/IssuerSwitchCard";
import { LlmGatewayCard } from "@/components/settings/LlmGatewayCard";
import { OtlpCard } from "@/components/settings/OtlpCard";
import { PriceTable } from "@/components/settings/PriceTable";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate, ownValue } from "@/lib/utils";

/**
 * 폼의 provider 한 줄. `api_key_input` 은 화면에서 받은 키이고 PATCH 에 실리지 않는다. 저장할 때
 * 따로 `PUT .../api-key` 로 보내면 플랫폼이 관리 Secret 에 넣는다 (D56). `routing_form` 은 업체 선호의
 * 편집 칸이고 저장할 때 `routing` 으로 바꾼다 (T19).
 */
type FormProvider = LLMProvider & { api_key_input?: string; routing_form: RoutingForm };

/** 플랫폼이 화면에서 받은 키를 넣어 두는 Secret 의 앞머리. 서버 `MANAGED_SECRET_PREFIX` 와 같다. */
export const MANAGED_KEY_SECRET_PREFIX = "daegam-llm-key-";

/** provider 키가 지금 어디에 있는지 한 줄로. 키 값은 화면에 다시 보이지 않는다. */
export function keyStateText(t: Messages, p: FormProvider): string {
  const text = t.admin.platformSettings.provider.key;
  if (p.api_key_input?.trim()) return text.pending;
  const secret = p.api_key_secret?.trim();
  if (!secret) return text.none;
  return secret.startsWith(MANAGED_KEY_SECRET_PREFIX) ? text.managed(secret) : text.operator(secret);
}

interface FormState {
  default_runtime_image: string;
  default_agent_image: string;
  default_model: string;
  default_cluster_id: string;
  namespace_template: string;
  oidc_enabled: boolean;
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_audience: string;
  llm_providers: FormProvider[];
  spec_generator: string;
  /* 청지기 (D31) */
  assistant_provider: string;
  assistant_enabled: boolean;
  assistant_confirm_deploy: boolean;
  assistant_effort: AssistantEffort;
  assistant_max_tool_calls: string;
  assistant_turn_timeout: string;
}

function fromSettings(s: PlatformSettings): FormState {
  const a = s.assistant;
  return {
    default_runtime_image: s.default_runtime_image,
    default_agent_image: s.default_agent_image,
    default_model: s.default_model,
    default_cluster_id: s.default_cluster_id ?? "",
    namespace_template: s.namespace_template,
    oidc_enabled: s.oidc !== null,
    oidc_issuer: s.oidc?.issuer ?? "",
    oidc_client_id: s.oidc?.client_id ?? "",
    oidc_audience: s.oidc?.audience ?? "",
    llm_providers: s.llm_providers.map((p) => ({ ...p, routing_form: routingForm(p.routing) })),
    spec_generator: s.defaults.spec_generator ?? "",
    assistant_provider: s.defaults.assistant ?? "",
    assistant_enabled: a.enabled,
    assistant_confirm_deploy: a.confirm_deploy,
    assistant_effort: a.effort,
    assistant_max_tool_calls: String(a.max_tool_calls_per_turn),
    assistant_turn_timeout: String(a.turn_timeout_seconds),
  };
}

/** 폼 → PATCH 본문. 원본과 다른 키만 넣는다. */
export function buildPatch(orig: PlatformSettings, f: FormState): PlatformSettingsPatch {
  const patch: PlatformSettingsPatch = {};
  for (const k of ["default_runtime_image", "default_agent_image", "default_model", "namespace_template"] as const) {
    if (f[k].trim() !== orig[k]) patch[k] = f[k].trim();
  }
  const cluster = f.default_cluster_id || null;
  if (cluster !== orig.default_cluster_id) patch.default_cluster_id = cluster;
  const oidc = f.oidc_enabled ? { issuer: f.oidc_issuer.trim(), client_id: f.oidc_client_id.trim(), audience: f.oidc_audience.trim() || null } : null;
  if (JSON.stringify(oidc) !== JSON.stringify(orig.oidc)) patch.oidc = oidc;
  // embedding_model 은 Knowledge Base 가 쓰는 임베딩 모델이다(D32). 빈 문자열은 null(kind 기본)이고 supportsEmbedding 이 거짓인 kind 는 항상 null 이다
  const providers = f.llm_providers.map((p) => ({
    name: p.name.trim(),
    kind: p.kind,
    model: p.model?.trim() || null,
    api_key_secret: p.api_key_secret?.trim() || null,
    base_url: p.base_url?.trim() || null,
    embedding_model: supportsEmbedding(p.kind) ? p.embedding_model?.trim() || null : null,
    // 업체 선호는 openrouter 만 받는다. 다른 kind 로 바꾸면 적혀 있던 값은 서버가 거부하므로 비운다
    routing: p.kind === "openrouter" ? routingFromForm(p.routing_form) : null,
    stream_usage: p.stream_usage ?? true,
  }));
  const origProviders = orig.llm_providers.map((p) => ({
    name: p.name,
    kind: p.kind,
    model: p.model ?? null,
    api_key_secret: p.api_key_secret ?? null,
    base_url: p.base_url ?? null,
    embedding_model: p.embedding_model ?? null,
    routing: routingFromForm(routingForm(p.routing)),
    stream_usage: p.stream_usage ?? true,
  }));
  if (JSON.stringify(providers) !== JSON.stringify(origProviders)) patch.llm_providers = providers;
  // defaults 는 통째로 보낸다. 부분 객체를 보내면 서버가 다른 키를 비울 수 있다
  const sg = f.spec_generator || null;
  const ap = f.assistant_provider || null;
  if (sg !== (orig.defaults.spec_generator ?? null) || ap !== (orig.defaults.assistant ?? null)) patch.defaults = { spec_generator: sg, assistant: ap };
  const origAssistant = orig.assistant;
  const assistant: AssistantSettings = {
    enabled: f.assistant_enabled,
    confirm_deploy: f.assistant_confirm_deploy,
    effort: f.assistant_effort,
    max_tool_calls_per_turn: Number(f.assistant_max_tool_calls) || origAssistant.max_tool_calls_per_turn,
    turn_timeout_seconds: Number(f.assistant_turn_timeout) || origAssistant.turn_timeout_seconds,
  };
  if ((Object.keys(assistant) as (keyof AssistantSettings)[]).some((k) => assistant[k] !== origAssistant[k])) patch.assistant = assistant;
  return patch;
}

type CheckState = { status: "pending" } | { status: "done"; result: LLMProviderCheck } | { status: "error"; code: string };

/** 연결 확인 배지. 결과 문구는 사전이 코드로 고른다. 서버는 고정 코드만 주고 provider 의 원문 메시지는 주지 않는다 (D25). */
function checkBadge(t: Messages, state: CheckState): { variant: BadgeProps["variant"]; text: string } {
  const text = t.admin.platformSettings.provider;
  if (state.status === "pending") return { variant: "muted", text: text.checking };
  if (state.status === "error") return { variant: "error", text: text.checkFailed(state.code) };
  return { variant: state.result.ok ? "ok" : "error", text: text.checkResult[state.result.code] };
}

/** 저장 결과 안내. 상태에는 문장 대신 무슨 일이 있었는지만 둔다. keyed 는 키를 Secret 에 넣은 provider 이름이다. */
type Notice = { kind: "saved"; keyed: string[] } | { kind: "noChange" };

function noticeText(t: Messages, notice: Notice): string {
  if (notice.kind === "noChange") return t.errors.noChange;
  return notice.keyed.length ? t.admin.platformSettings.savedKeys(notice.keyed.join(", ")) : t.shared.saved;
}

/** 이름 선택지. spec_generator·assistant 기본값 두 Select 가 같이 쓴다. Select 는 option 요소만 읽으므로 컴포넌트가 아니라 배열로 준다. */
function providerOptions(providers: FormProvider[]) {
  return providers.map((p) => (
    <option key={p.name} value={p.name}>
      {p.name}
    </option>
  ));
}

/**
 * 게이트웨이를 켰을 때 provider 줄의 "게이트웨이 반영" 상태 (P38 11절). 연결 확인 결과와 따로다. 연결 확인은 provider 를 바로
 * 부르고, 게이트웨이는 저장한 키를 2분 안에 반영한다. 거절은 주소 검사나 키가 없어 렌더하지 않은 까닭이고 제목에 그 글을 단다.
 */
function GatewayState({ state, i }: { state: LlmGatewayProviderState; i: number }) {
  const t = useT();
  const text = t.admin.platformSettings.provider.gateway;
  const badge = state.rejected
    ? { variant: "error" as const, label: text.rejected }
    : state.ready
      ? { variant: "ok" as const, label: text.applied }
      : { variant: "muted" as const, label: text.pending };
  return (
    <div className="flex flex-col items-start gap-0.5" title={state.rejected ?? text.hint}>
      <Badge variant={badge.variant} data-testid={`ps-provider-gateway-${i}`}>
        {badge.label}
      </Badge>
      {state.key_synced_at ? <span className="text-[11px] text-muted">{text.keySynced(formatDate(t, state.key_synced_at))}</span> : null}
    </div>
  );
}

/** provider 표의 한 줄. 연결 확인은 저장된 provider 만 부를 수 있어 `saved` 가 아니면 버튼을 잠근다. */
function ProviderRow({
  p,
  i,
  saved,
  check,
  gateway,
  onChange,
  onCheck,
  onRemove,
}: {
  p: FormProvider;
  i: number;
  saved: boolean;
  check: CheckState | undefined;
  /** 켠 게이트웨이의 이 provider 상태. 꺼져 있거나 모르면 없다 */
  gateway: LlmGatewayProviderState | undefined;
  onChange: (patch: Partial<FormProvider>) => void;
  onCheck: () => void;
  onRemove: () => void;
}) {
  const t = useT();
  const text = t.admin.platformSettings.provider;
  // 칸마다 표 머리의 이름을 낭독기 이름으로 쓴다
  const columns = t.admin.platformSettings.providers.columns;
  const badge = check ? checkBadge(t, check) : null;
  return (
    <TR data-testid={`ps-provider-row-${i}`}>
      <TD>
        <Input aria-label={columns.name} data-testid={`ps-provider-name-${i}`} className="h-8 min-w-[7.5rem]" value={p.name} onChange={(e) => onChange({ name: e.target.value })} />
      </TD>
      <TD>
        <Select
          aria-label={columns.kind}
          data-testid={`ps-provider-kind-${i}`}
          className="h-8 min-w-[8.5rem]"
          value={p.kind}
          onChange={(e) => {
            const kind = e.target.value as ProviderKind;
            // anthropic·openrouter·zai 는 임베딩 모델을 받지 않는다. 적혀 있던 값은 서버가 거부하므로 여기서 비운다
            onChange(supportsEmbedding(kind) ? { kind } : { kind, embedding_model: null });
          }}
        >
          {(Object.keys(text.kindNames) as ProviderKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {text.kindNames[kind]}
            </option>
          ))}
        </Select>
      </TD>
      <TD>
        <Input aria-label={columns.model} data-testid={`ps-provider-model-${i}`} className="h-8 min-w-[8.5rem]" placeholder={DEFAULT_MODELS[p.kind]} value={p.model ?? ""} onChange={(e) => onChange({ model: e.target.value })} />
      </TD>
      <TD>
        <Input
          aria-label={columns.embedding}
          data-testid={`ps-provider-embedding-model-${i}`}
          className="h-8 min-w-[8rem]"
          disabled={!supportsEmbedding(p.kind)}
          title={supportsEmbedding(p.kind) ? text.embeddingHint : (ownValue(text.noEmbedding, p.kind) ?? text.noEmbeddingApi)}
          placeholder={DEFAULT_EMBEDDING_MODELS[p.kind] ?? ""}
          value={p.embedding_model ?? ""}
          onChange={(e) => onChange({ embedding_model: e.target.value })}
        />
      </TD>
      <TD>
        {/* 키는 저장할 때 따로 보내 플랫폼이 Secret 에 넣는다(D56). 사용자는 클러스터에 들어갈 수 없다.
            이미 있는 Secret(운영자가 만든 것)을 쓰려면 접힌 칸에 이름을 적는다 */}
        <div className="flex min-w-[13rem] flex-col gap-1">
          <Input
            type="password"
            autoComplete="new-password"
            aria-label={columns.apiKey}
            aria-required={!p.api_key_secret}
            data-testid={`ps-provider-key-${i}`}
            className="h-8"
            placeholder={p.api_key_secret ? text.keyReplace : columns.apiKey}
            value={p.api_key_input ?? ""}
            onChange={(e) => onChange({ api_key_input: e.target.value })}
          />
          <span className={p.api_key_secret || p.api_key_input?.trim() ? "text-[11px] text-muted" : "text-[11px] text-warn-fg"} data-testid={`ps-provider-key-state-${i}`}>
            {keyStateText(t, p)}
          </span>
          <details className="text-[11px]">
            <summary className="cursor-pointer text-muted" data-testid={`ps-provider-secret-toggle-${i}`}>
              {text.useSecret}
            </summary>
            <Input
              aria-label={text.secretName}
              data-testid={`ps-provider-secret-${i}`}
              className="mt-1 h-7 font-mono text-xs placeholder:font-sans"
              placeholder={text.secretName}
              value={p.api_key_secret ?? ""}
              onChange={(e) => onChange({ api_key_secret: e.target.value })}
            />
          </details>
        </div>
      </TD>
      <TD>
        <Input aria-label={text.baseUrl} data-testid={`ps-provider-url-${i}`} className="h-8" placeholder={DEFAULT_BASE_URLS[p.kind] ?? text.baseUrlDefault} value={p.base_url ?? ""} onChange={(e) => onChange({ base_url: e.target.value })} />
        {p.kind === "openrouter" ? (
          <ProviderRoutingFields i={i} value={p.routing_form} onChange={(patch) => onChange({ routing_form: { ...p.routing_form, ...patch } })} />
        ) : null}
        {/* openai SDK 로 부르는 kind 만 스트리밍 사용량을 묻는 설정이 있다 (P37 2절) */}
        {sdkKind(p.kind) === "openai" ? (
          <label className="mt-1 flex items-center gap-1.5 text-[11px] text-muted" title={text.streamUsageHint}>
            <input type="checkbox" data-testid={`ps-provider-stream-usage-${i}`} checked={p.stream_usage ?? true} onChange={(e) => onChange({ stream_usage: e.target.checked })} />
            {text.streamUsage}
          </label>
        ) : null}
      </TD>
      <TD>
        <div className="flex flex-col items-start gap-1.5">
          <Button
            size="sm"
            variant="outline"
            data-testid={`ps-provider-check-${i}`}
            disabled={!saved || check?.status === "pending"}
            title={saved ? text.checkTitle : text.checkUnsaved}
            onClick={onCheck}
          >
            {text.check}
          </Button>
          {badge ? (
            <Badge variant={badge.variant} data-testid={`ps-provider-check-result-${i}`}>
              {badge.text}
            </Badge>
          ) : null}
          {gateway ? <GatewayState state={gateway} i={i} /> : null}
        </div>
      </TD>
      <TD>
        <Button size="sm" variant="ghost" data-testid={`ps-provider-remove-${i}`} onClick={onRemove}>
          {t.shared.remove}
        </Button>
      </TD>
    </TR>
  );
}

export function PlatformSettingsPage() {
  const t = useT();
  const text = t.admin.platformSettings;
  const userLabel = useUserLabel();
  const queryClient = useQueryClient();
  const { readOnly } = useAdminAccess();
  const q = useQuery({ queryKey: PLATFORM_SETTINGS_KEY, queryFn: getPlatformSettings });
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  // provider 줄의 게이트웨이 반영 상태 (P38 11절). 게이트웨이 카드와 같은 조회이고 플랫폼 관리자만 읽는다
  const gateway = useQuery({ queryKey: LLM_GATEWAY_KEY, queryFn: getLlmGateway, enabled: !readOnly });
  const gatewayOn = gateway.data !== undefined && gateway.data.switch !== "off";
  const [form, setForm] = useState<FormState | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // provider 이름 → 연결 확인 결과. 저장된 provider 만 확인할 수 있으므로 이름으로 둔다
  const [checks, setChecks] = useState<Record<string, CheckState>>({});
  // 이 폼의 절이 서버에서 바뀌었을 때만 폼을 다시 채운다. 아래 웹 Tool 카드의 저장이 이 폼의 고친 값을 지우지 않게 한다
  const own = q.data ? JSON.stringify(fromSettings(q.data)) : null;
  useEffect(() => {
    if (q.data) setForm(fromSettings(q.data));
  }, [own]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = (patch: Partial<FormState>) => setForm((f) => (f ? { ...f, ...patch } : f));

  async function runCheck(name: string) {
    setChecks((c) => ({ ...c, [name]: { status: "pending" } }));
    try {
      const result = await checkLlmProvider(name);
      setChecks((c) => ({ ...c, [name]: { status: "done", result } }));
    } catch (e) {
      setChecks((c) => ({ ...c, [name]: { status: "error", code: e instanceof ApiError ? e.code : "error" } }));
    }
  }

  // 설정 PATCH 뒤 키를 하나씩 넣는다. 새 provider 는 PATCH 가 먼저 만들어야 키를 받을 수 있다.
  // 키는 PATCH 본문에 실리지 않는다 (D56)
  const save = useMutation({
    mutationFn: async ({ patch, keys }: { patch: PlatformSettingsPatch; keys: { name: string; key: string }[] }) => {
      if (Object.keys(patch).length) await patchPlatformSettings(patch);
      for (const k of keys) await setLlmProviderKey(k.name, k.key);
      return keys.map((k) => k.name);
    },
    onSuccess: async (keyed) => {
      setNotice({ kind: "saved", keyed });
      await queryClient.invalidateQueries({ queryKey: PLATFORM_SETTINGS_KEY });
      // 비서를 켜고 끄면 헤더의 청지기 토글이 바로 따라온다 (D31)
      await queryClient.invalidateQueries({ queryKey: ASSISTANT_CONFIG_KEY });
      // 넣은 키가 실제로 통하는지 바로 확인한다
      for (const name of keyed) void runCheck(name);
    },
    onError: async () => {
      // PATCH 는 됐는데 키 넣기에서 멈췄을 수 있다. 서버 상태를 다시 읽어 폼이 그것을 보이게 한다
      await queryClient.invalidateQueries({ queryKey: PLATFORM_SETTINGS_KEY });
    },
  });

  if (q.isLoading || !form) return q.error ? <ErrorMessage error={q.error} /> : <p className="text-sm text-muted">{t.common.loading}</p>;
  const orig = q.data!;

  function submit() {
    const patch = buildPatch(orig, form!);
    const keys = form!.llm_providers.filter((p) => p.api_key_input?.trim()).map((p) => ({ name: p.name.trim(), key: p.api_key_input!.trim() }));
    if (!Object.keys(patch).length && !keys.length) {
      setNotice({ kind: "noChange" });
      return;
    }
    setNotice(null);
    save.mutate({ patch, keys });
  }

  function setProvider(i: number, patch: Partial<FormProvider>) {
    set({ llm_providers: form!.llm_providers.map((p, j) => (j === i ? { ...p, ...patch } : p)) });
  }

  return (
    <PageWidth data-testid="platform-settings-page">
      <PageHeader
        title={text.labels.title}
        testId="page-title"
        subtitleTestId="settings-scope-hint"
        subtitle={text.scopeHint(
          <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/tenant/settings">
            {text.labels.tenantSettings}
          </Link>,
        )}
      />
      {readOnly ? (
        <Alert variant="info" className="my-3" data-testid="readonly-notice">
          {t.admin.readOnly}
        </Alert>
      ) : null}
      <form
        className="grid gap-4 lg:grid-cols-2"
        data-testid="platform-settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {/* 권한이 없으면 칸도 버튼도 통째로 잠근다. 다 고쳐 놓고 저장할 때 403 을 만나지 않게 한다 */}
        <fieldset className="contents" disabled={readOnly}>
          <Card>
            <CardHeader>
              <CardTitle>{text.labels.defaults}</CardTitle>
              <span className="text-xs text-muted">
                {text.labels.updated} {formatDate(t, orig.updated_at)} {orig.updated_by ? t.shared.byUser(userLabel(orig.updated_by)) : ""}
              </span>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <Field label={text.labels.runtimeImage} hint={text.hints.runtimeImage} htmlFor="ps-runtime-image">
                <Input id="ps-runtime-image" data-testid="ps-runtime-image" value={form.default_runtime_image} onChange={(e) => set({ default_runtime_image: e.target.value })} />
              </Field>
              <Field label={text.labels.agentImage} hint={text.hints.agentImage} htmlFor="ps-agent-image">
                <Input id="ps-agent-image" data-testid="ps-agent-image" value={form.default_agent_image} onChange={(e) => set({ default_agent_image: e.target.value })} />
              </Field>
              <Field label={text.labels.model} hint={text.hints.model} htmlFor="ps-model">
                <Input id="ps-model" data-testid="ps-model" value={form.default_model} onChange={(e) => set({ default_model: e.target.value })} />
              </Field>
              <Field label={text.labels.cluster} hint={text.hints.cluster} htmlFor="ps-cluster">
                <Select id="ps-cluster" data-testid="ps-cluster" value={form.default_cluster_id} onChange={(e) => set({ default_cluster_id: e.target.value })}>
                  <option value="">{text.labels.homeCluster}</option>
                  {clusters.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={text.labels.namespace} htmlFor="ps-ns" hint={text.hints.namespace}>
                <Input id="ps-ns" data-testid="ps-ns" value={form.namespace_template} onChange={(e) => set({ namespace_template: e.target.value })} />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{text.labels.oidc}</CardTitle>
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" data-testid="ps-oidc-enabled" checked={form.oidc_enabled} onChange={(e) => set({ oidc_enabled: e.target.checked })} />
                {text.labels.configured}
              </label>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <Field label={text.labels.issuer} hint={text.hints.issuer} htmlFor="ps-oidc-issuer">
                <Input id="ps-oidc-issuer" data-testid="ps-oidc-issuer" disabled={!form.oidc_enabled} value={form.oidc_issuer} onChange={(e) => set({ oidc_issuer: e.target.value })} />
              </Field>
              <Field label={text.labels.clientId} hint={text.hints.clientId} htmlFor="ps-oidc-client">
                <Input id="ps-oidc-client" data-testid="ps-oidc-client" disabled={!form.oidc_enabled} value={form.oidc_client_id} onChange={(e) => set({ oidc_client_id: e.target.value })} />
              </Field>
              <Field label={text.labels.audience} hint={text.hints.audience} htmlFor="ps-oidc-audience">
                <Input id="ps-oidc-audience" data-testid="ps-oidc-audience" disabled={!form.oidc_enabled} value={form.oidc_audience} onChange={(e) => set({ oidc_audience: e.target.value })} />
              </Field>
              <p className="text-xs text-muted">{text.oidcRedirect}</p>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2" data-testid="ps-assistant">
            <CardHeader>
              <CardTitle>{text.assistant.title}</CardTitle>
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" data-testid="ps-assistant-enabled" checked={form.assistant_enabled} onChange={(e) => set({ assistant_enabled: e.target.checked })} />
                {text.labels.enabled}
              </label>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <p className="text-xs text-muted">{text.assistant.body}</p>
              <div className="grid gap-3 md:grid-cols-4">
                <Field label={text.labels.effort} htmlFor="ps-assistant-effort" hint={text.assistant.effortHint}>
                  <Select id="ps-assistant-effort" data-testid="ps-assistant-effort" disabled={!form.assistant_enabled} value={form.assistant_effort} onChange={(e) => set({ assistant_effort: e.target.value as AssistantEffort })}>
                    {ASSISTANT_EFFORTS.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label={text.labels.maxToolCalls} htmlFor="ps-assistant-max-tool-calls" hint={text.assistant.maxToolCallsHint(ASSISTANT_MAX_TOOL_CALLS)}>
                  <Input id="ps-assistant-max-tool-calls" data-testid="ps-assistant-max-tool-calls" type="number" max={ASSISTANT_MAX_TOOL_CALLS} min={1} disabled={!form.assistant_enabled} value={form.assistant_max_tool_calls} onChange={(e) => set({ assistant_max_tool_calls: e.target.value })} />
                </Field>
                <Field label={text.labels.turnTimeout} htmlFor="ps-assistant-turn-timeout" hint={text.assistant.timeoutHint}>
                  <Input id="ps-assistant-turn-timeout" data-testid="ps-assistant-turn-timeout" type="number" min={1} disabled={!form.assistant_enabled} value={form.assistant_turn_timeout} onChange={(e) => set({ assistant_turn_timeout: e.target.value })} />
                </Field>
                <div className="flex flex-col gap-1">
                  <span className="text-sm font-medium text-fg-soft">{text.assistant.confirmDeploy}</span>
                  <label className="flex h-9 items-center gap-2 text-sm">
                    <input type="checkbox" data-testid="ps-assistant-confirm-deploy" disabled={!form.assistant_enabled} checked={form.assistant_confirm_deploy} onChange={(e) => set({ assistant_confirm_deploy: e.target.checked })} />
                    confirm_deploy
                  </label>
                  <p className="text-xs text-muted">{text.assistant.confirmDeployHint}</p>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>{text.labels.providers}</CardTitle>
              <div className="flex items-center gap-2 text-xs">
                <span>{text.labels.specGenerator}</span>
                <Select aria-label={text.labels.specGenerator} data-testid="ps-spec-generator" className="h-8 w-56" value={form.spec_generator} onChange={(e) => set({ spec_generator: e.target.value })}>
                  <option value="">{text.labels.firstProvider}</option>
                  {providerOptions(form.llm_providers)}
                </Select>
                <span>{text.labels.assistantProvider}</span>
                <Select aria-label={text.labels.assistantProvider} data-testid="ps-assistant-provider" className="h-8 w-56" value={form.assistant_provider} onChange={(e) => set({ assistant_provider: e.target.value })}>
                  <option value="">{text.providers.followSpecGenerator}</option>
                  {providerOptions(form.llm_providers)}
                </Select>
                <Button
                  size="sm"
                  variant="outline"
                  data-testid="ps-provider-add"
                  onClick={() => set({ llm_providers: [...form.llm_providers, { name: "", kind: "openai", model: "", api_key_secret: "", base_url: "", embedding_model: "", routing_form: routingForm(null), stream_usage: true }] })}
                >
                  {text.providers.add}
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {/*
                칸 여백을 줄이고 이름·모델 칸에 최소 폭을 준다. 연결 결과 배지가 붙어도 openai-mini·gpt-4.1-mini 가 잘리지 않는다.
                칸 이름은 한 줄로 두고, 표가 카드보다 넓으면 가로로 스크롤한다
              */}
              <Table data-testid="ps-providers" className="[&_td]:px-2 [&_th]:px-2 [&_th]:whitespace-nowrap">
                <THead>
                  <tr>
                    <TH>{text.providers.columns.name}</TH>
                    <TH>{text.providers.columns.kind}</TH>
                    <TH>{text.providers.columns.model}</TH>
                    <TH>{text.providers.columns.embedding}</TH>
                    <TH>
                      {text.providers.columns.apiKey} <span className="text-err-fg" aria-hidden>*</span>
                    </TH>
                    <TH>{text.provider.baseUrl}</TH>
                    <TH>{text.providers.columns.connection}</TH>
                    <TH></TH>
                  </tr>
                </THead>
                <TBody>
                  {form.llm_providers.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="px-3 py-4 text-center text-sm text-muted" data-testid="ps-providers-empty">
                        {text.providers.empty}
                      </td>
                    </tr>
                  ) : null}
                  {form.llm_providers.map((p, i) => (
                    <ProviderRow
                      key={i}
                      p={p}
                      i={i}
                      // 확인은 저장된 provider 를 이름으로 부른다. 아직 저장하지 않은 행은 서버가 모른다
                      saved={orig.llm_providers.some((o) => o.name === p.name)}
                      check={checks[p.name]}
                      gateway={gatewayOn ? gateway.data?.providers.find((g) => g.name === p.name) : undefined}
                      onChange={(patch) => setProvider(i, patch)}
                      onCheck={() => void runCheck(p.name)}
                      onRemove={() => set({ llm_providers: form.llm_providers.filter((_, j) => j !== i) })}
                    />
                  ))}
                </TBody>
              </Table>
              <p className="mt-2 text-xs text-muted" data-testid="ps-provider-secret-hint">
                <span className="text-err-fg" aria-hidden>
                  *
                </span>{" "}
                {text.providers.secretHint}
              </p>
            </CardContent>
          </Card>

          <div className="flex items-center justify-end gap-3 lg:col-span-2">
            {notice ? (
              <Alert variant={notice.kind === "saved" ? "ok" : "info"} data-testid="ps-notice">
                {noticeText(t, notice)}
              </Alert>
            ) : null}
            <ErrorMessage error={save.error} testId="save-error" />
            <Button variant="ghost" onClick={() => setForm(fromSettings(orig))}>
              {t.shared.revert}
            </Button>
            <Button type="submit" data-testid="save-button" disabled={save.isPending}>
              {save.isPending ? t.shared.saving : t.shared.saveChanges}
            </Button>
          </div>
        </fieldset>
      </form>
      <IssuerSwitchCard />
      <LlmGatewayCard orig={orig} readOnly={readOnly} />
      <OtlpCard readOnly={readOnly} />
      <HubImagesCard orig={orig} readOnly={readOnly} />
      <PriceTable />
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <WebToolsSection orig={orig} readOnly={readOnly} />
      </div>
    </PageWidth>
  );
}
