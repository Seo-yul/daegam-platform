/**
 * 메트릭 탭의 껍데기. Agent 와 MCP Server·Gateway 의 메트릭 탭이 같이 쓴다. 기간 고르개 하나와, 고른 기간과 브라우저 시간대로
 * 읽고 pollMs 마다 다시 읽는 요청 하나다. 기간을 바꾸면 앞 기간의 수를 보이지 않고(keepStale false) 새로 읽는다.
 *
 * 시험 id 는 접두사 하나에서 나온다: `${testId}-tab`, `-window`, `-error`. 요청 열쇠는 `[...queryKey, "metrics", 기간, 시간대]` 다.
 */
import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { WindowSelect } from "@/components/metrics/WindowSelect";
import { queryGate } from "@/components/QueryGate";
import { browserZone } from "@/lib/utils";

export function WindowedMetrics<W extends string, D>({
  windows,
  initial,
  names,
  queryKey,
  load,
  testId,
  pollMs,
  children,
}: {
  windows: readonly W[];
  initial: W;
  names: Readonly<Record<W, string>>;
  queryKey: readonly unknown[];
  load: (period: W, tz: string) => Promise<D>;
  testId: string;
  pollMs: number;
  /** 받은 답을 그린다. 로딩과 오류는 껍데기가 그린다. */
  children: (data: D) => ReactNode;
}) {
  const [period, setPeriod] = useState<W>(initial);
  const tz = browserZone();
  const q = useQuery({
    queryKey: [...queryKey, "metrics", period, tz],
    queryFn: () => load(period, tz),
    refetchInterval: pollMs,
  });
  const gate = queryGate(q, { testId: `${testId}-error`, keepStale: false });
  return (
    <div className="flex flex-col gap-4" data-testid={`${testId}-tab`}>
      <WindowSelect windows={windows} names={names} value={period} onChange={setPeriod} testId={`${testId}-window`} />
      {gate ?? (q.data !== undefined ? children(q.data) : null)}
    </div>
  );
}
