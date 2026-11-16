/**
 * 사용량 보고 (P37 13절). 테넌트 화면 `/usage` 와 플랫폼 화면 `/admin/usage` 가 같은 컴포넌트를 쓴다.
 *
 * 서버는 시간 묶음만 읽으므로 최대 2분 늦다. 날은 브라우저의 시간대로 자르고, 기간의 끝(Until)은 고른 날까지라 그 다음 날 0시를
 * 보낸다(서버는 끝을 들지 않는다). 플랫폼 화면은 테넌트·경로 기준과 가격 규칙이 없는 모델, 원장 건강을 더 보이고, 건강 수가 0 이
 * 아니면 맨 위에 경고한다. CSV 는 표와 같은 칸으로 지금 기준의 줄을 내려받는다.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getPlatformUsage, getUsage } from "@/api/resources";
import type { AdminUsage, SourceKind, TenantUsageGroup, UsageFilters, UsageGroup, UsageReport as Report, UsageRow } from "@/api/types";
import { type BarDay, DailyBars } from "@/components/charts/DailyBars";
import { queryGate } from "@/components/QueryGate";
import { Button } from "@/components/ui/button";
import { Alert, Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type Messages, useT } from "@/i18n";
import { formatUsd } from "@/lib/usage";
import { toCsv } from "@/lib/csv";
import { saveBlob } from "@/lib/download";
import { usageKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { browserZone, dayStart, formatDay, formatNumber } from "@/lib/utils";

/* 묶는 기준은 서버의 타입에서 온다. 서버가 기준을 더하거나 빼면 여기서 컴파일이 깨진다. */
const TENANT_GROUP_SET: Record<TenantUsageGroup, true> = { day: true, agent: true, source: true, user: true, model: true };
const PLATFORM_GROUP_SET: Record<UsageGroup, true> = { ...TENANT_GROUP_SET, tenant: true, route: true };
const TENANT_GROUPS = Object.keys(TENANT_GROUP_SET) as TenantUsageGroup[];
const PLATFORM_GROUPS = Object.keys(PLATFORM_GROUP_SET) as UsageGroup[];
const HEALTH_KEYS = ["gateway_missing", "writer_conflicts", "gateway_extra_records", "join_mismatch"] as const;

function isTenantGroup(group: UsageGroup): group is TenantUsageGroup {
  return group in TENANT_GROUP_SET;
}

/** 모델 하나의 이름. provider 이름이 없으면 SDK 계열이다. 웹 검색은 모델 칸도 provider 이름이라, LLM 모델과
 * 헷갈리지 않게 계열(web_search)과 provider 를 보인다. */
function modelLabel(row: { provider_name?: string | null; provider_kind?: string | null; model?: string | null }): string {
  if (row.provider_kind === "web_search") return `${row.provider_kind} · ${row.provider_name ?? row.model ?? "-"}`;
  return `${row.provider_name ?? row.provider_kind ?? "-"} · ${row.model ?? "-"}`;
}

function sourceKind(t: Messages, kind: SourceKind | null | undefined): string {
  return kind ? t.usage.sourceKinds[kind] : "-";
}

/** 줄의 이름. 기준마다 칸이 다르고, 사람이 없는 줄은 잇지 못한 호출이다. */
export function rowLabel(t: Messages, group: UsageGroup, row: UsageRow): string {
  const text = t.usage;
  switch (group) {
    case "day":
      return row.day ? new Date(row.day).toLocaleDateString(t.locale) : "-";
    case "agent":
      return row.name ?? row.source_id ?? "-";
    case "source":
      // 청지기는 출처가 하나뿐이라 종류 이름이 곧 그 이름이다. 출처 id 는 제품 이름이라 붙이지 않는다
      if (row.source_kind === "assistant") return sourceKind(t, row.source_kind);
      return `${sourceKind(t, row.source_kind)} · ${row.name ?? row.source_id ?? "-"}`;
    case "user":
      if (!row.user_sub) return text.unjoined;
      return row.name ? `${row.name} (${row.user_sub})` : row.user_sub;
    case "model":
      return modelLabel(row);
    case "tenant":
      return row.tenant ?? "-";
    case "route":
      return row.route ?? "-";
  }
}

/** 표와 CSV 의 칸. 첫 칸은 줄의 이름이고 나머지는 수다. */
interface Column {
  label: (t: Messages) => string;
  value: (row: UsageRow) => number;
  usd?: boolean;
}

