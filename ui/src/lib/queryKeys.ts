/**
 * 여러 화면이 공유하는 react-query 키. 같은 응답을 서로 다른 키로 캐시하면 한 화면에서 저장해도 다른 화면이
 * 옛 값을 계속 보이므로, 화면 둘 이상이 쓰는 키는 여기에 이름을 두고 그 이름만 쓴다.
 * 한 화면 안에서만 쓰는 키(목록·상세 등)는 지금 자리에 그대로 둔다.
 *
 * 목록 프리픽스(`["data-services"]` 처럼 테넌트 없이 무효화하는 것)는 `lib/deletion.ts` 의 `DELETE_RULES.invalidate`
 * 가 정본이다.
 */
import type { ChatBase } from "@/api/client";

/** GET /settings. 플랫폼 전역이라 테넌트로 나누지 않는다. Platform Settings 저장이 이 키를 무효화한다. */
export const PLATFORM_SETTINGS_KEY = ["platform-settings"] as const;

/** GET /settings/data-plane-auth. 네트워크 카드의 안내가 읽고, 그 카드의 저장이 무효화한다. platform-admin 만 부른다. */
export const DATA_PLANE_AUTH_KEY = ["settings", "data-plane-auth"] as const;

/** GET /auth/config. */
export const AUTH_CONFIG_KEY = ["auth-config"] as const;

/** GET /version. 사이드바의 빌드 표시와 플랫폼 API 문서 팝업(P59)이 같은 응답을 본다. */
export const VERSION_KEY = ["version"] as const;

/** GET /clusters. 플랫폼 전역이다. */
export const CLUSTERS_KEY = ["clusters"] as const;

/**
 * GET /me/tokens (P32). 내 MCP 토큰 화면, 비밀번호 화면의 회수 예고, 연결 카드의 발급이 같은 목록을 본다.
 * 사람마다의 목록이라 테넌트로 나누지 않는다.
 */
export const MY_TOKENS_KEY = ["me", "tokens"] as const;

/**
 * GET /me/oauth-grants (P33). 내 MCP 토큰 화면의 "연결된 MCP 클라이언트" 가 본다. Users 화면의 대화상자는 관리자가 남의
 * 목록을 보는 `userOAuthGrantsKey` 다.
 */
export const MY_OAUTH_GRANTS_KEY = ["me", "oauth-grants"] as const;

/** GET /me/runs-as-me (P41 6절). 계정의 "나를 대신하는 자동 실행" 이 보고, 404 면 자동 실행이 꺼진 것이라 화면이 탭과 링크를 감춘다(lib/runs.ts). */
export const MY_RUNS_AS_ME_KEY = ["me", "runs-as-me"] as const;

/** GET /me/approval-choices (P43 5절). 계정 화면 "기억한 선택" 이 본다. 사람마다의 목록이라 테넌트로 나누지 않는다. */
export const MY_APPROVAL_CHOICES_KEY = ["me", "approval-choices"] as const;

/** GET /users/{id}/oauth-grants (P33). Users 화면의 사용자 토큰 대화상자가 그 사람의 연결을 본다. */
export function userOAuthGrantsKey(userId: string): readonly unknown[] {
  return ["users", userId, "oauth-grants"];
}

/** GET /assistant/config (D31). 비서를 켜고 끄면 헤더 토글이 이 키로 따라온다. */
export const ASSISTANT_CONFIG_KEY = ["assistant", "config"] as const;

/** GET /tenants/{tenant}/settings. */
export function tenantSettingsKey(tenant: string): readonly unknown[] {
  return ["tenant-settings", tenant];
}

/** GET /tenants/{tenant}/service-accounts (P53b 6.7절). Tenant Settings 의 Service accounts 카드가 본다. */
export function serviceAccountsKey(tenant: string): readonly unknown[] {
  return ["service-accounts", tenant];
}

/** GET /data-services (테넌트 범위). */
export function dataServicesKey(tenant: string): readonly unknown[] {
  return ["data-services", tenant];
}

/** GET /knowledge-bases (테넌트 범위, D32). */
export function knowledgeBasesKey(tenant: string): readonly unknown[] {
  return ["knowledge-bases", tenant];
}

