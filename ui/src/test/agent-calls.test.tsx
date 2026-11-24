/**
 * 게시한 Agent 를 다른 Agent 가 Tool 로 부르는 화면 (P46 6.8절).
 *  - Agent 상세: 허브에 게시(Tool 설명, 호출을 믿는 선택)와 게시 해제.
 *  - 허브: 종류 Agent, Tool 하나, 새로고침 없음, 게시 해제.
 *  - 캔버스: Agent 소스의 선택지와 노드의 둘째 줄, 고정된 표시, 호출받은 Agent 의 provider 와 본문 기록, 타임아웃의 안내.
 *  - 자동 실행 탭: 출처와 부른 Agent, 부른 쪽이 끝낸 까닭.
 *  - 채팅: Tool 카드의 실행 보기 링크.
 *  - 경고와 이슈: 사전의 문장으로 보인다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";
import { ToolsPanel } from "@/components/canvas/inspector/ToolsPanel";
import { sourceLabel, toolNamesOfSource } from "@/components/canvas/inspector/types";
import { ToolCallView } from "@/components/chat";
import { dictionary } from "@/i18n";
import { AGENT_SOURCE, DEFAULT_SERVICE, defaultNodeData, issueText, validateGraph, type Graph, type ToolsData } from "@/lib/agentGraph";
import { agentCallToolName, hubItemOfAgent, hubKindLabel, hubToolNames, isAgentCallTool, isExternalHubItem } from "@/lib/hub";
import { calledByText, runReasonText, runSourceText } from "@/lib/runs";
import { outsideFixed } from "@/lib/toolSafety";
import { describeWarning } from "@/lib/warningText";

import { submitConfirm } from "./confirm";
import { inspectorOptions } from "./inspector";
import { agentFixture, agentState, filledTemplateGraph, hubAgentFixture, hubState, lastRequest, mockValidationContext, runFixture, runsState } from "./msw";
import { renderWithProviders } from "./render";
import { selectValue } from "./select";

const ko = dictionary("ko");
const en = dictionary("en");
const NODE = "tools-1";
const ITEM = hubAgentFixture();
const TOOL = "ask_research_agent";

describe("lib: 게시한 Agent 의 항목과 그 Tool", () => {
  it("Tool 이름은 ask_ 뒤에 항목 이름의 - 를 _ 로 바꿔 붙인다", () => {
    expect(agentCallToolName("research-agent")).toBe(TOOL);
    expect(isAgentCallTool(TOOL)).toBe(true);
    expect(isAgentCallTool("list_orders")).toBe(false);
    expect(hubToolNames(ITEM)).toEqual([TOOL]);
  });

  it("agent 항목은 사전의 종류 이름을 쓰고 목록에서 지우는 외부 항목이 아니다", () => {
    expect(hubKindLabel(ko, "agent")).toBe("Agent");
    expect(hubKindLabel(ko, "remote")).toBe("remote");
    expect(isExternalHubItem("agent")).toBe(false);
    expect(hubItemOfAgent([ITEM], "ag-research")).toBe(ITEM);
    expect(hubItemOfAgent([ITEM], "ag-support")).toBeUndefined();
  });

  it("ask_* 의 바깥 표시는 바꿀 수 없다", () => {
    expect(outsideFixed("agent", "write")).toBe(true);
  });

  it("실행 기록은 부른 Agent 와 부른 쪽이 끝낸 까닭을 사전의 말로 적는다", () => {
    const run = runFixture({ source: "agent", caller: { agent_id: "ag-support", session_id: "s-1", chain: ["ag-support"] } });
    expect(runSourceText(ko, run.source)).toBe("Agent 호출");
    expect(calledByText(ko, run, () => "상담 에이전트")).toBe("부른 Agent: 상담 에이전트");
    expect(calledByText(en, run, () => "support")).toBe("Called by: support");
    expect(calledByText(ko, runFixture(), () => "x")).toBeNull();
    expect(runReasonText(ko, "caller_ended", 30)).toEqual({ text: "부른 Agent 의 턴이 끝나 실행을 취소했습니다.", code: false });
    expect(runReasonText(ko, "caller_timeout", 30)).toEqual({ text: "부른 Agent 가 기다리는 시간을 넘겨 실행을 취소했습니다.", code: false });
  });

  it("상태 경고는 경고 표의 문장이고, 게시를 거둔 소스와 꺼진 호출은 오류 표의 문장이다", () => {
    const notServing = describeWarning(ko, "agent_source_not_serving: agents connected as tools do not take conversations now, so calls are refused (tools-1)");
    expect(notServing).toMatchObject({ label: "호출할 Agent 멈춤", known: true, detail: "tools-1" });
    expect(describeWarning(ko, "agent_calls_broken_by_deploy: the deploy dropped the prerequisites").label).toBe("Agent 호출 불가");
    const unpublished = describeWarning(ko, "agent_source_unpublished: agents connected as tools are no longer published, so calls are refused (tools-1, tools-2)");
    expect(unpublished).toMatchObject({ code: "agent_source_unpublished", text: ko.errors.codes.agent_source_unpublished, known: true, detail: "tools-1, tools-2" });
    expect(describeWarning(en, "agent_calls_disabled: agent calls are turned off on this platform, so calls are refused").text).toBe(en.errors.codes.agent_calls_disabled);
  });

  it("서버만 보는 Agent 소스의 이슈는 오류 표와 경고 표의 문장으로 보인다", () => {
    const at = { level: "error" as const, node_id: NODE, edge_id: null, message: "server sentence" };
    expect(issueText(ko, { ...at, code: "agent_source_self" })).toBe(ko.errors.codes.agent_source_self);
    expect(issueText(ko, { ...at, level: "warning", code: "agent_call_cycle" })).toBe(ko.warnings.codes.agent_call_cycle.text);
    expect(issueText(ko, { ...at, code: "rule_added_later" })).toBe("server sentence");
  });
});

/** 템플릿 그래프의 tools 노드를 Agent 소스로 바꾼 것. */
function graphWithAgentSource(kind: string): Graph {
  const graph = filledTemplateGraph();
  return { ...graph, nodes: graph.nodes.map((n) => (n.type === "tools" ? { ...n, data: { ...defaultNodeData("tools"), source: { kind, id: ITEM.id } } } : n)) } as Graph;
}

