/**
 * 개요 탭의 2열 배치 (D44 셋째). 왼쪽 1.5, 오른쪽 1 이고 좁은 창에서는 한 줄로 쌓인다.
 *
 * 왼쪽은 읽는 것(주소·참조·최근 기록)이고 오른쪽은 판정하는 것(준비 상태·정의 요약)이다. MCP
 * Server·Gateway·Agent 셋이 같은 뼈대를 쓰므로 자리를 정하는 곳도 하나다.
 */
import type { ReactNode } from "react";

export function OverviewGrid({ left, right }: { left: ReactNode; right: ReactNode }) {
  return (
    <div className="grid gap-4 lg:grid-cols-[1.5fr_1fr]" data-testid="overview-grid">
      <div className="flex min-w-0 flex-col gap-4">{left}</div>
      <div className="flex min-w-0 flex-col gap-4">{right}</div>
    </div>
  );
}
