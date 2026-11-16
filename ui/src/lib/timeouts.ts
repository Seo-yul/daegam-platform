/**
 * 백엔드 호출 타임아웃(초)의 범위 (P50 7절). 서버의 `TimeoutSeconds` 와 같다(platform/daegam/store/base.py).
 * MCP Server 폼과 Gateway 폼이 같이 쓴다.
 */
export const TIMEOUT_MIN_SECONDS = 1;
export const TIMEOUT_MAX_SECONDS = 300;

/** 숫자 입력칸의 글을 정수 초로 읽는다. 비우거나 정수가 아니면 undefined 라 서버나 배포가 기본값을 정한다. */
export function timeoutFromInput(value: string): number | undefined {
  const trimmed = value.trim();
  return /^\d+$/.test(trimmed) ? Number(trimmed) : undefined;
}

/** Agent 런타임이 Tool 호출에 거는 기본 타임아웃(초). agent-runtime/runtime/mcp.py 의 DEFAULT_TOOL_TIMEOUT_SECONDS 와 같다. */
export const AGENT_TOOL_TIMEOUT_SECONDS = 60;
