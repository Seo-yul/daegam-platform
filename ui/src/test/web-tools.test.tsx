/**
 * 플랫폼 웹 Tool 의 화면 (P39b 14·15절). tools 노드의 효과 배지, 확인을 받는 읽기 전용 표시, 웹 Tool 이
 * 있는 노드의 도메인 칸, 웹 Tool 이 없는 캔버스의 안내, 채팅 Tool 카드의 "외부 웹 내용" 배지, 설정의 웹 Tool 절 PATCH 와 키·연결
 * 확인, 허브 상세와 Gateway 폼의 플랫폼 항목을 든다.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { TestApp } from "@/App";
import type { PlatformSettings, ToolEffect } from "@/api/types";
import { ToolsPanel } from "@/components/canvas/inspector/ToolsPanel";
import { ToolCallView } from "@/components/chat/ChatItemView";
import { ko } from "@/i18n/ko";
import { defaultNodeData, type ToolsData } from "@/lib/agentGraph";
import type { ToolOrigin } from "@/lib/toolSafety";
import { WebToolsSection, webPatch } from "@/pages/admin/WebToolsSettings";

import { HttpResponse, dataPlaneAuth, gatewayFixture, hubItemFixture, hubState, http, lastRequest, platformSettings, record, server } from "./msw";
import { inspectorOptions } from "./inspector";
import { renderWithProviders } from "./render";

const text = ko.canvas.tools;

function Harness({ effects, start }: { effects?: Record<string, Record<string, ToolEffect>>; start?: Partial<ToolsData> }) {
  const gw = gatewayFixture();
  const [data, setData] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id }, ...start } as ToolsData);
  // 검사는 효과와 함께 백엔드 종류를 준다. 읽기 전용 표시는 서버가 알려 주지 않는 Tool(remote 항목의 Add)에만 효과가 있다
  const options = inspectorOptions({ gateways: [gw], toolEffects: effects, toolOrigins: effects ? ORIGINS : undefined });
  return (
    <>
      <ToolsPanel nodeId="tools-1" data={data} update={(patch) => setData((d) => ({ ...d, ...patch }))} options={options} hasMemory />
      <output data-testid="data">{JSON.stringify(data)}</output>
    </>
  );
}

const WEB = { "tools-1": { Add: "write", web_fetch: "web" } } satisfies Record<string, Record<string, ToolEffect>>;
const ORIGINS = { "tools-1": { Add: "remote", web_fetch: "platform" } } satisfies Record<string, Record<string, ToolOrigin>>;

describe("tools 노드의 웹 Tool 표시", () => {
  it("검사 전에는 효과를 모른다고 알리고, 검사 뒤에는 효과를 단다", () => {
    const { unmount } = renderWithProviders(<Harness />);
    expect(screen.getByTestId("tools-effects-unknown")).toHaveTextContent(text.effectsUnknown);
    unmount();
    renderWithProviders(<Harness effects={WEB} start={{ allow_tools: ["Add", "web_fetch"] }} />);
    expect(screen.getByTestId("tool-effect-Add")).toHaveTextContent(text.effect.write);
    expect(screen.getByTestId("tool-effect-web_fetch")).toHaveTextContent(text.effect.web);
    expect(screen.queryByTestId("tools-web-hint")).toBeNull();
  });

  it("읽기 전용 표시는 확인을 받은 뒤에만 저장한다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness effects={WEB} start={{ allow_tools: ["Add", "web_fetch"] }} />);
    await user.click(screen.getByTestId("tool-read-only-Add"));
    expect(screen.getByTestId("tool-read-only-apply")).toBeDisabled();
    await user.click(screen.getByTestId("tool-read-only-confirm"));
    await user.click(screen.getByTestId("tool-read-only-apply"));
    expect(JSON.parse(screen.getByTestId("data").textContent ?? "{}").read_only).toEqual(["Add"]);
  });

  it("웹 Tool 이 있는 노드는 도메인 칸을 보이고 소문자 목록으로 저장한다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness effects={WEB} />);
    await user.type(screen.getByTestId("tools-allow-domains"), "Docs.Example.com, wiki.example.org");
    expect(JSON.parse(screen.getByTestId("data").textContent ?? "{}").allow_domains).toEqual(["docs.example.com", "wiki.example.org"]);
  });

  it("웹 Tool 이 없는 캔버스는 플랫폼 Tool 을 Gateway 에 넣으라고 안내한다", () => {
    renderWithProviders(<Harness effects={{ "tools-1": { Add: "write" } }} />);
    expect(screen.getByTestId("tools-web-hint")).toHaveTextContent(text.webHint);
    expect(screen.getByTestId("tools-web-hint-link")).toHaveAttribute("href", "/gateways/new?backend=web-search&backend=web-fetch");
    expect(screen.queryByTestId("tools-domains")).toBeNull();
  });
});

describe("채팅 Tool 카드", () => {
  it("웹 Tool 의 결과에 외부 웹 내용 배지를 단다", () => {
    const item = { kind: "tool_call" as const, call: { id: "c1", name: "web_fetch", args: {} }, result: { id: "c1", name: "web_fetch", status: "success" as const, content: "x", untrusted: "web" as const } };
    renderWithProviders(<ToolCallView item={item} testId="tool" />);
    expect(screen.getByTestId("tool-untrusted")).toHaveTextContent(ko.chat.tool.untrusted);
  });
});

describe("설정의 웹 Tool 절", () => {
  it("바뀐 절만 통째로 싣는다", () => {
    const orig = platformSettings as PlatformSettings;
    expect(webPatch(orig, { tools: structuredClone(orig.web_tools), caBundle: "", apiKey: "" })).toEqual({});
    const tools = structuredClone(orig.web_tools);
    tools.search!.enabled = true;
    const patch = webPatch(orig, { tools, caBundle: "-----BEGIN CERTIFICATE-----", apiKey: "" });
    expect(patch.web_tools?.search?.enabled).toBe(true);
    expect(patch.web_tools?.fetch).toEqual(orig.web_tools.fetch);
    expect(patch.network).toEqual({ ca_bundle: "-----BEGIN CERTIFICATE-----" });
  });
});

const webSearchItem = hubItemFixture({
  id: "hub-web-search",
  tenant: "",
  name: "web-search",
  display_name: "Web Search",
  kind: "platform",
  cluster_id: "",
  source: { ...hubItemFixture().source, url: "http://issuer/api/internal/mcp/web-search", transport: "streamable-http", instance_id: null, mcp_profile_id: null },
  k8s: { kind: null, name: "web-search", namespace: "", group: null, tool_config: null },
  tool_names: ["web_search"],
  status: { ...hubItemFixture().status!, external_url: null },
});

describe("허브와 Gateway 의 플랫폼 항목", () => {
  it("상세는 종류를 플랫폼으로 보이고 새로고침·삭제·MCP 주소가 없다", async () => {
    hubState.items = [...hubState.items, webSearchItem];
    render(<TestApp initialPath="/hub/hub-web-search" />);
    await screen.findByTestId("hub-detail-page");
    expect(screen.getByTestId("hub-kind")).toHaveTextContent(ko.hub.detail.platform);
    expect(screen.queryByTestId("refresh-tools-button")).toBeNull();
    expect(screen.queryByTestId("delete-button")).toBeNull();
    expect(screen.queryByTestId("hub-mcp-url")).toBeNull();
  });

  it("캔버스의 안내가 보낸 ?backend= 는 그 플랫폼 항목을 미리 골라 맨 앞에 둔다", async () => {
    hubState.items = [hubItemFixture(), webSearchItem];
    render(<TestApp initialPath="/gateways/new?backend=web-search" />);
    expect(await screen.findByTestId("backend-web-search-priority")).toHaveTextContent("#1");
    expect(screen.queryByTestId("backend-calc-priority")).toBeNull();
  });
});

describe("설정의 웹 Tool 카드 저장", () => {
  it("키는 PUT 으로 따로 넣고, 저장한 뒤에만 연결 확인을 보낸다. 위 폼의 고친 값은 그대로 남는다", async () => {
    const user = userEvent.setup();
    server.use(
      http.put("/settings/web-tools/search/api-key", async ({ request }) => {
        await record(request);
        return HttpResponse.json({ api_key_secret: "daegam-web-search-key" });
      }),
      http.post("/settings/web-tools/search/check", async ({ request }) => {
        await record(request);
        return HttpResponse.json({ ok: true, code: "ok", latency_ms: 120 });
      }),
    );
    render(<TestApp initialPath="/admin/settings" />);
    const model = await screen.findByTestId("ps-model");
    await user.clear(model);
    await user.type(model, "claude-sonnet-5");
    await user.type(screen.getByTestId("ps-web-search-key"), "brave-key");
    expect(screen.getByTestId("ps-web-search-key")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.getByTestId("ps-web-search-check")).toBeDisabled();
    await user.click(screen.getByTestId("ps-web-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/web-tools/search/api-key")?.body).toEqual({ api_key: "brave-key" }));
    expect(lastRequest("PATCH", "/settings")).toBeUndefined();
    await waitFor(() => expect(screen.getByTestId("ps-web-search-check")).toBeEnabled());
    await user.click(screen.getByTestId("ps-web-search-check"));
    expect(await screen.findByTestId("ps-web-search-check-result")).toHaveTextContent(ko.admin.platformSettings.webTools.checkResult.ok);
    expect(screen.getByTestId("ps-model")).toHaveValue("claude-sonnet-5");
  });
});

describe("설정의 네트워크 카드", () => {
  it("지금 CA bundle 과 다른 CA 로 배포된 MCP Server 가 있으면 그 수와 재배포 안내를 보인다", async () => {
    server.use(http.get("/settings/data-plane-auth", () => HttpResponse.json({ ...dataPlaneAuth, mcp_servers_stale_ca: 2 })));
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-network-stale-ca")).toHaveTextContent(ko.admin.platformSettings.network.staleCa(2));
    expect(screen.getByText(ko.admin.platformSettings.network.caHint)).toBeInTheDocument();
  });

  it("네트워크 카드를 저장하면 다른 CA 로 도는 서버 수를 다시 읽는다", async () => {
    const user = userEvent.setup();
    let stale = 2;
    server.use(
      http.get("/settings/data-plane-auth", () => HttpResponse.json({ ...dataPlaneAuth, mcp_servers_stale_ca: stale })),
      http.patch("/settings", async ({ request }) => {
        const r = await record(request);
        stale = 0;
        return HttpResponse.json({ ...platformSettings, ...(r.body as object) });
      }),
    );
    render(<TestApp initialPath="/admin/settings" />);
    expect(await screen.findByTestId("ps-network-stale-ca")).toBeInTheDocument();
    await user.type(screen.getByTestId("ps-network-ca"), "-----BEGIN CERTIFICATE-----");
    await user.click(screen.getByTestId("ps-web-save"));
    await waitFor(() => expect(screen.queryByTestId("ps-network-stale-ca")).toBeNull());
    expect(lastRequest("PATCH", "/settings")).toBeDefined();
  });

  it("GET /me 로 platform-admin 인 것을 알기 전에는 서버 수를 묻지 않는다", async () => {
    renderWithProviders(<WebToolsSection orig={platformSettings} readOnly={false} />);
    expect(await screen.findByTestId("ps-network-ca")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(lastRequest("GET", "/settings/data-plane-auth")).toBeUndefined();
  });

  it("다시 배포할 서버가 없으면 안내를 보이지 않는다", async () => {
    render(<TestApp initialPath="/admin/settings" />);
    await screen.findByTestId("ps-network-ca");
    await waitFor(() => expect(lastRequest("GET", "/settings/data-plane-auth")).toBeDefined());
    expect(screen.queryByTestId("ps-network-stale-ca")).toBeNull();
  });
});
