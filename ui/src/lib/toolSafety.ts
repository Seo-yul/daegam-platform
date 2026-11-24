/**
 * 캔버스가 보이는 바깥 내용 표시와 그 뒤의 승인 규칙 (P43 2·4·8절). 플랫폼의 `services/agent_tool_safety.py` 와
 * `services/tool_effects.py` 와 같은 규칙이고 상수는 platform/tests/test_tool_safety_parity.py 가 맞댄다.
 *
 * - **백엔드 종류.** 검사의 `tool_origins` 다. `internal` 은 우리 mcp-runtime, `platform` 은 웹 Tool, `image`·`remote`·`entry` 는
 *   서버가 읽기 전용 여부를 알려 주지 않는 Tool 이다. 종류를 모르는 이름은 없다.
 * - **바깥 표시.** 웹 Tool 과 종류를 모르는 이름은 늘 바깥이다. 그 밖은 `internal` 이 아니면 처음부터 바깥이고 tools 노드의
 *   `outside` 가 Tool 마다 기본값을 바꾼다.
 * - **읽기 전용 표시.** 캔버스의 표시는 `image`·`remote`·`entry` Tool 가운데 서버나 허브 항목의 표시가 서지 않는 Tool 에만 효과가 있다.
 *   검사의 `tool_marked` 가 그런 표시가 서는 이름이다(우리 mcp-runtime 의 Tool 과 규칙이 있는 image 항목의 Tool, P52 4절).
 * - **memory 노드.** 메인 에이전트에 memory 노드가 없으면 승인을 받을 수 없어 바깥 내용이 들어온 대화의 쓰기는 거절된다.
 */
import type { AgentValidateResponse, ToolEffect } from "@/api/types";
import { mainAgentNode } from "@/lib/agentGraph";

/** Tool 을 내는 백엔드의 종류 (서버 `ToolOrigin`). */
export type ToolOrigin = NonNullable<AgentValidateResponse["tool_origins"]>[string][string];

/** 우리 mcp-runtime 의 Tool 이다. 결과는 처음부터 바깥 내용이 아니다. */
export const ORIGIN_INTERNAL: ToolOrigin = "internal";
/** 서버가 읽기 전용 여부를 알려 주지 않는 백엔드 종류다. 결과는 처음부터 바깥 내용이고 캔버스의 읽기 전용 표시가 효과를 낸다. */
export const THIRD_PARTY_ORIGINS: ReadonlySet<ToolOrigin> = new Set<ToolOrigin>(["image", "remote", "entry"]);
/** code mode Gateway 의 execute_tool_script 다 (P56 6.5절). 결과는 늘 바깥 내용이고 캔버스의 어느 표시도 효과가 없다. */
export const ORIGIN_SCRIPT: ToolOrigin = "script";
/** 게시한 Agent 를 부르는 ask_* 다 (P46 6.6절). 늘 쓰기이고 결과는 늘 바깥 내용이며 캔버스의 어느 표시도 효과가 없다. */
export const ORIGIN_AGENT: ToolOrigin = "agent";

/**
 * 캔버스의 읽기 전용 표시가 효과를 내는 Tool 인가. 서버가 읽기 전용 여부를 알려 주지 않는 Tool 가운데 서버나 허브 항목의 표시가 서지 않는
 * Tool 만이다. marked 는 검사의 `tool_marked` 에 그 이름이 있는가다(플랫폼 `SourceTools.canvas_mark_counts`).
 */
export function canvasMarkApplies(origin: ToolOrigin | undefined, marked = false): boolean {
  return origin !== undefined && THIRD_PARTY_ORIGINS.has(origin) && !marked;
}

/** 바깥 표시를 바꿀 수 없는 Tool 인가. 웹 Tool, 백엔드 종류를 모르는 이름, code mode 스크립트와 게시한 Agent 를 부르는 Tool 은 늘 바깥이다. */
export function outsideFixed(origin: ToolOrigin | undefined, effect: ToolEffect | undefined): boolean {
  return origin === undefined || origin === ORIGIN_SCRIPT || origin === ORIGIN_AGENT || effect === "web";
}

/** 그 Tool 의 결과가 바깥 내용인가. overrides 는 tools 노드의 `outside` 다. */
export function isOutside(tool: string, origin: ToolOrigin | undefined, effect: ToolEffect | undefined, overrides: Record<string, boolean> | undefined): boolean {
  if (outsideFixed(origin, effect)) return true;
  return overrides?.[tool] ?? origin !== ORIGIN_INTERNAL;
}

/** 바깥 표시를 바꾼 tools 노드의 `outside` 다. 백엔드 종류의 기본값과 같아지면 그 키를 지운다. */
export function withOutside(overrides: Record<string, boolean> | undefined, tool: string, origin: ToolOrigin | undefined, on: boolean): Record<string, boolean> {
  const next = { ...(overrides ?? {}) };
  if (on === (origin !== ORIGIN_INTERNAL)) delete next[tool];
  else next[tool] = on;
  return next;
}

/** 메인 에이전트에 memory 노드가 붙었는가 (플랫폼 `analyze_structure` 의 `main_has_memory`). */
export function mainHasMemory(nodes: readonly { id: string; type?: string; data?: unknown }[], edges: readonly { source: string; data?: { kind?: string } }[]): boolean {
  const main = mainAgentNode(nodes);
  return main ? edges.some((e) => e.source === main.id && e.data?.kind === "memory") : false;
}
