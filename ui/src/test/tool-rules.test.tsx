/**
 * Tool 규칙 카드 (P52 7절). MCP Server 와 Gateway 의 정의 탭, 허브 image 항목의 Tools 탭이 같은 카드로 규칙을 고친다.
 *
 * - 저장은 그 리소스의 저장 API 로 규칙 전부를 보내고, 출처가 받지 않는 칸은 보내지 않는다(항목은 역할만, Gateway 는 대상만).
 * - 읽기 전용 표시는 기본값(GET·HEAD 는 읽기)과 다를 때만 저장한다. 조건의 값은 인자의 형식으로 바꿔 보낸다.
 * - 대상을 모두 지우면 저장 전에 모두에게 열린다고 말하고, 저장의 답이 열린 Tool 과 다시 뜰 수 있는 Agent 를 알린다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { BackendAPIVersion, MCPServer } from "@/api/types";
import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { PINNED_LIST_REFUSES_CALLS } from "@/lib/toolRules";
import { HttpResponse, apiError, authConfigState, oidcIdpAuthConfig, gatewayFixture, gatewayState, hubItemFixture, hubState, http, lastRequest, mcpServerFixture, mcpServerState, meViewer, record, server, versionDetail } from "./msw";
import { selectValue } from "./select";
import { oidcStub } from "./stubs/oidc-client-ts";

const ko = dictionary("ko");

const ORDERS_VERSION: BackendAPIVersion = {
  ...versionDetail,
  tool_names: ["CreateOrder", "GetOrder"],
  tool_details: [
    { operation_id: "CreateOrder", summary: null, description: null, method: "POST", arguments: { amount: "number", currency: "string", items: "array" } },
    { operation_id: "GetOrder", summary: null, description: null, method: "GET", arguments: { id: "string" } },
  ],
};

function useOrdersVersion() {
  server.use(http.get("/backend-apis/:id/versions/:n", () => HttpResponse.json(ORDERS_VERSION)));
}

/** PUT 의 답에 규칙 저장의 알림을 붙인다. 문서는 msw 의 저장소가 받은 본문으로 바꾼 것이다. */
function answerWithNotice(path: string, notice: { opened: string[]; rolling_agents: { id: string; name: string }[] }, saved: () => unknown) {
  server.use(
    http.put(path, async ({ request }) => {
      await record(request);
      return HttpResponse.json({ ...(saved() as object), rules_notice: notice });
    }),
  );
}

function serverWith(over: Partial<MCPServer["backends"][number]>): MCPServer {
  const base = mcpServerFixture();
  return { ...base, backends: [{ ...base.backends[0], ...over }] };
}

