/** 사용량과 비용 (/usage, /admin/usage) 과 Platform Settings 의 가격표 카드 (P37). 화면 이름은 두 언어 모두 영어다(D36). */
import type { SourceKind } from "@/api/types";

export const usage = {
  title: "Usage",
  platformTitle: "Platform Usage",
  subtitle: "LLM 호출과 웹 검색에 쓴 토큰과 비용입니다. 한 시간 단위로 집계한 값이라 최근 호출은 최대 2분 늦게 반영됩니다.",
  filters: {
    since: "Since",
    until: "Until",
    group: "Group by",
    tenant: "Tenant",
    apply: "적용",
  },
  /** 묶는 기준. */
  groups: {
    day: "날짜",
    agent: "Agent",
    source: "호출한 곳",
    user: "사용자",
    model: "모델",
    tenant: "테넌트",
    route: "경로",
  },
  /** 원장 행의 호출한 곳 종류. */
  sourceKinds: {
    agent: "Agent",
    assistant: "청지기",
    spec_generator: "Spec Generator",
    knowledge_indexer: "Knowledge 인덱서",
    gateway_instance: "Gateway",
  } satisfies Record<SourceKind, string>,
  totals: {
    cost: "비용",
    calls: "호출",
    tokens: "토큰",
    unpriced: "가격 규칙이 없는 호출",
  },
  columns: {
    name: "이름",
    calls: "호출",
    errors: "오류",
    input: "입력 토큰",
    output: "출력 토큰",
    cacheRead: "캐시 읽기 토큰",
    cost: "비용(USD)",
  },
  /** 사람을 잇지 못한 호출의 줄. 게이트웨이 기록이나 웹 검색처럼 사람 정보가 없는 호출이다. */
  unjoined: "사용자 정보 없음",
  other: "기타",
  download: "CSV 내려받기",
  csvName: (group: string): string => `usage-${group}.csv`,
  empty: "이 기간에 기록된 호출이 없습니다.",
  /** 줄이 많아 서버가 비용이 큰 줄만 보냈을 때. */
  truncated: (n: string): string => `비용이 큰 순서로 ${n}개 항목만 표시합니다.`,
  chart: {
    label: "날짜별 비용",
    empty: "그래프로 표시할 비용이 없습니다.",
  },
  health: {
    /** 0 이어야 하는 원장 건강 수의 이름. */
    names: {
      gateway_missing: "받지 못한 게이트웨이 기록",
      writer_conflicts: "기록 주체 충돌",
      gateway_extra_records: "중복된 게이트웨이 기록",
      join_mismatch: "테넌트가 맞지 않는 연결 정보",
    },
    warning: (counts: string): string => `원장에서 확인이 필요한 항목이 있습니다(${counts}). 정상이면 모두 0입니다.`,
    oldAgents: (n: number): string => `이전 런타임 이미지로 실행 중인 Agent ${n}개는 호출을 원장에 기록하지 못합니다. 다시 배포해 주세요.`,
  },
  unpriced: {
    title: "가격 규칙이 없는 모델",
    hint: "Platform Settings 의 가격표에 규칙을 추가하면 이후 호출부터 비용이 계산됩니다. 지난 호출의 비용은 다시 계산으로 반영합니다.",
  },
  pricing: {
    title: "가격표",
    hint: "금액은 백만 토큰당 달러(USD)입니다. 캐시 칸을 비우면 입력 가격으로 계산합니다. 비용은 호출이 기록될 때 계산되므로 규칙을 고치면 이후 호출부터 적용됩니다.",
    add: "규칙 추가",
    empty: "관리자 규칙이 없습니다. 기본 가격만 적용합니다.",
    copy: "복사해서 고치기",
    defaultsTitle: "기본 가격",
    defaultsHint: "공식 가격 페이지에서 가져온 값입니다. 가격 페이지는 예고 없이 바뀔 수 있으니 확인 날짜를 참고해 주세요. 같은 모델에 관리자 규칙이 있으면 관리자 규칙이 우선합니다.",
    /** 규칙의 호출 종류. */
    kinds: {
      openai: "OpenAI",
      anthropic: "Anthropic",
      stub: "stub",
      web_search: "웹 검색",
    },
    columns: {
      kind: "종류",
      provider: "Provider",
      model: "모델",
      input: "입력",
      output: "출력",
      cacheRead: "캐시 읽기",
      cacheWrite: "캐시 쓰기",
      request: "요청당",
      effectiveFrom: "적용 시작일",
      note: "메모",
      checkedAt: "확인일",
    },
    reprice: {
      open: "다시 계산",
      title: "비용 다시 계산",
      hint: "선택한 기간(최대 92일)의 호출 비용을 현재 가격표로 다시 계산합니다. 5,000건씩 나눠 처리하므로 몇 분 걸릴 수 있습니다.",
      since: "Since",
      until: "Until",
      start: "시작",
      progress: (done: string, total: string, eta: string | null): string => `${total}건 중 ${done}건을 다시 계산했습니다.${eta ? ` 약 ${eta} 남았습니다.` : ""}`,
      done: (n: string): string => `${n}건을 다시 계산했습니다.`,
    },
  },
};
