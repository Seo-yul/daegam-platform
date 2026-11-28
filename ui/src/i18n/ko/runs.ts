/**
 * 자동 실행 (P41 11절). Agent 상세의 자동 실행 탭이 쓴다.
 * 상태 몇(완료·승인 대기·실패·중지·시간 초과·예산 초과·진행 중)과 단추(저장·삭제·보내기·중단), 표의 이름·만료·상태 칸은 다른
 * 영역의 말을 다시 쓴다(lib/runs.ts). 일정의 출처 값은 `schedules.title` 이다.
 */
export const runs = {
  schedules: {
    title: "일정",
    empty: "아직 일정이 없습니다. 정해진 시각에 이 Agent 를 실행하려면 일정을 추가해 주세요.",
    add: "일정 추가",
    cron: "반복 규칙(cron)",
    cronHint: "다섯 칸 cron 식입니다. 예를 들어 매일 오전 9시는 0 9 * * * 입니다.",
    timeZone: "시간대",
    message: "보낼 메시지",
    nextRun: "다음 실행",
  },
  pause: "일시 정지",
  resume: "다시 시작",
  edit: "편집",
  /** 일정이나 트리거를 지울 때 확인 대화상자의 본문. */
  deleteBody: "지우면 열린 실행이 멈추고 되살릴 수 없습니다.",
  /** 일정과 트리거의 상태. */
  state: { active: "사용 중", paused: "일시 정지됨" },
  triggers: {
    title: "웹훅 트리거",
    empty: "아직 트리거가 없습니다. 다른 시스템이 이 Agent 를 실행하게 하려면 트리거를 추가해 주세요.",
    add: "트리거 추가",
    url: "호출 주소",
    template: "메시지 틀",
    templateHint: "받은 본문은 {{payload}}, 본문의 한 값은 {{payload./경로}} 자리에 들어갑니다.",
    secretOnce: "이 서명 비밀값은 지금 한 번만 보입니다. 호출하는 시스템에 저장해 주세요.",
    rotate: "비밀값 새로 만들기",
    /** 한 번만 보이는 서명 비밀의 복사 단추가 읽는 이름. 기술 용어라 영어 그대로 둔다. */
    secretLabel: "Signing secret",
  },
  history: {
    title: "자동 실행 기록",
    empty: "아직 자동 실행 기록이 없습니다.",
    runNow: "지금 실행",
    runNowPlaceholder: "Agent 에게 보낼 메시지를 적어 주세요.",
    columns: { source: "출처", started: "시작", duration: "걸린 시간", tokens: "토큰", actsAs: "실행 이름", reason: "까닭" },
    openConversation: "대화 보기",
    actsAs: {
      person: (name: string): string => `${name} 의 이름으로`,
      agent: "Agent 의 이름으로",
    },
  },
  source: { webhook: "웹훅", manual: "직접 실행", agent: "Agent 호출" },
  /** 다른 Agent 가 Tool 로 부른 실행의 부른 쪽 (P46 6.5절). 출처 칸의 둘째 줄이다. */
  calledBy: (name: string): string => `부른 Agent: ${name}`,
  status: {
    starting: "시작 중",
    refused: "마감으로 거절됨",
    skipped: (count: number): string => `건너뜀 ${count}번`,
  },
  /** 실행이 건너뛰거나 실패하거나 멈춘 까닭. 코드와 이 표의 짝은 lib/runs.ts 의 runReasonText 다. */
  reason: {
    previousRunning: "앞 실행이 아직 끝나지 않았습니다.",
    agentRunLimit: "이 Agent 의 실행 한도에 걸렸습니다.",
    missed: "플랫폼이 멈춰 있던 동안의 실행 시각이 지났습니다.",
    runLost: "실행의 끝을 확인하지 못했습니다.",
    actsAsLostAccess: "실행 이름의 사용자가 이 Agent 를 더 쓸 수 없습니다.",
    /** oidc 모드의 긴 한도를 넘었다 (P53b 6.3절). days 는 DAEGAM_IDP_CONFIRM_SECONDS 의 날 수다. */
    actsAsUnconfirmed: (days: number): string => `실행 이름의 사용자가 ${days}일 동안 대감에 로그인하지 않았습니다.`,
    agentChanged: "다른 사람이 Agent 를 다시 배포했습니다.",
    prerequisites: "실행에 필요한 설정이 빠졌습니다.",
    inputTooLong: "메시지가 이 Agent 의 입력 한도보다 깁니다.",
    rateOrSessions: "이 Agent 의 동시 세션이나 요청 한도에 걸렸습니다.",
    dailyBudget: "이 Agent 의 하루 토큰 예산을 다 썼습니다.",
    guardrail: "입력 가드레일이 메시지를 막았습니다.",
    unavailable: "Agent 가 응답하지 않았거나 배포되어 있지 않았습니다.",
    aborted: "파드가 내려가 실행이 끝까지 가지 못했습니다.",
    accessLost: "실행 이름의 사용자가 실행 도중 권한을 잃었습니다.",
    /** 다른 Agent 가 부른 실행을 부른 쪽이 끝냈다 (P46 6.3·6.5절). */
    callerEnded: "부른 Agent 의 턴이 끝나 실행을 취소했습니다.",
    callerTimeout: "부른 Agent 가 기다리는 시간을 넘겨 실행을 취소했습니다.",
  },
  /** 일정과 트리거가 멈춘 까닭. 손으로 멈춘 것은 상태만 보인다. */
  pausedReason: {
    actsAsLostAccess: "실행 이름의 사용자가 이 Agent 를 더 쓸 수 없어 멈췄습니다. 다시 시작하면 다시 시작한 사람의 이름으로 실행합니다.",
    actsAsUnconfirmed: (days: number): string =>
      `실행 이름의 사용자가 ${days}일 동안 대감에 로그인하지 않아 멈췄습니다. 그 사용자가 로그인한 뒤 다시 시작해 주세요.`,
    agentChanged: "다른 사람이 Agent 를 다시 배포해 멈췄습니다. 바뀐 Agent 를 확인한 뒤 다시 시작해 주세요.",
    agentUndeployed: "Agent 가 배포 해제되어 멈췄습니다.",
    expired: "만료되어 멈췄습니다. 다시 시작하면 만료일이 새로 정해집니다.",
    prerequisites:
      "실행에 필요한 설정(memory 노드, 세션 예산, 쓰기 Tool 의 승인 규칙, 메시지 길이)이 맞지 않아 멈췄습니다. 고친 뒤 다시 시작해 주세요.",
  },
};