const COLUMNS: Column[] = [
  { label: (t) => t.usage.columns.calls, value: (r) => r.calls },
  { label: (t) => t.usage.columns.errors, value: (r) => r.errors },
  { label: (t) => t.usage.columns.input, value: (r) => r.input_tokens },
  { label: (t) => t.usage.columns.output, value: (r) => r.output_tokens },
  { label: (t) => t.usage.columns.cacheRead, value: (r) => r.cache_read_tokens },
  { label: (t) => t.usage.columns.cost, value: (r) => r.cost_usd, usd: true },
];

/** 날마다의 막대. 칸 이름은 같은 열쇠의 줄 이름을 빌린다. 날짜 기준은 칸이 하루 전체라 이름이 비용이다. */
function bars(t: Messages, report: Report, group: UsageGroup): BarDay[] {
  const names = new Map(report.rows.map((r) => [JSON.stringify(keyOf(group, r)), rowLabel(t, group, r)]));
  const partLabel = (key: string | null, raw: (string | null)[] | null): string => {
    if (key === null) return t.usage.other;
    if (group === "day") return t.usage.totals.cost;
    return names.get(key) ?? raw?.join(" · ") ?? "-";
  };
  return report.series.map((day) => ({
    key: day.day,
    label: formatDay(t, day.day),
    parts: day.items.map((item) => {
      const key = item.key === null ? null : JSON.stringify(item.key);
      return { key, label: partLabel(key, item.key), value: item.cost_usd };
    }),
  }));
}

/** 서버의 series 열쇠와 같은 꼴(기준의 칸 값 목록). */
function keyOf(group: UsageGroup, row: UsageRow): (string | null)[] {
  switch (group) {
    case "day":
      return [];
    case "agent":
      return [row.source_id ?? null];
    case "source":
      return [row.source_kind ?? null, row.source_id ?? null];
    case "user":
      return [row.user_sub ?? null];
    case "model":
      return [row.provider_kind ?? null, row.provider_name ?? null, row.model ?? null];
    case "tenant":
      return [row.tenant ?? null];
    case "route":
      return [row.route ?? null];
  }
}

export function UsageReport({ platform }: { platform: boolean }) {
  const text = useT().usage;
  const [draft, setDraft] = useState({ since: "", until: "", group: "day" as UsageGroup, tenant: "" });
  const [filters, setFilters] = useState<UsageFilters>({ group: "day", tz: browserZone() });
  return (
    <div className="space-y-4" data-testid="usage-report">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({ group: draft.group, tz: browserZone(), since: dayStart(draft.since), until: dayStart(draft.until, 1), tenant: platform && draft.tenant ? draft.tenant : undefined });
        }}
      >
        <Field label={text.filters.since} htmlFor="usage-since">
          <Input id="usage-since" type="date" className="w-40" data-testid="usage-since" value={draft.since} onChange={(e) => setDraft({ ...draft, since: e.target.value })} />
        </Field>
        <Field label={text.filters.until} htmlFor="usage-until">
          <Input id="usage-until" type="date" className="w-40" data-testid="usage-until" value={draft.until} onChange={(e) => setDraft({ ...draft, until: e.target.value })} />
        </Field>
        <Field label={text.filters.group} htmlFor="usage-group">
          <Select id="usage-group" className="w-36" data-testid="usage-group" value={draft.group} onChange={(e) => setDraft({ ...draft, group: e.target.value as UsageGroup })}>
            {(platform ? PLATFORM_GROUPS : TENANT_GROUPS).map((g) => (
              <option key={g} value={g}>
                {text.groups[g]}
              </option>
            ))}
          </Select>
        </Field>
        {platform ? (
          <Field label={text.filters.tenant} htmlFor="usage-tenant">
            <Input id="usage-tenant" className="w-40" data-testid="usage-tenant" value={draft.tenant} onChange={(e) => setDraft({ ...draft, tenant: e.target.value })} />
          </Field>
        ) : null}
        <Button type="submit" data-testid="usage-apply">
          {text.filters.apply}
        </Button>
      </form>
      {platform ? <PlatformResult filters={filters} /> : <TenantResult filters={filters} />}
    </div>
  );
}

function TenantResult({ filters }: { filters: UsageFilters }) {
  const tenant = useTenant();
  const group = filters.group ?? "day";
  const q = useQuery({
    queryKey: usageKey(tenant, filters),
    queryFn: () => getUsage({ ...filters, group: isTenantGroup(group) ? group : "day" }),
  });
  return queryGate(q, { testId: "usage-error" }) ?? (q.data ? <ReportBody report={q.data} group={group} health={null} /> : null);
}

