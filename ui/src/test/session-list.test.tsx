/**
 * 대화 목록의 세션 관리 (P36 4절). 런타임이 `session_admin` 을 알린 에이전트에서 "대화 검색" 칸이 300ms 뒤 서버의 제목
 * 검색을 부르는지, 행 안 입력칸의 Enter·Escape, 삭제 확인 창, 청지기 행이 지금처럼 휴지통 단추 하나인지를 본다. Agent
 * 상세의 세션 탭은 제목 칸과 같은 검색칸만 있고 이름 바꾸기·삭제는 없다.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TestApp } from "@/App";
import { SESSION_SEARCH_DEBOUNCE_MS } from "@/components/chat";
import { dictionary } from "@/i18n";
import { RUNTIME_CANCEL, RUNTIME_SESSION_ADMIN } from "@/lib/runtimeFeatures";

import { openAgentChat, openHomeChat, openHomeSession, openHomeSessionList, sendMessage } from "./chat";
import { agentSessionState, agentState, agentWithRuntimeFeatures, apiError, assistantState, http, lastRequest, received, record, server } from "./msw";

const ko = dictionary("ko");

/**
 * 런타임이 세션 관리를 알린 에이전트로 바꾼다. 알리지 않으면 목록이 id 앞자리와 메뉴 없는 행이고 검색칸이 없다는 것은
 * resource-states.test 의 표가 본다.
 */
function withSessionAdmin() {
  agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_SESSION_ADMIN)];
}

function sessionRequests(): string[] {
  return received.filter((r) => r.method === "GET" && r.path.includes("/agents/ag-support/sessions?")).map((r) => r.path);
}

