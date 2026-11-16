/**
 * 가격표 카드 (P37 13절). Platform Settings 아래에 놓인다.
 *
 * 관리자 규칙은 줄마다 고치고 한 번의 PUT 으로 저장한다. 서버는 읽은 판(`version`)일 때만 받는다. 기본 가격은 읽기만 하고
 * 확인한 날짜를 늘 보인다. 공식 가격 페이지는 예고 없이 바뀌기 때문이다. "복사해서 고치기" 는 기본 가격 한 줄을 관리자 규칙으로
 * 옮긴다. 가격은 행이 들어올 때 매기므로 지난 값을 고치려면 "다시 계산" 을 연다.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { getPricing, getReprice, putPricing, startReprice } from "@/api/resources";
import type { DefaultPriceRule, PriceRule, Pricing } from "@/api/types";
import { ErrorMessage } from "@/components/ErrorMessage";
import { queryGate } from "@/components/QueryGate";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { PRICING_KEY, USAGE_KEY } from "@/lib/queryKeys";
import { dayStart, formatDuration, formatNumber } from "@/lib/utils";

const REPRICE_KEY = [...USAGE_KEY, "reprice"] as const;
/* 가격 규칙의 호출 종류는 서버의 타입에서 온다. 종류가 바뀌면 여기서 컴파일이 깨진다. */
const PRICE_KIND_SET: Record<PriceRule["provider_kind"], true> = { openai: true, anthropic: true, stub: true, web_search: true };
const PRICE_KINDS = Object.keys(PRICE_KIND_SET) as PriceRule["provider_kind"][];
/** 다시 계산을 지켜보는 간격(ms). */
const REPRICE_POLL_MS = 2000;

/** 입력칸의 값. 숫자는 지우고 다시 적는 동안 빈 글자일 수 있다. */
interface RuleForm {
  id: string | null;
  provider_kind: PriceRule["provider_kind"];
  provider_name: string;
  model: string;
  input: string;
  output: string;
  cache_read: string;
  cache_write: string;
  request: string;
  effective_from: string;
  note: string;
  /** 긴 프롬프트의 값. 칸은 없고 복사와 고침이 그대로 옮긴다 */
  tier: PriceRule["tier"];
}

function str(v: number | string | null | undefined): string {
  return v == null ? "" : String(v);
}

export function toForm(r: PriceRule): RuleForm {
  return {
    id: r.id ?? null,
    provider_kind: r.provider_kind,
    provider_name: str(r.provider_name),
    model: r.model,
    input: str(r.input),
    output: str(r.output),
    cache_read: str(r.cache_read),
    cache_write: str(r.cache_write),
    request: str(r.request),
    effective_from: r.effective_from,
    note: str(r.note),
    tier: r.tier ?? null,
  };
}

/** 칸 값을 규칙으로. 빈 cache·request 칸은 null 이고, 빈 이름·메모도 null 이다. tier 는 있을 때만 싣는다. */
export function fromForm(f: RuleForm): PriceRule {
  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  return {
    id: f.id,
    provider_kind: f.provider_kind,
    provider_name: f.provider_name.trim() || null,
    model: f.model.trim(),
    input: num(f.input) ?? 0,
    output: num(f.output) ?? 0,
    cache_read: num(f.cache_read),
    cache_write: num(f.cache_write),
    request: num(f.request),
    effective_from: f.effective_from,
    note: f.note.trim() || null,
    ...(f.tier ? { tier: f.tier } : {}),
  };
}

/** 기본 가격 한 줄을 관리자 규칙으로 옮긴다. 출처와 확인 날짜는 메모로 남긴다. */
function copyOf(d: DefaultPriceRule): RuleForm {
  return { ...toForm({ ...d, id: null }), note: d.note ?? d.source };
}

