/**
 * Agent 상세의 지표 탭 (P37 11절). 런타임의 지금 상태(세션·Tool 적재·일당 예산)와, 고른 기간(1시간·24시간) 안의 모든
 * 레플리카의 수를 플랫폼이 이벤트 저장소와 원장에서 센 값이다. 파드 하나의 카운터가 아니므로 파드마다 다르다는 말은 없다.
 *
 * 런타임에 연결할 수 없으면 런타임 몫만 비고 까닭이 경고로 선다. 이벤트가 파드 로그로만 오고 도는 이미지가 밀어 보낼 줄
 * 모르면(옛 이미지) 다시 배포하라는 안내가 선다. 다시 배포한 런타임은 이벤트를 바로 보낸다. 멈춘 턴은 "중지" 로 보이고 지연과
 * 오류율에 들지 않는다. 옛 플랫폼이 준 모양에 없는 몫은 비운 값으로 채워 그린다.
 * 잠든 Agent 의 런타임 몫은 플랫폼이 런타임을 부르지 않고 agent_asleep(깨우는 중이면 agent_waking)으로 비운다(P44 8절). 그것은 고장이
 * 아니라 런타임 몫이 빠진 까닭이라 경고 대신 안내로 그 문장만 보인다. 기간의 수는 이벤트 저장소와 원장의 것이라 그대로 보인다.
 */
