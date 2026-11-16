/**
 * Agent (D42) 화면 테스트는 주제마다 파일 하나다. 여기는 목록이고, 캔버스(정의)는 agent-canvas, 상세의 개요와 배포
 * 동작은 agent-detail, 채팅 탭은 agent-chat, 세션·승인함·메트릭·이벤트 탭은 agent-sessions 에 있다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { openCardMenu } from "./menu";
import { HttpResponse, agentDraftFixture, agentFixture, agentState, http, meViewer, server } from "./msw";

describe("agents (list)", () => {
  it("a row shows the state, the phase, the graph summary and pending approvals", async () => {
    render(<TestApp initialPath="/agents" />);
    const row = await screen.findByTestId("agent-row-support");
    expect(row).toHaveTextContent("상담 에이전트");
    expect(within(row).getByTestId("agent-state-support")).toHaveTextContent("배포");
    // 행의 phase 는 색 점이다. 낱말은 위 요약 줄이 이미 세고 있어 겹친다
    expect(within(row).getByTestId("agent-phase-support")).toHaveAttribute("data-phase", "Ready");
    expect(row).toHaveTextContent("openai-main");
    expect(row).toHaveTextContent("r2");
    expect(within(row).getByTestId("agent-pending-support")).toHaveTextContent("1");
    expect(screen.getByTestId("ag-count-ready")).toHaveTextContent("1");
    expect(screen.getByTestId("ag-count-pending")).toHaveTextContent("1");
    expect(screen.getByTestId("agent-new")).toHaveAttribute("href", "/agents/new");
  });

  it("counts 미배포 apart and offers a draft the way to deploy", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture(), agentDraftFixture()];
    render(<TestApp initialPath="/agents" />);
    await waitFor(() => expect(screen.getByTestId("ag-count-draft")).toHaveTextContent("1"));
    const draft = screen.getByTestId("agent-row-draft");
    expect(within(draft).getByTestId("agent-state-draft")).toHaveTextContent("미배포");
    expect(within(draft).queryByTestId("agent-phase-draft")).not.toBeInTheDocument();
    // 카드에서 배포로 가는 길은 더보기 안이다 (D47)
    expect(within(await openCardMenu(user, "draft")).getByTestId("deploy-draft")).toHaveAttribute("href", "/agents/ag-draft?tab=deployment");
  });

  it("배포 권한이 없으면 목록에 배포 버튼이 없다", async () => {
    // 목의 기본 사용자가 platform-admin 이라 viewer 로 바꿔야 scope 검사가 실제로 돈다.
    // 이 검사가 없던 scope(`agents:write`)를 보고 있어도 아무도 못 잡았다
    const user = userEvent.setup();
    server.use(http.get("/me", () => HttpResponse.json(meViewer)));
    agentState.items = [agentDraftFixture()];
    render(<TestApp initialPath="/agents" />);
    await screen.findByTestId("agent-row-draft");
    // 더보기를 열어야 없다는 것이 증명된다. 열지 않고 없다고 하면 카드가 늘 지나간다
    expect(within(await openCardMenu(user, "draft")).queryByTestId("deploy-draft")).not.toBeInTheDocument();
  });

  it("the warnings column names the warning instead of printing the server code", async () => {
    agentState.items = [agentFixture({ status: { ...agentFixture().status!, warnings: ["no_shared_state: the graph has no memory node, so session state lives only inside the pod"] } })];
    render(<TestApp initialPath="/agents" />);
    const row = await screen.findByTestId("agent-row-support");
    expect(row).toHaveTextContent("세션이 공유되지 않음");
    expect(row).not.toHaveTextContent("no_shared_state");
  });
});
