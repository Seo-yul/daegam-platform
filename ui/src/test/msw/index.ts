/**
 * MSW 로 플랫폼 API 를 흉내 낸다. 응답 모양은 platform/daegam/api/*.py 의 실제 응답을 따른다.
 * 요청마다 받은 헤더를 `received` 에 남겨 테넌트 헤더 주입을 검사할 수 있게 한다.
 *
 * 플랫폼 API 는 /api 아래에 있다. 핸들러는 리소스 경로("/hub")로 쓰고 core.ts 의 `http` 래퍼가
 * 접두사를 붙인다. 절대 URL 은 그대로 둔다.
 *
 * 파일은 리소스(API 경로) 하나에 하나이고, 큰 리소스는 주제마다 나눈다(Agent 는 문서·템플릿·채팅·SSE 대본·하위 경로).
 * 픽스처와 그 핸들러를 같은 파일에 두고, 이 index 가 전부 재수출하므로 테스트는 `./msw` 하나만 import 한다.
 *
 *   core.ts          http 래퍼, received/record/lastRequest/lastExact/countExact, apiError, docStore(id 로 찾기·404·교체·삭제),
 *                    downloadHeaders(내려받기 헤더 표본), NOW, /version
 *   sse.ts           agents·assistant 가 같이 쓰는 SSE 블록(sseBlock), 대본 응답(sseResponse), 열어 둔 응답(heldSse),
 *                    토큰 사용량(tokenUsage)
 *   backend-apis.ts  /backend-apis (verify·convert·versions·tokens·minify·apply-draft), backendApiState·backendTokenState
 *   spec-generator.ts /spec-generator/jobs (refine·overrides 모드 job 진행), refineJobState·overridesJobState
 *   data-services.ts /data-services
 *   knowledge.ts     /knowledge-bases (documents·upload·url·reindex·delete), knowledgeState (D32)
 *   exposure.ts      노출 옵션·노출 스냅샷 조각과 hostInUse·TAKEN_HOST (clusters·mcp-servers·gateways·agents 가 같이 쓴다)
 *   clusters.ts      /clusters (exposure-options·preflight)
 *   mcp-servers.ts   /mcp-servers (revisions·preview·deploy·undeploy·clone·publish·manifest), mcpServerState
 *   references.ts    /{리소스}/{id}/references 아홉 (D43), referencesState
 *   advanced.ts      /mcp-servers/{id}/overrides·drift, manifestState
 *   auth.ts          /auth/config, /me, /tenants, /settings, /group-mappings, 테넌트 설정·멤버, authConfigState·localLoginState
 *   tokens.ts        /me/tokens, /users/{id}/tokens (개인 MCP 토큰, P32), tokenState
 *   oauth.ts         /oauth/consent/{inspect,approve,deny} (MCP 클라이언트 연결 동의, P33), consentState
 *   oauth-grants.ts  /me/oauth-grants, /users/{id}/oauth-grants (MCP 클라이언트 연결, P33), grantState
 *   approval-choices.ts /me/approval-choices (기억한 승인 선택, P43), approvalChoiceState
 *   audit.ts         /audit, /audit/mcp
 *   hub.ts           /hub (access·refresh-tools·manifest), hubState, hubAgentFixture (게시한 Agent 의 항목)
 *   skills.ts        /skills (import·validate·access·clone·revisions·archive), skillState (D64)
 *   gateways.ts      /gateways (revisions·preview·deploy·undeploy·clone·publish·manifest·overrides·drift), gatewayState
 *   mcp-metrics.ts   /mcp-servers/{id}/metrics, /gateways/{id}/metrics (P51)
 *   agents.ts        /agents (templates·validate·summary·revisions·deploy·redeploy·rollback·evaluation-gate·undeploy·clone·manifest·
 *                    overrides·drift), agentState
 *   agent-templates.ts Agent 템플릿, 빈 자리를 채운 그래프, 그래프 검증의 목 문맥(mockValidationContext)
 *   agent-sse.ts     Agent 채팅 SSE 대본(턴·결정·다시 생성·중단)과 런타임 세션 색인, agentSessionState
 *   agent-chat.ts    /agents/{id}/chat SSE·sessions·pending·decisions·metrics·events (런타임 프록시)
 *   agent-feedback.ts /agents/{id}/…/feedback (답변 피드백 다섯 라우트, P36), agentFeedbackState
 *   agent-memories.ts /agents/{id}/memories/me (내 장기 기억 목록·읽기·삭제, P36)와 D34 모두 삭제, agentMemoryState
 *   agent-uploads.ts /agents/{id}/uploads (채팅 첨부의 업로드·삭제, P36), agentUploadState
 *   agent-session-files.ts /agents/{id}/sessions/{sid}/files (세션 파일 목록·다운로드, P36), agentSessionFileState
 *   agent-previews.ts /agents/{id}/preview·previews 와 미리 보기 아래 채팅 (P44b), agentPreviewState
 *   agent-evaluation.ts /agents/{id}/evaluation-sets (세트·사례·복사·실행, P45), evaluationState
 *   runs.ts          /agents/{id}/runs·schedules·triggers, /me/runs-as-me (자동 실행, P41), runsState
 *   assistant.ts     /assistant (config·sessions·turns SSE·decisions SSE), assistantState (D31)
 *   usage.ts         /usage, /admin/usage, /usage/reprice, /settings/pricing (P37), usageState
 *   llm-gateway.ts   /settings/llm-gateway, /agents/{id}/llm-token/rotate (P38), llmGatewayState
 *
 * index 자신은 핸들러 전부(`allHandlers`)와 vitest 의 `server`, 요청을 붙드는 `holdRequest`, 테스트마다 부르는
 * `resetMswState` 를 낸다.
 *
 * 핸들러가 바꾸는 mutable 상태(위 목록의 …State)는 resetHandlers 로 되돌아가지 않는다. 상태 `xState` 마다 그 모듈이
 * `resetXState()` 를 내보내고, `resetMswState` 가 테스트마다 그것을 전부 불러 기본값으로 되돌린다. 그래서 테스트는 기본값과
 * 다른 것만 세팅한다. 새 mutable 상태를 더하면 그 reset 을 `resetMswState` 에 같이 넣는다.
 */
