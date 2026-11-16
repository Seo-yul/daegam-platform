/** 감사 기록 (/audit 의 Platform API 와 MCP proxy 두 탭). 필터 칸 이름은 두 언어 모두 영어다(D36). */
export const audit = {
  /** 필터 폼의 두 단추. 적용해야 쿼리가 나간다. */
  apply: "적용",
  clear: "지우기",
  /** 서버가 최신순으로 limit 만큼 잘라 준 표 아래의 한 줄과 더 받는 단추. */
  truncated: (limit: number): string => `최신 ${limit}건만 보이고 있습니다. 더 오래된 기록은 Since·Until 로 기간을 옮기거나 Limit 을 늘려 주세요.`,
  more: (next: number): string => `${next}건까지 보기`,
  /** via 칸의 청지기 세션 링크와 "청지기가 한 것만" 필터 (D31). */
  openSession: "청지기 패널에서 이 세션을 엽니다.",
  assistantOnly: "청지기가 한 것만",
  assistantOnlyTitle: "via 가 assistant/ 로 시작하는 행만 봅니다.",
  /** 두 표의 머리. Tool 열은 두 언어 모두 Tool 이다. */
  columns: {
    time: "시각",
    subject: "주체",
    via: "경유",
    tenant: "테넌트",
    action: "동작",
    resource: "리소스",
    request: "요청",
    result: "결과",
    requestId: "요청 ID",
    type: "종류",
    component: "구성요소",
    /** Gateway(vMCP) 가 부른 백엔드, 곧 Entry 이름 (P50 4절). 서버 하나로 들어온 호출은 비어 있다. */
    backend: "백엔드",
    auditId: "감사 ID",
  },
  empty: "조건에 맞는 기록이 없습니다. 기간을 넓히거나 필터를 지워 주세요.",
  mcpEmpty: "조건에 맞는 호출 기록이 없습니다. 기간을 넓히거나 필터를 지워 주세요.",
  /** 감사 화면의 두 탭. */
  tabs: {
    api: "Platform API",
    mcp: "MCP proxy",
  },
  /** 감사 화면의 제목과 걸린 시간. */
  page: {
    title: "Audit log",
    duration: (ms: number): string => `${ms}ms`,
  },
};
