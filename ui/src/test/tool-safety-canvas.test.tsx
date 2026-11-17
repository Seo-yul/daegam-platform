/**
 * 바깥 내용 표시와 그 뒤의 승인을 캔버스와 채팅이 보이는 것 (P43 2·4·8절, lib/toolSafety.ts).
 *  - tools 노드: 외부 내용을 가져오는 Tool 선택(백엔드 종류의 기본값, 웹·모르는 이름은 고정), 모든 그래프의 읽기 전용 표시와
 *    적용되지 않는 표시의 안내, memory 노드에 따른 승인·거절 배지, 이름을 아는 노드에서 승인 설정이 있는 Tool 의 승인 기억 켜기.
 *  - knowledge 노드의 외부 내용으로 다루기.
 *  - 채팅 도구 카드의 외부 내용 배지와 그 안내.
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import type { ToolEffect } from "@/api/types";
import { Inspector } from "@/components/canvas/Inspector";
import { ToolsPanel } from "@/components/canvas/inspector/ToolsPanel";
import { makeEdge, makeNode, type CanvasState } from "@/components/canvas/types";
import { ToolCallView, ToolSafetyScope } from "@/components/chat";
import { dictionary } from "@/i18n";
import { DEFAULT_SERVICE, defaultNodeData, type KnowledgeData, type ToolsData } from "@/lib/agentGraph";
import { canvasMarkApplies, isOutside, mainHasMemory, outsideFixed, withOutside, type ToolOrigin } from "@/lib/toolSafety";

import { inspectorOptions } from "./inspector";
import { gatewayFixture } from "./msw";
import { renderWithProviders } from "./render";

const ko = dictionary("ko");
const text = ko.canvas.tools;
const NODE = "tools-1";
// 우리 Tool 의 읽기·쓰기, remote 항목의 쓰기, 웹 Tool, 백엔드 종류를 모르는 이름
const EFFECTS: Record<string, ToolEffect> = { list_orders: "read", create_order: "write", send_mail: "write", web_fetch: "web", ghost: "write" };
const ORIGINS: Record<string, ToolOrigin> = { list_orders: "internal", create_order: "internal", send_mail: "remote", web_fetch: "platform" };

function Harness({ start, hasMemory = true, effects = EFFECTS }: { start?: Partial<ToolsData>; hasMemory?: boolean; effects?: Record<string, ToolEffect> }) {
  const gw = gatewayFixture();
  // 배지는 허용 목록의 줄에 달리므로 검사가 본 이름을 모두 고른다
  const [data, setData] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id }, allow_tools: Object.keys(EFFECTS), ...start } as ToolsData);
  const options = inspectorOptions({ gateways: [gw], toolEffects: { [NODE]: effects }, toolOrigins: { [NODE]: ORIGINS } });
  return (
    <>
      <ToolsPanel nodeId={NODE} data={data} update={(patch) => setData((d) => ({ ...d, ...patch }))} options={options} hasMemory={hasMemory} />
      <output data-testid="data">{JSON.stringify(data)}</output>
    </>
  );
}

function stored(): ToolsData {
  return JSON.parse(screen.getByTestId("data").textContent ?? "{}") as ToolsData;
}

describe("lib/toolSafety", () => {
  it("바깥 표시는 백엔드 종류가 기본값이고, 웹 Tool 과 모르는 이름은 늘 바깥이다", () => {
    expect(isOutside("create_order", "internal", "write", undefined)).toBe(false);
    expect(isOutside("send_mail", "remote", "write", undefined)).toBe(true);
    expect(isOutside("create_order", "internal", "write", { create_order: true })).toBe(true);
    expect(isOutside("web_fetch", "platform", "web", { web_fetch: false })).toBe(true);
    expect(outsideFixed(undefined, "write") && outsideFixed("platform", "web") && !outsideFixed("internal", "write")).toBe(true);
    // code mode 스크립트는 읽기여도 늘 바깥이다 (P56 6.5절)
    expect(outsideFixed("script", "read")).toBe(true);
    expect(isOutside("execute_tool_script", "script", "read", { execute_tool_script: false })).toBe(true);
    // 기본값으로 돌아가면 키를 지운다
    expect(withOutside({ create_order: true }, "create_order", "internal", false)).toEqual({});
    expect(withOutside(undefined, "send_mail", "remote", false)).toEqual({ send_mail: false });
    expect(canvasMarkApplies("remote") && canvasMarkApplies("image") && !canvasMarkApplies("internal") && !canvasMarkApplies(undefined)).toBe(true);
    // 서버나 허브 항목의 표시가 서는 Tool 에서는 캔버스의 표시가 세지 않는다 (P52 4절)
    expect(canvasMarkApplies("image", true) || canvasMarkApplies("remote", true)).toBe(false);
  });

  it("memory 노드는 메인 에이전트에 붙어야 센다", () => {
    const nodes = [makeNode("agent", "agent", defaultNodeData("agent"), { x: 0, y: 0 }), makeNode("memory", "memory", defaultNodeData("memory"), { x: 0, y: 0 })];
    expect(mainHasMemory(nodes, [])).toBe(false);
    expect(mainHasMemory(nodes, [makeEdge("e-m", "agent", "memory", "memory")])).toBe(true);
  });
});

describe("tools 노드의 외부 내용 표시", () => {
  it("외부 내용을 가져오는 Tool 은 백엔드 종류로 미리 고르고, 바꾸면 기본값과 다른 것만 저장한다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    const section = screen.getByTestId("tools-outside");
    expect(section).toHaveTextContent(text.outside);
    expect(screen.getByTestId("tool-outside-create_order")).not.toBeChecked();
    expect(screen.getByTestId("tool-outside-send_mail")).toBeChecked();
    expect(screen.getByTestId("tool-outside-web_fetch")).toBeChecked();
    expect(screen.getByTestId("tool-outside-web_fetch")).toBeDisabled();
    expect(screen.getByTestId("tool-outside-ghost")).toBeDisabled();
    await user.click(screen.getByTestId("tool-outside-create_order"));
    expect(stored().outside).toEqual({ create_order: true });
    await user.click(screen.getByTestId("tool-outside-create_order"));
    expect(stored().outside).toEqual({});
  });

  it("code mode 스크립트는 외부 내용으로 고정되어 바꿀 수 없다 (P56 6.5절)", () => {
    const gw = gatewayFixture();
    function Script() {
      const [data] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id }, allow_tools: ["Add", "execute_tool_script"] } as ToolsData);
      const options = inspectorOptions({
        gateways: [gw],
        toolEffects: { [NODE]: { Add: "read", execute_tool_script: "read" } },
        toolOrigins: { [NODE]: { Add: "internal", execute_tool_script: "script" } },
      });
      return <ToolsPanel nodeId={NODE} data={data} update={() => undefined} options={options} hasMemory />;
    }
    renderWithProviders(<Script />);
    expect(screen.getByTestId("tool-outside-execute_tool_script")).toBeChecked();
    expect(screen.getByTestId("tool-outside-execute_tool_script")).toBeDisabled();
    expect(screen.getByTestId("tool-outside-Add")).not.toBeDisabled();
  });

  it("승인 설정이 없는 쓰기 Tool 은 memory 노드가 있으면 승인, 없으면 거절 배지다", () => {
    const { unmount } = renderWithProviders(<Harness start={{ approval: { send_mail: ["approve"] } }} />);
    expect(screen.getByTestId("tool-outside-approval-create_order")).toHaveTextContent(text.outsideApproval);
    expect(screen.getByTestId("tool-outside-approval-create_order")).toHaveAttribute("title", text.outsideApprovalHint);
    expect(screen.queryByTestId("tool-outside-approval-send_mail")).toBeNull();
    expect(screen.queryByTestId("tool-outside-approval-list_orders")).toBeNull();
    unmount();
    renderWithProviders(<Harness hasMemory={false} />);
    expect(screen.getByTestId("tool-outside-refused-create_order")).toHaveTextContent(text.outsideRefused);
    expect(screen.getByTestId("tool-outside-refused-create_order")).toHaveAttribute("title", text.outsideRefusedHint);
  });
});

describe("tools 노드의 읽기 전용 표시", () => {
  it("웹 Tool 이 없는 그래프에서도 서버가 알려 주지 않는 Tool 만 표시할 수 있다", () => {
    const gw = gatewayFixture();
    function NoWeb() {
      const [data] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id } } as ToolsData);
      const options = inspectorOptions({ gateways: [gw], toolEffects: { [NODE]: { create_order: "write", send_mail: "write" } }, toolOrigins: { [NODE]: ORIGINS } });
      return <ToolsPanel nodeId={NODE} data={data} update={() => undefined} options={options} hasMemory />;
    }
    renderWithProviders(<NoWeb />);
    const section = screen.getByTestId("tools-read-only");
    expect(section).toHaveTextContent(text.readOnlyHintOutside);
    expect(screen.getByTestId("tool-read-only-send_mail")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-read-only-create_order")).toBeNull();
  });

  it("서버가 정하는 Tool 에 남은 표시는 적용되지 않는다고 알린다", () => {
    renderWithProviders(<Harness start={{ read_only: ["create_order", "send_mail"] }} />);
    expect(screen.getByTestId("tool-read-only-ignored-create_order")).toHaveTextContent(text.readOnlyIgnored);
    expect(screen.queryByTestId("tool-read-only-ignored-send_mail")).toBeNull();
  });

  it("규칙이 있는 허브 항목의 Tool 은 표시할 수 없고 남은 표시는 적용되지 않는다 (P52 4절)", () => {
    const gw = gatewayFixture();
    const origins: Record<string, ToolOrigin> = { send_mail: "image", fetch: "image" };
    function Marked({ start }: { start?: Partial<ToolsData> }) {
      const [data] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id }, ...start } as ToolsData);
      const options = inspectorOptions({ gateways: [gw], toolEffects: { [NODE]: { send_mail: "write", fetch: "write" } }, toolOrigins: { [NODE]: origins }, toolMarked: { [NODE]: ["send_mail"] } });
      return <ToolsPanel nodeId={NODE} data={data} update={() => undefined} options={options} hasMemory />;
    }
    const { unmount } = renderWithProviders(<Marked />);
    expect(screen.getByTestId("tool-read-only-fetch")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-read-only-send_mail")).toBeNull();
    unmount();
    renderWithProviders(<Marked start={{ read_only: ["send_mail", "fetch"] }} />);
    expect(screen.getByTestId("tool-read-only-ignored-send_mail")).toHaveTextContent(text.readOnlyIgnored);
    expect(screen.queryByTestId("tool-read-only-ignored-fetch")).toBeNull();
  });
});

describe("tools 노드의 승인 기억 켜기", () => {
  it("승인 설정이 있는 Tool 에만 있고, 설정을 지우면 함께 지운다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness start={{ approval: { create_order: ["approve"] } }} />);
    await user.click(screen.getByRole("tab", { name: text.approval }));
    expect(screen.getByText(text.rememberableHint)).toBeInTheDocument();
    expect(screen.queryByTestId("approval-send_mail-rememberable")).toBeNull();
    await user.click(screen.getByTestId("approval-create_order-rememberable"));
    expect(stored().rememberable).toEqual(["create_order"]);
    await user.click(screen.getByTestId("approval-create_order-approve"));
    expect(stored().approval).toEqual({});
    expect(stored().rememberable).toEqual([]);
  });

  it("이름을 모르는 노드에서는 승인 기억이 효과가 없어 보이지 않는다", async () => {
    const user = userEvent.setup();
    // 고른 Tool 이 없고 검사가 소스의 이름을 보지 못했다(접두사 모드의 Gateway 등)
    renderWithProviders(<Harness effects={{}} start={{ allow_tools: [], approval: { create_order: ["approve"] } }} />);
    await user.click(screen.getByRole("tab", { name: text.approval }));
    expect(screen.getByTestId("approval-create_order-approve")).toBeChecked();
    expect(screen.queryByTestId("approval-create_order-rememberable")).toBeNull();
  });
});

describe("knowledge 노드의 외부 내용으로 다루기", () => {
  function KnowledgeHarness() {
    const data: KnowledgeData = { ...defaultNodeData("knowledge"), knowledge_base_id: "kb-faq" };
    const [state, setState] = useState<CanvasState>({ nodes: [makeNode("kn", "knowledge", data, { x: 0, y: 0 })], edges: [] });
    const options = inspectorOptions();
    return (
      <>
        <Inspector
          state={state}
          selection={{ nodeId: "kn", edgeId: null }}
          issues={[]}
          options={options}
          onChangeNodeData={(id, next) => setState((s) => ({ ...s, nodes: s.nodes.map((n) => (n.id === id ? { ...n, data: next as unknown as Record<string, unknown> } : n)) }))}
          onCommit={() => undefined}
          onDeleteSelection={() => undefined}
          service={DEFAULT_SERVICE}
          onChangeService={() => undefined}
          names={{ displayName: "", name: "" }}
          onChangeNames={() => undefined}
          readOnlyName={false}
        />
        <output data-testid="kn-data">{JSON.stringify(state.nodes[0].data)}</output>
      </>
    );
  }

  it("기본은 켜져 있고 끄면 outside 를 거짓으로 저장한다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<KnowledgeHarness />);
    const toggle = screen.getByTestId("knowledge-outside");
    expect(toggle).toBeChecked();
    expect(screen.getByText(ko.canvas.knowledge.outsideHint)).toBeInTheDocument();
    await user.click(toggle);
    expect(JSON.parse(screen.getByTestId("kn-data").textContent ?? "{}").outside).toBe(false);
  });
});

describe("채팅 도구 카드의 외부 내용 배지", () => {
  function card(untrusted: "web" | "outside") {
    return { kind: "tool_call" as const, call: { id: "c1", name: "read_mail", args: {} }, result: { id: "c1", name: "read_mail", status: "success" as const, content: "x", untrusted } };
  }

  it("외부 내용은 외부 내용 배지다", () => {
    renderWithProviders(<ToolCallView item={card("outside")} testId="tool" />);
    expect(screen.getByTestId("tool-outside")).toHaveTextContent(ko.chat.tool.outside);
    expect(screen.getByTestId("tool-outside")).toHaveAttribute("title", ko.chat.tool.outsideHint);
  });

  it("쓰기가 승인을 받는 런타임이면 두 배지의 안내에 한 줄을 더한다", () => {
    renderWithProviders(
      <ToolSafetyScope on>
        <div data-testid="cards">
          <ToolCallView item={card("web")} testId="web" />
          <ToolCallView item={card("outside")} testId="out" />
        </div>
      </ToolSafetyScope>,
    );
    const cards = within(screen.getByTestId("cards"));
    expect(cards.getByTestId("web-untrusted")).toHaveAttribute("title", `${ko.chat.tool.untrustedHint} ${ko.chat.tool.writeNeedsApproval}`);
    expect(cards.getByTestId("out-outside")).toHaveAttribute("title", `${ko.chat.tool.outsideHint} ${ko.chat.tool.writeNeedsApproval}`);
  });
});
