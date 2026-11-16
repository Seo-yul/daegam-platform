/**
 * 채팅의 작은 표시 (P36 11절). 스트림 상태로 정하는 "생각하는 중…", 예비 모델 알림(런타임 표본 chat/fallback.json), 턴 마감
 * 줄 아래 접힌 구조화된 결과를 본다.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { normalizePending } from "@/api/runtimeShapes";
import { ChatItemView, THINKING_SECONDS_FROM, ThinkingIndicator, applyChatEvent, fallbackNotice, showsThinking, toolLimitNotice, type ChatItem } from "@/components/chat";
import { dictionary } from "@/i18n";
import { itemsFromTranscript } from "@/lib/assistant";

import { firstTurn, openAgentChat, sendMessage, transcriptRows, waitForTurnEnd } from "./chat";
import { heldSse, http, server } from "./msw";
import { FALLBACK_FIXTURE, TOOL_LIMIT_FIXTURE, TURN_FIXTURE } from "./runtimeFixtures";
import { fakeClock } from "./timers";

const ko = dictionary("ko");
const THINKING = ko.chat.thinking.label;

describe("생각하는 중", () => {
  const user: ChatItem = { kind: "user", text: "3 더하기 4" };
  const call = { id: "call-1", name: "Add", args: { a: 3, b: 4 } };
  const running: ChatItem = { kind: "tool_call", call, result: null };
  const result = { id: "call-1", name: "Add", status: "success" as const, content: "7" };
  const finished: ChatItem = { kind: "tool_call", call, result };

  it("이번 턴에 흐르는 말풍선도 도는 도구 카드도 없을 때만 보인다", () => {
    expect(showsThinking([user])).toBe(true);
    expect(showsThinking([user, { kind: "assistant", text: "계산하겠습니다.", streaming: true }])).toBe(false);
    expect(showsThinking([user, running])).toBe(false);
    expect(showsThinking([user, { kind: "assistant", text: "계산하겠습니다.", streaming: false }, finished])).toBe(true);
    // 승인 카드가 붙든 호출은 사람을 기다린다. 결정을 보내 재개하면 다시 알린 호출이 오기 전까지 모델 차례다
    const resolved: ChatItem = { kind: "interrupt", pending: normalizePending({ action_requests: [] }), resolved: true };
    expect(showsThinking([user, { ...running, waiting: true }, resolved])).toBe(true);
    // 앞 턴에 결과 없이 남은 카드(중단한 턴)는 이번 턴을 막지 않는다
    expect(showsThinking([user, running, { kind: "user", text: "다시" }])).toBe(true);
    // 마감 항목이 오면 스트림이 닫히기 전이라도 물러난다
    expect(showsThinking([user, applyChatEvent([], { event: "done", data: TURN_FIXTURE.events.done })[0]])).toBe(false);
    // 결정을 기다리는 승인 카드가 서면 사람의 차례라 스트림이 닫히기 전에도 서지 않는다
    expect(showsThinking([user, { ...running, waiting: true }, { ...resolved, resolved: false }])).toBe(false);
  });

  describe("흐른 초", () => {
    beforeEach(() => fakeClock());

    it(`${THINKING_SECONDS_FROM}초부터 붙고 낭독기는 숫자를 읽지 않는다`, () => {
      render(<ThinkingIndicator />);
      const line = screen.getByTestId("chat-thinking");
      expect(line).toHaveTextContent(THINKING);
      act(() => vi.advanceTimersByTime((THINKING_SECONDS_FROM - 1) * 1000));
      expect(screen.queryByTestId("chat-thinking-seconds")).toBeNull();
      act(() => vi.advanceTimersByTime(1000));
      const seconds = screen.getByTestId("chat-thinking-seconds");
      expect(seconds).toHaveTextContent(`${THINKING_SECONDS_FROM}초`);
      expect(seconds).toHaveAttribute("aria-hidden", "true");
    });
  });

  it("채팅 패널은 턴이 도는 동안 모델 차례에만 보이고 턴이 끝나면 물러난다", async () => {
    const u = userEvent.setup();
    // 테스트가 이벤트를 하나씩 밀어 넣는 턴이다. 밀어 넣기 전에는 아무것도 오지 않은 채 열려 있다
    const turn = heldSse();
    server.use(http.post("/agents/:id/chat", () => turn.response));
    const live = () => screen.getByTestId("chat-thinking-live");
    await openAgentChat();
    await sendMessage(u, "3 더하기 4");
    expect(await screen.findByTestId("chat-thinking")).toHaveTextContent(THINKING);
    // 낭독기에는 섰다 사라지는 줄이 아니라 늘 있는 알림 줄이 알린다
    expect(live()).toHaveAttribute("role", "status");
    expect(live()).toHaveTextContent(THINKING);
    turn.push({ event: "token", data: { text: "계산하겠습니다. " } });
    await waitFor(() => expect(screen.queryByTestId("chat-thinking")).toBeNull());
    expect(live()).toBeEmptyDOMElement();
    turn.push({ event: "tool_call", data: call });
    await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-status", "running"));
    expect(screen.queryByTestId("chat-thinking")).toBeNull();
    turn.push({ event: "tool_result", data: result });
    await screen.findByTestId("chat-thinking");
    turn.push({ event: "token", data: { text: "결과는 7 입니다." } });
    turn.push({ event: "done", data: { ...TURN_FIXTURE.events.done, session_id: "s-new", tool_calls: 1 } });
    turn.close();
    await waitForTurnEnd();
    expect(screen.queryByTestId("chat-thinking")).toBeNull();
  });
});

describe("예비 모델 알림", () => {
  it("알림 문장은 예비 모델 이름에 맞는 조사를 고르고 서브 에이전트의 전환이면 그 이름을 밝힌다", () => {
    expect(fallbackNotice(ko, { kind: "fallback", from: "gpt-4.1-mini", to: "claude-sonnet-4-5" })).toBe("기본 모델이 답하지 못해 예비 모델 claude-sonnet-4-5가 대신 답했습니다.");
    expect(fallbackNotice(ko, { kind: "fallback", from: "a", to: "qwen3" })).toBe("기본 모델이 답하지 못해 예비 모델 qwen3이 대신 답했습니다.");
    expect(fallbackNotice(ko, { kind: "fallback", from: "a", to: "b", agent: "researcher" })).toBe("서브 에이전트 researcher의 기본 모델이 답하지 못해 예비 모델 b가 대신 답했습니다.");
  });
});

describe("Tool 호출 한도 알림", () => {
  it("알림 문장은 한도를 밝히고 서브 에이전트의 한도면 그 이름을 밝힌다", () => {
    expect(toolLimitNotice(ko, { kind: "tool_limit", limit: 8 })).toBe("턴당 최대 Tool 호출 수(8번)에 도달해 더 이상 Tool 을 호출하지 않았습니다.");
    expect(toolLimitNotice(ko, { kind: "tool_limit", limit: 4, agent: "researcher" })).toBe(
      "서브 에이전트 researcher의 턴당 최대 Tool 호출 수(4번)에 도달해 더 이상 Tool 을 호출하지 않았습니다.",
    );
    expect(toolLimitNotice(dictionary("en"), { kind: "tool_limit", limit: 1 })).toBe("The Agent reached the maximum of 1 Tool call per turn, so no more Tools were called.");
  });

  it("스트림으로 접은 항목과 되살린 전사가 런타임 표본과 같은 모양이고 경고 알림으로 그려진다", () => {
    const { events, transcript } = TOOL_LIMIT_FIXTURE;
    let items: ChatItem[] = [{ kind: "user", text: String(transcript[0]!.text) }];
    items = applyChatEvent(items, { event: "tool_limit", data: events.subagent_tool_limit });
    items = applyChatEvent(items, { event: "tool_limit", data: events.tool_limit });
    items = applyChatEvent(items, { event: "token", data: { text: events.done.text } });
    items = applyChatEvent(items, { event: "done", data: events.done });
    // 사용량은 되살릴 때 화면이 쓰는 칸만 남기므로 턴 마감 줄 앞까지 견준다
    expect(items.slice(0, -1)).toEqual(itemsFromTranscript(transcript).slice(0, -1));
    expect(items.map((it) => it.kind)).toEqual(transcript.map((it) => it.kind));
    render(<ChatItemView index={2} item={items[2]!} />);
    const notice = screen.getByTestId("chat-message-2");
    expect(notice).toHaveAttribute("data-role", "tool-limit");
    expect(notice).toHaveTextContent("턴당 최대 Tool 호출 수(8번)에 도달해 더 이상 Tool 을 호출하지 않았습니다.");
  });
});

describe("구조화된 결과", () => {
  it("채팅 패널은 예비 모델의 답에 알림 하나와 턴 마감 줄 아래 접힌 구조화된 결과를 보인다", async () => {
    const u = userEvent.setup();
    await firstTurn(u, "예비 모델로 답해 줘");
    const notices = transcriptRows({ role: "fallback" });
    expect(notices).toHaveLength(1);
    expect(notices[0]).toHaveTextContent("기본 모델이 답하지 못해 예비 모델 claude-sonnet-4-5가 대신 답했습니다.");

    const done = transcriptRows().at(-1)!;
    const structured = within(done).getByTestId(`${done.dataset.testid}-structured`);
    // jsdom 은 접힌 details 의 내용도 DOM 에 남기므로 접힘은 open 속성으로 본다
    expect(structured).not.toHaveAttribute("open");
    await u.click(within(structured).getByText("구조화된 결과"));
    expect(structured).toHaveAttribute("open");
    const value = FALLBACK_FIXTURE.events.done.structured_response;
    expect(within(structured).getByTestId(`${done.dataset.testid}-structured-code`).textContent).toBe(JSON.stringify(value, null, 2));
    expect(within(structured).getByTestId(`${done.dataset.testid}-structured-copy`)).toHaveAttribute("aria-label", "구조화된 결과 복사");
  });

  it("구조화된 결과가 없는 턴에는 접힌 칸도 없다", () => {
    const [done] = itemsFromTranscript(TURN_FIXTURE.transcript.filter((it) => it.kind === "done"));
    render(<ChatItemView index={0} item={done} />);
    expect(screen.getByTestId("chat-message-0")).toHaveAttribute("data-role", "done");
    expect(screen.queryByTestId("chat-message-0-structured")).toBeNull();
  });
});

describe("외부 내용 배지 (P43)", () => {
  it("다시 연 대화의 Tool 카드도 런타임 기록에 남은 표시로 배지를 그린다", () => {
    // 런타임 전사(transcript.py `_attach_result`)는 tool_result 를 통째로 결과에 둔다
    const result = { id: "c1", name: "search_faq", status: "success", content: "x", untrusted: "outside" };
    const [item] = itemsFromTranscript([{ kind: "tool_call", call: { id: "c1", name: "search_faq", args: {} }, result }]);
    render(<ChatItemView index={0} item={item} />);
    expect(screen.getByTestId("chat-message-0-outside")).toHaveTextContent(ko.chat.tool.outside);
  });
});
