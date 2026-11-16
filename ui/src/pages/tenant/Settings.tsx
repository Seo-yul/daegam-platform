import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { getTenantSettings, listClusters, listDataServices, listMembers, listUserDirectory, patchTenantSettings, putMembers } from "@/api/resources";
import { TENANT_ROLES, type Binding, type Membership, type TenantSettings, type TenantSettingsPatch } from "@/api/types";
import { BindingsEditor } from "@/components/BindingsEditor";
import { ErrorMessage } from "@/components/ErrorMessage";
import { Link } from "react-router";

import { PageHeader } from "@/components/Layout";
import { PageWidth } from "@/components/PageWidth";
import { StateRow } from "@/components/StateBlock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { hasScope, isAuthReady, tokenAuth, useAuthState } from "@/lib/auth";
import { parseJsonObject } from "@/lib/json";
import { useTenant } from "@/lib/tenant";
import { useUserLabel } from "@/lib/userLabel";
import { formatDate } from "@/lib/utils";
import { CLUSTERS_KEY, dataServicesKey, tenantSettingsKey } from "@/lib/queryKeys";
import { ServiceAccountsCard } from "@/pages/tenant/ServiceAccounts";

interface FormState {
  default_cluster_id: string;
  allowed_cluster_ids: string[] | null; // null = 제한 없음
  ns_name: string;
  ns_labels: string; // JSON
  quota: string; // JSON
  default_bindings: Binding[];
}

function fromSettings(s: TenantSettings): FormState {
  return {
    default_cluster_id: s.default_cluster_id ?? "",
    allowed_cluster_ids: s.allowed_cluster_ids,
    ns_name: s.namespace?.name ?? "",
    ns_labels: s.namespace?.labels ? JSON.stringify(s.namespace.labels, null, 2) : "",
    quota: s.quota ? JSON.stringify(s.quota, null, 2) : "",
    default_bindings: s.default_bindings ?? [],
  };
}

export function buildTenantPatch(orig: TenantSettings, f: FormState): { patch: TenantSettingsPatch; error: string | null } {
  const patch: TenantSettingsPatch = {};
  const cluster = f.default_cluster_id || null;
  if (cluster !== orig.default_cluster_id) patch.default_cluster_id = cluster;
  if (JSON.stringify(f.allowed_cluster_ids) !== JSON.stringify(orig.allowed_cluster_ids)) patch.allowed_cluster_ids = f.allowed_cluster_ids;
  const labels = parseJsonObject(f.ns_labels);
  if (labels.error) return { patch, error: `namespace.labels: ${labels.error}` };
  const ns = f.ns_name.trim() || labels.value ? { name: f.ns_name.trim() || null, labels: (labels.value as Record<string, string> | null) ?? null } : null;
  if (JSON.stringify(ns) !== JSON.stringify(orig.namespace)) patch.namespace = ns;
  const quota = parseJsonObject(f.quota);
  if (quota.error) return { patch, error: `quota: ${quota.error}` };
  if (JSON.stringify(quota.value) !== JSON.stringify(orig.quota)) patch.quota = quota.value;
  const bindings = f.default_bindings.length ? f.default_bindings : null;
  if (JSON.stringify(bindings) !== JSON.stringify(orig.default_bindings)) patch.default_bindings = bindings;
  return { patch, error: null };
}

/** 저장 결과 안내. 상태에는 문장 대신 무슨 일이 있었는지만 둔다. */
type Notice = "saved" | "noChange";

