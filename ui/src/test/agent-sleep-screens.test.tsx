/**
 * 잠든 Agent 의 목록 화면 (P44 8절). 상태가 Sleeping·Waking 인 동안 승인함, 세션 탭, 홈 챗의 대화 목록과 기억 서랍은 런타임
 * 읽기를 보내지 않고 휴면 줄과 깨우기 단추를 보이며, 상태가 그 둘을 떠나면 다시 읽는다. 홈 챗은 사람이 고르개에서 잠든 Agent 를 고를 때만 한 번
 * 깨운다. 테스트 채팅 서랍은 Sleeping 을 Ready 처럼 다루고, 메트릭 탭은 빠진 런타임 몫을 경고 대신 안내로 말한다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { RUNTIME_CANCEL, RUNTIME_MEMORY_FILES } from "@/lib/runtimeFeatures";
import { homeChatAgentKey, writeItem } from "@/lib/storage";

import { openHomeChat, pickHomeTarget } from "./chat";
import { selectValue } from "./select";
import {
  HttpResponse,
  agentFixture,
  agentMetricsFixture,
  agentState,
  agentWithLongTerm,
  asleepAgentFixture,
  assistantState,
  countExact,
  http,
  received,
  server,
} from "./msw";
import { advanceClock, tickingClock } from "./timers";

/** 그 Agent 의 런타임 읽기(세션 목록·상세·보류·기억)를 보냈는가. 상태와 메트릭은 런타임 읽기가 아니다. */
function runtimeReads(agentId = "ag-support"): string[] {
  const prefix = `/agents/${agentId}/`;
  return received.filter((r) => r.method === "GET" && r.path.includes(prefix) && /\/(sessions|memories)/.test(r.path)).map((r) => r.path);
}

