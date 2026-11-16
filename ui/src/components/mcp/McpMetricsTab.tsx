/**
 * MCP Server·Gateway 상세의 메트릭 탭 (P51 5절). 감사 기록을 시간마다 묶은 것을 플랫폼이 더한 값이라 모든 레플리카의 수이고,
 * 탭을 열 때만 요청한다. 두 상세가 같은 탭을 쓰고 경로만 다르다. Gateway 의 답에는 백엔드별 줄이 더 있다.
 *
 * 기록이 남지 않는 클러스터이거나 수집기가 꺼져 있으면 0 이 "호출이 없었다" 가 아니므로 숫자 대신 까닭만 선다. 다시 적용을
 * 기다리는 Gateway 와 아직 거슬러 세는 중인 기간은 숫자를 둔 채 곁에 안내가 선다. 숫자가 보지 못하는 것(6절)은 맨 아래에 적는다.
 */
import type { GatewayMetrics, McpMetrics, McpMetricsBackend, McpMetricsWindow, McpOutcomes } from "@/api/types";
import { DailyBars } from "@/components/charts/DailyBars";
import { type CallRow, CallsCard } from "@/components/metrics/CallsCard";
import { Facts } from "@/components/metrics/Facts";
import { WindowedMetrics } from "@/components/metrics/WindowedMetrics";
import { Alert, Card, CardContent } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { formatDate, formatDay, formatHour, metricNumber } from "@/lib/utils";

/** MCP Server 의 답이거나, 백엔드별 줄이 붙은 Gateway 의 답. */
type Report = McpMetrics & Partial<Pick<GatewayMetrics, "by_backend">>;
type Outcome = keyof McpOutcomes;

const WINDOWS: readonly McpMetricsWindow[] = ["24h", "7d", "30d"];
/**
 * 다시 읽는 주기. 묶음은 롤업 주기(120초)마다 바뀌고 30일 창은 묶음을 많이 더하므로 Agent 탭(15초)보다 드물게 읽는다.
 */
const MCP_METRICS_POLL_MS = 60_000;
/**
 * 결과 갈래마다 고정한 색. 기간을 바꿔도 같은 갈래가 같은 색이다. 적은 차례가 보이는 차례(사실 목록과 막대의 쌓는 차례)다.
 * 성공과 서버 오류는 상태색이다. 오류 응답(청록)과 거부(자홍)는 적록 색각 이상에서도 갈리는 색을 골랐다.
 */
const OUTCOME_COLORS: Readonly<Record<Outcome, string>> = {
  success: "var(--ui-ok)",
  application_error: "var(--ui-kind-entry)",
  denied: "var(--ui-kind-delegate)",
  failure: "var(--ui-warn)",
  error: "var(--ui-err)",
  unknown: "var(--ui-dim)",
};
const OUTCOMES = Object.keys(OUTCOME_COLORS) as Outcome[];

export function McpMetricsTab({ queryKey, load }: { queryKey: readonly unknown[]; load: (period: McpMetricsWindow, tz: string) => Promise<Report> }) {
  const t = useT();
  return (
    <WindowedMetrics windows={WINDOWS} initial="24h" names={t.mcpMetrics.window} queryKey={queryKey} load={load} testId="mcp-metrics" pollMs={MCP_METRICS_POLL_MS}>
      {(data) => <MetricsBody m={data} />}
    </WindowedMetrics>
  );
}

function MetricsBody({ m }: { m: Report }) {
  const t = useT();
  const text = t.mcpMetrics;
  const { source, totals } = m;
  if (source.collector === "off" || source.audit === "off") {
    return (
      <>
        {source.collector === "off" ? (
          <Alert variant="warn" data-testid="mcp-metrics-collector-off">
            {text.collectorOff}
          </Alert>
        ) : null}
        {source.audit === "off" ? (
          <Alert variant="warn" data-testid="mcp-metrics-audit-off">
            {text.auditOff}
          </Alert>
        ) : null}
      </>
    );
  }
  // 거슬러 세는 일이 기간의 시작에 아직 닿지 않았으면 그 앞의 시간은 0 으로 읽힌다
  const counting = source.counted_from !== null && Date.parse(source.counted_from) > Date.parse(m.since);
  const empty = totals.calls + totals.resource_reads + totals.prompt_gets === 0;
  return (
    <>
      {source.audit === "pending" ? (
        <Alert variant="info" data-testid="mcp-metrics-audit-pending">
          {text.auditPending}
        </Alert>
      ) : null}
      {counting ? (
        <Alert variant="info" data-testid="mcp-metrics-counting">
          {text.countingFrom(formatDate(t, source.counted_from))}
        </Alert>
      ) : null}
      {empty ? (
        <p className="text-sm text-muted" data-testid="mcp-metrics-empty">
          {t.shared.callsCard.noCalls}
        </p>
      ) : (
        <Numbers m={m} />
      )}
      <Card data-testid="mcp-metrics-notes">
        <CardContent>
          <ul className="flex list-disc flex-col gap-1 pl-4 text-xs text-muted">
            <li>{text.notes.toolErrors}</li>
            <li>{text.notes.collector}</li>
            <li>{text.notes.hops}</li>
          </ul>
        </CardContent>
      </Card>
    </>
  );
}