export function TenantSettingsPage() {
  const t = useT();
  const text = t.tenant.settings;
  const userLabel = useUserLabel();
  const tenant = useTenant();
  const auth = useAuthState();
  const queryClient = useQueryClient();
  const canWrite = hasScope(auth.me, tenant, "members:write");
  // /me 를 받기 전에는 scope 가 비어 있다. 그때 잠그면 관리자에게도 "보기만" 안내와 꺼진 저장이 번쩍인다
  const readOnly = isAuthReady(auth) && !canWrite;
  // 네임스페이스를 옮기는 것은 allowed_namespaces 와 같은 급이라 settings:write 다(D40). 저장하면 403 이
  // 나올 칸을 편집 가능하게 두지 않는다
  const canWriteSettings = hasScope(auth.me, tenant, "settings:write");

  const q = useQuery({ queryKey: tenantSettingsKey(tenant), queryFn: () => getTenantSettings(tenant) });
  const clusters = useQuery({ queryKey: CLUSTERS_KEY, queryFn: listClusters });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });

  const [form, setForm] = useState<FormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  useEffect(() => {
    if (q.data) setForm(fromSettings(q.data));
  }, [q.data]);
  const set = (patch: Partial<FormState>) => setForm((f) => (f ? { ...f, ...patch } : f));

  const save = useMutation({
    mutationFn: (patch: TenantSettingsPatch) => patchTenantSettings(tenant, patch),
    onSuccess: async () => {
      setNotice("saved");
      await queryClient.invalidateQueries({ queryKey: tenantSettingsKey(tenant) });
    },
  });

  if (q.isLoading || !form) return q.error ? <ErrorMessage error={q.error} /> : <p className="text-sm text-muted">{t.common.loading}</p>;
  const orig = q.data!;

  function submit() {
    const { patch, error } = buildTenantPatch(orig, form!);
    if (error) {
      setFormError(error);
      return;
    }
    setFormError(null);
    if (!Object.keys(patch).length) {
      setNotice("noChange");
      return;
    }
    setNotice(null);
    save.mutate(patch);
  }

  function toggleAllowed(id: string, on: boolean) {
    const cur = form!.allowed_cluster_ids ?? [];
    set({ allowed_cluster_ids: on ? [...cur, id] : cur.filter((c) => c !== id) });
  }

  return (
    <PageWidth data-testid="tenant-settings-page" className="flex flex-col gap-4">
      <PageHeader
        title={text.title(tenant)}
        testId="page-title"
        subtitleTestId="settings-scope-hint"
        subtitle={text.scopeHint(
          <span className="font-medium text-fg">{tenant}</span>,
          <Link className="font-medium text-fg underline-offset-2 hover:underline" to="/admin/settings">
            {text.platformSettings}
          </Link>,
        )}
      />
      {readOnly ? (
        <Alert variant="info" data-testid="readonly-notice">
          {text.readOnly}
        </Alert>
      ) : null}
      <form
        className="grid gap-4 lg:grid-cols-2"
        data-testid="tenant-settings-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle>{text.clustersTitle}</CardTitle>
            <span className="text-xs text-muted">
              {text.updated} {formatDate(t, orig.updated_at)} {orig.updated_by ? t.shared.byUser(userLabel(orig.updated_by)) : ""}
            </span>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Field label={text.defaultCluster} htmlFor="ts-cluster" hint={text.hints.cluster}>
              <Select id="ts-cluster" data-testid="ts-cluster" value={form.default_cluster_id} onChange={(e) => set({ default_cluster_id: e.target.value })}>
                <option value="">{text.platformDefault}</option>
                {clusters.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </Field>
            <div className="flex flex-col gap-1">
              <label className="flex items-center gap-2 text-sm font-medium text-fg-soft">
                <input
                  type="checkbox"
                  data-testid="ts-allowed-restrict"
                  checked={form.allowed_cluster_ids !== null}
                  onChange={(e) => set({ allowed_cluster_ids: e.target.checked ? [] : null })}
                />
                {text.restrictClusters}
              </label>
              {form.allowed_cluster_ids !== null ? (
                <ul className="flex flex-wrap gap-3 pl-6 text-sm" data-testid="ts-allowed">
                  {clusters.data?.map((c) => (
                    <li key={c.id}>
                      <label className="flex items-center gap-1">
                        <input type="checkbox" data-testid={`ts-allowed-${c.name}`} checked={form.allowed_cluster_ids!.includes(c.id)} onChange={(e) => toggleAllowed(c.id, e.target.checked)} />
                        {c.name}
                      </label>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{text.namespaceTitle}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Field label={text.namespaceName} htmlFor="ts-ns-name" hint={canWriteSettings ? text.hints.namespace : text.hints.namespaceLocked}>
              <Input id="ts-ns-name" data-testid="ts-ns-name" value={form.ns_name} disabled={!canWriteSettings} onChange={(e) => set({ ns_name: e.target.value })} />
            </Field>
            <Field label={text.namespaceLabels} hint={text.hints.labels} htmlFor="ts-ns-labels">
              <Textarea id="ts-ns-labels" data-testid="ts-ns-labels" rows={3} value={form.ns_labels} disabled={!canWriteSettings} onChange={(e) => set({ ns_labels: e.target.value })} />
            </Field>
            <Field label={text.quota} hint={text.hints.quota} htmlFor="ts-quota">
              <Textarea id="ts-quota" data-testid="ts-quota" rows={4} value={form.quota} onChange={(e) => set({ quota: e.target.value })} />
            </Field>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>{text.bindingsTitle}</CardTitle>
            <span className="text-xs text-muted">{text.hints.bindings}</span>
          </CardHeader>
          <CardContent>
            <BindingsEditor value={form.default_bindings} onChange={(b) => set({ default_bindings: b })} dataServices={dataServices.data ?? []} testId="ts-bindings" />
          </CardContent>
        </Card>

        <div className="flex items-center justify-end gap-3 lg:col-span-2">
          {formError ? (
            <Alert variant="error" data-testid="ts-form-error">
              {formError}
            </Alert>
          ) : null}
          {notice ? (
            <Alert variant={notice === "saved" ? "ok" : "info"} data-testid="ts-notice">
              {notice === "saved" ? t.shared.saved : t.errors.noChange}
            </Alert>
          ) : null}
          <ErrorMessage error={save.error} testId="save-error" />
          <Button variant="ghost" onClick={() => setForm(fromSettings(orig))}>
            {t.shared.revert}
          </Button>
          <Button type="submit" data-testid="save-button" disabled={readOnly || save.isPending}>
            {save.isPending ? t.shared.saving : t.shared.saveChanges}
          </Button>
        </div>
      </form>

      {canWrite ? <MembersCard tenant={tenant} /> : null}
      {/* 서비스 어카운트는 플랫폼이 사람 토큰을 내는 모드(local, oidc)에 있다 (P53b 6.7절) */}
      {canWrite && tokenAuth(auth.mode) ? <ServiceAccountsCard tenant={tenant} /> : null}
    </PageWidth>
  );
}

/** 멤버 표. manual 소속만 편집·PUT 하고 okta_group 소속은 읽기 전용이다. */
/** 저장 전에 걸린 manual 소속의 문제. 상태에는 문장 대신 무엇이 걸렸는지만 둔다. */
type MembersFormError = { code: "subRequired" } | { code: "duplicate"; sub: string };

function MembersCard({ tenant }: { tenant: string }) {
  const t = useT();
  const text = t.tenant.members;
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const q = useQuery({ queryKey: ["members", tenant], queryFn: () => listMembers(tenant) });
  // local 모드(D40)에서는 sub 가 사용자 id 라 손으로 적을 수 없다. 사용자 목록에서 고른다
  const localMode = auth.mode === "local";
  const usersQ = useQuery({ queryKey: ["users", "directory"], queryFn: listUserDirectory, enabled: localMode });
  const userLabel = useUserLabel();
  const [manual, setManual] = useState<{ sub: string; role: string }[]>([]);
  const [formError, setFormError] = useState<MembersFormError | null>(null);
  // 서버의 manual 소속. 편집칸의 처음 값이자 바뀌었는지 견줄 기준이다
  const origManual = useMemo(() => (q.data ?? []).filter((m) => m.source === "manual").map((m) => ({ sub: m.sub, role: m.role })), [q.data]);
  useEffect(() => {
    if (q.data) setManual(origManual);
  }, [q.data, origManual]);
  const save = useMutation({
    mutationFn: (items: { sub: string; role: string }[]) => putMembers(tenant, items),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["members", tenant] }),
  });
  const groupRows: Membership[] = (q.data ?? []).filter((m) => m.source !== "manual");
  const dirty = JSON.stringify(manual) !== JSON.stringify(origManual);
  const updateRow = (i: number, patch: Partial<{ sub: string; role: string }>) => setManual(manual.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  function submit() {
    const items = manual.map((m) => ({ sub: m.sub.trim(), role: m.role }));
    const subs = new Set<string>();
    for (const m of items) {
      if (!m.sub) {
        setFormError({ code: "subRequired" });
        return;
      }
      if (subs.has(m.sub)) {
        setFormError({ code: "duplicate", sub: m.sub });
        return;
      }
      subs.add(m.sub);
    }
    setFormError(null);
    save.mutate(items);
  }

  return (
    <Card data-testid="members-card">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" data-testid="member-add" onClick={() => setManual([...manual, { sub: "", role: "developer" }])}>
            {text.add}
          </Button>
          <Button size="sm" data-testid="members-save" disabled={!dirty || save.isPending} onClick={submit}>
            {save.isPending ? t.shared.saving : text.save}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {/* 목록을 못 읽은 것은 표 안이 말한다. 위 줄은 저장 실패와, 읽어 둔 표가 있는데 새로 읽지 못한 경우다 */}
        <ErrorMessage error={save.error ?? (q.data ? q.error : null)} testId="members-error" />
        {formError ? (
          <Alert variant="error" data-testid="members-form-error">
            {formError.code === "duplicate" ? text.formError.duplicate(formError.sub) : text.formError.subRequired}
          </Alert>
        ) : null}
        <Table data-testid="members-table">
          <THead>
            <tr>
              <TH>{text.columns.subject}</TH>
              <TH>{text.columns.role}</TH>
              <TH>{text.columns.source}</TH>
              <TH></TH>
            </tr>
          </THead>
          <TBody>
            {groupRows.map((m) => (
              <TR key={`g-${m.sub}`} data-testid={`member-group-${m.sub}`}>
                <TD className="font-mono text-xs">{userLabel(m.sub)}</TD>
                <TD>{m.role}</TD>
                <TD>
                  <Badge variant="info">okta_group</Badge>
                </TD>
                <TD className="text-right text-xs text-dim">{text.synced}</TD>
              </TR>
            ))}
            {manual.map((m, i) => (
              <TR key={`m-${i}`} data-testid={`member-manual-${i}`}>
                <TD>
                  {localMode ? (
                    <Select aria-label={text.columns.subject} data-testid={`member-sub-${i}`} className="h-8" value={m.sub} placeholder={text.pickUser} onChange={(e) => updateRow(i, { sub: e.target.value })}>
                      {(usersQ.data ?? []).map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.username}
                          {u.display_name ? ` (${u.display_name})` : ""}
                        </option>
                      ))}
                      {m.sub && !usersQ.data?.some((u) => u.id === m.sub) ? <option value={m.sub}>{m.sub}</option> : null}
                    </Select>
                  ) : (
                    <Input aria-label={text.columns.subject} data-testid={`member-sub-${i}`} className="h-8 font-mono text-xs" value={m.sub} onChange={(e) => updateRow(i, { sub: e.target.value })} />
                  )}
                </TD>
                <TD>
                  <Select aria-label={text.columns.role} data-testid={`member-role-${i}`} className="h-8" value={m.role} onChange={(e) => updateRow(i, { role: e.target.value })}>
                    {TENANT_ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </Select>
                </TD>
                <TD>
                  <Badge>{text.manual}</Badge>
                </TD>
                <TD className="text-right">
                  <Button size="sm" variant="ghost" data-testid={`member-remove-${i}`} onClick={() => setManual(manual.filter((_, j) => j !== i))}>
                    {t.shared.remove}
                  </Button>
                </TD>
              </TR>
            ))}
            {!groupRows.length && !manual.length ? (
              <StateRow colSpan={4} loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} empty={text.empty} />
            ) : null}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  );
}
