/**
 * 답변 피드백의 상한과 이유의 차례 (P36 6절). 채팅의 엄지(components/chat/FeedbackButtons.tsx)와 Agent 상세의 피드백 탭이 같은
 * 값을 쓴다. 평가와 이유의 이름은 사전(chat.feedback)에 있고, 그 표가 생성된 스키마의 값을 `Record` 로 받아 서버가 이유를 더하면
 * 컴파일이 그 표를 가리킨다. 의견 상한은 플랫폼 `store/agent_feedback.py` 의 `MAX_COMMENT_CHARS`, 두 날 수는
 * `services/agent_feedback.py` 의 `EXPORT_MAX_DAYS`·`RECENT_DAYS` 와 같고 platform/tests/test_chat_parity.py 가 맞댄다. 내보내기
 * 구간은 오류 사전도 읽어 lib/limits.ts 에 둔다.
 */
import type { FeedbackTag } from "@/api/types";

export { FEEDBACK_EXPORT_MAX_DAYS } from "@/lib/limits";

export const FEEDBACK_COMMENT_MAX_CHARS = 2000;
/** 피드백 탭 머리의 평가별 수가 세는 날 수다. */
export const FEEDBACK_RECENT_DAYS = 7;
/**
 * 좋아요나 아쉬워요가 마지막으로 바뀐 뒤 남는 날 수의 플랫폼 기본값이다(`DAEGAM_FEEDBACK_RETENTION_DAYS`, 서버
 * `config/__init__.py` 의 `feedback_retention_days`). 평가 사례로 쓰는 동의의 안내(P45 8절)는 서버가 세션 평가와 함께 보낸
 * 설치의 값(`retention_days`)을 적고, 그 값이 없을 때만 이것을 적는다. 그 값을 싣지 않는 옛 플랫폼이거나 세션 평가를 아직
 * 읽지 못한 때다.
 */
export const FEEDBACK_RETENTION_DEFAULT_DAYS = 180;

/**
 * 아쉬운 이유의 차례다. 칩과 탭의 거르개·표가 이 차례로 보인다. `satisfies Record<FeedbackTag, number>` 라 서버가 이유를
 * 더하면 사전의 표와 함께 이 표도 컴파일이 가리킨다.
 */
const FEEDBACK_TAG_ORDER = {
  inaccurate: 0,
  unhelpful: 1,
  incomplete: 2,
  unsafe: 3,
  other: 4,
} as const satisfies Record<FeedbackTag, number>;

export const FEEDBACK_TAGS: readonly FeedbackTag[] = (Object.keys(FEEDBACK_TAG_ORDER) as FeedbackTag[]).sort(
  (a, b) => FEEDBACK_TAG_ORDER[a] - FEEDBACK_TAG_ORDER[b],
);
