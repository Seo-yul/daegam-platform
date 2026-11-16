/**
 * 청지기 패널 (components/assistant/AssistantPanel.tsx). 세션 고르개와 새 대화 단추는 shared.chatPanel 과
 * shell.sessionList 의 말을, 닫기 단추의 이름은 shell.assistantToggle.close 를 쓴다.
 */
export const assistant = {
  /** 머리줄의 따라가기 토글. */
  follow: "따라가기",
  followTitle: "청지기가 여는 화면으로 함께 이동합니다.",
  closeTitle: "패널을 닫습니다.",
  deleteSession: "대화 삭제",
  loading: "대화를 불러오는 중입니다…",
  /** 다른 탭이나 파드에서 시작한 턴이 도는 동안의 줄. 배지 뒤에 seconds 초마다 다시 읽는다는 문장이 온다. */
  running: "진행 중",
  runningElsewhere: (seconds: number): string => `다른 곳에서 시작한 턴이 돌고 있습니다. ${seconds}초마다 확인합니다.`,
  empty:
    '무엇을 만들까요? 예를 들어 "petstore 스펙으로 MCP Server 를 만들어 주세요" 라고 하면 Backend API 부터 배포까지 대신 진행합니다. 배포·삭제·설정 변경은 승인을 받은 뒤에 합니다.',
  /** 입력칸의 자리표시 글. 결정을 기다리는 카드가 있으면 pendingPlaceholder 이고, hint 는 chat.inputHint 다. */
  pendingPlaceholder: "위 카드에서 결정합니다. 여기에 쓰면 보류 중인 요청은 취소됩니다.",
  placeholder: (hint: string): string => `무엇을 도와드릴까요? ${hint}`,
  input: "청지기에게 보낼 메시지",
};
