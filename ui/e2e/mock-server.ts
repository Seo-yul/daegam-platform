/**
 * Playwright smoke 용 목 API 서버 (포트 9300).
 * MSW 핸들러(src/test/msw/)를 @mswjs/http-middleware 로 실제 HTTP 서버로 만든다.
 * 플랫폼과 마찬가지로 API 는 /api 아래이고 /healthz 만 루트다.
 * vite dev(4173) 가 VITE_API_PROXY=http://127.0.0.1:9300 으로 그 경로들을 이리로 프록시한다.
 * 실행: npx vite-node e2e/mock-server.ts
 */
import { createMiddleware } from "@mswjs/http-middleware";
import express from "express";

import { RUNTIME_SESSION_FILES } from "../src/lib/runtimeFeatures";
import {
  agentFeedbackState,
  agentFixture,
  agentPreviewState,
  agentState,
  agentWithAttachments,
  allHandlers,
  authConfigState,
  evaluationCaseFixture,
  evaluationSetFixture,
  evaluationState,
  feedbackFixture,
  previewFixture,
} from "../src/test/msw";

const PORT = Number(process.env.MOCK_PORT ?? 9300);
// 스모크는 header 모드다. MOCK_AUTH_MODE=oidc 면 GET /auth/config 가 oidc 를 내어 Agent 노출 폼을 열어 볼 수 있다 (D27)
if (process.env.MOCK_AUTH_MODE === "oidc") authConfigState.mode = "oidc";

// 채팅 스펙(chat.spec.ts)이 쓰는 Agent 다. 첨부와 세션 파일을 아는 런타임에 배포됐다. 스모크가 보는 ag-support 는 런타임 기록이 없는
// 기본 픽스처 그대로 둔다 (P36)
agentState.items.push({ ...agentWithAttachments({}, RUNTIME_SESSION_FILES), id: "ag-files", name: "files", display_name: "파일 에이전트" });

// 미리 보기 스펙(preview.spec.ts)이 쓰는 draft Agent 와 다른 사람이 연 그 미리 보기다. 시작한 뒤 1.5초가 지나야 파드가 Ready 라
// 서랍의 진행 줄이 보인다 (P44b)
agentState.items.push(agentFixture({ id: "ag-preview", name: "preview", display_name: "미리 보기 에이전트", state: "draft", revision: 1, revisions: [1], deployed_revision: null, status: null }));
agentPreviewState.items.push(previewFixture({ id: "pv-other", source_id: "ag-preview", revision: 1 }));
agentPreviewState.readyAfterMs = 1_500;

// 평가 스펙(evaluation.spec.ts)이 쓰는 Agent 다. r2 를 배포했고 r3 을 저장만 했다. 세트 하나에 다른 사람이 쓴 사례 하나가 있고,
// 대화한 사람이 평가 사례로 쓰는 데 동의한 평가 하나가 있다. 실행은 시작한 뒤 1.5초가 지나야 끝나 진행 줄이 보인다 (P45)
agentState.items.push(agentFixture({ id: "ag-eval", name: "eval", display_name: "평가 에이전트", revision: 3, revisions: [1, 2, 3], deployed_revision: 2 }));
evaluationState.sets.push(evaluationSetFixture({ id: "es-e2e", agent_id: "ag-eval" }));
evaluationState.cases.push(evaluationCaseFixture({ id: "ec-e2e", agent_id: "ag-eval", set_id: "es-e2e" }));
agentFeedbackState.items.push(feedbackFixture({ id: "fb-e2e", agent_id: "ag-eval", rating: "up", tags: [], comment: null, evaluation_use: true }));
evaluationState.finishAfterMs = 1_500;

// Gateway 목(gatewayState, hubState)은 msw/ 의 기본 픽스처 그대로다. 상태는 프로세스 전역이라 스모크는 직렬로 돈다.
// 핸들러는 vitest 의 server 와 같은 allHandlers 다.
// Agent(D26) 의 채팅 SSE 는 ReadableStream 응답이고 http-middleware 가 그대로 pipe 하므로 브라우저도 스트림으로 받는다.
const app = express();
app.get("/healthz", (_req, res) => {
  res.json({ status: "ok", mock: true });
});
app.use(createMiddleware(...allHandlers));

app.listen(PORT, "127.0.0.1", () => {
  console.log(`mock API server on http://127.0.0.1:${PORT}`);
});
