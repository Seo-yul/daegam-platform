/**
 * LLM 게이트웨이의 상태와 Agent 의 LLM 토큰 교체 (P38 11절).
 *   /settings/llm-gateway, /agents/{id}/llm-token/rotate
 *
 * 기본 상태는 꺼진 게이트웨이다. 설치의 기본값이고, 다른 화면 테스트가 provider 줄의 반영 상태를 보지 않게 한다. 켠 상태는
 * `llmGatewayOn` 을 `llmGatewayState.status` 에 넣어 쓴다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { LlmGatewayStatus } from "@/api/types";

import { NOW, http, record } from "./core";

/** llmGatewayOn 에서 주소 검사에 걸린 provider 이름. Platform Settings 에 이 provider 를 더한 테스트가 쓴다. */
export const GATEWAY_REJECTED_PROVIDER = "internal-llm";

const OFF: LlmGatewayStatus = {
  switch: "off",
  url: "http://llm-gateway.daegam-llm.svc.cluster.local:8080",
  namespace: "daegam-llm",
  clashing_tenants: [],
  activated_at: null,
  ready_at: null,
  desired_rev: null,
  applied_rev: null,
  reason: null,
  warnings: [],
  pods: [],
  providers: [],
  limits_rev: null,
  revocations: 0,
  revocations_pending: 0,
  ingest_token_expires_at: null,
  ingest_received_at: null,
  synced_at: null,
  agents: { gateway: 0, direct: {}, moving: 0, cleanups_pending: 0 },
};

/** 켠 게이트웨이. openai-main 은 반영됐고, 둘째 provider(GATEWAY_REJECTED_PROVIDER)는 주소 검사에 걸렸다. */
export const llmGatewayOn: LlmGatewayStatus = {
  ...OFF,
  switch: "agentgateway",
  activated_at: NOW,
  ready_at: NOW,
  desired_rev: "3f2a9c1e",
  applied_rev: "3f2a9c1e",
  warnings: ["llm_ratelimit_unavailable"],
  pods: [
    { name: "llm-gateway-a", rev: "3f2a9c1e" },
    { name: "llm-gateway-b", rev: "3f2a9c1e" },
  ],
  providers: [
    {
      name: "openai-main",
      slug: "openai-main",
      ready: true,
      key_synced_at: NOW,
      route_url: "http://llm-gateway.daegam-llm.svc.cluster.local:8080/p/openai-main/v1",
      rejected: null,
    },
    {
      name: GATEWAY_REJECTED_PROVIDER,
      slug: GATEWAY_REJECTED_PROVIDER,
      ready: false,
      key_synced_at: null,
      route_url: `http://llm-gateway.daegam-llm.svc.cluster.local:8080/p/${GATEWAY_REJECTED_PROVIDER}/v1`,
      rejected: "base_url resolves to a private address",
    },
  ],
  revocations: 3,
  revocations_pending: 1,
  ingest_token_expires_at: NOW,
  ingest_received_at: NOW,
  synced_at: NOW,
  agents: { gateway: 2, direct: { runtime_features_unknown: 1, remote_cluster: 1 }, moving: 0, cleanups_pending: 1 },
};

export const llmGatewayState: { status: LlmGatewayStatus } = { status: OFF };

export function resetLlmGatewayState(): void {
  llmGatewayState.status = OFF;
}

export const llmGatewayHandlers: HttpHandler[] = [
  http.get("/settings/llm-gateway", async ({ request }) => {
    await record(request);
    return HttpResponse.json(llmGatewayState.status);
  }),
  http.post("/agents/:id/llm-token/rotate", async ({ request, params }) => {
    const r = await record(request);
    const body = (r.body ?? {}) as { immediate?: boolean };
    return HttpResponse.json({ agent_id: String(params.id), immediate: body.immediate ?? false });
  }),
];
