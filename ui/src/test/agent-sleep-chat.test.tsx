/**
 * 잠든 Agent 에 보낸 턴 (P44 8절). 플랫폼이 깨우는 동안 채팅·결정은 503 agent_waking 과 Retry-After 로 답한다. 채팅 패널과 승인함은
 * 보낼 것을 쥔 채 기다림 줄을 세우고 Retry-After 마다 다시 보내며, 중단은 기다림만 멈추고 보낼 것을 되돌린다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";

import { pdf, pick, waitReady } from "./attach";
import { openAgentChat, openHomeSession, sendMessage } from "./chat";
import { agentFixture, agentState, agentUploadState, agentWithAttachments, apiError, asleepAgentFixture, http, lastRequest, server } from "./msw";
import { advanceClock, tickingClock } from "./timers";

/** 그 경로의 POST 가 처음 times 번은 깨우는 중(503, Retry-After 1초)이고 그 뒤로는 원래 처리기로 간다. 받은 수를 센다. */
function wakingFor(path: string, times: number): { calls: () => number } {
  let n = 0;
  server.use(
    http.post(path, () => {
      n += 1;
      if (n > times) return undefined;
      return apiError(503, "agent_waking", "the agent is waking", {}, { headers: { "Retry-After": "1" } });
    }),
  );
  return { calls: () => n };
}