import type { HttpHandler } from "msw";
import { setupServer } from "msw/node";

import { advancedHandlers, resetManifestState } from "./advanced";
import { agentChatHandlers } from "./agent-chat";
import { agentEvaluationHandlers, resetEvaluationState } from "./agent-evaluation";
import { agentFeedbackHandlers, resetAgentFeedbackState } from "./agent-feedback";
import { agentMemoryHandlers, resetAgentMemoryState } from "./agent-memories";
import { agentPreviewHandlers, resetAgentPreviewState } from "./agent-previews";
import { agentSessionFileHandlers, resetAgentSessionFileState } from "./agent-session-files";
import { agentUploadHandlers, resetAgentUploadState } from "./agent-uploads";
import { agentHandlers, resetAgentState } from "./agents";
import { approvalChoiceHandlers, resetApprovalChoiceState } from "./approval-choices";
import { assistantHandlers, resetAssistantState } from "./assistant";
import { auditHandlers } from "./audit";
import { authSettingsHandlers, localAuthHandlers, resetAuthConfigState, resetLocalLoginState } from "./auth";
import { backendApiHandlers, resetBackendApiState, resetBackendTokenState } from "./backend-apis";
import { clusterHandlers } from "./clusters";
import { http, received, versionHandlers } from "./core";
import { dataServiceHandlers } from "./data-services";
import { gatewayHandlers, resetGatewayState } from "./gateways";
import { hubHandlers, resetHubState } from "./hub";
import { knowledgeHandlers, resetKnowledgeState } from "./knowledge";
import { llmGatewayHandlers, resetLlmGatewayState } from "./llm-gateway";
import { mcpMetricsHandlers } from "./mcp-metrics";
import { mcpServerHandlers, resetMcpServerState } from "./mcp-servers";
import { oauthHandlers, resetConsentState } from "./oauth";
import { grantHandlers, resetGrantState } from "./oauth-grants";
import { referenceHandlers, resetReferencesState } from "./references";
import { resetSkillState, skillHandlers } from "./skills";
import { resetRunsState, runsHandlers } from "./runs";
import { resetOverridesJobState, resetRefineJobState, specGeneratorHandlers } from "./spec-generator";
import { resetTokenState, tokenHandlers } from "./tokens";
import { resetUsageState, usageHandlers } from "./usage";

export * from "./core";
export * from "./sse";
export * from "./backend-apis";
export * from "./spec-generator";
export * from "./data-services";
export * from "./knowledge";
export * from "./exposure";
export * from "./clusters";
export * from "./mcp-servers";
export * from "./references";
export * from "./advanced";
export * from "./auth";
export * from "./tokens";
export * from "./oauth";
export * from "./oauth-grants";
export * from "./approval-choices";
export * from "./audit";
export * from "./hub";
export * from "./skills";
export * from "./gateways";
export * from "./mcp-metrics";
export * from "./agents";
export * from "./agent-templates";
export * from "./agent-sse";
export * from "./agent-chat";
export * from "./agent-feedback";
export * from "./agent-memories";
export * from "./agent-uploads";
export * from "./agent-session-files";
export * from "./agent-previews";
export * from "./agent-evaluation";
export * from "./runs";
export * from "./assistant";
export * from "./usage";
export * from "./llm-gateway";

