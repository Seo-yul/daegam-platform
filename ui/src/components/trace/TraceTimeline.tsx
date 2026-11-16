/**
 * 실행 하나의 폭포 그림 (P37 12절). 줄 하나가 span 하나(실행, 모델, Tool, 서브에이전트, 임베딩)이고 부모 아래로 들여 쓴다.
 * 막대는 실행 안의 시작과 길이다. 오류·재시도·대체 모델·캐시 적중·게이트웨이 경로를 배지로 단다. 상한에 걸린 트레이스는 걸린
 * 상한을 아래에 적는다.
 *
 * 줄은 단추라 키보드로 열고 닫는다. 열면 속성과 본문(기록됐을 때만)이 보인다. 둘 다 글로만 그린다(React 가 이스케이프한다).
 * 런타임의 Tool 결과는 남이 만든 글이라 마크다운으로 그리지 않는다. span 이 아닌 이벤트(재시도, 가드레일, 승인 요청 같은)는
 * 그림 아래에 시각 순으로 선다.
 */
import { useId, useState } from "react";

import type { AgentTrace, TraceSpan } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { CodePre } from "@/components/ui/code-snippet";
import { FOCUS_RING } from "@/components/ui/json-reveal";
import { useT, type Messages } from "@/i18n";
import { eventName } from "@/lib/agentEvents";
import { formatUsd } from "@/lib/usage";
import { cn, formatDate, formatNumber } from "@/lib/utils";

type Flag = "error" | "retry" | "fallback" | "cache" | "gateway";

function numberAt(record: Record<string, unknown> | undefined, key: string): number {
  const v = record?.[key];
  return typeof v === "number" ? v : 0;
}

/** 값이 객체일 때만 그것이다. 속성은 런타임이 보낸 값이라 모양을 믿지 않는다. */
function recordOf(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
}

/**
 * span 의 배지. 오류는 서버가 실행과 호출의 결과 규칙으로 정한 값이다(멈춘 턴과 도는 중은 오류가 아니다). 재시도는 같은 부모 아래
 * 앞 호출이 실패한 다음 호출이고(attempt), 캐시는 읽은 캐시 토큰이 있는 호출이다.
 */
export function spanFlags(span: TraceSpan): Flag[] {
  const a = span.attributes;
  const usage = span.ledger?.usage ?? recordOf(a.usage);
  const flags: Flag[] = [];
  if (span.error) flags.push("error");
  if (numberAt(a, "attempt") > 1) flags.push("retry");
  if (a.fallback === true || a.role === "fallback") flags.push("fallback");
  if (numberAt(usage, "cache_read_tokens") > 0) flags.push("cache");
  if (a.route === "gateway" || span.ledger?.route === "gateway") flags.push("gateway");
  return flags;
}

/** 부모 사슬의 깊이. 부모가 이 트레이스에 없으면 맨 위다. */
function depths(spans: TraceSpan[]): Map<string, number> {
  const parent = new Map(spans.map((s) => [s.span_id ?? "", s.parent_span_id ?? null]));
  const out = new Map<string, number>();
  for (const s of spans) {
    let depth = 0;
    let at = s.parent_span_id ?? null;
    const seen = new Set<string>();
    while (at && parent.has(at) && !seen.has(at)) {
      seen.add(at);
      depth += 1;
      at = parent.get(at) ?? null;
    }
    out.set(s.span_id ?? "", depth);
  }
  return out;
}

function msOf(iso: string): number {
  return new Date(iso).getTime();
}

function durationText(t: Messages, ms: number | null | undefined): string {
  return typeof ms === "number" ? t.common.time.ms(formatNumber(t, Math.round(ms))) : "-";
}

/** 상한에 걸린 트레이스의 안내. 서버가 걸린 상한(spans, events)과 그 수를 준다. */
function truncatedText(t: Messages, trace: AgentTrace): string {
  const text = t.agents.trace.truncated;
  return trace.truncated_by === "spans" ? text.spans(formatNumber(t, trace.limit)) : text.events;
}

