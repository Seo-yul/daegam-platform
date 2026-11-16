/**
 * 외부 경로의 엔드포인트 표 (D27, docs/guide/agents.md 7절). 런타임이 Ingress 뒤에서 직접 받는 경로이고 Agent 상세의 외부
 * 호출 카드가 그린다. 외부 경로는 모두 토큰 sub 가 소유한 세션과 그 sub 의 장기 기억만 다룬다.
 *
 * 런타임 능력에 기대는 경로는 `feature` 를 든다. 배포된 이미지가 그 이름을 알리지 않았으면 옛 런타임에는 없는 경로라
 * 카드에서 뺀다(`visibleEndpoints`). 계약은 agent-runtime/README.md 의 경로 표다.
 */
import type { AgentStatus } from "@/api/types";
import type { Messages } from "@/i18n";
import { MEMORY_FILE_MAX_BYTES, MEMORY_ROOT } from "@/lib/memories";
import {
  RUNTIME_ATTACHMENTS,
  RUNTIME_MEMORY_FILES,
  RUNTIME_REGENERATE,
  RUNTIME_SESSION_ADMIN,
  RUNTIME_SESSION_FILES,
  hasRuntimeFeature,
} from "@/lib/runtimeFeatures";
import { SESSION_FILE_MAX_BYTES } from "@/lib/sessionFiles";
import { SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitle";
import { formatBytes } from "@/lib/utils";

type EndpointMethod = "GET" | "POST" | "PATCH" | "DELETE";

/** 경로 설명의 사전 자리(agents.endpoints). */
type EndpointText = Messages["agents"]["endpoints"];

export interface ExternalEndpoint {
  method: EndpointMethod;
  path: string;
  /** 표에 적을 설명. 화면 언어의 사전에서 상한을 넣어 만든다. */
  description: (text: EndpointText) => string;
  /** 이 경로를 내는 런타임 능력 이름. 없으면 모든 런타임에 있다. */
  feature?: string;
}

const EXTERNAL_ENDPOINTS: ExternalEndpoint[] = [
  { method: "POST", path: "/chat", description: (text) => text.chat },
  { method: "POST", path: "/chat {attachments}", description: (text) => text.chatAttachments, feature: RUNTIME_ATTACHMENTS },
  { method: "GET", path: "/sessions", description: (text) => text.sessions },
  { method: "GET", path: "/sessions?q=", description: (text) => text.searchSessions(SESSION_TITLE_MAX_CHARS), feature: RUNTIME_SESSION_ADMIN },
  { method: "GET", path: "/sessions/{sid}", description: (text) => text.session },
  { method: "PATCH", path: "/sessions/{sid}", description: (text) => text.renameSession(SESSION_TITLE_MAX_CHARS), feature: RUNTIME_SESSION_ADMIN },
  { method: "DELETE", path: "/sessions/{sid}", description: (text) => text.deleteSession, feature: RUNTIME_SESSION_ADMIN },
  { method: "GET", path: "/sessions/{sid}/pending", description: (text) => text.pending },
  { method: "POST", path: "/sessions/{sid}/decisions", description: (text) => text.decisions },
  { method: "POST", path: "/sessions/{sid}/regenerate", description: (text) => text.regenerate, feature: RUNTIME_REGENERATE },
  { method: "GET", path: "/sessions/{sid}/files", description: (text) => text.files, feature: RUNTIME_SESSION_FILES },
  { method: "GET", path: "/sessions/{sid}/files/{path}", description: (text) => text.file(formatBytes(SESSION_FILE_MAX_BYTES)), feature: RUNTIME_SESSION_FILES },
  { method: "GET", path: "/memories", description: (text) => text.memories, feature: RUNTIME_MEMORY_FILES },
  {
    method: "GET",
    path: "/memories/files/{key}",
    description: (text) => text.memoryFile(MEMORY_ROOT, formatBytes(MEMORY_FILE_MAX_BYTES)),
    feature: RUNTIME_MEMORY_FILES,
  },
  { method: "DELETE", path: "/memories/files/{key}", description: (text) => text.deleteMemoryFile, feature: RUNTIME_MEMORY_FILES },
];

/** 이 에이전트의 런타임이 내는 경로만 고른다. */
export function visibleEndpoints(status: Pick<AgentStatus, "runtime"> | null | undefined): ExternalEndpoint[] {
  return EXTERNAL_ENDPOINTS.filter((e) => !e.feature || hasRuntimeFeature(status, e.feature));
}

/** 표 행의 테스트 id 조각이다. 같은 경로의 다른 메서드와 쿼리가 서로 다른 id 를 갖는다. */
export function endpointTestId(e: ExternalEndpoint): string {
  return `external-endpoint-${e.method}-${e.path.replace(/[^a-z]+/gi, "-").replace(/^-|-$/g, "")}`;
}
