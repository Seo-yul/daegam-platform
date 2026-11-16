/**
 * 플랫폼 비서(D31, phase-14) 의 화면 쪽 규칙. AssistantPanel·Layout·Audit 화면이 같이 쓴다.
 * 주제마다 파일이 하나이고 여기서 다시 내보낸다. import 는 `@/lib/assistant` 그대로다.
 *
 *  - store.ts: 패널 열림·"따라가기"·테넌트별 현재 세션
 *  - config.ts: GET /assistant/config 와 내 세션 목록
 *  - context.ts: 현재 경로 → 턴의 context{route, resource?}
 *  - decisions.ts: 승인 결정 본문과 결정 id 대응
 *  - proposal.ts: Agent 정의 제안의 링크·문장·노드 표시 (P48)
 *  - relay.ts: 패널 밖(캔버스)에서 연 결정 스트림을 패널에 넘기고 그 이벤트를 듣는 다리 (P48)
 *  - invalidate.ts: 쓰기 도구 결과 → 쿼리 무효화
 *  - transcript.ts: 저장된 전사 → 채팅 항목
 */
export * from "./store";
export * from "./config";
export * from "./context";
export * from "./decisions";
export * from "./proposal";
export * from "./relay";
export * from "./invalidate";
export * from "./transcript";
