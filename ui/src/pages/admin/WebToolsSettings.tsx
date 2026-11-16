import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { checkWebSearch, getDataPlaneAuth, patchPlatformSettings, setWebSearchKey } from "@/api/resources";
import type { PlatformSettings, PlatformSettingsPatch, WebSearchCheck, WebSearchProvider, WebToolsSettings } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { DATA_PLANE_AUTH_KEY, PLATFORM_SETTINGS_KEY } from "@/lib/queryKeys";

import { useAdminAccess } from "./permission";

/**
 * Platform Settings 의 웹 Tool 과 네트워크 카드 (P39b 15절). 위의 폼과 따로 저장하고, 서버의 자기 절이 바뀔 때만 다시 채운다. 웹 Tool 절은 PATCH 가 통째로 바꾸므로
 * 화면에 없는 칸(본문 상한, 포트 등)은 받은 값 그대로 다시 보낸다. 검색 키는 D56 의 길처럼 `PUT .../api-key` 로 따로 보내고
 * 응답에는 Secret 이름만 온다. 연결 확인은 저장한 설정으로 질의 하나를 보낸다.
 */
const LIMIT_KEYS = ["per_minute", "per_user_per_minute", "per_user_per_day"] as const;
const SEARCH_LIMIT_KEYS = ["per_minute", "per_day", "per_user_per_minute", "per_user_per_day"] as const;
const PROVIDERS: readonly WebSearchProvider[] = ["brave", "tavily", "searxng", "stub"];

interface WebForm {
  tools: WebToolsSettings;
  caBundle: string;
  apiKey: string;
}

function formOf(s: PlatformSettings): WebForm {
  return { tools: structuredClone(s.web_tools), caBundle: s.network.ca_bundle ?? "", apiKey: "" };
}

/** 원본과 다른 절만 싣는다. */
export function webPatch(orig: PlatformSettings, f: WebForm): PlatformSettingsPatch {
  const patch: PlatformSettingsPatch = {};
  if (JSON.stringify(f.tools) !== JSON.stringify(orig.web_tools)) patch.web_tools = f.tools;
  const ca = f.caBundle.trim() || null;
  if (ca !== (orig.network.ca_bundle ?? null)) patch.network = { ...orig.network, ca_bundle: ca };
  return patch;
}

function NumberField({ id, label, value, onChange }: { id: string; label: string; value: number; onChange: (v: number) => void }) {
  return (
    <Field label={label} htmlFor={id}>
      <Input id={id} data-testid={id} type="number" min={1} value={String(value)} onChange={(e) => onChange(Number(e.target.value))} />
    </Field>
  );
}