/**
 * 핸들러 전부. 한 요청에 둘이 맞으면 앞의 것이 답하므로 순서가 뜻을 가진다(Advanced 의 /mcp-servers/{id}/manifest 가
 * mcp-servers.ts 의 것보다 앞이다). vitest 의 `server` 와 Playwright 목 서버(ui/e2e/mock-server.ts)가 이 목록 하나를 쓴다.
 */
export const allHandlers: HttpHandler[] = [
  ...advancedHandlers,
  // 인증·설정·멤버·감사 (P7 단위 7), 개인 MCP 토큰 (P32), MCP 클라이언트 연결 동의와 연결 (P33)
  ...authSettingsHandlers,
  ...localAuthHandlers,
  ...tokenHandlers,
  ...oauthHandlers,
  ...grantHandlers,
  ...approvalChoiceHandlers,
  ...auditHandlers,
  // 사용량과 가격표 (P37). /settings/pricing 이 /settings 보다 앞이다
  ...usageHandlers,
  // LLM 게이트웨이의 상태와 토큰 교체 (P38)
  ...llmGatewayHandlers,
  // 버전, Backend API, Spec Generator, Database, Knowledge Base(D32), Cluster, 플랫폼 비서(D31), 참조(D43)
  ...versionHandlers,
  ...backendApiHandlers,
  ...specGeneratorHandlers,
  ...dataServiceHandlers,
  ...knowledgeHandlers,
  ...clusterHandlers,
  ...assistantHandlers,
  ...referenceHandlers,
  // MCP Server, MCP Hub, Gateway (D42), Skill (D64), 그 둘의 메트릭 (P51)
  ...mcpServerHandlers,
  ...hubHandlers,
  ...gatewayHandlers,
  ...mcpMetricsHandlers,
  ...skillHandlers,
  // Agent 문서와 채팅 프록시, 그 아래의 피드백·기억·업로드·세션 파일 (D26, P36)
  ...agentHandlers,
  ...agentChatHandlers,
  ...agentFeedbackHandlers,
  ...agentMemoryHandlers,
  ...agentUploadHandlers,
  ...agentSessionFileHandlers,
  // 미리 보기와 그 아래 채팅 (P44b)
  ...agentPreviewHandlers,
  // 평가 세트, 사례, 실행 (P45)
  ...agentEvaluationHandlers,
  // 자동 실행 (P41)
  ...runsHandlers,
];

/**
 * 요청 기록과 mutable 상태를 전부 기본값으로 되돌린다. setup.ts 의 `resetMockState` 가 테스트마다 부른다. 테스트 한가운데서
 * 한 리소스만 되돌리려면 그 모듈의 reset 을 부른다.
 */
export function resetMswState(): void {
  received.length = 0;
  resetAuthConfigState();
  resetLocalLoginState();
  resetApprovalChoiceState();
  resetBackendApiState();
  resetBackendTokenState();
  resetRefineJobState();
  resetOverridesJobState();
  resetKnowledgeState();
  resetMcpServerState();
  resetManifestState();
  resetReferencesState();
  resetHubState();
  resetGatewayState();
  resetSkillState();
  resetTokenState();
  resetConsentState();
  resetGrantState();
  resetAgentState();
  resetAgentFeedbackState();
  resetAgentMemoryState();
  resetAgentUploadState();
  resetAgentSessionFileState();
  resetAgentPreviewState();
  resetEvaluationState();
  resetAssistantState();
  resetUsageState();
  resetRunsState();
  resetLlmGatewayState();
}

export const server = setupServer(...allHandlers);

/** 붙든 요청이다. `release` 를 부르면 놓고, `started` 는 지금까지 붙든 요청의 수다. */
export interface HeldRequest {
  release: () => void;
  started: () => number;
}

/**
 * method·path 요청을 테스트가 `release` 를 부를 때까지 붙들어 둔다. 놓은 뒤에는 `respond` 가 답하고, 주지 않았으면 기본 목이
 * 답한다. 요청이 도는 동안의 화면(업로드 중, 보내는 중, 받는 중)을 보는 데 쓴다. 붙드는 핸들러는 요청을 기록하지 않는다.
 */
export function holdRequest(method: "get" | "post", path: string, respond?: () => Response): HeldRequest {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  let started = 0;
  server.use(
    http[method](path, async () => {
      started += 1;
      await gate;
      return respond?.();
    }),
  );
  return { release, started: () => started };
}