describe("session list", () => {
  it("행은 제목을 보이고 제목이 없는 옛 세션은 id 앞 8자다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    expect(await openHomeSessionList(user)).toHaveTextContent("환불 기한 문의");
    expect(screen.getByTestId("home-session-s-3")).toHaveTextContent("s-3");
  });

  it("검색은 입력을 멈추고 300ms 가 지나야 서버의 q 로 한 번만 묻는다", async () => {
    withSessionAdmin();
    await openHomeSessionList(userEvent.setup());
    const searched = () => sessionRequests().filter((p) => p.includes("q="));
    const input = screen.getByTestId("home-sessions-search");
    // user-event 는 Testing Library 의 비동기 감싸개가 vitest 의 가짜 시계를 몰라 멈춘다. 입력은 change 로 넣는다
    vi.useFakeTimers();
    try {
      fireEvent.change(input, { target: { value: "환" } });
      act(() => vi.advanceTimersByTime(SESSION_SEARCH_DEBOUNCE_MS - 100));
      // 입력이 이어지면 처음부터 다시 기다린다
      fireEvent.change(input, { target: { value: "환불" } });
      act(() => vi.advanceTimersByTime(SESSION_SEARCH_DEBOUNCE_MS - 1));
      expect(searched()).toEqual([]);
      act(() => vi.advanceTimersByTime(1));
      // 같은 글은 다시 올리지 않는다
      act(() => vi.advanceTimersByTime(SESSION_SEARCH_DEBOUNCE_MS * 2));
    } finally {
      vi.useRealTimers();
    }
    await waitFor(() => {
      expect(screen.queryByTestId("home-session-s-2")).not.toBeInTheDocument();
      expect(screen.getByTestId("home-session-s-1")).toBeInTheDocument();
    });
    expect(searched()).toEqual([expect.stringContaining(`q=${encodeURIComponent("환불")}`)]);
  });

  it("이름 바꾸기는 행 안 입력칸이다. Enter 로 저장하고 Escape 로 취소한다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    await openHomeSessionList(user);

    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-rename-s-1"));
    const input = screen.getByTestId("home-session-title-s-1");
    expect(input).toHaveValue("환불 기한 문의");
    await user.clear(input);
    await user.type(input, "취소할 이름{Escape}");
    expect(screen.queryByTestId("home-session-title-s-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("home-session-s-1")).toHaveTextContent("환불 기한 문의");
    // 입력칸이 닫히면 포커스가 body 로 떨어지지 않고 그 행으로 돌아온다
    expect(screen.getByTestId("home-session-s-1")).toHaveFocus();
    expect(lastRequest("PATCH", "/agents/ag-support/sessions/s-1")).toBeUndefined();

    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-rename-s-1"));
    await user.clear(screen.getByTestId("home-session-title-s-1"));
    await user.type(screen.getByTestId("home-session-title-s-1"), "  새 이름  {Enter}");
    await waitFor(() => expect(screen.getByTestId("home-session-s-1")).toHaveTextContent("새 이름"));
    expect(screen.getByTestId("home-session-s-1")).toHaveFocus();
    expect(lastRequest("PATCH", "/agents/ag-support/sessions/s-1")?.body).toEqual({ title: "새 이름" });
  });

  it("서버가 이름을 거절하면 입력칸을 남기고 까닭을 잇고, 글을 고치면 까닭을 걷는다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    server.use(
      http.patch("/agents/:id/sessions/:sid", async ({ request }) => {
        await record(request);
        return apiError(422, "invalid_title", "title must be 1 to 100 characters after cleaning");
      }),
    );
    await openHomeSessionList(user);
    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-rename-s-1"));
    const input = screen.getByTestId("home-session-title-s-1");
    await user.clear(input);
    await user.type(input, "거절될 이름{Enter}");
    const error = await screen.findByTestId("home-session-title-s-1-error");
    expect(error).toHaveTextContent(ko.errors.codes.invalid_title);
    expect(input).toHaveValue("거절될 이름");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(ko.errors.codes.invalid_title);
    // 저장하는 동안에도 포커스는 입력칸에 남는다
    expect(input).toHaveFocus();
    await user.type(input, "!");
    expect(screen.queryByTestId("home-session-title-s-1-error")).not.toBeInTheDocument();
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("빈 이름은 서버를 부르지 않고 그 자리에서 말한다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    await openHomeSessionList(user);
    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-rename-s-1"));
    await user.clear(screen.getByTestId("home-session-title-s-1"));
    await user.type(screen.getByTestId("home-session-title-s-1"), "   {Enter}");
    expect(screen.getByTestId("home-session-title-s-1-error")).toHaveTextContent("이름을 한 글자 이상 입력해 주세요.");
    expect(lastRequest("PATCH", "/agents/ag-support/sessions/s-1")).toBeUndefined();
  });

  it("삭제는 확인 창에서 한 번 더 묻고, 열려 있던 대화를 지우면 시작 화면으로 돌아간다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    await openHomeSession(user);

    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-menu-delete-s-1"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("이 대화를 삭제할까요?");
    expect(dialog).toHaveTextContent("대화와 업로드한 파일, Agent 가 만든 파일이 지워지고 되돌릴 수 없습니다. 장기 기억과 감사 기록은 남습니다.");
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    expect(lastRequest("DELETE", "/agents/ag-support/sessions/s-1")).toBeUndefined();

    await user.click(screen.getByTestId("home-session-menu-s-1"));
    await user.click(await screen.findByTestId("home-session-menu-delete-s-1"));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(screen.queryByTestId("home-session-s-1")).not.toBeInTheDocument());
    expect(lastRequest("DELETE", "/agents/ag-support/sessions/s-1")).toBeDefined();
    expect(agentSessionState.items.map((s) => s.session_id)).not.toContain("s-1");
    expect(await screen.findByTestId("home-start")).toBeInTheDocument();
  });

  /** 삭제 확인 창에서 삭제를 눌렀는데 서버가 이 오류로 거절하게 하고, 그 창을 돌려준다. */
  async function rejectedDelete(user: ReturnType<typeof userEvent.setup>, status: number, code: string, detail: string) {
    server.use(
      http.delete("/agents/:id/sessions/:sid", async ({ request }) => {
        await record(request);
        return apiError(status, code, detail);
      }),
    );
    await openHomeSessionList(user);
    await user.click(screen.getByTestId("home-session-menu-s-1"));
    expect(screen.getByTestId("home-session-menu-panel-s-1")).toHaveAccessibleName("환불 기한 문의 동작");
    await user.click(await screen.findByTestId("home-session-menu-delete-s-1"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    return dialog;
  }

  it("진행 중인 턴이 있어 서버가 삭제를 거절하면 확인 창을 닫지 않고 중단한 뒤 지우라고 말한다 (P78 4절)", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    const dialog = await rejectedDelete(user, 409, "turn_in_progress", "a turn is already running in this session");
    // 공용 문장은 다시 보내라고 하므로 삭제에 맞춘 문장이다
    await waitFor(() => expect(dialog).toHaveTextContent(ko.shell.sessionList.deleteRunning));
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(screen.getByTestId("home-session-s-1")).toBeInTheDocument();
  });

  it("기록이 많아 한 번에 다 지우지 못하면(503) 확인 창에 다시 삭제하라고 말하고, 다시 누르면 지운다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    const dialog = await rejectedDelete(user, 503, "session_delete_incomplete", "checkpoints remain after the delete passes; delete again to continue");
    await waitFor(() => expect(dialog).toHaveTextContent(ko.errors.codes.session_delete_incomplete));
    expect(screen.getByTestId("home-session-s-1")).toBeInTheDocument();
    // 색인 행이 남아 같은 창에서 다시 누르면 이어서 지운다
    server.resetHandlers();
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(screen.queryByTestId("home-session-s-1")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("청지기 행은 지금처럼 휴지통 단추 하나라 확인 없이 지운다", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    const first = assistantState.sessions[0];
    const del = await screen.findByTestId(`home-session-delete-${first.id}`);
    expect(screen.queryByTestId(`home-session-menu-${first.id}`)).not.toBeInTheDocument();
    expect(screen.queryByTestId("home-sessions-search")).not.toBeInTheDocument();
    await user.click(del);
    await waitFor(() => expect(lastRequest("DELETE", `/assistant/sessions/${first.id}`)).toBeDefined());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("sessions tab", () => {
  it("세션 탭은 제목 칸과 검색칸만 있고 이름 바꾸기·삭제는 없다", async () => {
    const user = userEvent.setup();
    withSessionAdmin();
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    expect(await screen.findByTestId("session-row-s-1")).toHaveTextContent("환불 기한 문의");
    expect(screen.getByTestId("session-row-s-3")).toHaveTextContent("제목 없음");
    expect(screen.queryByTestId("home-session-menu-s-1")).not.toBeInTheDocument();
    await user.type(screen.getByTestId("sessions-search"), "나누기");
    // 새 질의를 읽는 동안 표가 빌 수 있으므로 거른 결과가 설 때까지 함께 기다린다
    await waitFor(() => {
      expect(screen.queryByTestId("session-row-s-1")).not.toBeInTheDocument();
      expect(screen.getByTestId("session-row-s-2")).toBeInTheDocument();
    });
    expect(lastRequest("GET", "/agents/ag-support/sessions?")?.path).toContain(`q=${encodeURIComponent("나누기")}`);
  });
});

describe("chat panel session line", () => {
  it("세션 줄은 id 대신 제목(첫 메시지 첫 줄)을 보이고 id 는 풍선 도움말에 남긴다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    expect(screen.getByTestId("chat-session")).toHaveTextContent("(첫 메시지에서 새로 만듭니다)");
    await sendMessage(user, "3 더하기 4");
    await waitFor(() => expect(screen.getByTestId("chat-session")).toHaveAttribute("title", "s-new"));
    expect(screen.getByTestId("chat-session")).toHaveTextContent("3 더하기 4");
    await user.click(await screen.findByTestId("chat-new-session"));
    expect(screen.getByTestId("chat-session")).toHaveTextContent("(첫 메시지에서 새로 만듭니다)");
  });
});