function PlatformResult({ filters }: { filters: UsageFilters }) {
  const q = useQuery({ queryKey: usageKey("platform", filters), queryFn: () => getPlatformUsage(filters) });
  return queryGate(q, { testId: "usage-error" }) ?? (q.data ? <ReportBody report={q.data} group={filters.group ?? "day"} health={q.data} /> : null);
}

function ReportBody({ report, group, health }: { report: Report; group: UsageGroup; health: AdminUsage | null }) {
  const t = useT();
  const text = t.usage;
  const totals = report.totals;

  function download(): void {
    const header = [text.columns.name, ...COLUMNS.map((c) => c.label(t))];
    const rows = report.rows.map((r) => [rowLabel(t, group, r), ...COLUMNS.map((c) => c.value(r))]);
    saveBlob(new Blob([toCsv(header, rows)], { type: "text/csv;charset=utf-8" }), text.csvName(group));
  }

  return (
    <>
      {health ? <HealthWarning usage={health} /> : null}
      <div className="flex flex-wrap gap-6 text-sm" data-testid="usage-totals">
        <Total label={text.totals.cost} value={formatUsd(t, totals.cost_usd)} />
        <Total label={text.totals.calls} value={formatNumber(t, totals.calls)} />
        <Total label={text.totals.tokens} value={formatNumber(t, totals.input_tokens + totals.output_tokens)} />
        <Total label={text.totals.unpriced} value={formatNumber(t, totals.unpriced_calls)} />
      </div>
      <Card className="p-4">
        <DailyBars days={bars(t, report, group)} format={(v) => formatUsd(t, v)} label={text.chart.label} empty={text.chart.empty} />
      </Card>
      <div className="flex items-center justify-end gap-3">
        {report.truncated ? (
          <p className="text-xs text-muted" data-testid="usage-truncated">
            {text.truncated(formatNumber(t, report.rows.length))}
          </p>
        ) : null}
        <Button size="sm" variant="outline" data-testid="usage-download" onClick={download}>
          {text.download}
        </Button>
      </div>
      <Card>
        <Table data-testid="usage-rows">
          <THead>
            <TR>
              <TH>{text.columns.name}</TH>
              {COLUMNS.map((c, i) => (
                <TH key={i} className="text-right">
                  {c.label(t)}
                </TH>
              ))}
            </TR>
          </THead>
          <TBody>
            {report.rows.length === 0 ? (
              <EmptyRow colSpan={COLUMNS.length + 1}>{text.empty}</EmptyRow>
            ) : (
              report.rows.map((r, i) => (
                <TR key={i} data-testid="usage-row">
                  <TD>{rowLabel(t, group, r)}</TD>
                  {COLUMNS.map((c, j) => (
                    <TD key={j} className="text-right">
                      {c.usd ? formatUsd(t, c.value(r)) : formatNumber(t, c.value(r))}
                    </TD>
                  ))}
                </TR>
              ))
            )}
          </TBody>
        </Table>
      </Card>
      {health && health.unpriced.length > 0 ? <Unpriced usage={health} /> : null}
    </>
  );
}

function Total({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className="text-lg font-semibold text-heading">{value}</div>
    </div>
  );
}

/** 0 이어야 하는 원장 건강 수가 0 이 아니거나 옛 이미지 Agent 가 있으면 맨 위에 말한다. */
function HealthWarning({ usage }: { usage: AdminUsage }) {
  const text = useT().usage.health;
  const health = usage.health;
  const counts = HEALTH_KEYS.filter((k) => health[k] > 0);
  if (counts.length === 0 && health.agents_without_telemetry === 0) return null;
  return (
    <Alert variant="warn" data-testid="usage-health-warning">
      {counts.length > 0 ? <p>{text.warning(counts.map((k) => `${text.names[k]} ${health[k]}`).join(", "))}</p> : null}
      {health.agents_without_telemetry > 0 ? <p>{text.oldAgents(health.agents_without_telemetry)}</p> : null}
    </Alert>
  );
}

function Unpriced({ usage }: { usage: AdminUsage }) {
  const t = useT();
  const text = t.usage.unpriced;
  return (
    <section className="space-y-2" data-testid="usage-unpriced">
      <h2 className="text-sm font-semibold text-heading">{text.title}</h2>
      <p className="text-xs text-muted">{text.hint}</p>
      <Card>
        <Table>
          <THead>
            <TR>
              <TH>{t.usage.columns.name}</TH>
              <TH className="text-right">{t.usage.columns.calls}</TH>
            </TR>
          </THead>
          <TBody>
            {usage.unpriced.map((u, i) => (
              <TR key={i}>
                <TD>{modelLabel(u)}</TD>
                <TD className="text-right">{formatNumber(t, u.calls)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </Card>
    </section>
  );
}
