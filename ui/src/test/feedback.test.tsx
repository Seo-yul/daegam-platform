import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "@/App";
import { ChatItemView, type TurnFeedback } from "@/components/chat";
import { dictionary } from "@/i18n";

import { firstTurn, openHomeSession, transcriptRows } from "./chat";
import { stubDownloads } from "./downloads";
import { selectValue } from "./select";
import { HttpResponse, agentFeedbackState, feedbackFixture, http, lastRequest, meViewer, received, record, resetAgentFeedbackState, server } from "./msw";
import { renderWithProviders } from "./render";
import { TURN_FIXTURE } from "./runtimeFixtures";

const ko = dictionary("ko");

/** 열린 채팅의 턴 마감 줄이다. */
function doneRow(): HTMLElement {
  const [done] = transcriptRows({ role: "done" });
  if (!done) throw new Error("턴 마감 줄이 없다");
  return done;
}

function thumb(name: "좋아요" | "아쉬워요"): HTMLElement {
  return within(doneRow()).getByRole("button", { name });
}

const TURN_PATH = "/agents/ag-support/sessions/s-new/turns/turn-s-new-1/feedback";

describe("답변의 엄지 (P36 6절)", () => {
  it("좋아요는 바로 저장하고 의견 남기기를 세우며, 다시 누르면 거둔다", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    const up = thumb("좋아요");
    expect(up).toHaveAttribute("aria-pressed", "false");

    await user.click(up);
    await waitFor(() => expect(thumb("좋아요")).toHaveAttribute("aria-pressed", "true"));
    expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "up", tags: [], comment: null, ephemeral: true });
    expect(within(doneRow()).getByRole("button", { name: "의견 남기기" })).toBeInTheDocument();

    await user.click(thumb("좋아요"));
    await waitFor(() => expect(thumb("좋아요")).toHaveAttribute("aria-pressed", "false"));
    expect(lastRequest("DELETE", TURN_PATH)).toBeDefined();
    expect(agentFeedbackState.items).toEqual([]);
  });

  it("아쉬워요는 창에서 이유와 의견을 받은 뒤에 저장하고, 취소하면 아무것도 남기지 않는다", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    await user.click(thumb("아쉬워요"));
    let dialog = await screen.findByTestId("feedback-dialog");
    expect(dialog).toHaveTextContent("어떤 점이 아쉬웠나요?");
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    expect(screen.queryByTestId("feedback-dialog")).not.toBeInTheDocument();
    expect(lastRequest("PUT", TURN_PATH)).toBeUndefined();

    await user.click(thumb("아쉬워요"));
    dialog = await screen.findByTestId("feedback-dialog");
    const chip = within(dialog).getByRole("button", { name: "사실과 다릅니다" });
    await user.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    await user.click(within(dialog).getByRole("button", { name: "답변이 충분하지 않습니다" }));
    const comment = within(dialog).getByRole("textbox", { name: "의견" });
    expect(comment).toHaveAccessibleDescription(ko.chat.feedback.commentHint);
    await user.type(comment, "  환불 기한이 틀렸다  ");
    await user.click(within(dialog).getByTestId("feedback-submit"));

    await waitFor(() => expect(screen.queryByTestId("feedback-dialog")).not.toBeInTheDocument());
    expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "down", tags: ["inaccurate", "incomplete"], comment: "환불 기한이 틀렸다", ephemeral: true });
    expect(thumb("아쉬워요")).toHaveAttribute("aria-pressed", "true");

    // 의견 남기기는 같은 평가를 앞의 이유와 의견으로 채워 연다
    await user.click(within(doneRow()).getByRole("button", { name: "의견 남기기" }));
    dialog = await screen.findByTestId("feedback-dialog");
    expect(within(dialog).getByRole("button", { name: "사실과 다릅니다" })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByRole("textbox", { name: "의견" })).toHaveValue("환불 기한이 틀렸다");
  });

  it("평가를 고쳐도 평가 사례로 쓰는 동의는 남긴 값 그대로 보낸다 (P45 8절)", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    // 동의가 켜진 평가를 돌려주는 서버다
    const consented = (rating: "up" | "down") =>
      http.put("/agents/:id/sessions/:sid/turns/:tid/feedback", () => HttpResponse.json(feedbackFixture({ session_id: "s-new", turn_id: "turn-s-new-1", rating, tags: [], comment: null, evaluation_use: true })), { once: true });
    server.use(consented("down"));
    await user.click(thumb("아쉬워요"));
    await user.click(within(await screen.findByTestId("feedback-dialog")).getByTestId("feedback-submit"));
    await waitFor(() => expect(thumb("아쉬워요")).toHaveAttribute("aria-pressed", "true"));
    // 좋아요로 바꾸는 엄지 한 번(press)도 동의를 싣는다
    await user.click(thumb("좋아요"));
    await waitFor(() => expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "up", tags: [], comment: null, ephemeral: true, evaluation_use: true }));
    // 의견 창의 저장도 싣는다. 목 서버는 받은 동의를 그대로 남긴다
    await user.click(within(doneRow()).getByRole("button", { name: "의견 남기기" }));
    await user.click(within(await screen.findByTestId("feedback-dialog")).getByTestId("feedback-submit"));
    await waitFor(() => expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "up", tags: [], comment: null, ephemeral: true, evaluation_use: true }));
  });

  it("의견 창의 동의 스위치는 꺼진 채 시작하고 무엇을 누가 얼마나 읽는지 말하며, 켜면 동의를 싣고 끄면 거둔다 (P45 8절)", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    await user.click(thumb("아쉬워요"));
    let dialog = await screen.findByTestId("feedback-dialog");
    const consent = within(dialog).getByRole("switch", { name: ko.chat.feedback.evaluationConsent });
    expect(consent).not.toBeChecked();
    // 남는 날 수는 서버가 세션 평가와 함께 보낸 설치의 값이다(목은 플랫폼 기본값 180)
    expect(consent).toHaveAccessibleDescription(ko.chat.feedback.evaluationConsentHint(180));
    expect(within(dialog).getByTestId("feedback-evaluation-use-hint")).toHaveTextContent("developer 이상의 role 을 가진 사람과 플랫폼 관리자가");
    await user.click(consent);
    await user.click(within(dialog).getByTestId("feedback-submit"));
    await waitFor(() => expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "down", tags: [], comment: null, ephemeral: true, evaluation_use: true }));

    // 다시 연 창은 남긴 동의로 켜져 있고, 끄고 저장하면 동의를 싣지 않는다(서버는 없는 값을 거둔 것으로 읽는다)
    await user.click(within(doneRow()).getByRole("button", { name: "의견 남기기" }));
    dialog = await screen.findByTestId("feedback-dialog");
    expect(within(dialog).getByTestId("feedback-evaluation-use")).toBeChecked();
    await user.click(within(dialog).getByTestId("feedback-evaluation-use"));
    await user.click(within(dialog).getByTestId("feedback-submit"));
    await waitFor(() => expect(lastRequest("PUT", TURN_PATH)?.body).toEqual({ rating: "down", tags: [], comment: null, ephemeral: true }));
    expect(agentFeedbackState.items[0].evaluation_use).toBe(false);
  });

  it("저장이 거절되면 엄지 옆에 까닭을 보이고 눌린 상태를 바꾸지 않는다", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    server.use(http.put("/agents/:id/sessions/:sid/turns/:tid/feedback", () => HttpResponse.json({ detail: { code: "turn_not_found", detail: "gone" } }, { status: 404 })));
    await user.click(thumb("좋아요"));
    expect(await within(doneRow()).findByRole("alert")).toHaveTextContent("이 답변은 다시 생성됐거나 대화에서 사라져 평가할 수 없습니다.");
    expect(thumb("좋아요")).toHaveAttribute("aria-pressed", "false");
  });

  it("엄지 옆의 저장 실패는 의견 창을 열면 걷히고 창의 저장이 평가를 남긴다", async () => {
    const user = userEvent.setup();
    await firstTurn(user, "3 더하기 4");
    server.use(http.put("/agents/:id/sessions/:sid/turns/:tid/feedback", () => HttpResponse.json({ detail: { code: "turn_not_found", detail: "gone" } }, { status: 404 }), { once: true }));
    const thumbError = () => screen.queryByTestId(`${doneRow().dataset.testid}-error`);
    await user.click(thumb("좋아요"));
    await waitFor(() => expect(thumbError()).toBeInTheDocument());

    await user.click(thumb("아쉬워요"));
    const dialog = await screen.findByTestId("feedback-dialog");
    expect(thumbError()).toBeNull();
    await user.click(within(dialog).getByTestId("feedback-submit"));
    await waitFor(() => expect(screen.queryByTestId("feedback-dialog")).not.toBeInTheDocument());
    expect(thumb("아쉬워요")).toHaveAttribute("aria-pressed", "true");
    expect(thumbError()).toBeNull();
  });

  it("다시 연 대화는 내 평가로 눌린 상태를 되살린다", async () => {
    const user = userEvent.setup();
    resetAgentFeedbackState([feedbackFixture({ session_id: "s-1", turn_id: "turn-s-1-1", user_sub: "header-admin", rating: "down" })]);
    await openHomeSession(user);
    await waitFor(() => expect(thumb("아쉬워요")).toHaveAttribute("aria-pressed", "true"));
    expect(thumb("좋아요")).toHaveAttribute("aria-pressed", "false");
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-1/feedback")).toBeDefined();
  });

  it("세션에 아직 평가가 없어도 동의 안내는 서버가 보낸 설치의 보존 기간을 적고, 저장한 뒤에도 그 값을 지킨다 (P45 8절)", async () => {
    const user = userEvent.setup();
    resetAgentFeedbackState([], 45);
    await firstTurn(user, "3 더하기 4");
    await user.click(thumb("아쉬워요"));
    const dialog = await screen.findByTestId("feedback-dialog");
    await waitFor(() => expect(within(dialog).getByTestId("feedback-evaluation-use-hint")).toHaveTextContent(ko.chat.feedback.evaluationConsentHint(45)));
    await user.click(within(dialog).getByTestId("feedback-submit"));
    await waitFor(() => expect(screen.queryByTestId("feedback-dialog")).not.toBeInTheDocument());

    // 저장이 고친 캐시에도 보존 기간이 남는다
    await user.click(within(doneRow()).getByRole("button", { name: "의견 남기기" }));
    expect(within(await screen.findByTestId("feedback-dialog")).getByTestId("feedback-evaluation-use-hint")).toHaveTextContent(ko.chat.feedback.evaluationConsentHint(45));
  });

  it("보존 기간을 싣지 않는 옛 플랫폼이면 동의 안내는 플랫폼 기본값을 적는다", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/agents/:id/sessions/:sid/feedback", async ({ request }) => {
        await record(request);
        return HttpResponse.json({ items: [] });
      }),
    );
    await firstTurn(user, "3 더하기 4");
    await waitFor(() => expect(lastRequest("GET", "/agents/ag-support/sessions/s-new/feedback")).toBeDefined());
    await user.click(thumb("아쉬워요"));
    const dialog = await screen.findByTestId("feedback-dialog");
    expect(within(dialog).getByTestId("feedback-evaluation-use-hint")).toHaveTextContent(ko.chat.feedback.evaluationConsentHint(180));
  });

  it("엄지는 부르는 쪽이 피드백을 넘기고 턴에 turn_id 가 있을 때만 선다", () => {
    const feedback: TurnFeedback = { of: () => undefined, save: vi.fn(), withdraw: vi.fn() };
    const done = TURN_FIXTURE.events.done;
    const item = { kind: "done" as const, usage: done.usage, turn: done.turn, tool_calls: done.tool_calls };
    const { rerender } = renderWithProviders(<ChatItemView index={0} item={{ ...item, turn_id: done.turn_id }} feedback={feedback} />);
    expect(screen.getByRole("button", { name: "좋아요" })).toBeInTheDocument();
    // 옛 전사(turn_id 없음)와 읽기 전용 화면(피드백 없음)에는 엄지가 없다
    rerender(<ChatItemView index={0} item={item} feedback={feedback} />);
    expect(screen.queryByRole("button", { name: "좋아요" })).not.toBeInTheDocument();
    rerender(<ChatItemView index={0} item={{ ...item, turn_id: done.turn_id }} />);
    expect(screen.queryByRole("button", { name: "좋아요" })).not.toBeInTheDocument();
  });
});