describe("캔버스 검증기의 Agent 소스", () => {
  it("소스 종류 agent 는 형식이 맞고, 목록에 없어도 화면이 막지 않는다(서버가 본다)", () => {
    const codes = validateGraph(graphWithAgentSource(AGENT_SOURCE), DEFAULT_SERVICE, mockValidationContext()).map((i) => i.code);
    expect(codes).not.toContain("invalid_node_data");
    expect(codes).not.toContain("unknown_source");
  });

  it("모르는 소스 종류는 세 종류를 대며 거절한다", () => {
    const issues = validateGraph(graphWithAgentSource("webhook"), DEFAULT_SERVICE, mockValidationContext());
    const bad = issues.find((i) => i.code === "invalid_node_data");
    expect(JSON.stringify(bad)).toContain("'gateway_instance', 'mcp_instance' or 'agent'");
  });
});

function Panel({ reviewed }: { reviewed: boolean }) {
  const [data, setData] = useState<ToolsData>({ ...defaultNodeData("tools"), read_only: ["old"], outside: { old: false } });
  const call = { tool: TOOL, agent_id: "ag-research", agent_name: "research", provider: "openai-main", record_bodies: true, timeout_seconds: 150 };
  const options = inspectorOptions({ agentItems: [ITEM], ...(reviewed ? { agentCalls: { [NODE]: call }, toolEffects: { [NODE]: { [TOOL]: "write" } }, toolOrigins: { [NODE]: { [TOOL]: "agent" } } } : {}) });
  return (
    <>
      <ToolsPanel nodeId={NODE} data={data} update={(patch) => setData((d) => ({ ...d, ...patch }))} options={options} hasMemory />
      <output data-testid="data">{JSON.stringify(data)}</output>
    </>
  );
}

function stored(): ToolsData {
  return JSON.parse(screen.getByTestId("data").textContent ?? "{}") as ToolsData;
}

