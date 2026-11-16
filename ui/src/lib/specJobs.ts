/**
 * LLM 스펙 작업(job)이 공유하는 값과 판정. 스펙 다듬기(D25)와 툴 오버라이드 제안(D37)이 같이 쓴다.
 *
 * 넷 다 서버 값을 옮긴 것이다. 두 벌로 두면 서버가 바뀔 때 한쪽만 고치게 되고, 어긋나도 화면은
 * 조용히 다른 숫자를 보인다. 상한 둘은 `services/spec_generator/refine` 과
 * `api/spec_generator` 의 JobRequest 가 정본이다.
 */
import type { SpecChange } from "@/api/types";

/** 서버가 스펙 하나에 돌리는 되먹임 반복 횟수 상한. 화면은 "n/5" 로 보인다. */
export const MAX_ITERATIONS = 5;

/** job 상태를 다시 묻는 간격. 서버가 밀어 주지 않으므로 화면이 폴링한다. */
export const POLL_MS = 1500;

/** 자유 지시 한 줄의 길이 상한. 서버의 INSTRUCTION_MAX_CHARS 와 같아야 한다. */
export const INSTRUCTION_MAX = 200;

/** 변경이 없을 때 쓰는 고정 빈 배열. 매 렌더 새 배열을 만들면 memo 가 헛돈다. */
export const EMPTY_CHANGES: SpecChange[] = [];

/** 아직 도는 중인가. 이때만 폴링을 건다. */
export function isActive(state: string | undefined): boolean {
  return state === "queued" || state === "running";
}