describe("agent sleep (screens that poll)", () => {
  it("잠든 Agent 의 승인함은 목록을 읽지 않고 남긴 수를 보이다가, 깨면 다시 읽는다", async () => {
    tickingClock();
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    const notice = await screen.findByTestId("approval-sleeping");
    expect(notice).toHaveTextContent("승인 대기 2건은 내려 두기 직전의 값이고, 깨우면 목록을 불러옵니다.");
    // 탭의 수도 잠들 때 남긴 값이다
    expect(screen.getByTestId("tab-approvals")).toHaveTextContent("2");
    expect(screen.queryByTestId("approval-inbox-count")).not.toBeInTheDocument();
    // 두 창(5초 폴링 여러 번)이 지나도 런타임 읽기가 없다
    await advanceClock(20_000);
    expect(runtimeReads()).toEqual([]);

    // 다른 사람의 대화가 Agent 를 깨웠다. 상태가 Sleeping 을 떠나면 목록을 읽는다
    agentState.items = [agentFixture()];
    await advanceClock(5_000);
    expect(await screen.findByTestId("pending-session-s-2")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-sleeping")).not.toBeInTheDocument();
  });

  it("잠들 때 수를 읽지 못했으면 승인함은 수를 말하지 않는다", async () => {
    const asleep = asleepAgentFixture();
    agentState.items = [{ ...asleep, status: { ...asleep.status!, pending_approvals: null, active_sessions: null } }];
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    const notice = await screen.findByTestId("approval-sleeping");
    expect(notice).toHaveTextContent("이 Agent 는 사용하지 않아 파드를 내려 두었습니다. 깨우면 목록을 불러옵니다.");
    expect(notice).not.toHaveTextContent("건은");
  });

  it("목록 카드는 잠든 Agent 의 상태를 숨기지 않는다", async () => {
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents" />);
    expect(await screen.findByTestId("agent-sleeping-support")).toHaveTextContent("휴면");
  });

  it("승인함의 깨우기는 POST /wake 를 부르고 깨우는 동안 단추를 잠근다", async () => {
    const user = userEvent.setup();
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await user.click(await screen.findByTestId("approval-sleeping-wake"));
    await waitFor(() => expect(screen.getByTestId("approval-sleeping-wake")).toHaveTextContent("깨우는 중…"));
    expect(screen.getByTestId("approval-sleeping-wake")).toBeDisabled();
    expect(countExact("POST", "/agents/ag-support/wake")).toBe(1);
    // 깨우는 중에도 읽지 않는다. 읽어도 503 agent_waking 이다
    expect(runtimeReads()).toEqual([]);
  });

  it("잠든 Agent 의 세션 탭은 목록을 읽지 않고 휴면 줄을 보인다", async () => {
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    expect(await screen.findByTestId("sessions-sleeping")).toHaveTextContent("이 Agent 는 사용하지 않아 파드를 내려 두었습니다. 깨우면 목록을 불러옵니다.");
    expect(screen.queryByTestId("sessions-table")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("sessions-sleeping")).getByTestId("sessions-sleeping-wake")).toHaveTextContent("깨우기");
    expect(runtimeReads()).toEqual([]);
  });

  it("홈 챗은 고르개에서 잠든 Agent 를 고를 때 한 번 깨우고, 대화 목록 자리에 휴면 줄을 보인다", async () => {
    const user = userEvent.setup();
    agentState.items = [asleepAgentFixture()];
    await pickHomeTarget(user);
    await waitFor(() => expect(countExact("POST", "/agents/ag-support/wake")).toBe(1));
    const notice = await screen.findByTestId("home-sessions-sleeping");
    expect(notice).toHaveTextContent("깨우면 목록을 불러옵니다.");
    // 깨우기의 답이 Waking 이라 단추는 잠긴다
    await waitFor(() => expect(within(notice).getByTestId("home-sessions-sleeping-wake")).toBeDisabled());
    expect(runtimeReads()).toEqual([]);
    // 잠든 Agent 도 대화를 받는다
    expect(screen.getByTestId("home-start-input")).toBeEnabled();
    expect(screen.queryByTestId("home-agent-not-ready")).not.toBeInTheDocument();
  });

  it("홈 챗은 앞서 깨어 있던 Agent 를 다시 고를 때 캐시가 아니라 고른 뒤의 상태로 가른다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture()];
    await pickHomeTarget(user);
    await screen.findByTestId("home-session-s-1");
    // 청지기로 옮긴 사이 Agent 가 잠들었다. 다시 고르면 캐시의 Ready 가 아니라 새로 읽은 Sleeping 으로 깨운다
    await selectValue(user, "home-agent-select", "assistant");
    agentState.items = [asleepAgentFixture()];
    await selectValue(user, "home-agent-select", "ag-support");
    await waitFor(() => expect(countExact("POST", "/agents/ag-support/wake")).toBe(1));
  });

  it("홈 챗이 마지막 고름을 되살릴 때는 깨우지 않는다", async () => {
    tickingClock();
    agentState.items = [asleepAgentFixture()];
    writeItem(homeChatAgentKey("default"), "ag-support");
    await openHomeChat();
    expect(await screen.findByTestId("home-sessions-sleeping")).toBeInTheDocument();
    await advanceClock(20_000);
    expect(countExact("POST", "/agents/ag-support/wake")).toBe(0);
    expect(runtimeReads()).toEqual([]);
    // 상태를 폴링하다 깨면 목록을 읽는다
    agentState.items = [agentFixture()];
    await advanceClock(5_000);
    expect(await screen.findByTestId("home-session-s-1")).toBeInTheDocument();
  });

  it("잠든 Agent 의 기억 서랍은 목록을 읽지 않고 휴면 줄을 보인다", async () => {
    const user = userEvent.setup();
    agentState.items = [asleepAgentFixture(agentWithLongTerm(RUNTIME_CANCEL, RUNTIME_MEMORY_FILES))];
    // 비서를 끄면 되살린 고름 없이도 첫 Agent 가 상대다. 고르지 않았으므로 깨우지 않는다
    assistantState.config = { ...assistantState.config, enabled: false };
    await openHomeChat();
    await user.click(await screen.findByTestId("memory-drawer-open"));
    const drawer = await screen.findByTestId("memory-drawer");
    expect(within(drawer).getByTestId("memory-sleeping")).toHaveTextContent("깨우면 목록을 불러옵니다.");
    expect(runtimeReads()).toEqual([]);
    expect(countExact("POST", "/agents/ag-support/wake")).toBe(0);
  });

  it("테스트 채팅 서랍은 잠든 Agent 와도 대화한다", async () => {
    const user = userEvent.setup();
    agentState.items = [asleepAgentFixture()];
    render(<TestApp initialPath="/agents/ag-support?tab=definition" />);
    const drawer = await screen.findByTestId("test-chat-drawer");
    await waitFor(() => expect(drawer).toHaveTextContent("배포된 r2와 대화합니다"));
    expect(within(drawer).queryByTestId("test-chat-empty")).not.toBeInTheDocument();
    await user.click(within(drawer).getByTestId("test-chat-toggle"));
    expect(within(drawer).getByTestId("chat-input")).toBeEnabled();
  });

  it("메트릭 탭은 잠들어 빠진 런타임 몫을 경고가 아니라 안내로 말한다", async () => {
    agentState.items = [asleepAgentFixture()];
    const runtime_error = { code: "agent_asleep", message: "the agent is asleep" };
    server.use(http.get("/agents/:id/metrics", () => HttpResponse.json({ ...agentMetricsFixture, sessions: null, tools: null, warnings: null, runtime_error })));
    render(<TestApp initialPath="/agents/ag-support?tab=metrics" />);
    expect(await screen.findByTestId("metrics-runtime-asleep")).toHaveTextContent("이 Agent 는 사용하지 않아 파드를 내려 두었습니다.");
    expect(screen.queryByTestId("metrics-runtime-error")).not.toBeInTheDocument();
    // 저장소가 센 수는 그대로 있다
    expect(screen.getByTestId("metrics-turns")).toHaveTextContent("38");
  });
});