/** GET /hub (테넌트 범위, D23). 허브 목록·상세와 Gateway 폼·상세가 같은 목록을 본다. */
export function hubItemsKey(tenant: string): readonly unknown[] {
  return ["hub", tenant];
}

/** GET /hub/{id}. 허브 상세와 그 카드(헤더 값, Secret env 값, Tool 규칙)의 저장이 같은 항목을 본다. */
export function hubItemKey(tenant: string, id: string): readonly unknown[] {
  return [...hubItemsKey(tenant), id];
}

/** GET /skills (테넌트 범위, D64). 목록·상세와 캔버스의 Skill 고르개가 같은 목록을 본다. */
export function skillsKey(tenant: string): readonly unknown[] {
  return ["skills", tenant];
}

/** GET /skills/{id}. 상세와 새 리비전 화면이 같은 Skill 을 본다. */
export function skillKey(tenant: string, id: string): readonly unknown[] {
  return ["skills", tenant, id];
}

/** GET /skills/{id}/revisions. 리비전 탭의 목록이다. */
export function skillRevisionsKey(tenant: string, id: string): readonly unknown[] {
  return [...skillKey(tenant, id), "revisions"];
}

/** GET /skills/{id}/revisions/{n}. 쓴 리비전은 바뀌지 않으므로 개요·파일 탭·차이·새 리비전 화면이 같은 캐시를 오래 쓴다. */
export function skillRevisionKey(tenant: string, id: string, n: number): readonly unknown[] {
  return [...skillRevisionsKey(tenant, id), n];
}

/** GET /assistant/sessions (테넌트 범위, D31). */
export function assistantSessionsKey(tenant: string): readonly unknown[] {
  return ["assistant", tenant, "sessions"];
}

/**
 * GET /assistant/sessions/{id}/proposal (P48 6.5절). 세션 id 는 테넌트마다 따로 생기므로 테넌트를 앞에 둔다. 주소의 제안 id 를
 * 끝에 두어 같은 세션의 다른 제안을 연 캔버스가 앞 제안의 답을 다시 쓰지 않게 한다. 제안 id 를 빼면 그 세션의 모든 키를 가리킨다.
 */
export function assistantProposalKey(tenant: string, sessionId: string, proposalId?: string): readonly unknown[] {
  return proposalId === undefined ? ["assistant", tenant, "proposal", sessionId] : ["assistant", tenant, "proposal", sessionId, proposalId];
}

/** GET /agents/summary (테넌트 범위, D35 F2). 홈 챗의 고르개가 첫 요청으로 쓴다. */
export function agentSummariesKey(tenant: string): readonly unknown[] {
  return ["agents", tenant, "summary"];
}

/** 홈 챗이 고른 Agent 하나의 상세(상태). Agent 상세 화면의 키(["agents", tenant, id])와 따로 둔다. */
export function homeAgentDetailKey(tenant: string, agentId: string): readonly unknown[] {
  return ["agents", tenant, agentId, "detail"];
}

/**
 * GET /agents/{id}/sessions (테넌트 범위). 조건(상태·제목 검색어)을 주면 그 목록 하나의 키이고, 안 주면 모든 조건의 목록을
 * 함께 가리키는 앞머리다. 홈 챗이 턴을 끝내거나 이름을 바꾸면 앞머리를 무효화해 목록을 맞춘다.
 */
export function agentSessionsKey(tenant: string, agentId: string, filter?: { status?: string; q?: string }): readonly unknown[] {
  const prefix = ["agents", tenant, agentId, "sessions"];
  return filter ? [...prefix, { status: filter.status ?? "", q: filter.q ?? "" }] : prefix;
}

/**
 * 승인함이 보는 모든 사람의 자동 실행 가운데 승인을 기다리는 세션 (P41 7절, `runs=only`·`all_users`). 세션 목록의 앞머리
 * 아래라 그 앞머리나 테넌트째 무효화하면 함께 다시 읽는다.
 */
export function pendingRunSessionsKey(tenant: string, agentId: string): readonly unknown[] {
  return [...agentSessionsKey(tenant, agentId), "pending", "runs"];
}

