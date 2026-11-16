/**
 * 코드 실행(P40) 화면. 메인 에이전트의 코드 실행 칸과 명령 실행 전 승인, 켤 수 없을 때의 까닭(파일 Tool 이 꺼짐, 클러스터 게이트가
 * 닫힘), Cluster 화면의 격리 카드(점검, 결과, 용량, 메모리 한도 없이 센 파드, 노드를 함께 씀과 허용), 게이트의 409 문장, 세션 파일의
 * "작업 공간에서 만든 파일" 묶음이다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { TestApp } from "@/App";
import { ApiError } from "@/api/client";
import { Inspector, type InspectorOptions } from "@/components/canvas/Inspector";
import { makeNode, type CanvasState } from "@/components/canvas/types";
import { describeError } from "@/components/ErrorMessage";
import { dictionary } from "@/i18n";
import { DEFAULT_SERVICE, defaultNodeData, validateGraph, type AgentData } from "@/lib/agentGraph";
import { SANDBOX_COMMAND_MAX_SECONDS } from "@/lib/codeExecution";
import { sandboxResult } from "@/pages/clusters/SandboxCard";

import { inspectorOptions } from "./inspector";
import { clusterHome, clusterSandboxFixture, filledTemplateGraph, http, HttpResponse, lastRequest, mockValidationContext, server } from "./msw";
import { renderWithProviders } from "./render";

const ko = dictionary("ko");
const en = dictionary("en");

function mainAgent(over: Partial<AgentData> = {}): CanvasState {
  return { nodes: [makeNode("agent", "agent", { ...defaultNodeData("agent"), ...over }, { x: 0, y: 0 })], edges: [] };
}

function Harness({ initial, opts, seen }: { initial: CanvasState; opts: InspectorOptions; seen: AgentData[] }) {
  const [state, setState] = useState(initial);
  return (
    <Inspector
      state={state}
      selection={{ nodeId: "agent", edgeId: null }}
      issues={[]}
      options={opts}
      onChangeNodeData={(id, data) => {
        seen.push(data as AgentData);
        setState((s) => ({ ...s, nodes: s.nodes.map((n) => (n.id === id ? { ...n, data: data as unknown as Record<string, unknown> } : n)) }));
      }}
      onCommit={() => undefined}
      onDeleteSelection={() => undefined}
      service={DEFAULT_SERVICE}
      onChangeService={() => undefined}
      names={{ displayName: "", name: "" }}
      onChangeNames={() => undefined}
      readOnlyName={false}
    />
  );
}

async function openTools(initial: CanvasState, opts: InspectorOptions = inspectorOptions()) {
  const user = userEvent.setup();
  const seen: AgentData[] = [];
  renderWithProviders(<Harness initial={initial} opts={opts} seen={seen} />);
  await user.click(screen.getByTestId("inspector-tabs-tools"));
  return { user, seen };
}

describe("코드 실행 칸 (P40 9절)", () => {
  it("켜면 칸이 생기고 승인을 고르면 승인·거부로 묻고 끄면 칸이 사라진다", async () => {
    const { user, seen } = await openTools(mainAgent());
    const on = screen.getByTestId("agent-code-execution-on");
    expect(within(screen.getByTestId("agent-code-execution")).getByText(ko.canvas.codeExecution.label)).toBeInTheDocument();
    expect(screen.queryByTestId("agent-code-execution-approval")).not.toBeInTheDocument();
    await user.click(on);
    expect(seen.at(-1)?.code_execution).toEqual({ approval: [] });
    await user.click(screen.getByTestId("agent-code-execution-approval"));
    expect(seen.at(-1)?.code_execution).toEqual({ approval: ["approve", "reject"] });
    await user.click(screen.getByTestId("agent-code-execution-on"));
    expect(seen.at(-1)?.code_execution).toBeUndefined();
    expect(JSON.parse(JSON.stringify(seen.at(-1)))).not.toHaveProperty("code_execution");
  });

  it("파일 Tool 이 꺼졌으면 켤 수 없고 그 까닭을 보인다", async () => {
    await openTools(mainAgent({ builtin_tools: { filesystem: false } }));
    expect(screen.getByTestId("agent-code-execution-on")).toBeDisabled();
    expect(screen.getByTestId("agent-code-execution-reason")).toHaveTextContent(ko.canvas.codeExecution.filesystemReason);
  });

  it("클러스터 게이트가 닫혔으면 켤 수 없고 까닭을 보이지만 켜진 칸은 끌 수 있다", async () => {
    await openTools(mainAgent(), inspectorOptions({ codeExecutionGate: "unaccepted" }));
    expect(screen.getByTestId("agent-code-execution-on")).toBeDisabled();
    expect(screen.getByTestId("agent-code-execution-reason")).toHaveTextContent(ko.clusters.sandbox.reason.unaccepted);
  });

  it("켜진 칸은 게이트가 닫혀도 끌 수 있다", async () => {
    await openTools(mainAgent({ code_execution: { approval: [] } }), inspectorOptions({ codeExecutionGate: "capacity" }));
    expect(screen.getByTestId("agent-code-execution-on")).toBeEnabled();
  });

  it("서브 에이전트에는 칸이 없다", async () => {
    const sub = { nodes: [makeNode("agent", "agent", defaultNodeData("agent", "sub"), { x: 0, y: 0 })], edges: [] };
    await openTools(sub);
    expect(screen.queryByTestId("agent-code-execution")).not.toBeInTheDocument();
  });
});

describe("격리 카드 (P40 8절)", () => {
  it("점검 결과의 문장을 고른다", () => {
    const base = clusterHome.sandbox;
    const at = "2026-11-09T00:00:00Z";
    expect(sandboxResult(ko, base)).toBeNull();
    expect(sandboxResult(ko, { ...base, run: { ...base.run, error: "pull" } })?.text).toBe(ko.clusters.sandbox.couldNotRun("pull"));
    expect(sandboxResult(ko, { ...base, check: { ...base.check, passed: true, checked_at: at } })?.variant).toBe("ok");
    expect(sandboxResult(ko, { ...base, check: { ...base.check, passed: true, checked_at: at }, void_reason: "placement" })?.text).toBe(ko.clusters.sandbox.voided);
    expect(sandboxResult(ko, { ...base, check: { ...base.check, checked_at: at, void_reason: "self_check" } })?.text).toBe(ko.clusters.sandbox.failedSelfCheck);
    expect(sandboxResult(ko, { ...base, check: { ...base.check, checked_at: at, userns: false } })?.text).toBe(ko.clusters.sandbox.failedUserns);
    expect(sandboxResult(en, { ...base, check: { ...base.check, checked_at: at, userns: true, network: false } })?.text).toBe(en.clusters.sandbox.failedNetwork);
  });

  it("용량, 메모리 한도 없이 센 파드, 게이트의 까닭을 보이고 점검을 시작한다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters/cl-home" />);
    const card = await screen.findByTestId("cluster-sandbox-card");
    expect(within(card).getByText(ko.clusters.sandbox.title)).toBeInTheDocument();
    expect(await within(card).findByTestId("sandbox-capacity")).toHaveTextContent(ko.clusters.sandbox.capacity(3));
    expect(within(card).getByTestId("sandbox-assumed")).toHaveTextContent(ko.clusters.sandbox.assumedLimits(2));
    expect(within(card).getByTestId("sandbox-assumed")).toHaveTextContent("daegam-default/a");
    expect(within(card).getByTestId("sandbox-gate-reason")).toHaveTextContent(ko.clusters.sandbox.reason.unchecked);
    await user.click(within(card).getByTestId("sandbox-check-button"));
    await waitFor(() => expect(lastRequest("POST", "/clusters/cl-home/sandbox/check")).toBeTruthy());
  });

  it("노드를 함께 쓰는 통과한 판정은 허용을 받는다", async () => {
    const user = userEvent.setup();
    const at = "2026-11-09T00:00:00Z";
    const passed = { ...clusterHome, sandbox: { ...clusterHome.sandbox, check: { ...clusterHome.sandbox.check, passed: true, shared_nodes: true, checked_at: at } } };
    server.use(
      http.get("/clusters/cl-home", () => HttpResponse.json(passed)),
      http.get("/clusters/cl-home/sandbox", () => HttpResponse.json(clusterSandboxFixture({ gate: { open: false, reason: "unaccepted" }, shared_nodes: true }))),
    );
    render(<TestApp initialPath="/clusters/cl-home" />);
    const card = await screen.findByTestId("cluster-sandbox-card");
    expect(await within(card).findByTestId("sandbox-shared-nodes")).toHaveTextContent(ko.clusters.sandbox.sharedNodes);
    await user.click(within(card).getByTestId("sandbox-accept"));
    await waitFor(() => expect(lastRequest("POST", "/clusters/cl-home/sandbox/accept")?.body).toEqual({ checked_at: at }));
  });

  it("허용한 판정은 누가 언제 허용했는지 보인다", async () => {
    const at = "2026-11-09T00:00:00Z";
    const check = { ...clusterHome.sandbox.check, passed: true, shared_nodes: true, checked_at: at, accepted_by: "root", accepted_at: at };
    server.use(http.get("/clusters/cl-home", () => HttpResponse.json({ ...clusterHome, sandbox: { ...clusterHome.sandbox, check } })));
    render(<TestApp initialPath="/clusters/cl-home" />);
    expect(await screen.findByTestId("sandbox-accepted")).toHaveTextContent("root 님이");
    expect(screen.queryByTestId("sandbox-accept")).not.toBeInTheDocument();
  });
});

describe("게이트의 409 와 내려받기 거절", () => {
  it("배포의 409 sandbox_gate_closed 는 까닭의 문장이다", () => {
    const error = new ApiError(409, { detail: { code: "sandbox_gate_closed", detail: "x", reason: "rights" } });
    expect(describeError(ko, error).text).toBe(ko.clusters.sandbox.reason.rights);
    expect(describeError(en, error).text).toBe(en.clusters.sandbox.reason.rights);
  });

  it("작업 공간 파일의 거절에 문장이 있다", () => {
    const error = new ApiError(403, { detail: { code: "file_refused_executable", detail: "x" } });
    expect(describeError(ko, error).text).toBe(ko.errors.codes.file_refused_executable);
  });
});

describe("코드 실행 검증 (P40 9절)", () => {
  it("턴 타임아웃 칸이 없는 메인은 서버처럼 기본값 120초로 읽고 경고한다", () => {
    const g = filledTemplateGraph();
    const main = g.nodes.find((n) => n.type === "agent" && (n.data as AgentData).role === "main")!.data as Partial<AgentData>;
    main.code_execution = { approval: [] };
    delete main.turn_timeout_seconds;
    const warning = validateGraph(g, DEFAULT_SERVICE, mockValidationContext()).find((i) => i.code === "code_execution_turn_timeout_short");
    expect(warning?.level).toBe("warning");
    main.turn_timeout_seconds = SANDBOX_COMMAND_MAX_SECONDS;
    expect(validateGraph(g, DEFAULT_SERVICE, mockValidationContext()).map((i) => i.code)).not.toContain("code_execution_turn_timeout_short");
  });
});
