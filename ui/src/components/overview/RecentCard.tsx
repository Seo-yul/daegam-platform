/**
 * "최근 기록" 카드 (D44 셋째). 이 리소스에 최근 무슨 일이 있었는지 짧은 시간선이다.
 *
 * 줄은 문장이 아니라 낱말이다("생성", "정의 r2 저장"). 시각이 오른쪽에 붙는 시간선이라 줄마다 종결
 * 어미를 달면 같은 말이 여섯 번 되풀이되고 정작 무슨 일이었는지가 뒤로 밀린다.
 *
 * 감사 로그는 쓰지 않는다. `/api/audit` 은 `resource_id` 로 거르지 못하고 `audit:read` scope 가 있어야
 * 200 이라 개요를 보는 사람 대부분이 403 을 받는다. 재료는 이미 화면에 와 있는 넷이다.
 * 생성, 리비전 이력의 `created_at`, 배포 쪽을 마지막으로 쓴 시각(`status.since`), 상태를 마지막으로
 * 읽은 시각이다. 허브 게시 시각은 응답에 없어서 지금은 줄이 서지 않는다.
 *
 * Agent 는 리비전 목록 API 가 없어 이력 대신 `last_spec_change` 한 줄을 받는다.
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type Messages, useT } from "@/i18n";
import { cn, formatDate } from "@/lib/utils";

/** 리비전 번호가 붙는 갈래. */
type RevisionKind = "saved" | "deployed";

/** 시간선 줄의 갈래. 사전의 `detail.recentCard.event` 가 갈래마다 낱말을 둔다. */
export type RecentKind = "created" | "edited" | "deployChanged" | "observed" | RevisionKind;

/** 시간선 줄 하나. 낱말 대신 갈래와 리비전을 들고, 낱말은 그릴 때 화면 언어로 만든다. */
export type RecentEvent = {
  /** ISO 시각. 못 읽는 값이면 줄이 서지 않는다. */
  at: string | null | undefined;
} & ({ kind: Exclude<RecentKind, RevisionKind> } | { kind: RevisionKind; revision: number });

export interface RecentInput {
  createdAt?: string | null;
  /** 리비전 이력. 시각이 있는 것만 줄이 된다. */
  revisions?: { revision: number; created_at: string }[];
  /** 리비전 이력을 못 받을 때 쓰는 "정의를 마지막으로 수정". */
  lastSpecChange?: string | null;
  /** 돌고 있는 리비전. 배포 줄의 번호다. */
  deployedRevision?: number | null;
  /** 배포 쪽을 마지막으로 쓴 시각(`status.since`). */
  since?: string | null;
  /** 상태를 마지막으로 읽은 시각. */
  observedAt?: string | null;
  /** 몇 줄까지 보일까. */
  limit?: number;
}

/** 시간선 줄들. 새것이 위다. */
export function recentEvents({
  createdAt,
  revisions = [],
  lastSpecChange,
  deployedRevision,
  since,
  observedAt,
  limit = 6,
}: RecentInput): RecentEvent[] {
  const events: RecentEvent[] = [];
  if (createdAt) events.push({ at: createdAt, kind: "created" });
  for (const r of revisions) events.push({ at: r.created_at, kind: "saved", revision: r.revision });
  // 이력이 있으면 마지막 리비전 줄이 같은 말을 하므로 겹쳐 적지 않는다
  if (!revisions.length && lastSpecChange) events.push({ at: lastSpecChange, kind: "edited" });
  if (since) {
    events.push(deployedRevision ? { at: since, kind: "deployed", revision: deployedRevision } : { at: since, kind: "deployChanged" });
  }
  if (observedAt) events.push({ at: observedAt, kind: "observed" });
  return events
    .map((e) => ({ e, time: Date.parse(e.at ?? "") }))
    .filter((x) => !Number.isNaN(x.time))
    .sort((a, b) => b.time - a.time)
    .slice(0, limit)
    .map((x) => x.e);
}

/** 줄의 낱말. 리비전이 있는 갈래는 번호를 넣는다. */
function recentEventText(t: Messages, event: RecentEvent): string {
  const words = t.detail.recentCard.event;
  return "revision" in event ? words[event.kind](event.revision) : words[event.kind];
}

export function RecentCard({ events, testId = "recent-card" }: { events: RecentEvent[]; testId?: string }) {
  const t = useT();
  const text = t.detail.recentCard;
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{text.title}</CardTitle>
      </CardHeader>
      <CardContent>
        {events.length ? (
          <ol className="flex flex-col" data-testid={`${testId}-list`}>
            {events.map((e, i) => (
              <li key={`${e.at}-${i}`} className="flex gap-3 text-sm">
                {/* 점과 선으로 순서를 보인다. 줄 간격은 글 쪽 아래 여백이 맡고, 선은 다음 줄의 점 위 여백(mt-1.5)만큼 더 내려가 점에 닿는다. 마지막 줄은 이어지지 않는다 */}
                <span aria-hidden className="mt-1.5 flex flex-col items-center">
                  <span className={i === 0 ? "status-dot-ok inline-block h-2 w-2 rounded-full" : "inline-block h-2 w-2 rounded-full bg-dim/60"} />
                  {i < events.length - 1 ? <span className="mt-0.5 -mb-1.5 w-px flex-1 bg-dim/40" /> : null}
                </span>
                <span className={cn("flex flex-1 flex-wrap items-baseline justify-between gap-x-3", i < events.length - 1 && "pb-2.5")}>
                  <span>{recentEventText(t, e)}</span>
                  <span className="text-xs text-muted">{formatDate(t, e.at)}</span>
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-sm text-muted">{text.empty}</p>
        )}
      </CardContent>
    </Card>
  );
}