export function PriceTable() {
  const t = useT();
  const text = t.usage.pricing;
  const client = useQueryClient();
  const q = useQuery({ queryKey: PRICING_KEY, queryFn: getPricing });
  const [rules, setRules] = useState<RuleForm[] | null>(null);
  const [reprice, setReprice] = useState(false);
  useEffect(() => {
    if (q.data && rules === null) setRules(q.data.rules.map(toForm));
  }, [q.data, rules]);
  const save = useMutation({
    mutationFn: (p: { version: number; rules: RuleForm[] }) => putPricing(p.version, p.rules.map(fromForm)),
    onSuccess: (saved: Pricing) => {
      client.setQueryData(PRICING_KEY, saved);
      setRules(saved.rules.map(toForm));
    },
  });
  const gate = queryGate(q, { testId: "pricing-error" });
  const set = (i: number, patch: Partial<RuleForm>) => setRules((rs) => (rs ?? []).map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Card className="mt-4" data-testid="price-table">
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
        <p className="text-xs text-muted">{text.hint}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {gate ??
          (q.data && rules ? (
            <>
              <RulesTable rules={rules} onChange={set} onRemove={(i) => setRules(rules.filter((_, j) => j !== i))} />
              <div className="flex flex-wrap items-center justify-end gap-2">
                <ErrorMessage error={save.error} testId="pricing-save-error" />
                <Button size="sm" variant="outline" data-testid="pricing-add" onClick={() => setRules([...rules, toForm(blankRule())])}>
                  {text.add}
                </Button>
                <Button size="sm" variant="outline" data-testid="pricing-reprice" onClick={() => setReprice(true)}>
                  {text.reprice.open}
                </Button>
                <Button size="sm" data-testid="pricing-save" disabled={save.isPending} onClick={() => save.mutate({ version: q.data.version, rules })}>
                  {save.isPending ? t.shared.saving : t.shared.saveChanges}
                </Button>
              </div>
              <DefaultsTable defaults={q.data.defaults} onCopy={(d) => setRules([...rules, copyOf(d)])} />
            </>
          ) : null)}
      </CardContent>
      <RepriceDialog
        open={reprice}
        onClose={() => {
          setReprice(false);
          void client.invalidateQueries({ queryKey: USAGE_KEY });
        }}
      />
    </Card>
  );
}

function blankRule(): PriceRule {
  return { id: null, provider_kind: "openai", model: "", input: 0, output: 0, effective_from: new Date().toISOString().slice(0, 10) };
}

