import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Bubble, lastTurn, rewindLastTurn, type ChatItem } from "@/components/chat";
import { dictionary } from "@/i18n";
import { consumeChatStream } from "@/lib/chatTurn";
import { RUNTIME_CANCEL, RUNTIME_REGENERATE } from "@/lib/runtimeFeatures";

import { firstTurn, sendMessage, transcriptRows, waitForTurnEnd } from "./chat";
import { stubClipboard, stubPlainHttpCopy } from "./clipboard";
import { submitConfirm } from "./confirm";
import { agentState, agentWithRuntimeFeatures, lastRequest, sseBlock, type SseEvent } from "./msw";
import { TURN_FIXTURE } from "./runtimeFixtures";

const ko = dictionary("ko");

/** 말풍선의 동작 (D61). 복사는 모든 말풍선에 있고 마크다운 원문을 그대로 옮긴다. */
describe("말풍선 복사", () => {
  it("어시스턴트 답은 그린 글이 아니라 마크다운 원문을 복사한다", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    render(<Bubble role="assistant" text={"**굵게** 쓴 답\n\n- 하나"} testId="b" />);
    const copy = screen.getByTestId("b-copy");
    expect(copy).toHaveAttribute("aria-label", "답변 복사");
    await user.click(copy);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("**굵게** 쓴 답\n\n- 하나"));
    expect(copy).toHaveAttribute("aria-label", "답변을 복사했습니다");
  });

  it("복사하지 못하면 그 자리에서 직접 선택하라고 말한다", async () => {
    const user = userEvent.setup();
    // 평문 http 라 Clipboard API 가 없고 폴백도 거절된 경우다
    stubPlainHttpCopy(false);
    render(<Bubble role="user" text="질문입니다" testId="u" />);
    const copy = screen.getByTestId("u-copy");
    await user.click(copy);
    await waitFor(() => expect(copy).toHaveAttribute("aria-label", "메시지를 복사하지 못했습니다"));
    expect(copy).toHaveAttribute("title", "복사하지 못했습니다. 메시지를 직접 선택해 복사해 주세요.");
  });

  it("사용자 말풍선은 쓴 그대로 평문이다", () => {
    const { container } = render(<Bubble role="user" text={"**별표** 그대로\n```\n코드 펜스도\n```"} testId="u" />);
    expect(container.querySelector("strong, pre")).toBeNull();
    expect(screen.getByTestId("u")).toHaveTextContent("**별표** 그대로 ``` 코드 펜스도 ```");
  });
});

/** 런타임이 다시 생성을 알린 에이전트로 바꾼다. 알리지 않으면 두 단추가 없다는 것은 resource-states.test 의 표가 본다. */
function withRegenerate() {
  agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_REGENERATE)];
}

const REGENERATE_PATH = "/agents/ag-support/sessions/s-new/regenerate";

function transcriptRoles(): (string | undefined)[] {
  return transcriptRows().map((el) => el.dataset.role);
}

function userTexts(): string[] {
  return transcriptRows({ role: "user" }).map((el) => el.textContent ?? "");
}

