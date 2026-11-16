/**
 * 런타임이 정한 표본(`agent-runtime/schema/fixtures/`)을 읽는다 (P36). 런타임·플랫폼 테스트와 UI 의 vitest·msw 목이 같은 파일을
 * 읽으므로 손으로 적은 목이 서버와 함께 틀리지 않는다. Node 에서 돈다(vitest, e2e 목 서버). 표본 폴더는 이 파일 자리에서 찾으므로
 * 작업 폴더가 ui/ 가 아니어도 된다. UI 이미지 빌드의 컨텍스트에는 런타임 폴더가 없어 JSON 을 import 하지 않고 돌 때 읽는다.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { AssistantTranscriptItem } from "@/api/types";
import type { CitationsEvent, DoneEvent, ErrorEvent, ModelFallbackEvent, ToolCallEvent, ToolLimitEvent, ToolResultEvent } from "@/lib/chatStream";

// `new URL("…", import.meta.url)` 꼴은 Vite 가 자산 주소(http)로 바꾸므로 이 파일의 경로에서 폴더를 셈한다
const FIXTURES_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../../agent-runtime/schema/fixtures");

export function readRuntimeFixture<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIXTURES_DIR, name), "utf8")) as T;
}

/** done·error 이벤트와 그 둘을 접은 전사의 표본이다. 런타임 test_transcript.py 가 실제 `/chat` 이 낸 칸과 견준다. */
export interface TurnFixture {
  events: { done: DoneEvent; error: ErrorEvent };
  transcript: AssistantTranscriptItem[];
}

export const TURN_FIXTURE = readRuntimeFixture<TurnFixture>("chat/turn.json");

/** 검색 턴의 tool_call·tool_result·citations 이벤트와 그것을 접은 전사의 표본이다(P36 7절). 런타임 test_knowledge·test_transcript 가 견준다. */
export interface CitationsFixture {
  events: { tool_call: ToolCallEvent; tool_result: ToolResultEvent; citations: CitationsEvent };
  transcript: AssistantTranscriptItem[];
}

export const CITATIONS_FIXTURE = readRuntimeFixture<CitationsFixture>("chat/citations.json");

/** 예비 모델이 대신 답한 턴의 model_fallback·done 이벤트와 그것을 접은 전사의 표본이다(P36 11절). done 은 구조화된 결과를 싣는다. 런타임 test_transcript 가 견준다. */
export interface FallbackFixture {
  events: { model_fallback: ModelFallbackEvent; done: DoneEvent };
  transcript: AssistantTranscriptItem[];
}

export const FALLBACK_FIXTURE = readRuntimeFixture<FallbackFixture>("chat/fallback.json");

/** 한 실행의 Tool 호출 한도에 닿은 턴의 tool_limit·done 이벤트와 전사 표본이다(T82). 서브에이전트의 한도와 메인의 한도가 따로 남는다. 런타임 test_transcript 가 견준다. */
export interface ToolLimitFixture {
  events: { tool_limit: ToolLimitEvent; subagent_tool_limit: ToolLimitEvent; done: DoneEvent };
  transcript: AssistantTranscriptItem[];
}

export const TOOL_LIMIT_FIXTURE = readRuntimeFixture<ToolLimitFixture>("chat/tool_limit.json");

/**
 * 첨부 턴의 done 과 그것을 접은 전사의 표본이다(P36 9절). 사용자 항목이 첨부의 이름과 크기를 든다. 런타임 test_transcript 가 견주고,
 * msw 목은 done 의 받은 첨부 수 칸을 여기서 읽는다.
 */
export interface AttachmentsFixture {
  events: { done: DoneEvent };
  transcript: AssistantTranscriptItem[];
}

export const ATTACHMENTS_FIXTURE = readRuntimeFixture<AttachmentsFixture>("chat/attachments.json");
