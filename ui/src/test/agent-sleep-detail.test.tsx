/**
 * Agent 상세의 쉬는 상태 (P44 6절, 8절, 10절). 잠든 Agent 의 개요는 언제부터 잠들었는지와 그때의 세션·보류 수를 말하고 깨우기를
 * 둔다. 깨어 있고 잠들 수 있으면 agent:deploy 에게 "지금 내려 두기" 가 선다. 배포 탭과 배포·재배포 대화상자는 Scale to zero
 * 스위치와 그래도 줄이지 않는 까닭, 또는 쉬는 창의 풀이를 보인다. 잠든 Agent 도 채팅을 받는다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import type { Agent } from "@/api/types";

import {
  HttpResponse,
  agentDraftFixture,
  agentFixture,
  agentState,
  apiError,
  asleepAgentFixture,
  http,
  lastRequest,
  meViewer,
  server,
  withSleep,
} from "./msw";
import { selectValue } from "./select";

/** 메인 에이전트의 memory 간선을 뗀 그래프. 대화와 승인 대기가 파드에 살아 0 으로 줄일 수 없다. */
function withoutMemory(agent: Agent): Agent {
  return { ...agent, graph: { ...agent.graph, edges: agent.graph.edges.filter((e) => e.kind !== "memory") } };
}