/** 마지막 턴의 다시 생성과 메시지 수정 (P36 5절). */
describe("다시 생성과 메시지 수정", () => {
  it("도구를 부른 턴은 먼저 묻고, 확인하면 마지막 질문 뒤를 걷어 낸 채 다시 생성한다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    await firstTurn(user, "3 더하기 4");
    await user.click(await screen.findByTestId("chat-regenerate"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent(ko.chat.turnActions.confirmTitle);
    expect(dialog).toHaveTextContent("이미 실행된 작업은 취소되지 않습니다.");
    // 그만두면 아무것도 보내지 않는다
    await user.click(within(dialog).getByTestId("confirm-cancel"));
    expect(lastRequest("POST", REGENERATE_PATH)).toBeUndefined();

    await user.click(screen.getByTestId("chat-regenerate"));
    await submitConfirm(user);
    const transcript = screen.getByTestId("chat-transcript");
    await waitFor(() => expect(transcript).toHaveTextContent("다시 생성한 답변입니다."));
    expect(transcript).not.toHaveTextContent("결과는 7 입니다.");
    expect(userTexts()).toEqual(["3 더하기 4"]);
    // 상세의 채팅은 테스트 채팅이라 ephemeral 을 다시 싣는다. 질문을 그대로 쓰면 message 가 없다
    expect(lastRequest("POST", REGENERATE_PATH)?.body).toEqual({ ephemeral: true });
  });

  it("연필은 마지막 사용자 말을 입력칸으로 바꾸고 Escape 로 그만두며 Enter 로 고친 글을 보낸다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    // 도구를 부르지 않은 턴이라 묻지 않고 곧바로 보낸다
    await firstTurn(user, "출력 차단");
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    const input = await screen.findByTestId("chat-edit-input");
    expect(input).toHaveValue("출력 차단");
    expect(input).toHaveAccessibleName("수정할 메시지");
    expect(input).toHaveFocus();
    expect(screen.queryByTestId("chat-regenerate")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("chat-edit-input")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("출력 차단");
    // 입력칸이 닫히면 포커스가 body 로 떨어지지 않고 다시 선 연필 단추로 돌아온다
    expect(screen.getByTestId("chat-message-1-edit")).toHaveFocus();
    expect(screen.getByTestId("chat-message-1-edit")).toHaveAccessibleName("메시지 수정");

    await user.click(screen.getByTestId("chat-message-1-edit"));
    const again = await screen.findByTestId("chat-edit-input");
    await user.clear(again);
    await user.type(again, "환불 기한은?{Enter}");
    // 보내면 사라지는 입력칸 대신 아래 입력칸이 포커스를 받는다
    expect(screen.getByTestId("chat-input")).toHaveFocus();
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("고친 질문에 답합니다: 환불 기한은?"));
    expect(userTexts()).toEqual(["환불 기한은?"]);
    expect(lastRequest("POST", REGENERATE_PATH)?.body).toEqual({ message: "환불 기한은?", ephemeral: true });
  });

  it("도구를 부른 턴의 메시지를 수정해 보내다 묻는 창을 취소하면 입력칸과 고친 글이 그대로 남는다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    await firstTurn(user, "3 더하기 4");
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    const input = await screen.findByTestId("chat-edit-input");
    await user.clear(input);
    await user.type(input, "5 더하기 6{Enter}");
    await user.click(within(await screen.findByTestId("confirm-dialog")).getByTestId("confirm-cancel"));
    expect(screen.getByTestId("chat-edit-input")).toHaveValue("5 더하기 6");
    expect(lastRequest("POST", REGENERATE_PATH)).toBeUndefined();

    await user.click(screen.getByTestId("chat-edit-send"));
    await submitConfirm(user);
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("고친 질문에 답합니다: 5 더하기 6"));
    expect(screen.queryByTestId("chat-edit-input")).not.toBeInTheDocument();
  });

  it("연필을 누른 채 새 세션으로 가도 새 첫 턴에서 입력칸이 저절로 열리지 않는다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    await firstTurn(user, "출력 차단");
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    expect(await screen.findByTestId("chat-edit-input")).toHaveFocus();
    await user.click(screen.getByTestId("chat-new-session"));
    // 새 세션의 첫 턴도 0번이 인사말, 1번이 사용자 말이라 앞 세션에서 고치던 자리와 같다
    await sendMessage(user, "새 질문");
    await waitForTurnEnd();
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("새 질문");
    // 턴이 끝나 단추가 다시 선 뒤에 본다. 고치던 자리를 번호로만 쥐면 이때 입력칸이 열려 포커스를 가져간다
    expect(await screen.findByTestId("chat-message-1-edit")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-edit-input")).not.toBeInTheDocument();
    expect(document.activeElement).not.toHaveAttribute("data-testid", "chat-edit-input");
  });

  it("스트림을 열기 전에 거절되면 걷기 전 화면으로 되돌리고 까닭을 보인다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    await firstTurn(user, "출력 차단");
    const before = transcriptRoles();
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    const input = await screen.findByTestId("chat-edit-input");
    await user.clear(input);
    await user.type(input, "busy{Enter}");
    expect(await screen.findByTestId("chat-error")).toHaveTextContent(ko.errors.codes.turn_in_progress);
    expect(transcriptRoles()).toEqual(before);
    expect(userTexts()).toEqual(["출력 차단"]);
  });

  it("고친 질문이 입력 가드레일에 막히면 걷기 전 화면으로 되돌리고 안내문을 오류 줄로 보이며 두 단추가 남는다", async () => {
    const user = userEvent.setup();
    withRegenerate();
    await firstTurn(user, "출력 차단");
    const before = transcriptRoles();
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    const input = await screen.findByTestId("chat-edit-input");
    await user.clear(input);
    await user.type(input, "카드 4111 1111{Enter}");
    expect(await screen.findByTestId("chat-error")).toHaveTextContent("카드 번호 같은 민감 정보는 보낼 수 없다.");
    // 되돌린 전사 끝에 입력 가드레일 항목이 붙으면 마지막 턴이 사라져 두 단추도 없어진다
    expect(transcriptRoles()).toEqual(before);
    expect(userTexts()).toEqual(["출력 차단"]);
    expect(screen.getByTestId("chat-regenerate")).toBeInTheDocument();
    expect(screen.getByTestId("chat-message-1-edit")).toBeInTheDocument();
    expect(lastRequest("POST", REGENERATE_PATH)?.body).toEqual({ message: "카드 4111 1111", ephemeral: true });
  });
});