describe("MCP Server 의 Tool 규칙", () => {
  it("역할과 인자 조건을 정의 탭에서 저장하고, 조건의 값은 인자의 형식으로 보낸다", async () => {
    const user = userEvent.setup();
    useOrdersVersion();
    mcpServerState.items = [mcpServerFixture()];
    answerWithNotice("/mcp-servers/:id", { opened: [], rolling_agents: [{ id: "ag-support", name: "support" }] }, () => mcpServerFixture({ revision: 3, revisions: [1, 2, 3] }));
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    const row = await screen.findByTestId("tool-rule-CreateOrder");
    // 오버라이드한 Tool 은 지금 이름으로 보인다
    expect(within(screen.getByTestId("tool-rule-GetOrder")).getByText("get_order")).toBeInTheDocument();
    // GET 은 표시가 없어도 읽기 전용이 기본값이다
    expect(screen.getByTestId("tool-rule-GetOrder-read-only")).toBeChecked();
    expect(screen.getByTestId("tool-rule-CreateOrder-read-only")).not.toBeChecked();
    expect(screen.getByTestId("tool-rules-save")).toBeDisabled();

    await user.click(within(row).getByTestId("tool-rule-CreateOrder-role-developer"));
    await user.click(within(row).getByTestId("tool-rule-CreateOrder-agent-ag-support"));
    await user.click(within(row).getByTestId("tool-rule-CreateOrder-add-condition"));
    await selectValue(user, within(row).getByTestId("tool-rule-CreateOrder-condition-0-op"), "max");
    await user.type(within(row).getByTestId("tool-rule-CreateOrder-condition-0-value"), "100");
    await user.click(screen.getByTestId("tool-rules-save"));

    await waitFor(() => expect(lastRequest("PUT", "/mcp-servers/ms-calc")).toBeDefined());
    const body = lastRequest("PUT", "/mcp-servers/ms-calc")!.body as { name: string; backends: { tool_rules: unknown; timeout_seconds: number }[] };
    expect(body.name).toBe("calc");
    expect(body.backends[0].timeout_seconds).toBe(30);
    expect(body.backends[0].tool_rules).toEqual({
      CreateOrder: {
        callers: { roles: ["developer"], groups: [], agents: ["ag-support"] },
        conditions: [{ path: "amount", op: "max", value: 100, optional: false }],
      },
    });
    // 이미 id 를 실은 토큰을 든 Agent 는 다시 뜨지 않으므로 "수 있습니다" 다
    expect(await screen.findByTestId("tool-rules-rolling")).toHaveTextContent(ko.toolRules.rollingSaved("support"));
  });

  it("배열은 조건의 인자로 고를 수 없다", async () => {
    useOrdersVersion();
    mcpServerState.items = [serverWith({ tool_rules: { CreateOrder: { callers: { roles: [], groups: [], agents: [] }, conditions: [{ path: "amount", op: "min", value: 1, optional: true }] } } })];
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    const path = await screen.findByTestId("tool-rule-CreateOrder-condition-0-path");
    expect(screen.getByTestId("tool-rule-CreateOrder-condition-0-value")).toHaveValue("1");
    expect(screen.getByTestId("tool-rule-CreateOrder-condition-0-optional")).toBeChecked();
    await userEvent.setup().click(path);
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["amount", "currency"]);
  });

  it("대상을 모두 지우면 저장 전에 열린다고 말하고, 저장의 답이 열린 Tool 을 지금 이름으로 알린다", async () => {
    const user = userEvent.setup();
    useOrdersVersion();
    mcpServerState.items = [serverWith({ tool_rules: { GetOrder: { callers: { roles: ["viewer"], groups: [], agents: [] } } } })];
    answerWithNotice("/mcp-servers/:id", { opened: ["bp-orders/GetOrder"], rolling_agents: [] }, () => mcpServerFixture({ revision: 3, revisions: [1, 2, 3] }));
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    await waitFor(() => expect(screen.getByTestId("tool-rules-count")).toHaveTextContent(ko.toolRules.ruledTools(1)));
    await user.click(await screen.findByTestId("tool-rule-GetOrder-role-viewer"));
    expect(screen.getByTestId("tool-rule-GetOrder-opens")).toHaveTextContent(ko.toolRules.opensNotice);
    await user.click(screen.getByTestId("tool-rules-save"));
    expect(await screen.findByTestId("tool-rules-opened")).toHaveTextContent(ko.toolRules.openedSaved("get_order"));
    const body = lastRequest("PUT", "/mcp-servers/ms-calc")!.body as { backends: { tool_rules: unknown }[] };
    expect(body.backends[0].tool_rules).toEqual({});
  });

  it("고칠 권한이 없으면 고치지 못하고 그 까닭을 말한다", async () => {
    useOrdersVersion();
    server.use(http.get("/me", () => HttpResponse.json(meViewer)));
    mcpServerState.items = [mcpServerFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    expect(await screen.findByTestId("tool-rules-read-only")).toHaveTextContent(ko.toolRules.editRequired);
    expect(screen.queryByTestId("tool-rules-save")).not.toBeInTheDocument();
    expect(await screen.findByTestId("tool-rule-CreateOrder-role-viewer")).toBeDisabled();
  });

  it("Backend API 버전을 읽지 못하면 그 오류를 보이고 저장을 막는다", async () => {
    // 메서드를 모른 채 저장하면 GET 의 기본값이 쓰기로 읽혀 읽기 전용 표시가 바뀐다
    server.use(http.get("/backend-apis/:id/versions/:n", () => apiError(500, "internal_error", "boom")));
    mcpServerState.items = [mcpServerFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    expect(await screen.findByTestId("tool-rules-load-error")).toBeInTheDocument();
    expect(screen.getByTestId("tool-rules-save")).toBeDisabled();
  });

  it("목록에서 빠진 operationId 의 규칙과 oidc 밖의 그룹은 걷을 수 있는 줄이다", async () => {
    const user = userEvent.setup();
    useOrdersVersion();
    mcpServerState.items = [serverWith({ tool_rules: { OldOrder: { callers: { roles: [], groups: ["ops"], agents: [] } } } })];
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    expect(await screen.findByTestId("tool-rule-OldOrder-stale")).toHaveTextContent(ko.toolRules.notOffered);
    // header 모드라 그룹 칸은 없고 저장된 그룹은 걷는 줄로만 보인다
    expect(screen.queryByTestId("tool-rule-OldOrder-groups")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("tool-rule-OldOrder-group-ops"));
    await user.click(screen.getByTestId("tool-rules-save"));
    await waitFor(() => expect(lastRequest("PUT", "/mcp-servers/ms-calc")).toBeDefined());
    expect((lastRequest("PUT", "/mcp-servers/ms-calc")!.body as { backends: { tool_rules: unknown }[] }).backends[0].tool_rules).toEqual({});
  });

  it("개요의 정의 요약이 규칙이 있는 Tool 의 수를 센다", async () => {
    mcpServerState.items = [serverWith({ tool_rules: { GetOrder: { read_only: false }, CreateOrder: { callers: { roles: ["viewer"], groups: [], agents: [] } } } })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    // 읽기 전용 표시만 둔 규칙은 세지 않는다
    expect(await screen.findByTestId("summary-ruled-tools")).toHaveTextContent(ko.toolRules.ruledTools(1));
  });
});

describe("Gateway 의 Tool 규칙", () => {
  it("백엔드마다 대상만 저장하고 지운 Agent 는 그 사실을 보인다", async () => {
    const user = userEvent.setup();
    const base = gatewayFixture();
    gatewayState.items = [{ ...base, backends: [{ ...base.backends[0], tool_rules: { Add: { callers: { roles: [], groups: [], agents: ["ag-gone"] } } } }, base.backends[1]] }];
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    const row = await screen.findByTestId("tool-rule-Add");
    expect(within(row).getAllByText(ko.toolRules.deletedAgent).length).toBeGreaterThan(0);
    // Gateway 의 규칙에는 읽기 전용 표시와 조건이 없다
    expect(screen.queryByTestId("tool-rule-Add-read-only")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-rule-Add-add-condition")).not.toBeInTheDocument();
    await user.click(within(row).getByTestId("tool-rule-Add-role-tenant-admin"));
    await user.click(screen.getByTestId("tool-rules-save"));
    await waitFor(() => expect(lastRequest("PUT", "/gateways/gw-support")).toBeDefined());
    const body = lastRequest("PUT", "/gateways/gw-support")!.body as { backends: { hub_item_id: string; tool_rules: unknown; timeout_seconds?: number }[] };
    expect(body.backends[0]).toMatchObject({ hub_item_id: "hub-calc", timeout_seconds: 90, tool_rules: { Add: { callers: { roles: ["tenant-admin"], groups: [], agents: ["ag-gone"] } } } });
    expect(body.backends[1].tool_rules).toEqual({});
  });

  it("백엔드가 더는 내놓지 않는 Tool 의 규칙은 줄로 남는다", async () => {
    const base = gatewayFixture();
    gatewayState.items = [{ ...base, backends: [{ ...base.backends[0], tool_rules: { Divide: { callers: { roles: ["viewer"], groups: [], agents: [] } } } }, base.backends[1]] }];
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    expect(await screen.findByTestId("tool-rule-Divide-stale")).toHaveTextContent(ko.toolRules.notOffered);
    expect(screen.getByTestId("tool-rule-Add")).toBeInTheDocument();
  });

  it("IdP 를 믿는 oidc 모드에 저장된 Agent 는 이름으로 보이고 걷을 수만 있다", async () => {
    const user = userEvent.setup();
    authConfigState.mode = "oidc";
    oidcStub.user = { access_token: "tok-oidc" };
    server.use(http.get("/auth/config", () => HttpResponse.json(oidcIdpAuthConfig)));
    const base = gatewayFixture();
    gatewayState.items = [{ ...base, backends: [{ ...base.backends[0], tool_rules: { Add: { callers: { roles: [], groups: [], agents: ["ag-support"] } } } }, base.backends[1]] }];
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    const stored = await screen.findByTestId("tool-rule-Add-agent-ag-support");
    expect(stored).toBeChecked();
    // 저장된 Agent 는 삭제된 Agent 가 아니라 제 이름이고, 새로 넣을 Agent 선택지는 없다
    await waitFor(() => expect(within(screen.getByTestId("tool-rule-Add")).queryByText(ko.toolRules.deletedAgent)).not.toBeInTheDocument());
    expect(within(screen.getByTestId("tool-rule-Add")).getAllByRole("checkbox").filter((c) => c.getAttribute("data-testid")?.includes("-agent-"))).toHaveLength(1);
    expect(screen.queryByText(ko.toolRules.agentHint)).not.toBeInTheDocument();
    await user.click(stored);
    await user.click(screen.getByTestId("tool-rules-save"));
    await waitFor(() => expect(lastRequest("PUT", "/gateways/gw-support")).toBeDefined());
    expect((lastRequest("PUT", "/gateways/gw-support")!.body as { backends: { tool_rules: unknown }[] }).backends[0].tool_rules).toEqual({});
  });
});

describe("플랫폼 토큰으로 전환한 oidc 모드의 Gateway 규칙 (P53b 6.1절)", () => {
  it("Gateway 가 플랫폼 토큰을 받으면 Agent 를 규칙에 넣을 수 있다", async () => {
    authConfigState.mode = "oidc";
    oidcStub.user = { access_token: "tok-oidc" };
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    const row = await screen.findByTestId("tool-rule-Add");
    await waitFor(() => expect(within(row).getByTestId("tool-rule-Add-agent-ag-support")).not.toBeChecked());
    expect(screen.getByTestId("tool-rules-card")).toHaveTextContent(ko.toolRules.agentHint);
  });
});

describe("고정한 Tool 목록의 안내", () => {
  it("F0b 9번이 서기 전에는 Gateway 와 항목의 고정 목록 안내를 보이지 않는다", async () => {
    // 목록에 없는 이름의 호출까지 거절하는지는 아직 재지 않았다. 지금은 tools/list 에서 빠질 뿐이다
    expect(PINNED_LIST_REFUSES_CALLS).toBe(false);
    const app = render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    await screen.findByTestId("tool-rule-Add");
    expect(screen.getByTestId("tool-rules-card")).not.toHaveTextContent(ko.toolRules.gatewayPinHint);
    expect(screen.getByTestId("tool-rules-card")).toHaveTextContent(ko.toolRules.gatewayHint);
    app.unmount();
    hubState.items = [hubItemFixture({ id: "hub-mail", kind: "image", tool_names: ["send_mail"] })];
    render(<TestApp initialPath="/hub/hub-mail?tab=tools" />);
    await screen.findByTestId("tool-rule-send_mail");
    expect(screen.getByTestId("tool-rules-card")).not.toHaveTextContent(ko.toolRules.itemPinHint);
    expect(screen.getByTestId("tool-rules-card")).toHaveTextContent(ko.toolRules.itemHint);
  });
});

describe("허브 image 항목의 Tool 규칙", () => {
  const image = () =>
    hubItemFixture({
      id: "hub-mail",
      name: "mail",
      display_name: "mail",
      kind: "image",
      source: { url: null, transport: "streamable-http", image: "ghcr.io/x/mail:1", port: 8080, instance_id: null, mcp_profile_id: null, tools_filter: null, gateway_instance_id: null },
      tool_names: ["send_mail", "list_mail"],
      tool_rules: { list_mail: { read_only: true }, old_mail: { callers: { roles: ["viewer"] } } },
      status: { ...hubItemFixture().status!, warnings: ["rules_tool_missing: some Tools with rules are no longer offered (old_mail)"] },
    });

  it("역할과 읽기 전용 표시만 저장하고 항목의 경고는 항목의 고치는 법으로 말한다", async () => {
    const user = userEvent.setup();
    hubState.items = [image()];
    server.use(
      http.put("/hub/:id/tool-rules", async ({ request }) => {
        const r = await record(request);
        return HttpResponse.json({ ...image(), tool_rules: (r.body as { rules: unknown }).rules, rules_notice: { opened: [], rolling_agents: [] } });
      }),
    );
    render(<TestApp initialPath="/hub/hub-mail?tab=tools" />);
    expect(await screen.findByTestId("hub-status-warning")).toHaveTextContent(ko.warnings.hubCodes.rules_tool_missing.text);
    const row = await screen.findByTestId("tool-rule-send_mail");
    // 항목은 역할만 받는다
    expect(within(row).queryByTestId("tool-rule-send_mail-agent-ag-support")).not.toBeInTheDocument();
    expect(screen.getByTestId("tool-rule-list_mail-read-only")).toBeChecked();
    await user.click(within(row).getByTestId("tool-rule-send_mail-role-developer"));
    await user.click(screen.getByTestId("tool-rule-list_mail-read-only"));
    // 이미지가 더는 내놓지 않는 Tool 의 규칙은 줄로 보이고, 대상을 비우면 걷힌다. 말없이 버리지 않는다
    expect(screen.getByTestId("tool-rule-old_mail-stale")).toHaveTextContent(ko.toolRules.notOffered);
    await user.click(screen.getByTestId("tool-rule-old_mail-role-viewer"));
    await user.click(screen.getByTestId("tool-rules-save"));
    await waitFor(() => expect(lastRequest("PUT", "/hub/hub-mail/tool-rules")).toBeDefined());
    // 항목의 기본값은 쓰기라 표시를 끈 것은 보내지 않는다
    expect(lastRequest("PUT", "/hub/hub-mail/tool-rules")!.body).toEqual({ rules: { send_mail: { callers: { roles: ["developer"] } } } });
  });

  it("image 가 아닌 항목에는 규칙 카드가 없다", async () => {
    hubState.items = [hubItemFixture()];
    render(<TestApp initialPath="/hub/hub-calc?tab=tools" />);
    await screen.findByTestId("hub-tools-card");
    expect(screen.queryByTestId("tool-rules-card")).not.toBeInTheDocument();
  });
});