describe("tools 노드 패널의 Agent 소스", () => {
  it("이름을 허브 목록에서 풀고 Tool 은 항목 이름에서 나온 하나다", () => {
    const options = inspectorOptions({ agentItems: [ITEM] });
    expect(sourceLabel(ko, { kind: AGENT_SOURCE, id: ITEM.id }, options)).toBe("리서치 에이전트");
    expect(toolNamesOfSource(AGENT_SOURCE, ITEM.id, options)).toEqual([TOOL]);
    expect(toolNamesOfSource(AGENT_SOURCE, "hub-gone", options)).toBeNull();
  });

  it("Agent 를 선택하면 앞 소스의 규칙과 표시를 비우고, 고른 뒤 그 Tool 과 고정된 표시를 보인다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Panel reviewed={false} />);
    // 선택지의 이름은 사전의 "Agent" 다
    await selectValue(user, "tools-source-kind", "Agent");
    expect(stored()).toMatchObject({ source: { kind: AGENT_SOURCE, id: "" }, read_only: [], outside: {}, allow_tools: [] });
    // 고르기 전에는 Tool 이 없고, 허용 목록과 표시 절도 없다
    expect(screen.queryByTestId("tools-allow-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-allow-extra")).not.toBeInTheDocument();

    await selectValue(user, "tools-source", ITEM.id);
    expect(screen.getByTestId("tools-agent-tool")).toHaveTextContent(TOOL);
    expect(screen.getByTestId("tools-agent-marks")).toHaveTextContent(ko.canvas.tools.agentMarks);
    // 여는 화면은 항목이 가리키는 Agent 의 상세다
    expect(screen.getByTestId("tools-source-link")).toHaveAttribute("href", "/agents/ag-research");
    // 검사 전에는 대화의 글이 가는 곳을 모른다
    expect(screen.queryByTestId("tools-agent-data-flow")).not.toBeInTheDocument();
  });

  it("검사 뒤에는 호출받은 Agent 의 provider 와 본문 기록을 보이고, 타임아웃은 호출이 멈추는 것을 알린다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Panel reviewed />);
    await selectValue(user, "tools-source-kind", AGENT_SOURCE);
    await selectValue(user, "tools-source", ITEM.id);
    expect(screen.getByTestId("tools-agent-data-flow")).toHaveTextContent(ko.canvas.tools.agentDataFlow("openai-main", "켜짐"));

    await user.click(screen.getByTestId("inspector-tabs-timeouts"));
    expect(screen.getByTestId("tools-timeouts-help")).toHaveTextContent(ko.canvas.tools.agentTimeout);
    // 비우면 호출받은 Agent 의 턴 시간에서 나온 기본값이다
    expect(screen.getByTestId(`timeout-${TOOL}`)).toHaveAttribute("placeholder", "150");

    await user.click(screen.getByTestId("inspector-tabs-approval"));
    await user.click(screen.getByTestId(`approval-${TOOL}-approve`));
    expect(stored().approval).toEqual({ [TOOL]: ["approve"] });
  });
});

describe("Agent 상세의 허브 게시", () => {
  it("배포된 Agent 를 Tool 설명과 함께 게시하면 배지와 해제 단추가 서고, 해제하면 게시 단추로 돌아온다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    const button = await screen.findByTestId("publish-button");
    expect(button).toHaveTextContent("허브에 게시");
    await user.click(button);
    const dialog = await screen.findByTestId("publish-dialog");
    expect(dialog).toHaveTextContent(ko.agents.detail.publish.hint);
    expect(within(dialog).getByTestId("publish-answer-notice")).toHaveTextContent(ko.agents.detail.publish.answerNotice);
    expect(within(dialog).getByTestId("publish-name")).toHaveValue("support");
    const description = within(dialog).getByTestId("publish-description");
    await user.clear(description);
    await user.type(description, "Answers support questions.");
    await user.click(within(dialog).getByTestId("publish-calls-trusted"));
    await user.click(within(dialog).getByTestId("publish-submit"));

    await waitFor(() =>
      expect(lastRequest("POST", "/agents/ag-support/publish")?.body).toEqual({ name: "support", display_name: "상담 에이전트", description: "Answers support questions.", calls_trusted: true }),
    );
    expect(await screen.findByTestId("published-badge")).toBeInTheDocument();
    expect(screen.getByTestId("hub-link")).toHaveAttribute("href", "/hub/hub-support");

    await user.click(await screen.findByTestId("unpublish-button"));
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/agents/ag-support/publish")).toBeDefined());
    expect(await screen.findByTestId("publish-button")).toBeInTheDocument();
  });

  it("배포하지 않은 Agent 에는 게시 단추가 없다", async () => {
    agentState.items = [agentFixture({ state: "draft", deployed_revision: null, status: null })];
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    expect(screen.queryByTestId("publish-button")).not.toBeInTheDocument();
  });
});

