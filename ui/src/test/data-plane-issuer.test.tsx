/**
 * 데이터 플레인 발급자 (P53b 6.1절). Gateway 가 IdP 토큰만 받는 oidc 모드(idp)에서는 MCP 노드의 Gateway 소스 안내가
 * agent_oauth 를 말하고, 플랫폼 토큰으로 전환했거나 local·header 모드면 말하지 않는다.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { HttpResponse, authConfigState, http, oidcIdpAuthConfig, server } from "./msw";
import { oidcStub } from "./stubs/oidc-client-ts";

const ko = dictionary("ko");

async function gatewayHint(): Promise<HTMLElement> {
  render(<TestApp initialPath="/agents/ag-support?tab=definition" />);
  fireEvent.click(await screen.findByTestId("node-tools-calculator"));
  await waitFor(() => expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", "tools-calculator"));
  return screen.getByTestId("inspector");
}

describe("MCP 노드의 Gateway 소스 안내", () => {
  it("header 모드에서는 agent_oauth 를 말하지 않는다", async () => {
    const inspector = await gatewayHint();
    expect(inspector).toHaveTextContent(ko.canvas.tools.sourceHint.gateway_instance);
    expect(inspector).not.toHaveTextContent(ko.canvas.tools.sourceHintIdpGateway);
  });

  it("Gateway 가 IdP 토큰만 받는 oidc 모드(idp)에서는 agent_oauth 를 말한다", async () => {
    authConfigState.mode = "oidc";
    oidcStub.user = { access_token: "tok-oidc" };
    server.use(http.get("/auth/config", () => HttpResponse.json(oidcIdpAuthConfig)));
    const inspector = await gatewayHint();
    await waitFor(() => expect(inspector).toHaveTextContent(ko.canvas.tools.sourceHintIdpGateway));
  });

  it("플랫폼 토큰을 받는 oidc 모드(기본)에서는 말하지 않는다", async () => {
    authConfigState.mode = "oidc";
    oidcStub.user = { access_token: "tok-oidc" };
    const inspector = await gatewayHint();
    await waitFor(() => expect(inspector).toHaveTextContent(ko.canvas.tools.sourceHint.gateway_instance));
    expect(inspector).not.toHaveTextContent(ko.canvas.tools.sourceHintIdpGateway);
  });
});
