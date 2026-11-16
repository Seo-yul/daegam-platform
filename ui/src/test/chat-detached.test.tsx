/**
 * 연결과 떨어져 도는 Agent 의 턴 (P78 4절, D92). 응답 머리 `X-Daegam-Detached-Turn` 이 있으면 패널은 스트림을 여는 순간 세션을 잡고,
 * 마감 없이 끊긴 스트림을 세션 읽기로 따라가며, 중단은 시도 id 를 실은 중지 API 하나다. 머리가 없는 옛 이미지의 두 길(D65)은
 * agent-chat.test.tsx 가 본다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "@/App";
import type { AssistantTranscriptItem } from "@/api/types";
import { STOP_PENDING_MS } from "@/components/ChatPanel";
import { SESSION_GONE_MS } from "@/components/chat";
import { dictionary } from "@/i18n";
import { RUNTIME_CANCEL } from "@/lib/runtimeFeatures";

import { openAgentChat, openHomeSession, pickHomeTarget, sendMessage, startHomeChat, transcriptRows } from "./chat";
import {
  DETACHED_TURN_HEADER,
  DETACHED_TURN_PREFIX,
  HttpResponse,
  SESSION_ID_HEADER,
  SLOW_TURN_TOKEN,
  TURN_ID_HEADER,
  agentSessionState,
  agentState,
  agentWithRuntimeFeatures,
  apiError,
  heldSse,
  http,
  lastRequest,
  received,
  record,
  server,
  tokenUsage,
} from "./msw";

const ko = dictionary("ko");

/** 따라가는 턴의 시도 id 다. 목의 s-1 은 턴 셋을 마쳤고 넷째가 돈다. */
const TURN_ID = "turn-s-1-4";
const ASKED: AssistantTranscriptItem[] = [
  { kind: "user", text: "보고서 써 줘" },
  { kind: "assistant", text: "쓰는 중입니다.", streaming: false },
];
const DONE: AssistantTranscriptItem[] = [...ASKED, { kind: "done", usage: tokenUsage(10, 5), turn: 4, tool_calls: 0, turn_id: TURN_ID }];

interface SessionState {
  running: boolean;
  items: AssistantTranscriptItem[];
  /** 주면 세션 읽기가 이 상태로 실패한다. */
  status?: number;
  /** 실패의 오류 코드. 없으면 상태마다의 기본 코드다(readFailure). */
  code?: string;
  /** 세션 읽기가 네트워크 오류로 실패한다. */
  network?: boolean;
  /** 전체 읽기만 이 값으로 답한다. 가벼운 읽기와 전체 읽기 사이에 새 시도가 리스를 잡은 때다. */
  fullRunning?: boolean;
}

/**
 * s-1 의 세션 읽기를 상태 하나로 답한다. 테스트가 상태를 바꿔 턴의 흐름을 만든다. 가벼운 읽기(`view=progress`)는 다시 읽을
 * 간격 3초를 싣는다(플랫폼이 Agent 의 Service 로 바로 닿는 때).
 */
function serveSession(state: SessionState): void {
  server.use(
    http.get("/agents/:id/sessions/:sid", async ({ request }) => {
      const r = await record(request);
      if (state.network) return HttpResponse.error();
      if (state.status) return apiError(state.status, state.code ?? readFailure(state.status), "read failed");
      const row = agentSessionState.items.find((s) => s.session_id === "s-1")!;
      const progress = new URL(r.path, "http://localhost").searchParams.get("view") === "progress";
      const running = progress ? state.running : (state.fullRunning ?? state.running);
      return HttpResponse.json({
        session: { ...row, running, turn_id: running ? TURN_ID : null },
        messages: [],
        pending: false,
        transcript: { items: state.items, limit: 500 },
        ...(progress ? { poll_seconds: 3 } : {}),
      });
    }),
  );
}

/** 실패한 세션 읽기의 코드다. */
function readFailure(status: number): string {
  if (status === 404) return "session_not_found";
  if (status === 409) return "not_deployed";
  return "runtime_unreachable";
}

/** s-1 의 턴을 토큰 하나 뒤에 마감 없이 끊는 스트림으로 연다. 런타임은 턴을 이어 간다. */
function serveBrokenStream(): void {
  server.use(
    http.post("/agents/:id/chat", async ({ request }) => {
      await record(request);
      const turn = heldSse([{ event: "token", data: { text: "쓰는 중" } }], {
        headers: { [SESSION_ID_HEADER]: "s-1", [TURN_ID_HEADER]: TURN_ID, [DETACHED_TURN_HEADER]: "1" },
      });
      setTimeout(() => turn.close(), 20);
      return turn.response;
    }),
  );
}

