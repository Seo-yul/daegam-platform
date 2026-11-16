/** 이름과 값의 두 칸 목록. 메트릭 탭의 카드 안의 짧은 사실들이 쓴다(Agent·MCP Server·Gateway). */
import type { ReactNode } from "react";

export interface Fact {
  label: ReactNode;
  value: ReactNode;
  testId?: string;
}

export function Facts({ rows, testId }: { rows: Fact[]; testId?: string }) {
  return (
    <dl className="grid grid-cols-[10rem_1fr] gap-y-1 text-sm" data-testid={testId}>
      {rows.map((r, i) => (
        <div key={i} className="contents">
          <dt className="text-xs text-muted">{r.label}</dt>
          <dd data-testid={r.testId}>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}
