/**
 * LLM 게이트웨이의 화면 (P38 11절): Platform Settings 의 게이트웨이 카드와 한도, provider 줄의 반영 상태, Agent 개요의 LLM 경로와
 * 토큰 교체, 게이트웨이의 세 오류 코드 문구.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { windingDown } from "@/components/settings/LlmGatewayCard";
import { dictionary } from "@/i18n";
import { errorText } from "@/lib/errorText";

import { GATEWAY_REJECTED_PROVIDER, HttpResponse, agentFixture, agentState, apiError, http, lastRequest, llmGatewayOn, llmGatewayState, platformSettings, server } from "./msw";
import { selectValue } from "./select";

const KO = dictionary("ko");
const EN = dictionary("en");
const GATEWAY = KO.admin.platformSettings.llmGateway;
const ROUTE = KO.agents.overview.llmRoute;

describe("LLM gateway screens (P38 11절)", () => {
  it("shows the gateway status and each provider's gateway state when the gateway is on", async () => {
    llmGatewayState.status = { ...llmGatewayOn, clashing_tenants: ["llm", "ops"] };
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-llm-gateway");
    expect(await within(card).findByTestId("ps-llm-gateway-switch")).toHaveTextContent(GATEWAY.on);
    // 조사는 마지막 테넌트 이름의 끝소리를 따른다
    expect(within(card).getByTestId("ps-llm-gateway-clashing")).toHaveTextContent("테넌트 llm, ops가 게이트웨이 네임스페이스를");
    expect(within(card).getByTestId("ps-llm-gateway-revision")).toHaveTextContent("반영된 리비전 3f2a9c1e");
    expect(within(card).getByTestId("ps-llm-gateway-revision")).toHaveTextContent("게이트웨이 파드 2개");
    expect(within(card).getByTestId("ps-llm-gateway-agents")).toHaveTextContent("게이트웨이 경로 Agent 2개 · 직접 경로 2개 · 옮기는 중 0개");
    expect(within(card).getByTestId("ps-llm-gateway-reason-runtime_features_unknown")).toHaveTextContent(ROUTE.reasons.runtime_features_unknown);
    expect(within(card).getByTestId("ps-llm-gateway-code-llm_ratelimit_unavailable")).toHaveTextContent(GATEWAY.codes.llm_ratelimit_unavailable);
    expect(within(card).getByTestId("ps-llm-gateway-tokens")).toHaveTextContent("회수한 토큰 3개 · 아직 반영되지 않은 회수 1개");
    // 연결 확인과 따로인 반영 상태다
    expect(await screen.findByTestId("ps-provider-gateway-0")).toHaveTextContent(KO.admin.platformSettings.provider.gateway.applied);
  });

  it("marks a provider the gateway does not use", async () => {
    llmGatewayState.status = llmGatewayOn;
    const rejected = { name: GATEWAY_REJECTED_PROVIDER, kind: "openai" as const, model: null, api_key_secret: "internal-key", base_url: "http://10.0.0.5/v1", stream_usage: true };
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [...platformSettings.llm_providers, rejected] })));
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-provider-gateway-0")).toHaveTextContent(KO.admin.platformSettings.provider.gateway.applied);
    expect(await screen.findByTestId("ps-provider-gateway-1")).toHaveTextContent(KO.admin.platformSettings.provider.gateway.rejected);
  });

  it("keeps showing and polling the status after turning off until nothing is left", async () => {
    // 끈 뒤에도 게이트웨이 경로의 Agent 와 정리가 남았다. 되돌리기 전에 기다릴 수를 보인다 (llm-gateway.md 11절)
    llmGatewayState.status = { ...llmGatewayOn, switch: "off", agents: { gateway: 1, direct: {}, moving: 0, cleanups_pending: 1 } };
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-llm-gateway");
    expect(await within(card).findByTestId("ps-llm-gateway-switch")).toHaveTextContent(GATEWAY.off);
    expect(within(card).getByTestId("ps-llm-gateway-agents")).toHaveTextContent("게이트웨이 경로 Agent 1개");
    expect(windingDown(llmGatewayState.status)).toBe(true);
    expect(windingDown({ ...llmGatewayState.status, pods: [], agents: { gateway: 0, direct: { gateway_off: 3 }, moving: 0, cleanups_pending: 0 } })).toBe(false);
  });

  it("an off gateway shows no status and no provider state", async () => {
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("ps-llm-gateway");
    expect(await within(card).findByTestId("ps-llm-gateway-switch")).toHaveTextContent(GATEWAY.off);
    expect(within(card).queryByTestId("ps-llm-gateway-status")).toBeNull();
    await screen.findByTestId("ps-provider-name-0");
    expect(screen.queryByTestId("ps-provider-gateway-0")).toBeNull();
  });

  it("saves the token limits with a Platform Settings PATCH and refuses values out of range", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    const limits = await screen.findByTestId("ps-llm-limits");
    await user.type(within(limits).getByTestId("ps-llm-limit-default"), "0");
    expect(within(limits).getByTestId("ps-llm-limits-save")).toBeDisabled();
    await user.clear(within(limits).getByTestId("ps-llm-limit-default"));
    await user.type(within(limits).getByTestId("ps-llm-limit-default"), "100000");
    await user.click(within(limits).getByTestId("ps-llm-limit-tenants-add"));
    await user.type(within(limits).getByTestId("ps-llm-limit-tenants-name-0"), "nobody");
    await user.type(within(limits).getByTestId("ps-llm-limit-tenants-tokens-0"), "5000");
    // 테넌트는 등록부가 없어 꼴만 본다. 아는 테넌트가 아닌 이름에는 안내만 붙는다
    expect(await within(limits).findByTestId("ps-llm-limit-unknown-0")).toHaveTextContent(GATEWAY.limits.unknownTenant);
    await user.click(within(limits).getByTestId("ps-llm-limit-providers-add"));
    await selectValue(user, "ps-llm-limit-providers-name-0", "openai-main");
    await user.type(within(limits).getByTestId("ps-llm-limit-providers-tokens-0"), "200000");
    await user.click(within(limits).getByTestId("ps-llm-limits-save"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/settings")?.body).toEqual({
      llm_gateway: { limits: { tenant_tokens_per_minute: 100000, tenants: { nobody: 5000 }, providers: { "openai-main": 200000 } } },
    });
  });

  it("shows a direct Agent's reason and no token replacement", async () => {
    agentState.items = [agentFixture({ llm_route: { route: "direct", reason: "runtime_image_too_old" } })];
    render(<TestApp initialPath="/agents/ag-support" />);
    const row = await screen.findByTestId("summary-llm-route");
    expect(within(row).getByTestId("llm-route")).toHaveTextContent(ROUTE.direct);
    expect(within(row).getByTestId("llm-route-reason")).toHaveTextContent(ROUTE.reasons.runtime_image_too_old);
    expect(within(row).queryByTestId("llm-token-rotate")).toBeNull();
  });

  it("replaces the LLM token of a gateway Agent, right away when chosen", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture({ llm_route: { route: "gateway", reason: null } })];
    render(<TestApp initialPath="/agents/ag-support" />);
    const row = await screen.findByTestId("summary-llm-route");
    expect(within(row).getByTestId("llm-route")).toHaveTextContent(ROUTE.gateway);
    await user.click(within(row).getByTestId("llm-token-immediate"));
    await user.click(within(row).getByTestId("llm-token-rotate"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/llm-token/rotate")?.body).toEqual({ immediate: true }));
    expect(await within(row).findByTestId("llm-token-rotated")).toHaveTextContent(ROUTE.rotatedImmediate);
  });

  it("explains a refused token replacement", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture({ llm_route: { route: "gateway", reason: null } })];
    server.use(http.post("/agents/:id/llm-token/rotate", () => apiError(409, "llm_token_missing", "agent calls its providers directly")));
    render(<TestApp initialPath="/agents/ag-support" />);
    const row = await screen.findByTestId("summary-llm-route");
    await user.click(within(row).getByTestId("llm-token-rotate"));
    expect(await within(row).findByTestId("llm-token-rotate-error")).toHaveTextContent(KO.errors.codes.llm_token_missing);
  });

  it("has a sentence for each gateway error code in both languages", () => {
    for (const code of ["llm_gateway_unreachable", "llm_gateway_denied", "llm_gateway_rate_limited"]) {
      expect(errorText(KO, code)).toBeTruthy();
      expect(errorText(EN, code)).toBeTruthy();
      expect(errorText(KO, code)).not.toContain(code);
    }
    expect(errorText(KO, "llm_gateway_rate_limited")).toContain("1분");
  });
});