export function TraceTimeline({ trace, testId = "trace-timeline" }: { trace: AgentTrace; testId?: string }) {
  const t = useT();
  const text = t.agents.trace;
  const spans = [...trace.spans].sort((a, b) => msOf(a.start) - msOf(b.start));
  const start = Math.min(...spans.map((s) => msOf(s.start)));
  const end = Math.max(...spans.map((s) => msOf(s.start) + (s.duration_ms ?? 0)));
  const total = Math.max(1, end - start);
  const depth = depths(spans);
  return (
    <div className="flex flex-col gap-2" data-testid={testId}>
      <ol className="flex flex-col gap-0.5" aria-label={text.timeline}>
        {spans.map((s, i) => (
          <SpanRow key={`${s.span_id}-${i}`} span={s} depth={depth.get(s.span_id ?? "") ?? 0} left={((msOf(s.start) - start) / total) * 100} width={((s.duration_ms ?? 0) / total) * 100} testId={`${testId}-span-${i}`} />
        ))}
      </ol>
      {trace.truncated ? (
        <p className="text-xs text-muted" data-testid={`${testId}-truncated`}>
          {truncatedText(t, trace)}
        </p>
      ) : null}
      {trace.points.length ? (
        <div className="flex flex-col gap-1" data-testid={`${testId}-points`}>
          <span className="text-xs font-medium text-muted">{text.points}</span>
          <ul className="flex flex-col gap-0.5 text-xs">
            {trace.points.map((p, i) => (
              <li key={`${p.event}-${p.ts}-${i}`} className="flex flex-wrap gap-2">
                <span className="text-muted">{formatDate(t, p.ts)}</span>
                <span title={p.event}>{eventName(t, p.event)}</span>
                <span className="break-all text-muted">{compact(p.attributes)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** 속성 한 줄 요약. 열지 않은 점 이벤트를 읽게 한다. */
function compact(attributes: Record<string, unknown>): string {
  return Object.entries(attributes)
    .filter(([, v]) => v !== null && typeof v !== "object")
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(" ");
}

function SpanRow({ span, depth, left, width, testId }: { span: TraceSpan; depth: number; left: number; width: number; testId: string }) {
  const t = useT();
  const text = t.agents.trace;
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const flags = spanFlags(span);
  return (
    <li data-testid={testId} data-kind={span.kind}>
      <button type="button" aria-expanded={open} aria-controls={detailId} className={cn("flex w-full items-center gap-2 rounded-chip px-1 py-0.5 text-left text-xs hover:bg-hover", FOCUS_RING)} onClick={() => setOpen(!open)} data-testid={`${testId}-toggle`}>
        <span className="flex w-56 shrink-0 items-center gap-1.5 truncate" style={{ paddingLeft: `${depth * 12}px` }}>
          <Badge variant="muted">{text.kind[span.kind]}</Badge>
          <span className="truncate font-mono">{span.name}</span>
        </span>
        <span className="relative h-2 flex-1 rounded-full bg-surface-strong" aria-hidden>
          <span className={cn("absolute top-0 h-2 rounded-full", flags.includes("error") ? "bg-err" : "bg-accent")} style={{ left: `${left}%`, width: `${Math.max(width, 0.5)}%` }} />
        </span>
        <span className="w-20 shrink-0 text-right text-muted">{durationText(t, span.duration_ms)}</span>
        <span className="flex shrink-0 gap-1">
          {flags.map((f) => (
            <Badge key={f} variant={f === "error" ? "error" : "muted"} data-testid={`${testId}-flag-${f}`}>
              {text.flag[f]}
            </Badge>
          ))}
        </span>
      </button>
      {open ? (
        <div id={detailId} className="ml-4 flex flex-col gap-1 py-1 text-xs" data-testid={`${testId}-detail`}>
          {span.cost_usd != null ? <span>{text.cost(formatUsd(t, span.cost_usd))}</span> : null}
          <span className="font-medium text-muted">{text.attributes}</span>
          <CodePre>{JSON.stringify(span.attributes, null, 2)}</CodePre>
          {span.ledger ? <CodePre>{JSON.stringify(span.ledger, null, 2)}</CodePre> : null}
          {span.body ? (
            <>
              <span className="font-medium text-muted">{text.body}</span>
              <CodePre data-testid={`${testId}-body`}>{JSON.stringify(span.body, null, 2)}</CodePre>
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
