/**
 * LLM 게이트웨이 카드 (P38 11절). Platform Settings 아래에 놓인다.
 *
 * 위는 상태다. `GET /settings/llm-gateway` 는 테넌트를 가로질러 세므로 플랫폼 관리자만 읽는다. 켜져 있거나, 끈 뒤에도 게이트웨이
 * 경로의 Agent·옮기는 중인 Agent·정리·게이트웨이 파드가 남아 있으면(되돌리기를 기다리는 중, llm-gateway.md 11절) 상태를 보이고 다시
 * 읽는다. 켜고 끄는 것은 설치 env 라 이 화면에 단추가 없다. 아래는 분당 토큰 한도(`llm_gateway.limits`)이고 이 카드의 단추 하나가 Platform Settings
 * PATCH 로 저장한다. 테넌트는 등록부가 없어 이름의 꼴만 서버가 보고, 화면은 아는 테넌트가 아닌 이름에 안내를 붙인다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { getLlmGateway, listMyTenants, patchPlatformSettings } from "@/api/resources";
import type { LlmGatewayLimits, LlmGatewayStatus, PlatformSettings } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { useT, type Messages } from "@/i18n";
import { useAuthState } from "@/lib/auth";
import { LLM_GATEWAY_KEY, PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";
import { TELEMETRY_POLL_MS } from "@/lib/usage";
import { formatDate, ownValue } from "@/lib/utils";

/** 서버 `LIMIT_MAX` 와 같다. 한도 값은 1 에서 이 값까지의 정수다 (P38 7절). */
export const LLM_LIMIT_MAX = 1_000_000_000;

/** 꺼진 게이트웨이의 스위치 값이다. */
const SWITCH_OFF = "off";

interface LimitRow {
  name: string;
  tokens: string;
}

interface LimitsForm {
  tenantDefault: string;
  tenants: LimitRow[];
  providers: LimitRow[];
}

function rowsOf(table: Record<string, number> | undefined): LimitRow[] {
  return Object.entries(table ?? {}).map(([name, n]) => ({ name, tokens: String(n) }));
}

function formOf(limits: LlmGatewayLimits): LimitsForm {
  return {
    tenantDefault: limits.tenant_tokens_per_minute == null ? "" : String(limits.tenant_tokens_per_minute),
    tenants: rowsOf(limits.tenants),
    providers: rowsOf(limits.providers),
  };
}

/** 한도 칸 하나의 값. 비면 null 이고, 1 에서 LLM_LIMIT_MAX 밖이거나 정수가 아니면 undefined 다. */
export function limitOf(text: string): number | null | undefined {
  if (!text.trim()) return null;
  const n = Number(text);
  return Number.isInteger(n) && n >= 1 && n <= LLM_LIMIT_MAX ? n : undefined;
}

/** 폼 → `llm_gateway.limits`. 이름이 빈 줄은 뺀다. 값이 틀린 칸이 있으면 null 이라 저장하지 않는다. */
export function limitsFromForm(form: LimitsForm): LlmGatewayLimits | null {
  const table = (rows: LimitRow[]): Record<string, number> | null => {
    const out: Record<string, number> = {};
    for (const row of rows) {
      if (!row.name.trim()) continue;
      const n = limitOf(row.tokens);
      if (n == null) return null;
      out[row.name.trim()] = n;
    }
    return out;
  };
  const tenantDefault = limitOf(form.tenantDefault);
  const tenants = table(form.tenants);
  const providers = table(form.providers);
  if (tenantDefault === undefined || tenants === null || providers === null) return null;
  return { tenant_tokens_per_minute: tenantDefault, tenants, providers };
}

/** 끈 뒤에도 남은 것이 있는가. 게이트웨이 경로의 Agent, 옮기는 중인 Agent, 남은 정리, 게이트웨이 파드다. */
export function windingDown(s: LlmGatewayStatus): boolean {
  return s.agents.gateway + s.agents.moving + s.agents.cleanups_pending > 0 || s.pods.length > 0;
}

/** 상태를 보이고 다시 읽을 때. 켜져 있거나 끈 뒤에 남은 것이 있을 때다. */
function shown(s: LlmGatewayStatus | undefined): boolean {
  return !!s && (s.switch !== SWITCH_OFF || windingDown(s));
}

function codeText(t: Messages, code: string): string {
  return ownValue(t.admin.platformSettings.llmGateway.codes, code) ?? code;
}