describe("마지막 턴 고르기", () => {
  const done: ChatItem = { kind: "done", usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }, turn: 1, tool_calls: 0 };

  it("done·error 로 끝난 마지막 사용자 말만 가리키고 도구 호출을 센다", () => {
    const call: ChatItem = { kind: "tool_call", call: { id: "c1", name: "Add", args: {} }, result: null };
    const items: ChatItem[] = [{ kind: "user", text: "하나" }, done, { kind: "user", text: "둘" }, call, { kind: "error", code: "turn_failed", message: "x" }];
    expect(lastTurn(items)).toEqual({ userAt: 2, text: "둘", calledTools: true });
    expect(lastTurn([...items.slice(0, 3), done])).toEqual({ userAt: 2, text: "둘", calledTools: false });
  });

  it("끝나지 않은 말, 입구에서 거절된 말, 승인 대기는 가리키지 않는다", () => {
    const user: ChatItem = { kind: "user", text: "하나" };
    expect(lastTurn([])).toBeNull();
    expect(lastTurn([user])).toBeNull();
    expect(lastTurn([user, done, { kind: "user", text: "4111" }, { kind: "guardrail", action: "block", message: "막힘", position: "input" }])).toBeNull();
    const pending = { session_id: "s1", action_requests: [], review_configs: [] };
    expect(lastTurn([user, { kind: "interrupt", pending, resolved: false }])).toBeNull();
  });

  it("미리 걷은 화면은 마지막 사용자 말까지이고 고친 글이면 그 말을 바꾼다", () => {
    const items: ChatItem[] = [{ kind: "user", text: "하나" }, done, { kind: "user", text: "둘" }, done];
    const turn = lastTurn(items)!;
    expect(rewindLastTurn(items, turn)).toEqual([...items.slice(0, 3)]);
    expect(rewindLastTurn(items, turn, "고친 둘").at(-1)).toEqual({ kind: "user", text: "고친 둘" });
  });
});

/**
 * SSE 계약의 화면 쪽 (P36 1절). 모르는 이벤트와 필드는 버린다. 런타임이 이벤트(인용, 첨부)나 칸을 먼저 더해도 이 화면의 전사가
 * 깨지지 않는다.
 */
describe("모르는 SSE 이벤트와 필드", () => {
  /** 사용자 말 하나 뒤에 스트림을 끝까지 접은 전사다. */
  async function folded(events: SseEvent[]): Promise<ChatItem[]> {
    let items: ChatItem[] = [{ kind: "user", text: "환불 기한이 언제까지야?" }];
    const result = await consumeChatStream(new Response(events.map(sseBlock).join("")), { update: (fn) => (items = fn(items)) });
    expect(result).toEqual({ status: "done" });
    return items;
  }

  it("모르는 이벤트는 전사를 바꾸지 않고 done 의 모르는 칸은 항목에 실리지 않는다", async () => {
    const { done } = TURN_FIXTURE.events;
    const token = { event: "token", data: { text: done.text } };
    const known = await folded([token, { event: "done", data: done }]);
    const withUnknown = await folded([
      token,
      { event: "citations", data: { items: [{ cite: 1, kb_id: "kb", doc_id: "d", chunk: 0 }] } },
      { event: "foo", data: { bar: 1 } },
      { event: "done", data: { ...done, attachments_received: 2, future_field: "x" } },
    ]);
    expect(withUnknown).toStrictEqual(known);
    expect(withUnknown.map((it) => it.kind)).toEqual(["user", "assistant", "done"]);
    expect(withUnknown[2]).not.toHaveProperty("attachments_received");
    expect(withUnknown[2]).not.toHaveProperty("future_field");
  });
});
