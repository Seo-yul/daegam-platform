/**
 * 승인함(ApprovalInbox). 결정 뒤의 재개 스트림은 채팅과 같은 루프(lib/chatTurn)로 읽고, 스트림이 실은 오류
 * 코드는 채팅 전사와 같이 errorText 표의 말로 보인다 (D43 첫째).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { HttpResponse, NOW, agentPendingFixture, agentSessionState, apiError, http, lastRequest, meViewer, received, runsState, server, sseResponse } from "./msw";

/** 다른 사람에게 묶여 승인을 기다리는 자동 실행의 세션. 승인자만 runs=only 로 받는다 (P41 7절). */
function addWaitingRun(): void {
  agentSessionState.items.push({
    session_id: "s-run-2",
    user_sub: "dev@example.com",
    created_at: NOW,
    last_turn_at: NOW,
    turns: 1,
    usage: { input_tokens: 10, output_tokens: 2 },
    status: "pending",
    pending_since: NOW,
    run: { id: "run-2", status: "waiting_approval", deadline: "2030-01-02T00:00:00+00:00" },
  });
  agentSessionState.pending["s-run-2"] = agentPendingFixture;
}

describe("approval inbox", () => {
  it("승인자는 모든 사람의 자동 실행 승인을 마감과 함께 보고, 이미 닫힌 실행의 결정은 그 까닭을 보인다", async () => {
    addWaitingRun();
    server.use(http.post("/agents/:id/sessions/:sid/decisions", () => apiError(409, "run_closed", "the run is no longer waiting", { run_id: "run-2" })));
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    const row = await screen.findByTestId("pending-session-s-run-2");
    expect(within(row).getByTestId("pending-session-s-run-2-run")).toHaveTextContent("자동 실행");
    expect(within(row).getByTestId("pending-session-s-run-2-deadline")).toHaveTextContent("까지 결정하지 않으면 거절됩니다.");
    // 자기 세션도 그대로 있다
    expect(screen.getByTestId("pending-session-s-2")).toBeInTheDocument();
    expect(lastRequest("GET", "/agents/ag-support/sessions?status=pending&runs=only")?.path).toContain("all_users=true");
    await user.click(row);
    await user.click(within(await screen.findByTestId("approval-card")).getByTestId("decision-approve"));
    expect(await screen.findByTestId("approval-outcome-error")).toHaveTextContent("이 자동 실행은 더 이상 결정을 기다리지 않습니다.");
  });

  it("자동 실행이 꺼진 플랫폼에서는 승인자도 실행 줄을 묻지 않는다", async () => {
    addWaitingRun();
    runsState.enabled = false;
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    expect(await screen.findByTestId("pending-session-s-2")).toBeInTheDocument();
    await waitFor(() => expect(lastRequest("GET", "/me/runs-as-me")).toBeDefined());
    expect(screen.queryByTestId("pending-session-s-run-2")).not.toBeInTheDocument();
    expect(received.some((r) => r.path.includes("runs=only"))).toBe(false);
  });

  it("승인 권한이 없으면 다른 사람의 자동 실행을 묻지 않는다", async () => {
    addWaitingRun();
    const reader = { ...meViewer, scopes_by_tenant: { default: ["agent:read", "agent:chat"] } };
    server.use(http.get("/me", () => HttpResponse.json(reader)));
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    expect(await screen.findByTestId("pending-session-s-2")).toBeInTheDocument();
    expect(screen.queryByTestId("pending-session-s-run-2")).not.toBeInTheDocument();
    expect(received.some((r) => r.path.includes("runs=only"))).toBe(false);
  });

  it("a resumed stream that fails shows the errorText sentence, not the raw code", async () => {
    server.use(
      http.post("/agents/:id/sessions/:sid/decisions", () =>
        sseResponse([
          { event: "token", data: { text: "재개합니다." } },
          { event: "error", data: { code: "provider_rate_limited", message: "429 from provider" } },
        ]),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await user.click(await screen.findByTestId("pending-session-s-2"));
    const card = await screen.findByTestId("approval-card");
    await user.click(within(card).getByTestId("decision-approve"));
    const outcome = await screen.findByTestId("approval-outcome");
    expect(await within(outcome).findByTestId("approval-outcome-1")).toHaveTextContent("LLM provider 의 요청 한도를 넘었습니다");
    expect(outcome).not.toHaveTextContent("provider_rate_limited");
    // 재개 말은 그대로 앞 줄에 남는다
    expect(within(outcome).getByTestId("approval-outcome-0")).toHaveTextContent("재개합니다.");
  });

  it("재개한 답은 채팅처럼 마크다운으로 그린다", async () => {
    server.use(
      http.post("/agents/:id/sessions/:sid/decisions", () =>
        sseResponse([
          { event: "token", data: { text: "**승인**을 받아 재개합니다.\n\n```sh\nls\n```" } },
          { event: "done", data: { session_id: "s-2", turn: 2, tool_calls: 0, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } },
        ]),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await user.click(await screen.findByTestId("pending-session-s-2"));
    await user.click(within(await screen.findByTestId("approval-card")).getByTestId("decision-approve"));
    const answer = await within(await screen.findByTestId("approval-outcome")).findByTestId("approval-outcome-0");
    expect(await within(answer).findByText("승인", { selector: "strong" })).toBeInTheDocument();
    expect(answer).not.toHaveTextContent("**");
    expect(await within(answer).findByTestId("approval-outcome-0-code")).toHaveTextContent("ls");
  });
});
