import type { McpMetricsWindow, McpOutcomes } from "@/api/types";

/**
 * MCP Server·Gateway 상세의 메트릭 탭 (components/mcp/McpMetricsTab, P51 5절). 탭 이름은 ui.tabs.standard.metrics, 기간
 * 고르개의 이름은 detail.windowSelect.label, 호출 표의 열과 기간에 호출이 없을 때의 문장은 shared.callsCard 다.
 */
export const mcpMetrics = {
  window: { "24h": "최근 24시간", "7d": "최근 7일", "30d": "최근 30일" } satisfies Record<McpMetricsWindow, string>,
  calls: "Tool 호출",
  /** 성공이 아닌 모든 갈래. */
  errors: "오류",
  /** 결과 갈래의 이름. 키는 서버의 칸 이름(ToolHive 의 outcome)이고 보이는 차례는 화면이 정한다. */
  outcome: {
    success: "성공",
    application_error: "오류 응답",
    denied: "거부",
    failure: "잘못된 요청",
    error: "서버 오류",
    unknown: "알 수 없음",
  } satisfies Record<keyof McpOutcomes, string>,
  duration: "지연 p50 / p95 / 최대 (ms)",
  approx: "p50 과 p95 는 시간 구간으로 어림한 값입니다.",
  /** Tool 호출 밖의 호출. 둘 중 하나라도 0 이 아닐 때만 선다. */
  otherCalls: (reads: number, prompts: number): string => `리소스 읽기 ${reads}건 · 프롬프트 가져오기 ${prompts}건`,
  rolledUpAt: "마지막 집계 시각",
  /** 막대 그림의 접근 이름과, 그릴 Tool 호출이 없을 때의 문장. */
  chartLabel: "결과별 Tool 호출",
  chartEmpty: "그래프로 표시할 호출이 없습니다.",
  byTool: "Tool 별 호출",
  /** Gateway 에만 있다. */
  byBackend: "백엔드별 호출",
  /** 광고하지 않는 이름과 접힌 나머지를 모은 줄. */
  otherTools: "그 밖의 Tool",
  /** 배포 스냅샷에 더는 없는 Entry. Entry 이름 아래에 붙는다. */
  removedBackend: "지금 배포에 없는 백엔드",
  /** vMCP 가 백엔드 없이 남긴 호출(가진 적 없는 Tool). */
  noBackend: "백엔드를 정하지 못한 호출",
  /** 숫자가 보지 못하는 것 (P51 6절). */
  notes: {
    toolErrors: "Tool 이 오류 결과(isError)로 답한 호출과 스트리밍 답 안의 오류는 성공으로 셉니다. Agent 가 부른 Tool 의 실패는 그 Agent 의 메트릭 탭에서 확인합니다.",
    collector: "호출 기록은 파드 로그에서 읽습니다. 로그가 회전되기 전에 읽지 못한 기록은 빠지고, 수는 몇 분 늦게 반영됩니다.",
    hops: "Gateway 를 거친 호출은 거쳐 간 Gateway 마다, 그리고 백엔드 MCP Server 에서 한 번씩 셉니다. 걸린 시간은 각 단계에서 잰 값입니다.",
  },
  /** 숫자 대신 서는 안내. */
  auditOff: "이 클러스터의 ToolHive 버전은 Gateway 의 호출 기록을 남기지 않아 메트릭을 표시할 수 없습니다.",
  collectorOff: "호출 기록 수집이 꺼져 있어 메트릭을 표시할 수 없습니다.",
  /** 숫자 곁에 서는 안내. */
  auditPending: "이 Gateway 는 호출 기록을 남기도록 다시 적용되기를 기다리고 있습니다. 그 전의 호출은 기록되지 않습니다.",
  /** time 은 formatDate 로 적은 시각이다. */
  countingFrom: (time: string): string => `${time} 이전의 호출은 아직 집계하고 있습니다.`,
};