describe("피드백 탭", () => {
  let saved: string[] = [];
  beforeEach(() => {
    saved = stubDownloads();
    resetAgentFeedbackState([
      feedbackFixture({ id: "fb-1", created_at: "2026-09-02T10:00:00+00:00" }),
      feedbackFixture({ id: "fb-2", session_id: "s-3", turn_id: "turn-s-3-2", rating: "up", tags: [], comment: null, superseded: true, created_at: "2026-09-02T09:00:00+00:00" }),
    ]);
  });

  it("평가를 새것부터 보이고 다시 생성된 답변을 표시하며 지난 7일 수를 세고, 거르개는 서버에 평가와 이유를 넘기며 내보내기는 지금 평가 거르개로 받는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=feedback" />);
    const table = await screen.findByTestId("feedback-table");
    await waitFor(() => expect(within(table).getByTestId("feedback-row-fb-1")).toBeInTheDocument());
    const first = within(table).getByTestId("feedback-row-fb-1");
    expect(first).toHaveTextContent("dev@example.com");
    expect(first).toHaveTextContent("아쉬워요");
    expect(first).toHaveTextContent("사실과 다릅니다");
    expect(first).toHaveTextContent("환불 기한이 틀렸습니다.");
    expect(within(table).getByTestId("feedback-row-fb-2")).toHaveTextContent(ko.chat.feedback.superseded);
    expect(screen.getByTestId("feedback-recent")).toHaveTextContent("지난 7일 · 좋아요 1 · 아쉬워요 1");

    await selectValue(user, "feedback-rating", "up");
    await waitFor(() => expect(screen.queryByTestId("feedback-row-fb-1")).not.toBeInTheDocument());
    expect(lastRequest("GET", "/agents/ag-support/feedback?")?.path).toContain("rating=up");
    await selectValue(user, "feedback-tag", "inaccurate");
    expect(await screen.findByText("조건에 맞는 피드백이 없습니다.")).toBeInTheDocument();
    expect(lastRequest("GET", "/agents/ag-support/feedback?")?.path).toContain("tag=inaccurate");

    await user.click(screen.getByTestId("feedback-export"));
    await waitFor(() => expect(saved).toEqual(["feedback-support.ndjson"]));
    expect(lastRequest("GET", "/agents/ag-support/feedback/export")?.path).toBe("/api/agents/ag-support/feedback/export?rating=up");
  });

  it("행을 누르면 세션 탭이 그 대화를 열고 그 답변을 강조한다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=feedback" />);
    await user.click(await screen.findByTestId("feedback-row-fb-1"));
    const detail = await screen.findByTestId("session-detail");
    expect(detail).toHaveTextContent("s-1");
    const focused = await within(detail).findByTestId("session-focused-turn");
    expect(within(focused).getByTestId(/^chat-message-\d+$/)).toHaveAttribute("data-turn-id", "turn-s-1-1");
    // 세션 탭은 남의 대화도 보이는 읽기 전용이라 엄지가 없다
    expect(within(detail).queryByRole("button", { name: "좋아요" })).not.toBeInTheDocument();

    // 넘겨받은 세션과 답변은 한 번만 쓴다. 다른 탭에 다녀오면 아무 세션도 고르지 않은 탭이다
    await user.click(screen.getByTestId("tab-overview"));
    await user.click(screen.getByTestId("tab-sessions"));
    expect(await screen.findByTestId("session-detail")).toHaveTextContent("세션을 선택하면");
    expect(screen.queryByTestId("session-focused-turn")).not.toBeInTheDocument();
  });

  it("승인 권한이 없으면 목록을 부르지 않고 그 사실을 말한다", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meViewer)));
    render(<TestApp initialPath="/agents/ag-support?tab=feedback" />);
    expect(await screen.findByTestId("feedback-forbidden")).toHaveTextContent("승인 권한이 있는 사람만 볼 수 있습니다.");
    expect(received.some((r) => r.path.startsWith("/api/agents/ag-support/feedback"))).toBe(false);
  });
});