describe("agent sleep (turns while waking)", () => {
  beforeEach(() => {
    agentState.items = [asleepAgentFixture()];
  });

  it("채팅은 메시지를 쥐고 기다림 줄을 세운 뒤 Retry-After 마다 다시 보내고, 파드가 뜨면 답을 흘린다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    const chat = wakingFor("/agents/:id/chat", 2);
    await openAgentChat();
    await sendMessage(user, "3 더하기 4");
    const notice = await screen.findByTestId("chat-notice");
    expect(notice).toHaveTextContent("Agent 의 파드를 다시 띄우는 중입니다. 파드가 뜨면 이 메시지를 바로 보냅니다.");
    // 보낸 말풍선은 그대로 있고 오류 줄은 없다
    expect(screen.getByTestId("chat-transcript")).toHaveTextContent("3 더하기 4");
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    expect(chat.calls()).toBe(1);
    await advanceClock(1_000);
    expect(chat.calls()).toBe(2);
    await advanceClock(1_000);
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 7 입니다."));
    expect(chat.calls()).toBe(3);
    expect(lastRequest("POST", "/agents/ag-support/chat")?.body).toEqual({ session_id: null, message: "3 더하기 4", ephemeral: true });
    expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
  });

  it("기다리는 동안 중단하면 더 보내지 않고 쓴 글을 입력칸에 되돌린다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    const chat = wakingFor("/agents/:id/chat", 100);
    await openAgentChat();
    await sendMessage(user, "날씨 알려 줘");
    await screen.findByTestId("chat-notice");
    await user.click(screen.getByTestId("chat-abort"));
    await waitFor(() => expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument());
    expect(screen.getByTestId("chat-input")).toHaveValue("날씨 알려 줘");
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-transcript")).not.toHaveTextContent("날씨 알려 줘");
    await advanceClock(10_000);
    expect(chat.calls()).toBe(1);
  });

  it("잠든 Agent 에도 첨부를 붙이고, 깨우는 동안 첨부를 쥔 채 다시 보낸다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    // 상태는 잠들 때 남긴 받는 첨부를 낸다. 그래서 첨부 단추가 그대로 선다
    agentState.items = [asleepAgentFixture(agentWithAttachments())];
    const chat = wakingFor("/agents/:id/chat", 1);
    await openAgentChat();
    await pick(user, pdf());
    await waitReady(0);
    const ids = agentUploadState.items.map((u) => u.id);
    await sendMessage(user, "정리해 줘");
    await screen.findByTestId("chat-notice");
    await advanceClock(1_000);
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/chat")?.body).toEqual({ session_id: null, message: "정리해 줘", ephemeral: true, attachments: ids }));
    expect(chat.calls()).toBe(2);
    await waitFor(() => expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument());
  });

  it("기다리는 동안 다른 대화로 옮기면 앞 대화의 글을 새 대화의 입력칸에 넣지 않는다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    agentState.items = [agentFixture()];
    const chat = wakingFor("/agents/:id/chat", 100);
    await openHomeSession(user, "s-1");
    await sendMessage(user, "날씨 알려 줘");
    await screen.findByTestId("chat-notice");
    await user.click(screen.getByTestId("home-session-s-2"));
    await waitFor(() => expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument());
    await advanceClock(5_000);
    expect(screen.getByTestId("chat-input")).toHaveValue("");
    expect(chat.calls()).toBe(1);
  });

  it("5분을 넘기도록 깨지 않으면 기다림을 거두고 깨우는 중이라는 오류를 보인다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    wakingFor("/agents/:id/chat", 10_000);
    await openAgentChat();
    await sendMessage(user, "안녕");
    await screen.findByTestId("chat-notice");
    await advanceClock(5 * 60_000 + 2_000);
    expect(await screen.findByTestId("chat-error")).toHaveTextContent("사용하지 않아 내려 두었던 Agent 의 파드를 다시 띄우는 중입니다.");
    expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-input")).toHaveValue("안녕");
  });

  it("승인 카드의 결정도 쥐고 기다렸다 보내고, 중단하면 카드를 다시 고를 수 있다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    await openAgentChat();
    await sendMessage(user, "8 나누기 2");
    const card = await screen.findByTestId("approval-card");
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());

    // 결정을 보내려는 사이 Agent 가 잠들었다. 기다림을 멈추면 결정은 가지 않았으므로 카드가 다시 열린다
    const first = wakingFor("/agents/:id/sessions/:sid/decisions", 100);
    await user.click(within(card).getByTestId("decision-approve"));
    expect(await screen.findByTestId("chat-notice")).toHaveTextContent("파드가 뜨면 이 결정을 바로 보냅니다.");
    await user.click(screen.getByTestId("chat-abort"));
    await waitFor(() => expect(card).not.toHaveAttribute("data-resolved", "true"));
    expect(first.calls()).toBe(1);
    server.resetHandlers();

    const second = wakingFor("/agents/:id/sessions/:sid/decisions", 1);
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    await user.click(within(card).getByTestId("decision-approve"));
    await screen.findByTestId("chat-notice");
    await advanceClock(1_000);
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 4 입니다."));
    expect(second.calls()).toBe(2);
    expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/decisions")?.body).toEqual({ decisions: [{ type: "approve" }], ephemeral: true });
  });

  it("승인함의 결정은 결과 자리에 기다림 줄과 중단 단추를 세우고, 깨면 재개 스트림을 보인다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    // 상태 폴링이 아직 잠든 것을 모르는 사이에 결정했다
    agentState.items = [agentFixture()];
    const decisions = wakingFor("/agents/:id/sessions/:sid/decisions", 1);
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await user.click(await screen.findByTestId("pending-session-s-2"));
    await user.click(within(await screen.findByTestId("approval-card")).getByTestId("decision-approve"));
    expect(await screen.findByTestId("approval-outcome-waking")).toHaveTextContent("파드가 뜨면 이 결정을 바로 보냅니다.");
    await advanceClock(1_000);
    await waitFor(() => expect(screen.queryByTestId("approval-outcome-waking")).not.toBeInTheDocument());
    expect(decisions.calls()).toBe(2);
    expect(await screen.findByTestId("approval-outcome")).not.toHaveTextContent("실패");
  });

  it("승인함에서 기다림을 멈추면 결과 자리를 걷고 카드로 돌아간다", async () => {
    tickingClock();
    const user = userEvent.setup({ advanceTimers: (ms) => advanceClock(ms) });
    agentState.items = [agentFixture()];
    const decisions = wakingFor("/agents/:id/sessions/:sid/decisions", 100);
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await user.click(await screen.findByTestId("pending-session-s-2"));
    await user.click(within(await screen.findByTestId("approval-card")).getByTestId("decision-approve"));
    await user.click(await screen.findByTestId("approval-outcome-stop"));
    expect(await screen.findByTestId("approval-card")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-outcome")).not.toBeInTheDocument();
    await advanceClock(5_000);
    expect(decisions.calls()).toBe(1);
  });
});
