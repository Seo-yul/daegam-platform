/**
 * 배포된 런타임 이미지가 아는 기능·능력 이름 (D65). 상태의 `runtime.features` 는 그 파드의 init 이 `agent.json` 을 받을 때
 * 스스로 알린 이름 가운데 플랫폼이 아는 것이고, 아직 알리지 않았으면(첫 배포, 옛 이미지) null 이다. 런타임 능력에 기대는
 * 단추는 이름이 있을 때만 그 API 를 부르고 없으면 옛 길로 물러선다. 플랫폼 프록시도 같은 판정으로 409 runtime_feature_missing
 * 을 내므로 화면이 먼저 가르면 그 오류를 볼 일이 없다.
 *
 * 채팅 화면은 이름과 함께 상태의 `chat`(런타임 `/metrics` 의 받는 첨부, D61)을 `useChatFeatures` 하나로 읽는다. `chat` 은
 * 파드가 준비되기 전과 옛 런타임에서 null 이고 그때는 첨부를 받지 않는 것으로 본다. 도는 리비전의 사실 둘(장기 기억을 쓰는가,
 * 인용 원문을 가리는가, P36)도 상태에서 함께 읽는다. 저장만 하고 재배포하지 않은 정의가 아니라 도는 리비전의 값이다.
 */
import { useMemo } from "react";

import type { AgentChatLimits, AgentStatus } from "@/api/types";

/** 진행 중 턴을 멈추는 `POST /sessions/{sid}/cancel` (agent-runtime/runtime/features.py 의 CAPABILITIES). */
export const RUNTIME_CANCEL = "cancel";
/** 세션 제목과 소유자의 이름 바꾸기·지우기·제목 검색 (P36). 없으면 목록이 지금처럼 id 앞자리를 쓰고 메뉴와 검색칸이 없다. */
export const RUNTIME_SESSION_ADMIN = "session_admin";
/** 마지막 턴의 다시 생성과 메시지 수정 (P36). 없으면 채팅에 두 단추가 없다. */
export const RUNTIME_REGENERATE = "regenerate";
/** 검색 결과의 인용 번호와 출처 (P36). 없으면 번호 칩의 작은 창에 원문 다운로드 단추가 없다. */
export const RUNTIME_CITATIONS = "citations";
/** 호출자 자신의 장기 기억 파일 목록·읽기·하나 삭제 (P36). 없으면 홈 챗에 "이 에이전트가 기억하는 것" 단추가 없다. */
export const RUNTIME_MEMORY_FILES = "memory_files";
/** `/chat` 본문의 첨부(문서 글과 이미지)와 `done.attachments_received` (P36). 옛 런타임은 첨부를 조용히 버리므로 없으면 첨부를 싣지 않는다. */
export const RUNTIME_ATTACHMENTS = "attachments";
/** 세션 파일의 목록과 다운로드 (P36). 없으면 채팅 위의 "파일" 단추와 도구 카드의 다운로드 단추가 없다. */
export const RUNTIME_SESSION_FILES = "session_files";
/** 바깥 내용 표시, 그 뒤의 승인, 기억한 승인 선택 (P43). 없으면 승인 카드에 기억하기와 이유가 없다. */
export const RUNTIME_TOOL_SAFETY = "tool_safety";

/** 런타임 능력 판정과 받는 첨부, 도는 리비전의 사실이 읽는 상태의 칸이다. */
type RuntimeFacts = Pick<AgentStatus, "runtime" | "chat"> & Partial<Pick<AgentStatus, "long_term_memory" | "sources_masked">>;

export function hasRuntimeFeature(status: Pick<AgentStatus, "runtime"> | null | undefined, name: string): boolean {
  return Boolean(status?.runtime?.features.includes(name));
}

/** 채팅 화면이 아는 런타임의 사실. 능력 이름과 받는 첨부다. 첨부 세 칸(documents·images·limits)은 입력칸의 첨부(lib/attachments.ts)가 읽는다. */
export interface ChatFeatures {
  /** 런타임 능력 이름이 이 에이전트의 기록에 있는가. */
  has: (name: string) => boolean;
  /** 문서 첨부를 받는가. 메인 에이전트의 파일 도구가 꺼졌으면 거짓이다. */
  documents: boolean;
  /** 이미지 첨부를 받는가. 메인 모델 프로필이 이미지를 막으면 거짓이다. */
  images: boolean;
  /** 한 턴·세션의 첨부 상한. 런타임이 알리지 않았으면 null 이다. */
  limits: AgentChatLimits | null;
  /** 첨부가 웹 fetch 를 허용 목록으로 좁히는가(P39b 14절 2부). 입력칸이 파일을 붙일 때 알린다. */
  attachmentsLimitFetch: boolean;
  /** 도는 리비전이 장기 기억을 쓰는가. 홈 챗의 "이 에이전트가 기억하는 것" 단추가 `memory_files` 와 함께 본다. */
  longTermMemory: boolean;
  /** 도는 리비전이 도구 결과나 답을 가려 인용 원문을 열지 않는가. 인용 창이 원문 다운로드 대신 안내를 보인다. */
  sourcesMasked: boolean;
}

export function chatFeatures(status: RuntimeFacts | null | undefined): ChatFeatures {
  const chat = status?.chat ?? null;
  return {
    has: (name) => hasRuntimeFeature(status, name),
    documents: chat?.documents ?? false,
    images: chat?.images ?? false,
    limits: chat?.limits ?? null,
    attachmentsLimitFetch: chat?.attachments_limit_fetch ?? false,
    longTermMemory: status?.long_term_memory ?? false,
    sourcesMasked: status?.sources_masked ?? false,
  };
}

/** 상태의 `runtime`·`chat` 과 도는 리비전의 사실이 바뀔 때만 새로 만든다. 상태를 다시 읽어도 그 칸들이 같으면 같은 값이다. */
export function useChatFeatures(status: RuntimeFacts | null | undefined): ChatFeatures {
  const runtime = status?.runtime;
  const chat = status?.chat;
  const longTerm = status?.long_term_memory;
  const masked = status?.sources_masked;
  return useMemo(() => chatFeatures({ runtime, chat, long_term_memory: longTerm, sources_masked: masked }), [runtime, chat, longTerm, masked]);
}
