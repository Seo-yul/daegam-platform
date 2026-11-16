/**
 * 배포 상태 목록의 점 요약 줄 (D44 넷째). MCP Server·Gateway·Agent 목록이 같이 쓴다.
 *
 * 다섯 칸(전체·Ready·Progressing·Failed·미배포)을 세 목록이 각자 적고 있었고, 그러다 Agent 쪽에서 Failed
 * 가 한 번 빠졌다. 실패한 Agent 가 요약에서 안 보였다. 다섯 칸은 여기서만 적고 목록은 제 칸만 뒤에 붙인다.
 */
import { CountLine, type CountItem } from "@/components/ListControls";
import { useT } from "@/i18n";
import type { StateCounts } from "@/lib/deployment";

/** `prefix` 는 testid 접두어다. "ms" 면 줄이 ms-counts, 칸이 ms-count-ready 다. */
export function StateCountLine({ counts, prefix, extra = [] }: { counts: StateCounts; prefix: string; extra?: CountItem[] }) {
  const t = useT();
  return (
    <CountLine
      testId={`${prefix}-counts`}
      items={[
        { label: t.detail.stateCountLine.total, value: counts.total, testId: `${prefix}-count-total` },
        { label: "Ready", value: counts.ready, dot: "ok", testId: `${prefix}-count-ready` },
        { label: "Progressing", value: counts.progressing, dot: "warn", testId: `${prefix}-count-progressing` },
        { label: "Failed", value: counts.failed, dot: "err", testId: `${prefix}-count-failed` },
        { label: t.resources.state.draft, value: counts.draft, dot: "muted", testId: `${prefix}-count-draft` },
        ...extra,
      ]}
    />
  );
}
