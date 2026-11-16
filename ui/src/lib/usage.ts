import type { Messages } from "@/i18n";

/** 관측 화면(Agent 지표 탭, OTLP 내보내기 상태)이 다시 읽는 간격. 서버가 지표를 기억하는 시간(15초)과 같다. */
export const TELEMETRY_POLL_MS = 15_000;

/** 달러. 작은 값이 0 으로 보이지 않게 유효 숫자를 넉넉히 둔다. */
export function formatUsd(t: Messages, value: number): string {
  return value.toLocaleString(t.locale, { style: "currency", currency: "USD", maximumSignificantDigits: 6 });
}

/**
 * LLM 사용량 한 줄. 스펙 다듬기(D25)와 오버라이드 제안(D37) 화면이 같이 쓴다.
 *
 * 서버가 벤더 응답을 `input_tokens`·`output_tokens` 둘로 줄여서 준다
 * (`services/llm_providers._usage`). 그런데 job 워커는 들어온 키를 그대로 더하므로
 * (`services/spec_generator/worker.push_iteration`), 언젠가 `total_tokens` 같은 키가 하나 늘면
 * "값을 다 더하는" 셈법은 같은 토큰을 두 번 센다. 그래서 둘만 이름으로 집어 더한다. 서버가 턴마다
 * 세는 방식(`services/assistant/loop._tokens`)과 같다.
 */
export function tokensOf(usage: { input_tokens?: number; output_tokens?: number } | undefined | null): number {
  if (!usage) return 0;
  return (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
}