import { getAgentMetrics } from "@/api/resources";
import type { AgentMetrics, MetricsWindow } from "@/api/types";
import { DailyBars } from "@/components/charts/DailyBars";
import { CallsCard } from "@/components/metrics/CallsCard";
import { Facts } from "@/components/metrics/Facts";
import { WindowedMetrics } from "@/components/metrics/WindowedMetrics";
import { Tile } from "@/components/overview/Tile";
import { Alert, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { WarningDetail } from "@/components/WarningDetail";
import { useT, type Messages } from "@/i18n";
import { AGENT_ASLEEP, AGENT_WAKING } from "@/lib/agentSleep";
import { describeCode } from "@/lib/errorText";
import { useTenant } from "@/lib/tenant";
import { formatUsd, TELEMETRY_POLL_MS, tokensOf } from "@/lib/usage";
import { formatDate, formatDay, metricNumber } from "@/lib/utils";
import { describeWarning, unshownWarnings, warningKey } from "@/lib/warningText";

const WINDOWS: readonly MetricsWindow[] = ["1h", "24h"];
/** 런타임 몫이 빈 까닭이 쉬는 상태인 코드. 고장이 아니므로 경고로 세우지 않는다. */
const SLEEP_CODES: ReadonlySet<string> = new Set([AGENT_ASLEEP, AGENT_WAKING]);
/** 턴 결과 줄의 차례. 이름은 사전의 agents.metrics.turnResult 다. */
const TURN_RESULTS = ["done", "pending", "turn_failed", "turn_timeout", "quota_exhausted", "budget_exceeded", "cancelled"] as const satisfies readonly (keyof AgentMetrics["turns"])[];

/** 런타임이 주는 epoch 초(부동소수)를 사람이 읽는 시각으로. */
function fromEpoch(t: Messages, seconds: number | null | undefined): string {
  return typeof seconds === "number" ? formatDate(t, new Date(seconds * 1000).toISOString()) : "-";
}

export function MetricsTab({ agentId, shownWarnings = [] }: { agentId: string; shownWarnings?: string[] }) {
  const t = useT();
  const tenant = useTenant();
  return (
    <WindowedMetrics
      windows={WINDOWS}
      initial="1h"
      names={t.agents.metrics.window}
      queryKey={["agents", tenant, agentId]}
      load={(period, tz) => getAgentMetrics(agentId, period, tz)}
      testId="metrics"
      pollMs={TELEMETRY_POLL_MS}
    >
      {(data) => <MetricsBody m={withDefaults(data)} shownWarnings={shownWarnings} />}
    </WindowedMetrics>
  );
}

/** 비운 지표. 옛 플랫폼(롤링 업그레이드 중의 레플리카)이 준 모양에 없는 몫을 채운다. */
const EMPTY_CALLS = { calls: 0, errors: 0, p95_ms: null };
const EMPTY_TURNS = { done: 0, pending: 0, turn_failed: 0, turn_timeout: 0, quota_exhausted: 0, budget_exceeded: 0, cancelled: 0 };

/** 받은 지표에 없는 몫을 비운 값으로 채운다. 옛 모양에도 탭이 그려진다. */
export function withDefaults(raw: Partial<AgentMetrics>): AgentMetrics {
  return {
    window: raw.window ?? "1h",
    sessions: raw.sessions ?? null,
    totals: raw.totals ?? null,
    daily_tokens: raw.daily_tokens ?? null,
    tools: raw.tools ?? null,
    warnings: raw.warnings ?? null,
    runtime: raw.runtime ?? null,
    chat: raw.chat ?? null,
    runtime_error: raw.runtime_error ?? null,
    turns: { ...EMPTY_TURNS, ...raw.turns },
    rejected: { total: 0, by_code: {}, ...raw.rejected },
    errors: { turns: 0, error_rate: 0, ...raw.errors },
    latency_ms: { avg: 0, p50: 0, p95: 0, max: 0, ...raw.latency_ms },
    model: { ...EMPTY_CALLS, retries: 0, fallbacks: 0, by_model: [], ...raw.model },
    tool_calls: { ...EMPTY_CALLS, by_tool: [], ...raw.tool_calls },
    usage: raw.usage ?? {},
    cost: { usd: 0, unpriced_calls: 0, ...raw.cost },
    cost_daily: raw.cost_daily ?? [],
    telemetry: { mode: "none", pods: [], dropped: { cost: 0, other: 0 }, image_pushes: null, ...raw.telemetry },
  };
}

function MetricsBody({ m, shownWarnings }: { m: AgentMetrics; shownWarnings: string[] }) {
  const t = useT();
  const text = t.agents.metrics;
  const sessions = m.sessions ?? {};
  // 다시 배포하라는 안내는 이벤트가 파드 로그로만 오고 도는 이미지가 밀어 보낼 줄 모를 때만이다(18절). none 은
  // 이 기간에 이벤트가 없는 것이라 안내하지 않는다
  const redeploy = m.telemetry.mode === "log" && m.telemetry.image_pushes === false;
  return (
    <>
      {/* 경고는 숫자보다 먼저 온다. 머리에 이미 선 것은 뺀다. 같은 문장이 한 화면에 두 번 서면 둘 다 안 읽힌다 */}
      {unshownWarnings(m.warnings ?? [], shownWarnings).map((w) => {
        const d = describeWarning(t, w);
        return (
          <Alert key={w} variant="warn" data-testid={`metrics-warning-${warningKey(w)}`}>
            <span className="font-medium">{d.label}</span>
            {d.text ? <span> {d.text}</span> : null}
            <WarningDetail detail={d.detail} />
          </Alert>
        );
      })}
      {m.runtime_error && SLEEP_CODES.has(m.runtime_error.code) ? (
        <Alert variant="info" data-testid="metrics-runtime-asleep">
          {describeCode(t, m.runtime_error.code, m.runtime_error.message).text}
        </Alert>
      ) : m.runtime_error ? (
        <Alert variant="warn" data-testid="metrics-runtime-error">
          {text.runtimeError(describeCode(t, m.runtime_error.code, m.runtime_error.message).text)}
        </Alert>
      ) : null}
      {redeploy ? (
        <Alert variant="info" data-testid="metrics-redeploy">
          {text.redeployToPush}
        </Alert>
      ) : null}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Tile label={text.sessions} value={`${metricNumber(t, sessions.active)} / ${metricNumber(t, sessions.pending)} / ${metricNumber(t, sessions.closed)}`} testId="metrics-sessions" />
        <Tile label={text.turns} value={metricNumber(t, m.turns.done)} testId="metrics-turns" />
        <Tile label={text.tokens} value={metricNumber(t, tokensOf(m.usage))} testId="metrics-tokens" />
        <Tile label={text.latency} value={metricNumber(t, m.latency_ms.p95)} testId="metrics-latency" />
        <Tile label={text.cost} value={formatUsd(t, m.cost.usd)} note={m.cost.unpriced_calls ? <span className="text-xs text-muted">{text.unpriced(m.cost.unpriced_calls)}</span> : null} testId="metrics-cost" />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <TurnResultsCard m={m} />
        <RejectedCard m={m} />
        <CallsCard title={text.modelTitle} totals={m.model} extra={[[text.retries, m.model.retries], [text.fallbacks, m.model.fallbacks]]} nameLabel={text.model} rows={m.model.by_model.map((r) => ({ name: r.model, ...r }))} testId="metrics-model" />
        <CallsCard title={text.toolTitle} totals={m.tool_calls} nameLabel={t.shared.columns.tool} rows={m.tool_calls.by_tool.map((r) => ({ name: r.tool, ...r }))} testId="metrics-tool-calls" />
        <ToolLoadingCard m={m} />
        <DailyBudgetCard m={m} />
        <Card>
          <CardHeader>
            <CardTitle>{text.costDaily}</CardTitle>
          </CardHeader>
          <CardContent>
            <DailyBars
              days={m.cost_daily.map((d) => ({ key: d.day, label: formatDay(t, d.day), parts: [{ key: "cost", label: t.usage.totals.cost, value: d.cost_usd }] }))}
              format={(v) => formatUsd(t, v)}
              label={t.usage.chart.label}
              empty={t.usage.chart.empty}
              testId="metrics-cost-daily"
            />
          </CardContent>
        </Card>
        <TelemetryCard m={m} />
      </div>
    </>
  );
}

function TurnResultsCard({ m }: { m: AgentMetrics }) {
  const t = useT();
  const text = t.agents.metrics;
  const latency = m.latency_ms;
  const rows = TURN_RESULTS.map((k) => ({ label: text.turnResult[k], value: metricNumber(t, m.turns[k]), testId: `metrics-turns-${k}` }));
  return (
    <Card>
      <CardHeader>
        <CardTitle>{text.turnResults}</CardTitle>
      </CardHeader>
      <CardContent>
        <Facts
          testId="metrics-turn-results"
          rows={[
            ...rows,
            { label: text.errorRate, value: `${(m.errors.error_rate * 100).toFixed(1)}%`, testId: "metrics-error-rate" },
            { label: text.latencyStats, value: text.latencyValues(metricNumber(t, latency.avg), metricNumber(t, latency.p50), metricNumber(t, latency.max)) },
          ]}
        />
      </CardContent>
    </Card>
  );
}

function RejectedCard({ m }: { m: AgentMetrics }) {
  const t = useT();
  const text = t.agents.metrics;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{text.rejected}</CardTitle>
        <span className="text-xs text-muted" data-testid="metrics-rejected-total">
          {metricNumber(t, m.rejected.total)}
        </span>
      </CardHeader>
      <CardContent>
        {m.rejected.total ? (
          <dl className="grid grid-cols-[1fr_auto] gap-y-1 text-sm" data-testid="metrics-rejected">
            {Object.entries(m.rejected.by_code).map(([code, n]) => (
              <div key={code} className="contents">
                <dt className="text-xs">{describeCode(t, code, code).text}</dt>
                <dd>{metricNumber(t, n)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-sm text-muted">{text.noRejected}</p>
        )}
      </CardContent>
    </Card>
  );
}

function ToolLoadingCard({ m }: { m: AgentMetrics }) {
  const t = useT();
  const text = t.agents.metrics;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{text.toolLoading}</CardTitle>
      </CardHeader>
      <CardContent>
        {/* 런타임은 개수와 적재 상태만 준다. 이름은 정의 탭이 보인다 */}
        <Facts
          testId="metrics-tools"
          rows={[
            { label: text.boundTools, value: text.toolCount(metricNumber(t, m.tools?.count)), testId: "metrics-tools-count" },
            { label: text.loadedAt, value: fromEpoch(t, m.tools?.loaded_at) },
          ]}
        />
        {m.tools?.last_error ? (
          <Alert variant="error" className="mt-3" data-testid="metrics-tools-error">
            {text.lastLoadError(m.tools.last_error)}
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}

function DailyBudgetCard({ m }: { m: AgentMetrics }) {
  const t = useT();
  const text = t.agents.metrics;
  const daily = m.daily_tokens;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{text.dailyBudget}</CardTitle>
      </CardHeader>
      <CardContent>
        {daily ? (
          <Facts
            testId="metrics-daily"
            rows={[
              { label: text.usedOfLimit, value: `${metricNumber(t, daily.used)} / ${metricNumber(t, daily.limit)}` },
              { label: text.budgetWindow, value: <span className="font-mono text-xs">{daily.window ?? "-"}</span> },
            ]}
          />
        ) : (
          <p className="text-sm text-muted" data-testid="metrics-daily-none">
            {text.noDailyBudget}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** 이벤트 수집의 상태. 어느 파드가 보내고 있는지와 파드가 버린 수다. */
function TelemetryCard({ m }: { m: AgentMetrics }) {
  const t = useT();
  const text = t.agents.metrics;
  const { mode, pods, dropped } = m.telemetry;
  return (
    <Card data-testid="metrics-telemetry">
      <CardHeader>
        <CardTitle>{text.telemetryTitle}</CardTitle>
        <span className="text-xs text-muted" data-testid="metrics-telemetry-mode">
          {text.telemetryMode[mode]}
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Table>
          <THead>
            <tr>
              <TH>{text.podColumns.pod}</TH>
              <TH>{text.podColumns.lastAt}</TH>
              <TH>{text.podColumns.received}</TH>
              <TH>{text.podColumns.refused}</TH>
              <TH>{text.podColumns.dropped}</TH>
            </tr>
          </THead>
          <TBody>
            {pods.length ? (
              pods.map((p) => (
                <TR key={p.uid ?? p.name ?? "-"} data-testid="metrics-telemetry-pod">
                  <TD className="break-all font-mono text-xs">{p.name ?? "-"}</TD>
                  <TD className="text-xs text-muted">{formatDate(t, p.last_at)}</TD>
                  <TD>{metricNumber(t, p.received)}</TD>
                  <TD>{metricNumber(t, p.refused)}</TD>
                  <TD>{metricNumber(t, p.dropped.cost + p.dropped.other)}</TD>
                </TR>
              ))
            ) : (
              <EmptyRow colSpan={5}>{text.noPods}</EmptyRow>
            )}
          </TBody>
        </Table>
        {dropped.cost || dropped.other ? <p className="text-xs text-muted">{text.dropped(metricNumber(t, dropped.cost), metricNumber(t, dropped.other))}</p> : null}
      </CardContent>
    </Card>
  );
}
