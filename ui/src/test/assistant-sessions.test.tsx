import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { ASSISTANT_POLL_MS, assistantSessionKey, setAssistantOpen } from "@/lib/assistant";
import { DEPLOY_ARGS, assistantSessionFixture, assistantState, countExact, lastRequest } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

/**
 * 플랫폼 비서 패널의 세션 (D31): 기억한 세션의 전사 복원, 테넌트 전환, 승인 대기 세션의 복원과 결정, 도는 세션의 폴링.
 * 패널을 열어 말을 보내는 턴 SSE 쪽은 assistant.test.tsx 가 본다.
 */

describe("assistant panel (sessions)", () => {
  it("switching the tenant swaps the session list and the remembered session", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(assistantSessionKey("default"), "as-1");
    window.localStorage.setItem(assistantSessionKey("acme"), "as-acme-1");
    setAssistantOpen(true);
    render(<TestApp initialPath="/hub" />);
    const panel = await screen.findByTestId("assistant-panel");
    await waitFor(() => expect(within(panel).getByTestId("assistant-session")).toHaveValue("as-1"));
    // 다시 열면 GET /sessions/{id} 로 전사를 복원한다
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("calc-1 (Ready)"));
    expect(lastRequest("GET", "/assistant/sessions/as-1")?.headers["x-daegam-tenant"]).toBe("default");

    await selectValue(user, "tenant-select", "acme");
    await waitFor(() => expect(within(panel).getByTestId("assistant-session")).toHaveValue("as-acme-1"));
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("acme 백엔드"));
    expect(lastRequest("GET", "/assistant/sessions/as-acme-1")?.headers["x-daegam-tenant"]).toBe("acme");
    expect(within(panel).queryByText("calc-1 (Ready)")).not.toBeInTheDocument();

    // 새 대화를 시작하면 기억한 세션이 지워진다
    await user.click(within(panel).getByTestId("assistant-new-session"));
    expect(window.localStorage.getItem(assistantSessionKey("acme"))).toBeNull();
    expect(within(panel).getByTestId("assistant-empty")).toBeInTheDocument();
  });

  it("restores a pending session with the approval card open and decides without re-reading the session", async () => {
    const user = userEvent.setup();
    assistantState.sessions = [
      assistantSessionFixture({
        state: "pending",
        pending: { kind: "confirm", calls: [{ id: "call-deploy-9", name: "deploy_mcp_server", args: DEPLOY_ARGS, risk: "deploy" }] },
        transcript: [
          { kind: "user", text: "배포해 줘" },
          { kind: "assistant", text: "배포는 승인이 필요합니다.", streaming: false },
          { kind: "interrupt", pending: { session_id: "as-1", kind: "confirm", action_requests: [{ name: "deploy_mcp_server", args: DEPLOY_ARGS }], review_configs: [{ action_name: "deploy_mcp_server", allowed_decisions: ["approve", "reject"] }] }, resolved: false },
        ],
      }),
    ];
    window.localStorage.setItem(assistantSessionKey("default"), "as-1");
    setAssistantOpen(true);
    render(<TestApp initialPath="/hub" />);
    const panel = await screen.findByTestId("assistant-panel");
    const card = await within(panel).findByTestId("approval-card");
    expect(card).not.toHaveAttribute("data-resolved");
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    const reads = countExact("GET", "/assistant/sessions/as-1");
    await user.click(within(card).getByTestId("decision-approve"));
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-1/decisions")?.body).toEqual({ decisions: [{ id: "call-deploy-9", type: "approve" }] }));
    // 복원할 때 pending.calls 의 id 를 카드에 실어 두므로 결정 전에 다시 읽지 않는다
    expect(countExact("GET", "/assistant/sessions/as-1")).toBe(reads);
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("calc-2 를 만들었습니다"));
  });

  it("polls a running session every 3 seconds until it settles", async () => {
    tickingClock();
    assistantState.sessions = [assistantSessionFixture({ state: "running", transcript: [{ kind: "user", text: "느린 작업" }] })];
    window.localStorage.setItem(assistantSessionKey("default"), "as-1");
    setAssistantOpen(true);
    render(<TestApp initialPath="/hub" />);
    const panel = await screen.findByTestId("assistant-panel");
    expect(await within(panel).findByTestId("assistant-running")).toBeInTheDocument();
    const reads = countExact("GET", "/assistant/sessions/as-1");
    // 서버 쪽 턴이 끝났다
    assistantState.sessions[0].state = "idle";
    assistantState.sessions[0].transcript = [
      { kind: "user", text: "느린 작업" },
      { kind: "assistant", text: "끝났습니다.", streaming: false },
      { kind: "done", usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 }, turn: 1, tool_calls: 0 },
    ];
    await advanceClock(ASSISTANT_POLL_MS + 100);
    await waitFor(() => expect(countExact("GET", "/assistant/sessions/as-1")).toBe(reads + 1));
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("끝났습니다."));
    expect(within(panel).queryByTestId("assistant-running")).not.toBeInTheDocument();
    await advanceClock(ASSISTANT_POLL_MS + 100);
    expect(countExact("GET", "/assistant/sessions/as-1")).toBe(reads + 1);
  });
});