export function WebToolsSection({ orig, readOnly }: { orig: PlatformSettings; readOnly: boolean }) {
  const t = useT();
  const text = t.admin.platformSettings.webTools;
  const qc = useQueryClient();
  const [form, setForm] = useState<WebForm>(() => formOf(orig));
  const [check, setCheck] = useState<WebSearchCheck | null>(null);
  // 이 카드의 절(web_tools·network)이 서버에서 바뀌었을 때만 다시 채운다. 위 폼의 저장이 이 카드의 고친 값을 지우지 않게 한다
  const own = JSON.stringify([orig.web_tools, orig.network]);
  useEffect(() => setForm(formOf(orig)), [own]); // eslint-disable-line react-hooks/exhaustive-deps
  const fetch = form.tools.fetch!;
  const search = form.tools.search!;
  const setTools = (patch: Partial<WebToolsSettings>) => setForm((f) => ({ ...f, tools: { ...f.tools, ...patch } }));
  const save = useMutation({
    mutationFn: async () => {
      const patch = webPatch(orig, form);
      if (Object.keys(patch).length) await patchPlatformSettings(patch);
      if (form.apiKey.trim()) await setWebSearchKey(form.apiKey.trim());
    },
    onSuccess: () => {
      setForm((f) => ({ ...f, apiKey: "" }));
      void qc.invalidateQueries({ queryKey: PLATFORM_SETTINGS_KEY });
      void qc.invalidateQueries({ queryKey: DATA_PLANE_AUTH_KEY });
    },
  });
  const probe = useMutation({ mutationFn: checkWebSearch, onSuccess: setCheck });
  // 지금 CA bundle 과 다른 CA 로 배포된 MCP Server 수. 이 응답은 platform-admin 만 받으므로 GET /me 로 그것을 안 뒤에야 묻는다
  const { isPlatformAdmin } = useAdminAccess();
  const dataPlane = useQuery({ queryKey: DATA_PLANE_AUTH_KEY, queryFn: getDataPlaneAuth, enabled: isPlatformAdmin });
  const staleCa = dataPlane.data?.mcp_servers_stale_ca ?? 0;
  const dirty = Object.keys(webPatch(orig, form)).length > 0 || Boolean(form.apiKey.trim());
  // 키 상태와 연결 확인의 말은 provider 표의 것이다. 검색 키는 늘 화면에서 넣은 관리 Secret 이다
  const providerText = t.admin.platformSettings.provider;
  const keyText = form.apiKey.trim() ? providerText.key.pending : search.api_key_secret ? providerText.key.managed(search.api_key_secret) : providerText.key.none;
  return (
    <fieldset className="contents" disabled={readOnly}>
      <Card className="lg:col-span-2" data-testid="ps-web-tools">
        <CardHeader>
          <CardTitle>{text.title}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-xs text-muted">{text.body}</p>
          <section className="flex flex-col gap-3" aria-label={text.fetch}>
            <label className="flex items-center gap-2 text-sm font-medium">
              <input type="checkbox" data-testid="ps-web-fetch-enabled" checked={fetch.enabled} onChange={(e) => setTools({ fetch: { ...fetch, enabled: e.target.checked } })} />
              {text.fetch}
            </label>
            <div className="grid gap-3 md:grid-cols-3">
              {LIMIT_KEYS.map((key) => (
                <NumberField key={key} id={`ps-web-fetch-${key}`} label={text[key === "per_minute" ? "perMinute" : key === "per_user_per_minute" ? "perUserPerMinute" : "perUserPerDay"]} value={fetch[key]} onChange={(v) => setTools({ fetch: { ...fetch, [key]: v } })} />
              ))}
            </div>
          </section>
          <section className="flex flex-col gap-3" aria-label={text.search}>
            <label className="flex items-center gap-2 text-sm font-medium">
              <input type="checkbox" data-testid="ps-web-search-enabled" checked={search.enabled} onChange={(e) => setTools({ search: { ...search, enabled: e.target.checked } })} />
              {text.search}
            </label>
            <Alert variant="info" data-testid="ps-web-search-notice">
              {text.searchNotice}
            </Alert>
            <div className="grid gap-3 md:grid-cols-2">
              <Field label={text.provider} htmlFor="ps-web-search-provider">
                <Select id="ps-web-search-provider" data-testid="ps-web-search-provider" value={search.provider} onChange={(e) => setTools({ search: { ...search, provider: e.target.value as WebSearchProvider } })}>
                  {PROVIDERS.map((p) => (
                    <option key={p} value={p}>
                      {text.providerNames[p]}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={text.endpoint} htmlFor="ps-web-search-endpoint" hint={text.endpointHint}>
                <Input id="ps-web-search-endpoint" data-testid="ps-web-search-endpoint" value={search.endpoint ?? ""} onChange={(e) => setTools({ search: { ...search, endpoint: e.target.value.trim() || null } })} />
              </Field>
            </div>
            <Field label={text.apiKey} htmlFor="ps-web-search-key" hint={text.keyHint}>
              <div className="flex flex-wrap items-center gap-2">
                <Input id="ps-web-search-key" data-testid="ps-web-search-key" type="password" autoComplete="new-password" className="max-w-sm" placeholder={providerText.keyReplace} value={form.apiKey} onChange={(e) => setForm((f) => ({ ...f, apiKey: e.target.value }))} />
                <span className="text-xs text-muted" data-testid="ps-web-search-key-state">
                  {keyText}
                </span>
              </div>
            </Field>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" data-testid="ps-web-search-check" disabled={dirty || probe.isPending} title={dirty ? text.checkUnsaved : undefined} onClick={() => probe.mutate()}>
                {probe.isPending ? providerText.checking : providerText.check}
              </Button>
              {check ? (
                <Badge variant={check.ok ? "ok" : "error"} data-testid="ps-web-search-check-result">
                  {text.checkResult[check.code]} {text.latency(check.latency_ms)}
                </Badge>
              ) : null}
              <ErrorMessage error={probe.error} testId="ps-web-search-check-error" />
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              {SEARCH_LIMIT_KEYS.map((key) => (
                <NumberField
                  key={key}
                  id={`ps-web-search-${key}`}
                  label={text[key === "per_minute" ? "perMinute" : key === "per_day" ? "perDay" : key === "per_user_per_minute" ? "perUserPerMinute" : "perUserPerDay"]}
                  value={search[key]}
                  onChange={(v) => setTools({ search: { ...search, [key]: v } })}
                />
              ))}
            </div>
          </section>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" data-testid="ps-web-external-clients" checked={form.tools.external_clients} onChange={(e) => setTools({ external_clients: e.target.checked })} />
            <span>
              {text.externalClients}
              <span className="block text-xs text-muted">{text.externalClientsHint}</span>
            </span>
          </label>
        </CardContent>
      </Card>
      <Card className="lg:col-span-2" data-testid="ps-network">
        <CardHeader>
          <CardTitle>{t.admin.platformSettings.network.title}</CardTitle>
        </CardHeader>
        <CardContent>
          <Field label={t.admin.platformSettings.network.caBundle} htmlFor="ps-network-ca" hint={t.admin.platformSettings.network.caHint}>
            <Textarea id="ps-network-ca" data-testid="ps-network-ca" rows={4} className="font-mono text-xs" value={form.caBundle} onChange={(e) => setForm((f) => ({ ...f, caBundle: e.target.value }))} />
          </Field>
          {staleCa > 0 ? (
            <Alert variant="warn" className="mt-3" data-testid="ps-network-stale-ca">
              {t.admin.platformSettings.network.staleCa(staleCa)}
            </Alert>
          ) : null}
        </CardContent>
      </Card>
      <div className="flex items-center justify-end gap-3 lg:col-span-2">
        {save.isSuccess && !dirty ? (
          <Alert variant="ok" data-testid="ps-web-notice">
            {t.shared.saved}
          </Alert>
        ) : null}
        <ErrorMessage error={save.error} testId="ps-web-save-error" />
        <Button variant="ghost" onClick={() => setForm(formOf(orig))}>
          {t.shared.revert}
        </Button>
        <Button data-testid="ps-web-save" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t.shared.saving : t.shared.saveChanges}
        </Button>
      </div>
    </fieldset>
  );
}