function RulesTable({ rules, onChange, onRemove }: { rules: RuleForm[]; onChange: (i: number, patch: Partial<RuleForm>) => void; onRemove: (i: number) => void }) {
  const t = useT();
  const columns = t.usage.pricing.columns;
  const numeric = (i: number, key: "input" | "output" | "cache_read" | "cache_write" | "request", label: string) => (
    <TD>
      <Input type="number" min={0} step="any" aria-label={label} data-testid={`pricing-${key}-${i}`} className="h-8 w-24" value={rules[i][key]} onChange={(e) => onChange(i, { [key]: e.target.value })} />
    </TD>
  );
  return (
    <Table data-testid="pricing-rules" className="[&_td]:px-2 [&_th]:px-2 [&_th]:whitespace-nowrap">
      <THead>
        <TR>
          <TH>{columns.kind}</TH>
          <TH>{columns.provider}</TH>
          <TH>{columns.model}</TH>
          <TH>{columns.input}</TH>
          <TH>{columns.output}</TH>
          <TH>{columns.cacheRead}</TH>
          <TH>{columns.cacheWrite}</TH>
          <TH>{columns.request}</TH>
          <TH>{columns.effectiveFrom}</TH>
          <TH>{columns.note}</TH>
          <TH></TH>
        </TR>
      </THead>
      <TBody>
        {rules.length === 0 ? <EmptyRow colSpan={11}>{t.usage.pricing.empty}</EmptyRow> : null}
        {rules.map((r, i) => (
          <TR key={i} data-testid={`pricing-rule-${i}`}>
            <TD>
              <Select aria-label={columns.kind} data-testid={`pricing-kind-${i}`} className="h-8 w-32" value={r.provider_kind} onChange={(e) => onChange(i, { provider_kind: e.target.value as RuleForm["provider_kind"] })}>
                {PRICE_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {t.usage.pricing.kinds[k]}
                  </option>
                ))}
              </Select>
            </TD>
            <TD>
              <Input aria-label={columns.provider} data-testid={`pricing-provider-${i}`} className="h-8 w-28" value={r.provider_name} onChange={(e) => onChange(i, { provider_name: e.target.value })} />
            </TD>
            <TD>
              <Input aria-label={columns.model} data-testid={`pricing-model-${i}`} className="h-8 w-36" value={r.model} onChange={(e) => onChange(i, { model: e.target.value })} />
            </TD>
            {numeric(i, "input", columns.input)}
            {numeric(i, "output", columns.output)}
            {numeric(i, "cache_read", columns.cacheRead)}
            {numeric(i, "cache_write", columns.cacheWrite)}
            {numeric(i, "request", columns.request)}
            <TD>
              <Input type="date" aria-label={columns.effectiveFrom} data-testid={`pricing-from-${i}`} className="h-8 w-36" value={r.effective_from} onChange={(e) => onChange(i, { effective_from: e.target.value })} />
            </TD>
            <TD>
              <Input aria-label={columns.note} data-testid={`pricing-note-${i}`} className="h-8 w-40" value={r.note} onChange={(e) => onChange(i, { note: e.target.value })} />
            </TD>
            <TD>
              <Button size="sm" variant="ghost" data-testid={`pricing-remove-${i}`} onClick={() => onRemove(i)}>
                {t.shared.remove}
              </Button>
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function DefaultsTable({ defaults, onCopy }: { defaults: DefaultPriceRule[]; onCopy: (d: DefaultPriceRule) => void }) {
  const t = useT();
  const text = t.usage.pricing;
  return (
    <section className="space-y-2" data-testid="pricing-defaults">
      <h3 className="text-sm font-semibold text-heading">{text.defaultsTitle}</h3>
      <p className="text-xs text-muted">{text.defaultsHint}</p>
      <Table className="[&_td]:px-2 [&_th]:px-2 [&_th]:whitespace-nowrap">
        <THead>
          <TR>
            <TH>{text.columns.kind}</TH>
            <TH>{text.columns.model}</TH>
            <TH>{text.columns.input}</TH>
            <TH>{text.columns.output}</TH>
            <TH>{text.columns.cacheRead}</TH>
            <TH>{text.columns.cacheWrite}</TH>
            <TH>{text.columns.request}</TH>
            <TH>{text.columns.checkedAt}</TH>
            <TH></TH>
          </TR>
        </THead>
        <TBody>
          {defaults.map((d, i) => (
            <TR key={d.id ?? i} data-testid={`pricing-default-${i}`}>
              <TD>{text.kinds[d.provider_kind]}</TD>
              <TD className="font-mono text-xs">{d.model}</TD>
              <TD>{d.input}</TD>
              <TD>{d.output}</TD>
              <TD>{d.cache_read ?? "-"}</TD>
              <TD>{d.cache_write ?? "-"}</TD>
              <TD>{d.request ?? "-"}</TD>
              <TD title={d.note ? `${d.note} (${d.source})` : d.source}>{d.checked_at}</TD>
              <TD>
                <Button size="sm" variant="ghost" data-testid={`pricing-copy-${i}`} onClick={() => onCopy(d)}>
                  {text.copy}
                </Button>
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </section>
  );
}

/** 다시 계산. 기간을 정해 시작하고, 끝날 때까지 진행을 2초마다 다시 읽는다. */
function RepriceDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const text = t.usage.pricing.reprice;
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [running, setRunning] = useState(false);
  const status = useQuery({
    queryKey: REPRICE_KEY,
    queryFn: getReprice,
    enabled: open && running,
    refetchInterval: (query) => (query.state.data && !query.state.data.finished_at ? REPRICE_POLL_MS : false),
  });
  const start = useMutation({
    // 끝은 고른 날까지라 그 다음 날 0시를 보낸다(서버는 끝을 들지 않는다)
    mutationFn: () => startReprice(dayStart(since) ?? "", dayStart(until, 1) ?? ""),
    onSuccess: () => setRunning(true),
  });
  const s = status.data;
  return (
    <Modal open={open} title={text.title} testId="reprice-dialog" onClose={onClose}>
      <div className="space-y-3 p-5">
        <p className="text-sm text-muted">{text.hint}</p>
        <div className="flex flex-wrap gap-2">
          <Field label={text.since} htmlFor="reprice-since">
            <Input id="reprice-since" type="date" className="h-8 w-40" data-testid="reprice-since" value={since} onChange={(e) => setSince(e.target.value)} />
          </Field>
          <Field label={text.until} htmlFor="reprice-until">
            <Input id="reprice-until" type="date" className="h-8 w-40" data-testid="reprice-until" value={until} onChange={(e) => setUntil(e.target.value)} />
          </Field>
        </div>
        <ErrorMessage error={start.error} testId="reprice-error" />
        {s ? (
          <p className="text-sm" data-testid="reprice-progress">
            {s.finished_at ? text.done(formatNumber(t, s.done)) : text.progress(formatNumber(t, s.done), formatNumber(t, s.done + s.left), s.eta_seconds == null ? null : formatDuration(t, s.eta_seconds))}
          </p>
        ) : null}
        <div className="flex justify-end">
          <Button size="sm" data-testid="reprice-start" disabled={!since || !until || start.isPending || (running && !s?.finished_at)} onClick={() => start.mutate()}>
            {text.start}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