export function LlmGatewayCard({ orig, readOnly }: { orig: PlatformSettings; readOnly: boolean }) {
  const t = useT();
  const text = t.admin.platformSettings.llmGateway;
  // 플랫폼 관리자만 읽는다. 읽기 전용 화면은 부르지 않아 403 을 띄우지 않는다
  const status = useQuery({
    queryKey: LLM_GATEWAY_KEY,
    queryFn: getLlmGateway,
    enabled: !readOnly,
    refetchInterval: (q) => (shown(q.state.data) ? TELEMETRY_POLL_MS : false),
  });
  const s = status.data;
  const on = s ? s.switch !== SWITCH_OFF : null;
  return (
    <Card className="mt-4" data-testid="ps-llm-gateway">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        {on === null ? null : (
          <Badge variant={on ? "ok" : "muted"} data-testid="ps-llm-gateway-switch">
            {on ? text.on : text.off}
          </Badge>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted">{text.body}</p>
        <ErrorMessage error={status.error} testId="ps-llm-gateway-load-error" />
        {s && shown(s) ? <GatewayStatus s={s} /> : null}
        <LimitsSection key={JSON.stringify(orig.llm_gateway.limits)} orig={orig} readOnly={readOnly} />
      </CardContent>
    </Card>
  );
}

function GatewayStatus({ s }: { s: LlmGatewayStatus }) {
  const t = useT();
  const text = t.admin.platformSettings.llmGateway;
  const reasons = t.agents.overview.llmRoute.reasons;
  const direct = Object.entries(s.agents.direct);
  const directCount = direct.reduce((sum, [, n]) => sum + n, 0);
  const date = (at: string | null) => (at ? formatDate(t, at) : null);
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="ps-llm-gateway-status">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">{text.address}</dt>
        <dd className="font-mono">{s.url}</dd>
        <dt className="text-muted">{text.namespace}</dt>
        <dd className="font-mono">{s.namespace}</dd>
      </dl>
      {s.clashing_tenants.length ? (
        <Alert variant="error" data-testid="ps-llm-gateway-clashing">
          {text.clashing(s.clashing_tenants.join(", "))}
        </Alert>
      ) : null}
      {[s.reason, ...s.warnings].filter((c): c is string => !!c).map((code) => (
        <Alert key={code} variant="warn" data-testid={`ps-llm-gateway-code-${code}`}>
          {codeText(t, code)}
        </Alert>
      ))}
      <p className="text-xs" data-testid="ps-llm-gateway-revision">
        {text.revision(s.applied_rev, s.desired_rev)} · {text.pods(s.pods.length)}
      </p>
      <p className="text-xs" data-testid="ps-llm-gateway-agents">
        {text.agents(s.agents.gateway, directCount, s.agents.moving)}
      </p>
      {direct.length ? (
        <div className="text-xs" data-testid="ps-llm-gateway-reasons">
          <span className="text-muted">{text.reasonsTitle}</span>
          <ul className="ml-4 list-disc">
            {direct.map(([reason, n]) => (
              <li key={reason} data-testid={`ps-llm-gateway-reason-${reason}`}>
                {ownValue(reasons, reason) ?? reason} ({n})
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="text-xs text-muted" data-testid="ps-llm-gateway-tokens">
        {s.agents.cleanups_pending ? `${text.cleanups(s.agents.cleanups_pending)} · ` : ""}
        {text.revocations(s.revocations, s.revocations_pending)}
      </p>
      <p className="text-xs text-muted" data-testid="ps-llm-gateway-ingest">
        {text.ingest(date(s.ingest_token_expires_at), date(s.ingest_received_at))}
      </p>
    </div>
  );
}

function LimitsSection({ orig, readOnly }: { orig: PlatformSettings; readOnly: boolean }) {
  const t = useT();
  const text = t.admin.platformSettings.llmGateway.limits;
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const [form, setForm] = useState<LimitsForm>(() => formOf(orig.llm_gateway.limits ?? {}));
  // 플랫폼 관리자의 테넌트 목록이다. 테넌트는 등록부가 없어 안내로만 쓴다 (P38 7절)
  const tenants = useQuery({ queryKey: ["my-tenants", auth.me?.sub ?? null], queryFn: listMyTenants, enabled: auth.me !== null && !readOnly });
  const known = new Set((tenants.data ?? []).map((m) => m.tenant));
  const limits = limitsFromForm(form);
  const save = useMutation({
    mutationFn: (next: LlmGatewayLimits) => patchPlatformSettings({ llm_gateway: { limits: next } }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PLATFORM_SETTINGS_KEY });
      await queryClient.invalidateQueries({ queryKey: LLM_GATEWAY_KEY });
    },
  });
  const setRows = (key: "tenants" | "providers", rows: LimitRow[]) => setForm((f) => ({ ...f, [key]: rows }));
  const rowsEditor = (key: "tenants" | "providers") => {
    const rows = form[key];
    const change = (i: number, patch: Partial<LimitRow>) => setRows(key, rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
    return (
      <div className="flex flex-col gap-1.5" data-testid={`ps-llm-limits-${key}`}>
        {rows.map((row, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2">
            {key === "providers" ? (
              <Select aria-label={text.provider} className="h-8 w-48" data-testid={`ps-llm-limit-${key}-name-${i}`} value={row.name} onChange={(e) => change(i, { name: e.target.value })}>
                <option value=""></option>
                {orig.llm_providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </Select>
            ) : (
              <Input aria-label={text.tenant} className="h-8 w-48" data-testid={`ps-llm-limit-${key}-name-${i}`} value={row.name} onChange={(e) => change(i, { name: e.target.value })} />
            )}
            <Input
              aria-label={text.tokens}
              type="number"
              min={1}
              max={LLM_LIMIT_MAX}
              className="h-8 w-40"
              data-testid={`ps-llm-limit-${key}-tokens-${i}`}
              value={row.tokens}
              onChange={(e) => change(i, { tokens: e.target.value })}
            />
            <Button size="sm" variant="ghost" data-testid={`ps-llm-limit-${key}-remove-${i}`} onClick={() => setRows(key, rows.filter((_, j) => j !== i))}>
              {t.shared.remove}
            </Button>
            {key === "tenants" && row.name.trim() && tenants.data && !known.has(row.name.trim()) ? (
              <span className="text-[11px] text-warn-fg" data-testid={`ps-llm-limit-unknown-${i}`}>
                {text.unknownTenant}
              </span>
            ) : null}
            {row.name.trim() && limitOf(row.tokens) == null ? <span className="text-[11px] text-err-fg">{text.invalid}</span> : null}
          </div>
        ))}
        <div>
          <Button size="sm" variant="outline" data-testid={`ps-llm-limit-${key}-add`} onClick={() => setRows(key, [...rows, { name: "", tokens: "" }])}>
            {text.add}
          </Button>
        </div>
      </div>
    );
  };
  const defaultInvalid = limitOf(form.tenantDefault) === undefined;
  return (
    <form
      className="flex flex-col gap-3 border-t border-line pt-3"
      data-testid="ps-llm-limits"
      onSubmit={(e) => {
        e.preventDefault();
        if (limits) save.mutate(limits);
      }}
    >
      <fieldset className="contents" disabled={readOnly}>
        <h3 className="text-sm font-semibold">{text.title}</h3>
        <p className="text-xs text-muted">{text.body}</p>
        <Field label={text.tenantDefault} hint={text.tenantDefaultHint} error={defaultInvalid ? text.invalid : null} htmlFor="ps-llm-limit-default">
          <Input
            id="ps-llm-limit-default"
            type="number"
            min={1}
            max={LLM_LIMIT_MAX}
            className="w-48"
            data-testid="ps-llm-limit-default"
            value={form.tenantDefault}
            onChange={(e) => setForm((f) => ({ ...f, tenantDefault: e.target.value }))}
          />
        </Field>
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium text-fg-soft">{text.tenants}</span>
          {rowsEditor("tenants")}
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium text-fg-soft">{text.providers}</span>
          <p className="text-xs text-muted">{text.providersHint}</p>
          {rowsEditor("providers")}
        </div>
        <ErrorMessage error={save.error} testId="ps-llm-limits-error" />
        <div className="flex items-center gap-3">
          <Button type="submit" size="sm" disabled={save.isPending || limits === null} data-testid="ps-llm-limits-save">
            {t.shared.save}
          </Button>
          {save.isSuccess ? <span className="text-xs text-muted">{text.saved}</span> : null}
        </div>
      </fieldset>
    </form>
  );
}