describe("허브의 agent 항목", () => {
  it("목록은 종류 Agent 와 Tool 하나를 보이고 지우는 메뉴가 없다", async () => {
    hubState.items = [ITEM];
    render(<TestApp initialPath="/hub" />);
    const row = await screen.findByTestId("hub-row-research-agent");
    expect(row).toHaveTextContent("Agent");
    expect(within(row).getByTestId("hub-tools-research-agent")).toHaveTextContent("1");
    expect(within(row).getByTestId("hub-tool-preview-research-agent")).toHaveTextContent(TOOL);
    expect(screen.queryByTestId("row-delete-research-agent")).not.toBeInTheDocument();
  });

  it("상세는 Agent 로 이어지고 Tool 새로고침이 없으며, 도구 탭은 그 Tool 과 설명을 물어보지 않고 보인다", async () => {
    const user = userEvent.setup();
    hubState.items = [ITEM];
    render(<TestApp initialPath={`/hub/${ITEM.id}`} />);
    expect(await screen.findByTestId("hub-kind")).toHaveTextContent("Agent");
    expect(screen.getByTestId("goto-agent-button")).toHaveAttribute("href", "/agents/ag-research");
    expect(screen.queryByTestId("refresh-tools-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("delete-button")).toHaveTextContent("게시 해제");

    await user.click(screen.getByTestId("tab-tools"));
    const card = await screen.findByTestId("hub-tools-card");
    expect(card).toHaveTextContent(TOOL);
    expect(within(card).getByTestId("hub-agent-description")).toHaveTextContent("Researches a topic");
    expect(lastRequest("GET", `/hub/${ITEM.id}/tools`)).toBeUndefined();
  });

  it("게시 해제 확인 창은 삭제 문장을 빌리지 않고 빈 문단도 두지 않는다", async () => {
    const user = userEvent.setup();
    hubState.items = [ITEM];
    render(<TestApp initialPath={`/hub/${ITEM.id}`} />);
    await user.click(await screen.findByTestId("delete-button"));
    const target = await screen.findByTestId("confirm-target");
    expect(target).toHaveTextContent(ITEM.name);
    expect(target.parentElement?.querySelectorAll("p:empty")).toHaveLength(0);
  });
});

describe("자동 실행 탭의 Agent 호출", () => {
  it("출처 아래에 부른 Agent 를 이름 링크로 적고 까닭을 말한다", async () => {
    runsState.runs = [runFixture({ id: "run-call", source: "agent", status: "cancelled", reason: "caller_ended", caller: { agent_id: "ag-support", session_id: "s-1", chain: ["ag-support"] } })];
    render(<TestApp initialPath="/agents/ag-support?tab=runs" />);
    const row = await screen.findByTestId("run-run-call");
    expect(row).toHaveTextContent("Agent 호출");
    const caller = await within(row).findByTestId("run-run-call-caller");
    await waitFor(() => expect(caller).toHaveTextContent("부른 Agent: 상담 에이전트"));
    expect(caller).toHaveAttribute("href", "/agents/ag-support");
    expect(screen.getByTestId("run-run-call-reason")).toHaveTextContent("부른 Agent 의 턴이 끝나 실행을 취소했습니다.");
  });
});

describe("채팅의 Tool 카드", () => {
  const call = { id: "c1", name: TOOL, args: { message: "find it" } };
  const result = { id: "c1", name: TOOL, status: "success" as const, content: "done" };

  it("다른 Agent 를 부른 호출은 그 Agent 의 자동 실행 탭으로 가는 링크를 단다", async () => {
    hubState.items = [ITEM];
    renderWithProviders(<ToolCallView item={{ kind: "tool_call", call, result }} testId="tool" />);
    const link = await screen.findByTestId("tool-view-run");
    expect(link).toHaveTextContent("실행 보기");
    expect(link).toHaveAttribute("href", "/agents/ag-research?tab=runs");
  });

  it("답이 오기 전과 게시한 항목을 찾지 못한 호출에는 링크가 없다", async () => {
    hubState.items = [];
    renderWithProviders(
      <>
        <ToolCallView item={{ kind: "tool_call", call, result }} testId="gone" />
        <ToolCallView item={{ kind: "tool_call", call, result: null }} testId="running" />
      </>,
    );
    await waitFor(() => expect(lastRequest("GET", "/hub")).toBeDefined());
    expect(screen.queryByTestId("gone-view-run")).not.toBeInTheDocument();
    expect(screen.queryByTestId("running-view-run")).not.toBeInTheDocument();
  });
});