/** 중지 API 의 답을 정한다. `then` 은 답하기 전에 부른다(턴이 그사이 끝난 것처럼). */
function serveCancel(answer: () => Response, then?: () => void): void {
  server.use(
    http.post("/agents/:id/sessions/:sid/cancel", async ({ request }) => {
      await record(request);
      then?.();
      return answer();
    }),
  );
}

/** 가짜 시계로 돈다. 따라가기는 3초마다, 중단의 안내는 45초 뒤, 대화를 잃은 판정은 30초 뒤다. */
async function withFakeTimers(run: (user: UserEvent) => Promise<void>): Promise<void> {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    await run(userEvent.setup({ advanceTimers: vi.advanceTimersByTime }));
  } finally {
    vi.useRealTimers();
  }
}

/** 홈 챗에서 s-1 을 열고 그 대화의 턴이 돈다는 안내가 설 때까지 기다린다. */
async function openRunningSession(user: UserEvent): Promise<void> {
  await openHomeSession(user, "s-1");
  expect(await screen.findByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.turnRunning);
}

function cancelBody(sessionId = "s-1"): unknown {
  return lastRequest("POST", `/agents/ag-support/sessions/${sessionId}/cancel`)?.body;
}

describe("detached agent turns", () => {
  beforeEach(() => {
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL)];
  });

  it("중단은 응답 머리의 시도 id 를 실어 그 턴만 멈추고, 스트림이 런타임의 마감을 보인다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, `${DETACHED_TURN_PREFIX} 답해 줘`);
    await waitFor(() => expect(screen.getByTestId("chat-message-2")).toHaveTextContent(SLOW_TURN_TOKEN.trim()));
    // 첫 턴의 세션을 done 이 아니라 스트림을 여는 순간 잡는다
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-session-id", "s-new");
    await user.click(screen.getByTestId("chat-abort"));
    await waitFor(() => expect(cancelBody("s-new")).toEqual({ turn_id: "turn-s-new-1" }));
    const ended = await screen.findByTestId("chat-message-3");
    expect(ended).toHaveAttribute("data-role", "error");
    expect(ended).toHaveTextContent(ko.errors.codes.turn_cancelled);
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
  });

  it("마감 없이 끊긴 스트림은 오류 대신 세션을 읽어 따라가고, 턴이 끝나면 결과를 보인다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      serveBrokenStream();
      await openAgentChat();
      await sendMessage(user, "보고서 써 줘");
      expect(await screen.findByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.turnRunning);
      expect(screen.getByTestId("chat-running")).toHaveTextContent(ko.chat.runningBadge);
      expect(screen.getByTestId("chat-abort")).toBeInTheDocument();
      expect(screen.getByTestId("chat-send")).toBeDisabled();
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
      // 저장된 항목으로 바꿔 보인다
      expect(transcriptRows().at(-1)).toHaveTextContent("쓰는 중입니다.");

      state.running = false;
      state.items = DONE;
      await vi.advanceTimersByTimeAsync(3000);
      await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
      expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
      expect(screen.queryByTestId("chat-running")).not.toBeInTheDocument();
      expect(screen.getByTestId("chat-new-session")).toBeInTheDocument();
      // 따라가는 동안은 가벼운 읽기다
      expect(lastRequest("GET", "/agents/ag-support/sessions/s-1?view=progress")).toBeDefined();
    });
  });

  it("되살린 대화의 도는 턴은 홈 챗 입력칸의 중단으로 멈추고, 중지가 실패하면 턴이 그대로 돈다며 다시 누르라고 한다", async () => {
    const user = userEvent.setup();
    serveSession({ running: true, items: ASKED });
    serveCancel(() => apiError(503, "runtime_unreachable", "the runtime did not answer"));
    await openRunningSession(user);
    // 세션 줄이 없는 홈 챗은 입력칸의 단추가 중단이다
    await user.click(screen.getByTestId("chat-abort"));
    expect(await screen.findByTestId("chat-error")).toHaveTextContent(ko.chat.notice.cancelFailed);
    expect(cancelBody()).toEqual({ turn_id: TURN_ID });
    expect(screen.getByTestId("chat-abort")).toBeInTheDocument();
  });

  it("막 끝난 턴의 중단(409 no_turn_in_progress)은 오류 대신 끝난 결과를 보인다", async () => {
    const user = userEvent.setup();
    const state: SessionState = { running: true, items: ASKED };
    serveSession(state);
    serveCancel(
      () => apiError(409, "no_turn_in_progress", "no turn is running in this session"),
      () => {
        state.running = false;
        state.items = DONE;
      },
    );
    await openRunningSession(user);
    await user.click(screen.getByTestId("chat-abort"));
    await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
  });

  it("받아들여진 중단 뒤 45초가 지나도 턴이 돌면 기다리라고 안내하고 계속 따라간다", async () => {
    await withFakeTimers(async (user) => {
        serveSession({ running: true, items: ASKED });
      serveCancel(() => HttpResponse.json({ session_id: "s-1" }, { status: 202 }));
      await openRunningSession(user);
      await user.click(screen.getByTestId("chat-abort"));
      await waitFor(() => expect(cancelBody()).toEqual({ turn_id: TURN_ID }));
      await vi.advanceTimersByTimeAsync(STOP_PENDING_MS - 1_000);
      expect(screen.getByTestId("chat-notice")).not.toHaveTextContent(ko.chat.notice.stopPending);
      await vi.advanceTimersByTimeAsync(2_000);
      await waitFor(() => expect(screen.getByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.stopPending));
      expect(screen.getByTestId("chat-abort")).toBeInTheDocument();
    });
  });

  it("따라가는 동안 다른 답 없이 404 만 30초 받으면 대화를 잃었다고 말하고 멈춘다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      await openRunningSession(user);
      // 한 번의 404 는 그 세션을 모르는 다른 파드일 수 있다. 30초는 첫 404(3초 뒤 읽기)부터 잰다
      state.status = 404;
      await vi.advanceTimersByTimeAsync(SESSION_GONE_MS);
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(4_000);
      expect(await screen.findByTestId("chat-error")).toHaveTextContent(ko.chat.notice.sessionGone);
      expect(screen.queryByTestId("chat-abort")).not.toBeInTheDocument();
    });
  });

  it("5xx 로 실패한 읽기는 보이던 항목을 두고 간격을 늘려 다시 읽으며 대화를 잃었다고 보지 않는다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      await openRunningSession(user);
      state.status = 503;
      await vi.advanceTimersByTimeAsync(60_000);
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
      expect(transcriptRows().at(-1)).toHaveTextContent("쓰는 중입니다.");
      state.status = undefined;
      state.running = false;
      state.items = DONE;
      await vi.advanceTimersByTimeAsync(30_000);
      await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
    });
  });

  it("마감 항목 없이 끝난 턴(파드가 죽었다)에는 화면이 turn_aborted 줄을 붙인다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      await openRunningSession(user);
      // 리스가 끝나 더는 돌지 않는데 done 이 없다
      state.running = false;
      await vi.advanceTimersByTimeAsync(3000);
      const last = await waitFor(() => {
        const row = transcriptRows().at(-1)!;
        expect(row).toHaveAttribute("data-role", "error");
        return row;
      });
      expect(last).toHaveTextContent(ko.errors.codes.turn_aborted);
      expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
    });
  });

  it("홈 챗의 첫 턴은 스트림을 열 때 세션을 잡아도 흐르던 항목을 지우지 않고, 다른 대화로 옮겨도 턴을 멈추지 않는다", async () => {
    const user = userEvent.setup();
    await pickHomeTarget(user);
    await startHomeChat(user, `${DETACHED_TURN_PREFIX} 답해 줘`);
    await waitFor(() => expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-session-id", "s-new"));
    await waitFor(() => expect(screen.getByTestId("chat-message-2")).toHaveTextContent(SLOW_TURN_TOKEN.trim()));
    // 위가 돌려준 세션은 이 패널이 연 대화라 다시 읽지 않는다
    expect(received.some((r) => r.method === "GET" && r.path === "/agents/ag-support/sessions/s-new")).toBe(false);
    // 목록이 그 대화를 진행 중으로 보인다
    expect(await screen.findByTestId("home-session-running-s-new")).toHaveTextContent(ko.chat.runningBadge);

    // 다른 대화로 옮기면 읽기만 닫는다. 중단이 아니다
    await user.click(screen.getByTestId("home-session-s-1"));
    await waitFor(() => expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-session-id", "s-1"));
    expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/cancel")).toBeUndefined();
    expect(screen.queryByTestId("chat-abort")).not.toBeInTheDocument();
    expect(screen.queryByText(SLOW_TURN_TOKEN.trim())).not.toBeInTheDocument();

    // 돌아오면 다시 읽고, 턴이 아직 도니 따라간다
    await user.click(screen.getByTestId("home-session-s-new"));
    expect(await screen.findByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.turnRunning);
    expect(screen.getByTestId("chat-abort")).toBeInTheDocument();
  });

  it.each([
    ["네트워크 오류", { network: true }],
    ["503", { status: 503 }],
    ["404", { status: 404 }],
  ] as const)("끊긴 스트림 뒤 첫 세션 읽기가 실패해도(%s) 오류 대신 따라가고 결과를 보인다", async (_, failure) => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED, ...failure };
      serveSession(state);
      serveBrokenStream();
      await openAgentChat();
      await sendMessage(user, "보고서 써 줘");
      expect(await screen.findByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.turnRunning);
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
      expect(screen.getByTestId("chat-abort")).toBeInTheDocument();
      // 연결이 돌아오면 다음 읽기가 결과를 보인다
      Object.assign(state, { network: false, status: undefined, running: false, items: DONE });
      await vi.advanceTimersByTimeAsync(30_000);
      await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    });
  });

  it("가벼운 읽기는 끝났다는데 전체 읽기가 돈다고 하면(새 시도) 계속 따라간다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      await openRunningSession(user);
      Object.assign(state, { running: false, fullRunning: true });
      await vi.advanceTimersByTimeAsync(3000);
      expect(screen.getByTestId("chat-notice")).toHaveTextContent(ko.chat.notice.turnRunning);
      Object.assign(state, { fullRunning: undefined, items: DONE });
      await vi.advanceTimersByTimeAsync(3000);
      await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
      expect(screen.queryByTestId("chat-abort")).not.toBeInTheDocument();
    });
  });

  it.each([
    ["배포 해제", "not_deployed"],
    // 그사이 Agent 가 잠들었다. 읽기는 깨우지 않으므로 다시 읽어도 같은 답이다 (P44 8절)
    ["잠든 Agent", "agent_asleep"],
  ])("따라가는 동안 404 가 아닌 4xx(%s)는 다시 읽지 않고 따라가기를 멈춘다", async (_name, code) => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      await openRunningSession(user);
      Object.assign(state, { status: 409, code });
      await vi.advanceTimersByTimeAsync(3000);
      expect(await screen.findByTestId("chat-error")).toBeInTheDocument();
      expect(screen.queryByTestId("chat-abort")).not.toBeInTheDocument();
      expect(screen.queryByTestId("chat-notice")).not.toBeInTheDocument();
    });
  });

  it("다시 눌러도 소용없는 중지 실패(409 not_deployed)는 다시 누르라고 하지 않고 따라가기를 멈춘다", async () => {
    const user = userEvent.setup();
    serveSession({ running: true, items: ASKED });
    serveCancel(() => apiError(409, "not_deployed", "the agent is not deployed"));
    await openRunningSession(user);
    await user.click(screen.getByTestId("chat-abort"));
    const error = await screen.findByTestId("chat-error");
    expect(error).not.toHaveTextContent(ko.chat.notice.cancelFailed);
    expect(screen.queryByTestId("chat-abort")).not.toBeInTheDocument();
  });

  it("중지가 실패한 뒤 턴이 스스로 끝나면 다시 누르라는 안내를 걷는다", async () => {
    await withFakeTimers(async (user) => {
        const state: SessionState = { running: true, items: ASKED };
      serveSession(state);
      serveCancel(() => apiError(503, "runtime_unreachable", "the runtime did not answer"));
      await openRunningSession(user);
      await user.click(screen.getByTestId("chat-abort"));
      expect(await screen.findByTestId("chat-error")).toHaveTextContent(ko.chat.notice.cancelFailed);
      Object.assign(state, { running: false, items: DONE });
      await vi.advanceTimersByTimeAsync(3000);
      await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
      expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    });
  });

  it("세션 탭은 턴이 도는 세션에 진행 중 배지를 단다", async () => {
    Object.assign(agentSessionState.items[0], { running: true, turn_id: TURN_ID });
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    const row = await screen.findByTestId("session-row-s-1");
    expect(within(row).getByTestId("session-running-s-1")).toHaveTextContent(ko.chat.runningBadge);
    expect(within(await screen.findByTestId("session-row-s-2")).queryByTestId("session-running-s-2")).not.toBeInTheDocument();
  });
});
