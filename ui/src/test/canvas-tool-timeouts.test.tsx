/**
 * MCP 노드 패널의 타임아웃 경고 (P50 7절). Agent 의 Tool 타임아웃(정하지 않으면 런타임 기본 60초)이 소스 Gateway 가 그 Tool 의
 * 백엔드를 기다리는 시간보다 짧으면 Agent 가 먼저 끊으므로 알린다. 허용 Tool 을 정했으면 그것만 보고, 경고는 한 상자에 모은다.
 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import type { Gateway } from "@/api/types";
import { ToolsPanel } from "@/components/canvas/inspector/ToolsPanel";
import { defaultNodeData, type ToolsData } from "@/lib/agentGraph";
import { gatewayToolTimeout } from "@/lib/gateway";

import { gatewayFixture } from "./msw";
import { inspectorOptions } from "./inspector";
import { renderWithProviders } from "./render";

type Backend = Gateway["deployed_backends"][number];

/** 픽스처의 calc(Add) 백엔드와, 원하면 Tool 목록을 아는 둘째 백엔드. weather(Tool 목록 모름)는 keepUnknown 일 때만 둔다. */
function gateway({ calc, second, keepUnknown = false, prefix = false }: { calc: number; second?: Partial<Backend>; keepUnknown?: boolean; prefix?: boolean }): Gateway {
  const gw = gatewayFixture();
  const [calcBackend, weather] = gw.deployed_backends;
  const backends = [{ ...calcBackend, timeout_seconds: calc }];
  if (keepUnknown) backends.push(weather);
  if (second) backends.push({ ...calcBackend, hub_item_id: "hub-search", hub_item_name: "search", workload: "gw-support-search", tools: [], tool_names: ["Find"], ...second });
  return { ...gw, deployed_backends: backends, deployed_conflict_resolution: prefix ? "prefix" : gw.deployed_conflict_resolution };
}

function Harness({ gw, allow }: { gw: Gateway; allow: string[] }) {
  const [data, setData] = useState<ToolsData>({ ...defaultNodeData("tools"), source: { kind: "gateway_instance", id: gw.id }, allow_tools: allow } as ToolsData);
  const options = inspectorOptions({ gateways: [gw] });
  return <ToolsPanel nodeId="tools-1" data={data} update={(patch) => setData((d) => ({ ...d, ...patch }))} options={options} hasMemory />;
}

describe("MCP 노드의 타임아웃 경고", () => {
  it("Gateway 가 런타임 기본값보다 오래 기다리면 알리고, Agent 타임아웃을 늘리면 사라진다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness gw={gateway({ calc: 90 })} allow={["Add"]} />);
    expect(screen.getByTestId("timeout-shorter-Add")).toHaveTextContent(
      "Add 의 타임아웃(60초)이 Gateway 가 이 Tool 을 기다리는 시간(90초)보다 짧습니다. 느린 호출은 Agent 가 먼저 끊습니다.",
    );
    await user.click(screen.getByTestId("inspector-tabs-timeouts"));
    await user.type(screen.getByTestId("timeout-Add"), "120");
    expect(screen.queryByTestId("timeout-shorter")).not.toBeInTheDocument();
  });

  it("Gateway 가 더 짧게 기다리면 알리지 않는다", () => {
    renderWithProviders(<Harness gw={gateway({ calc: 35 })} allow={[]} />);
    expect(screen.queryByTestId("timeout-shorter")).not.toBeInTheDocument();
  });

  it("두 백엔드는 각자의 타임아웃으로 보고 경고를 한 상자에 모은다. 허용 Tool 을 정했으면 그것만 본다", () => {
    const gw = gateway({ calc: 90, second: { timeout_seconds: 100 } });
    const { unmount } = renderWithProviders(<Harness gw={gw} allow={[]} />);
    const box = screen.getByTestId("timeout-shorter");
    expect(box).toContainElement(screen.getByTestId("timeout-shorter-Add"));
    expect(box).toContainElement(screen.getByTestId("timeout-shorter-Find"));
    expect(screen.getByTestId("timeout-shorter-Find")).toHaveTextContent("(100초)");
    unmount();
    renderWithProviders(<Harness gw={gw} allow={["Find"]} />);
    expect(screen.queryByTestId("timeout-shorter-Add")).not.toBeInTheDocument();
    expect(screen.getByTestId("timeout-shorter-Find")).toBeInTheDocument();
  });

  it("prefix 모드는 <workload>_<tool> 이름으로 백엔드를 찾는다", () => {
    renderWithProviders(<Harness gw={gateway({ calc: 90, prefix: true })} allow={["gw-support-calc_Add"]} />);
    expect(screen.getByTestId("timeout-shorter-gw-support-calc_Add")).toHaveTextContent("(90초)");
  });
});

describe("gatewayToolTimeout", () => {
  it("광고하지 않는 이름은 Tool 목록을 모르는 백엔드가 있을 때만 그 백엔드의 값이다", () => {
    expect(gatewayToolTimeout(gateway({ calc: 90 }), "Gone")).toBeNull();
    const unknown = gateway({ calc: 35, keepUnknown: true });
    expect(gatewayToolTimeout(unknown, "Gone")).toBe(60);
    expect(gatewayToolTimeout(unknown, "Add")).toBe(35);
  });

  it("바꾼 이름(tool_overrides)으로 찾고, 배포한 백엔드가 없으면 null 이다", () => {
    const gw = gateway({ calc: 90 });
    const renamed = { ...gw, deployed_conflict_resolution: "priority" as const, tool_overrides: { "gw-support-calc": { Add: "calc_Add" } } };
    expect(gatewayToolTimeout(renamed, "calc_Add")).toBe(90);
    expect(gatewayToolTimeout(renamed, "Add")).toBeNull();
    expect(gatewayToolTimeout({ ...gw, deployed_backends: [] }, "Add")).toBeNull();
  });

  it("빈 Gateway 를 소스로 고르면 경고가 없다", () => {
    renderWithProviders(<Harness gw={{ ...gateway({ calc: 90 }), deployed_backends: [] }} allow={["Add"]} />);
    expect(screen.queryByTestId("timeout-shorter")).not.toBeInTheDocument();
  });
});