/** Agent 하나의 자동 실행 (P41). 일정·트리거·실행 기록이 이 앞머리 아래에 있어 하나를 바꾸면 앞머리째 다시 읽는다. */
export function agentRunsKey(tenant: string, agentId: string, part?: "schedules" | "triggers" | "history"): readonly unknown[] {
  const prefix = ["agents", tenant, agentId, "runs"];
  return part ? [...prefix, part] : prefix;
}

/** 세션의 턴 기록 (P37 12절). 세션 탭의 턴 표와 트레이스 창이 같이 읽는다. */
export function agentTurnsKey(tenant: string, agentId: string, sessionId: string | null | undefined): readonly unknown[] {
  return ["agents", tenant, agentId, "turns", sessionId];
}

/** GET /agents/{id}/sessions/{sid}/feedback (P36 6절). 채팅의 엄지가 다시 연 대화의 내 평가를 읽는다. */
export function sessionFeedbackKey(tenant: string, agentId: string, sessionId: string | null): readonly unknown[] {
  return ["agents", tenant, agentId, "feedback", "session", sessionId];
}

/**
 * GET /agents/{id}/feedback (P36 6절). 거르개를 주면 피드백 탭의 목록 하나의 키이고, 안 주면 모든 거르개의 목록을 함께 가리키는
 * 앞머리다. 채팅에서 평가를 바꾸면 앞머리를 무효화해 탭이 따라온다.
 */
export function agentFeedbackListKey(tenant: string, agentId: string, filter?: { rating?: string; tag?: string }): readonly unknown[] {
  const prefix = ["agents", tenant, agentId, "feedback", "list"];
  return filter ? [...prefix, { rating: filter.rating ?? "", tag: filter.tag ?? "" }] : prefix;
}

/** GET /agents/{id}/sessions/{sid}/files (P36 10절). 세션 파일 서랍이 업로드한 파일과 에이전트가 만든 파일을 읽는다. 미리 보기의 것은 `preview` 를 더 붙인다 (P44b). */
export function sessionFilesKey(tenant: string, chat: ChatBase, sessionId: string): readonly unknown[] {
  return ["agents", tenant, chat.agentId, ...(chat.preview ? ["preview"] : []), "files", sessionId];
}

/** 세션 파일 하나의 미리 보기 내용이다. 목록과 같은 앞머리라 목록을 무효화하면 함께 따라온다. */
export function sessionFilePreviewKey(tenant: string, chat: ChatBase, sessionId: string, path: string | null): readonly unknown[] {
  return [...sessionFilesKey(tenant, chat, sessionId), "file", path];
}

/**
 * GET /agents/{id}/previews (P44b 6절). 개요의 열린 미리 보기 목록이고 내 미리 보기(`myAgentPreviewKey`)의 앞머리다. 서랍이나 개요가
 * 미리 보기를 시작하거나 끝내면 이것을 무효화해 둘이 함께 따라온다.
 */
export function agentPreviewsKey(tenant: string, agentId: string): readonly unknown[] {
  return ["agents", tenant, agentId, "previews"];
}

/** GET /agents/{id}/preview. 테스트 채팅 서랍이 보는 내 미리 보기다. */
export function myAgentPreviewKey(tenant: string, agentId: string): readonly unknown[] {
  return [...agentPreviewsKey(tenant, agentId), "mine"];
}

/** 홈 챗 서랍이 보는 내 장기 기억 (P36 8절)의 앞머리다. 삭제하면 이것을 무효화해 목록과 파일 내용이 함께 따라온다. */
export function myMemoriesKey(tenant: string, agentId: string): readonly unknown[] {
  return ["agents", tenant, agentId, "memories"];
}

/** GET /agents/{id}/memories/me. 내 기억 파일 목록이다. */
export function myMemoryListKey(tenant: string, agentId: string): readonly unknown[] {
  return [...myMemoriesKey(tenant, agentId), "list"];
}

