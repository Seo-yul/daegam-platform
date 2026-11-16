/**
 * 호출 카드. 합계 줄 아래에 이름마다 호출·오류·p95 표가 선다. Agent 의 모델 호출과 Tool 실행, MCP Server·Gateway 의 Tool 별·
 * 백엔드별 호출이 쓴다. 오류는 성공이 아닌 모든 갈래이고 그 수는 서버가 센다.
 */
import type { ReactNode } from "react";

import { Facts } from "@/components/metrics/Facts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyRow, Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { useT } from "@/i18n";
import { metricNumber } from "@/lib/utils";

export interface CallRow {
  /** 줄의 React 열쇠. 없으면 이름이다. 보이는 이름이 겹칠 수 있는 표(백엔드별 호출)가 준다. */
  id?: string;
  /** 줄의 이름. null 이면 "-" 다. */
  name: string | null;
  /** 이름 칸에 이름 대신 서는 것. "그 밖의 Tool" 처럼 데이터가 아닌 말이다. */
  label?: ReactNode;
  calls: number;
  errors: number;
  p95_ms: number | null;
}

export function CallsCard({
  title,
  totals,
  extra = [],
  nameLabel,
  rows,
  testId,
}: {
  title: string;
  totals: { calls: number; errors: number; p95_ms: number | null };
  /** 합계 아래에 붙는 수 (모델 호출의 재시도·예비 모델 전환). */
  extra?: [string, number][];
  nameLabel: string;
  rows: CallRow[];
  testId: string;
}) {
  const t = useT();
  const text = t.shared.callsCard;
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <span className="text-xs text-muted" data-testid={`${testId}-totals`}>
          {text.callTotals(metricNumber(t, totals.calls), metricNumber(t, totals.errors), metricNumber(t, totals.p95_ms))}
        </span>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {extra.length ? <Facts rows={extra.map(([label, n]) => ({ label, value: metricNumber(t, n) }))} /> : null}
        <Table>
          <THead>
            <tr>
              <TH>{nameLabel}</TH>
              <TH>{text.calls}</TH>
              <TH>{text.errors}</TH>
              <TH>{text.p95}</TH>
            </tr>
          </THead>
          <TBody>
            {rows.length ? (
              rows.map((r) => (
                <TR key={r.id ?? r.name ?? "-"} data-testid={`${testId}-row`}>
                  <TD className={r.label ? "text-xs" : "font-mono text-xs"}>{r.label ?? r.name ?? "-"}</TD>
                  <TD>{metricNumber(t, r.calls)}</TD>
                  <TD>{metricNumber(t, r.errors)}</TD>
                  <TD>{metricNumber(t, r.p95_ms)}</TD>
                </TR>
              ))
            ) : (
              <EmptyRow colSpan={4}>{text.noCalls}</EmptyRow>
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  );
}