describe("agent sleep (detail)", () => {
  it("잠든 Agent 의 개요는 잠든 때와 그때의 수를 말하고, 깨우면 다시 띄우는 중의 배너가 선다", async () => {
    const user = userEvent.setup();
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support" />);
    const note = await screen.findByTestId("overview-sleeping");
    expect(note).toHaveTextContent("부터 사용하지 않아 파드를 내려 두었습니다");
    expect(note).toHaveTextContent("활성 세션과 보류 승인은 내려 두기 직전의 값입니다.");
    expect(screen.getByTestId("status-phase-text")).toHaveTextContent("휴면");
    // 런타임을 읽지 않고 잠들 때 남긴 수다
    expect(screen.getByTestId("status-sessions")).toHaveTextContent("3");
    expect(screen.getByTestId("status-pending")).toHaveTextContent("2");
    // 잠든 Agent 는 기다릴 것이 없어 진행 배너가 없다
    expect(screen.queryByTestId("status-progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("overview-sleep")).not.toBeInTheDocument();

    await user.click(within(note).getByTestId("overview-sleeping-wake"));
    expect(lastRequest("POST", "/agents/ag-support/wake")).toBeDefined();
    expect(await screen.findByTestId("status-progress")).toHaveTextContent("사용하지 않아 내려 두었던 파드를 다시 띄우는 중입니다.");
    expect(screen.getByTestId("status-phase-text")).toHaveTextContent("깨우는 중");
    expect(screen.queryByTestId("overview-sleeping")).not.toBeInTheDocument();
  });

  it("agent:chat 이 없으면 깨우기 단추가 없다", async () => {
    agentState.items = [asleepAgentFixture()];
    server.use(http.get("/me", () => HttpResponse.json({ ...meViewer, scopes_by_tenant: { default: ["agent:read"] } })));
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("overview-sleeping")).toBeInTheDocument();
    expect(screen.queryByTestId("overview-sleeping-wake")).not.toBeInTheDocument();
  });

  it("깨어 있고 잠들 수 있으면 지금 내려 두기가 서고, 턴이 돌면 그 까닭을 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    const sleep = await screen.findByTestId("overview-sleep");
    expect(sleep).toHaveTextContent("지금 내려 두기");
    server.use(http.post("/agents/:id/sleep", () => apiError(409, "agent_busy", "a turn holds a lease")));
    await user.click(sleep);
    expect(await screen.findByTestId("overview-sleep-error")).toHaveTextContent("Agent 가 답변하는 중이라 지금 파드를 내릴 수 없습니다.");
    server.resetHandlers();
    await user.click(screen.getByTestId("overview-sleep"));
    expect(await screen.findByTestId("overview-sleeping")).toBeInTheDocument();
    expect(lastRequest("POST", "/agents/ag-support/sleep")).toBeDefined();
  });

  it("지금 내려 두기는 요청이 끝날 때까지 내려 두는 중으로 잠긴다", async () => {
    const user = userEvent.setup();
    let release = (): void => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("/agents/:id/sleep", async () => {
        await gate;
        agentState.items = [asleepAgentFixture()];
        return HttpResponse.json(asleepAgentFixture());
      }),
    );
    render(<TestApp initialPath="/agents/ag-support" />);
    await user.click(await screen.findByTestId("overview-sleep"));
    await waitFor(() => expect(screen.getByTestId("overview-sleep")).toHaveTextContent("내려 두는 중…"));
    expect(screen.getByTestId("overview-sleep")).toBeDisabled();
    release();
    expect(await screen.findByTestId("overview-sleeping")).toBeInTheDocument();
  });

  it("지금 내려 두기의 409 in_progress 는 이미 잠든 것이라 오류 없이 상태를 다시 읽는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    const sleep = await screen.findByTestId("overview-sleep");
    // 남이 클레임을 쥐어 0 으로 줄이기는 워커가 마친다. 문서는 이미 asleep 이다
    server.use(
      http.post("/agents/:id/sleep", () => {
        agentState.items = [asleepAgentFixture()];
        return apiError(409, "in_progress", "agent is asleep but another action holds it");
      }),
    );
    await user.click(sleep);
    expect(await screen.findByTestId("overview-sleeping")).toBeInTheDocument();
    expect(screen.queryByTestId("overview-sleep-error")).not.toBeInTheDocument();
  });

  it.each([
    // 관리자(목의 기본 /me)라도 잠들 수 없으면 없다
    ["잠들 수 없는 Agent", withSleep({ ineligible: "exposed" }), null],
    ["agent:deploy 가 없는 사람", agentFixture(), ["agent:read", "agent:chat"]],
  ])("%s 에는 지금 내려 두기가 없다", async (_name, agent, scopes) => {
    agentState.items = [agent];
    if (scopes) server.use(http.get("/me", () => HttpResponse.json({ ...meViewer, scopes_by_tenant: { default: scopes } })));
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Ready");
    expect(screen.queryByTestId("overview-sleep")).not.toBeInTheDocument();
  });

  it("배포 탭은 스위치와, 켜져 있으면 쉬는 창의 풀이나 줄이지 않는 까닭을 보인다", async () => {
    agentState.items = [agentFixture()];
    const { unmount } = render(<TestApp initialPath="/agents/ag-support?tab=deployment" />);
    const field = await screen.findByTestId("deployment-scale-to-zero");
    expect(field).toBeChecked();
    expect(field).toBeDisabled();
    expect(screen.getByTestId("deployment-scale-to-zero-hint")).toHaveTextContent("켜면 30분 동안 대화와 자동 실행이 없을 때 파드를 0개로 줄이고");
    unmount();

    agentState.items = [withSleep({ ineligible: "replicas_override" })];
    const second = render(<TestApp initialPath="/agents/ag-support?tab=deployment" />);
    expect(await screen.findByTestId("deployment-scale-to-zero-off")).toHaveTextContent("Advanced 탭에서 spec.replicas 를 지워 주세요.");
    second.unmount();

    // 스위치를 끈 Agent 는 꺼진 체크만 보인다
    agentState.items = [withSleep({ ineligible: "agent_off" }, agentFixture({ scale_to_zero: false }))];
    render(<TestApp initialPath="/agents/ag-support?tab=deployment" />);
    expect(await screen.findByTestId("deployment-scale-to-zero")).not.toBeChecked();
    expect(screen.queryByTestId("deployment-scale-to-zero-off")).not.toBeInTheDocument();
    expect(screen.queryByTestId("deployment-scale-to-zero-hint")).not.toBeInTheDocument();
  });

  it("쉬는 창이 0 이면 직접 내려 둘 때만 줄인다고 풀이한다", async () => {
    agentState.items = [withSleep({ window_seconds: 0 })];
    render(<TestApp initialPath="/agents/ag-support?tab=deployment" />);
    expect(await screen.findByTestId("deployment-scale-to-zero")).toBeChecked();
    expect(screen.getByTestId("deployment-scale-to-zero-hint")).toHaveTextContent("자동으로 줄이는 기준 시간이 없어 직접 내려 둘 때만 줄입니다.");
  });

  it("첫 배포 대화상자는 쉬는 창을 몰라 분 없이 풀이한다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentDraftFixture()];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    expect(within(dialog).getByTestId("ag-scale-to-zero-hint")).toHaveTextContent("플랫폼이 정한 시간 동안 대화와 자동 실행이 없을 때 파드를 0개로 줄이고");
  });

  it("첫 배포 대화상자는 memory 노드가 없으면 까닭을 보이고 끈 스위치를 본문에 싣는다", async () => {
    const user = userEvent.setup();
    agentState.items = [withoutMemory(agentDraftFixture())];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    const field = within(dialog).getByTestId("ag-scale-to-zero");
    expect(field).toBeChecked();
    expect(within(dialog).getByTestId("ag-scale-to-zero-off")).toHaveTextContent("memory 노드가 없는 Agent 는");
    expect(field).toHaveAccessibleDescription(/memory 노드가 없는 Agent 는/);
    await user.click(field);
    expect(within(dialog).queryByTestId("ag-scale-to-zero-off")).not.toBeInTheDocument();
    await selectValue(user, "ag-cluster", "cl-home");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-draft/deploy")?.body).toMatchObject({ scale_to_zero: false }));
  });

  it("재배포 대화상자는 바꾼 스위치만 싣고, 옛 이미지는 재배포가 고치므로 까닭으로 보이지 않는다", async () => {
    const user = userEvent.setup();
    agentState.items = [withSleep({ ineligible: "runtime_image" })];
    render(<TestApp initialPath="/agents/ag-support" />);
    await user.click(await screen.findByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    expect(within(dialog).getByTestId("redeploy-scale-to-zero")).toBeChecked();
    expect(within(dialog).queryByTestId("redeploy-scale-to-zero-off")).not.toBeInTheDocument();
    expect(within(dialog).getByTestId("redeploy-scale-to-zero-hint")).toHaveTextContent("30분");
    await user.click(within(dialog).getByTestId("redeploy-scale-to-zero"));
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/redeploy")?.body).toEqual({ scale_to_zero: false }));
  });

  it("잠든 Agent 의 채팅 탭은 입력을 받는다", async () => {
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support?tab=chat" />);
    const panel = await screen.findByTestId("chat-tabpanel");
    expect(within(panel).getByTestId("chat-input")).toBeEnabled();
    expect(panel).toHaveTextContent("플랫폼 프록시로 런타임과 SSE 로 대화합니다.");
  });
});