/** GET /agents/{id}/memories/me/files/{key}. 내 기억 파일 하나의 내용이다. */
export function myMemoryFileKey(tenant: string, agentId: string, path: string | null): readonly unknown[] {
  return [...myMemoriesKey(tenant, agentId), "file", path];
}

/**
 * GET /{리소스}/{id}/references (D43). 상세의 "참조" 카드와 삭제·배포 해제 대화상자가
 * 같은 응답을 보므로 키가 하나여야 한다. 첫 조각이 그 리소스의 목록 키라 삭제 뒤 무효화가 같이 닿는다.
 */
export function referencesKey(tenant: string, segment: string, id: string): readonly unknown[] {
  return [segment, tenant, id, "references"];
}

/** 사용량 보고(P37)와 다시 계산의 진행. 가격표의 다시 계산이 끝나면 이 앞머리 전체를 무효화한다. */
export const USAGE_KEY = ["usage"] as const;

export function usageKey(scope: string, filters: object): readonly unknown[] {
  return [...USAGE_KEY, scope, filters];
}

/** GET /settings/pricing (P37). 가격표 카드가 읽고 저장이 덮는다. */
export const PRICING_KEY = ["settings", "pricing"] as const;
/** OTLP 내보내기 설정과 그 상태 (P37 15절). Platform Settings 와 따로인 문서다. */
export const OTLP_SETTINGS_KEY = ["settings", "otlp"] as const;
export const OTLP_STATUS_KEY = [...OTLP_SETTINGS_KEY, "status"] as const;
/** GET /settings/llm-gateway (P38 11절). 게이트웨이 카드와 provider 줄의 반영 상태가 같이 읽는다. */
export const LLM_GATEWAY_KEY = ["settings", "llm-gateway"] as const;

/** GET /settings/hub-images/status (P55 6절). Platform Settings 의 허브 이미지 카드가 보고 그 카드의 저장이 무효화한다. */
export const HUB_IMAGES_STATUS_KEY = ["settings", "hub-images", "status"] as const;

/** GET /agents/{id}/revisions (P45 6절). 배포 탭의 리비전 표와 되돌리기 대화상자가 내용의 지문을 견준다. */
export function agentRevisionsKey(tenant: string, agentId: string): readonly unknown[] {
  return ["agents", tenant, agentId, "revisions"];
}

/**
 * 평가 탭의 앞머리 (P45 7절, 9절). part 가 없으면 세트 목록이고, 세트 id 를 주면 그 세트의 사례(`cases`)나 실행(`runs`), 실행
 * 하나(`run`, runId)다. 사례를 바꾸면 세트의 지문이 바뀌므로 앞머리째 무효화해 세트, 사례, 실행의 확인할 사례가 함께 따라온다.
 * 대화에서 사례를 복사하는 피드백 탭과 세션 탭도 같은 앞머리를 무효화한다.
 */
export function agentEvaluationKey(tenant: string, agentId: string, setId?: string, part?: "cases" | "runs" | "run", runId?: string): readonly unknown[] {
  const prefix = ["agents", tenant, agentId, "evaluation"];
  if (!setId) return prefix;
  return [...prefix, setId, ...(part ? [part] : []), ...(runId ? [runId] : [])];
}

/**
 * `POST .../cases/from-turn` 의 복사본 (P45 8절). 감사에 남는 쓰기라 평가 탭의 앞머리(`agentEvaluationKey`)와 `["agents"]` 밖에
 * 두어, 사례를 저장하거나 Agent 를 다시 읽는 무효화가 열린 복사 창의 복사를 다시 보내지 않게 한다.
 */
export function evaluationCopyKey(tenant: string, agentId: string, setId: string, sessionId: string, turnId: string): readonly unknown[] {
  return ["evaluation-copy", tenant, agentId, setId, sessionId, turnId];
}

/**
 * GET /hub/catalog (P55 4절). 카탈로그 대화상자와 등록 폼이 같은 스냅숏을 본다. 플랫폼 전역이고, 허브 이미지 설정이 바뀌면
 * `admission` 과 `mirrored_image` 가 바뀌므로 그 저장이 이 키를 무효화한다.
 */
export const HUB_CATALOG_KEY = ["hub-catalog"] as const;