function Numbers({ m }: { m: Report }) {
  const t = useT();
  const text = t.mcpMetrics;
  const { totals } = m;
  const duration = totals.duration_ms;
  const stepLabel = m.step === "hour" ? formatHour : formatDay;
  const callTotals = { calls: totals.calls, errors: totals.errors, p95_ms: duration.p95 };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardContent className="flex flex-col gap-2">
          <Facts
            testId="mcp-metrics-facts"
            rows={[
              { label: text.calls, value: metricNumber(t, totals.calls), testId: "mcp-metrics-calls" },
              { label: text.errors, value: metricNumber(t, totals.errors), testId: "mcp-metrics-errors" },
              ...OUTCOMES.map((k) => ({ label: <OutcomeName outcome={k} />, value: metricNumber(t, totals.outcomes[k]), testId: `mcp-metrics-outcome-${k}` })),
              { label: text.duration, value: `${metricNumber(t, duration.p50)} / ${metricNumber(t, duration.p95)} / ${metricNumber(t, duration.max)}`, testId: "mcp-metrics-duration" },
              { label: text.rolledUpAt, value: formatDate(t, m.source.rolled_up_at), testId: "mcp-metrics-rolled-up" },
            ]}
          />
          <p className="text-xs text-muted">{text.approx}</p>
          {totals.resource_reads || totals.prompt_gets ? (
            <p className="text-xs text-muted" data-testid="mcp-metrics-other-calls">
              {text.otherCalls(totals.resource_reads, totals.prompt_gets)}
            </p>
          ) : null}
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <DailyBars
            days={m.series.map((s) => ({
              key: s.start,
              label: stepLabel(t, s.start),
              parts: OUTCOMES.map((k) => ({ key: k, label: text.outcome[k], value: s.outcomes[k] })),
            }))}
            format={(v) => metricNumber(t, v)}
            label={text.chartLabel}
            empty={text.chartEmpty}
            colors={OUTCOME_COLORS}
            testId="mcp-metrics-bars"
          />
        </CardContent>
      </Card>
      <CallsCard
        title={text.byTool}
        totals={callTotals}
        nameLabel={t.shared.columns.tool}
        rows={m.by_tool.map((r) => ({ ...r, name: r.tool, label: r.tool === null ? text.otherTools : undefined }))}
        testId="mcp-metrics-by-tool"
      />
      {m.by_backend ? (
        <CallsCard title={text.byBackend} totals={callTotals} nameLabel={t.gateways.columns.backend} rows={m.by_backend.map((r) => backendRow(text, r))} testId="mcp-metrics-by-backend" />
      ) : null}
    </div>
  );
}

/** 갈래 이름 앞에 막대의 색 점을 둔다. 사실 목록이 막대의 범례를 겸한다. */
function OutcomeName({ outcome }: { outcome: Outcome }) {
  const t = useT();
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className="size-2 rounded-full" style={{ background: OUTCOME_COLORS[outcome] }} />
      {t.mcpMetrics.outcome[outcome]}
    </span>
  );
}

/**
 * 백엔드 줄 하나. 지금 배포에 있는 Entry 는 허브 항목의 이름으로, 스냅샷에 없는 Entry 는 Entry 이름과 그 까닭으로, 백엔드를
 * 정하지 못한 호출은 그 말로 선다. 셋을 더하면 합계다. 보이는 이름은 허브 항목의 이름과 Entry 이름이 겹칠 수 있어 줄의
 * 열쇠는 Entry 이름이고, 백엔드가 없는 줄은 Entry 이름이 될 수 없는 열쇠다.
 */
function backendRow(text: Messages["mcpMetrics"], r: McpMetricsBackend): CallRow {
  if (r.entry === null) return { ...r, id: "(no backend)", name: null, label: text.noBackend };
  const id = `entry:${r.entry}`;
  if (r.hub_item_id === null) {
    return {
      ...r,
      id,
      name: r.entry,
      label: (
        <span className="flex flex-col">
          <span className="font-mono">{r.entry}</span>
          <span className="text-muted">{text.removedBackend}</span>
        </span>
      ),
    };
  }
  return { ...r, id, name: r.name ?? r.entry };
}
