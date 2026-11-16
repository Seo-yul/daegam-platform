/** Agent 상세의 채팅 탭. 스트림과 도구 카드, 승인 카드, 가드레일과 오류, 중단(D65)을 본다. */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

import { RUNTIME_CANCEL } from "@/lib/runtimeFeatures";
import { openAgentChat, sendMessage } from "./chat";
import { SLOW_TURN_PREFIX, SLOW_TURN_TOKEN, agentFixture, agentState, agentWithRuntimeFeatures, apiError, http, lastRequest, record, server } from "./msw";

/** 채팅 탭을 열고 느린 턴을 보내 그 턴이 토큰 하나를 낸 채 열려 있을 때까지 기다린다(msw/agent-sse.ts 의 slowTurnResponse). */
async function startSlowTurn(user: UserEvent): Promise<void> {
  await openAgentChat();
  await sendMessage(user, `${SLOW_TURN_PREFIX} 답해 줘`);
  await waitFor(() => expect(screen.getByTestId("chat-message-2")).toHaveTextContent(SLOW_TURN_TOKEN.trim()));
}

describe("agents (chat)", () => {
  it("streams a chat turn with a collapsible tool call and keeps the session", async () => {
    const user = userEvent.setup();
    // 배포 상태의 인사말이 편집 중인 정의의 것과 다르다. 첫 말 전의 말풍선도 첫 이벤트도 배포된 쪽이다 (D65)
    agentState.items = [agentFixture({ status: { ...agentFixture().status!, greeting: "배포된 첫 문장입니다." } })];
    await openAgentChat();
    expect(screen.getByTestId("chat-greeting")).toHaveTextContent("배포된 첫 문장입니다.");
    await sendMessage(user, "3 더하기 4");
    // 새 세션의 첫 이벤트 greeting 이 사용자 말 앞에 "인사말" 말풍선으로 서고 빈 화면의 말풍선은 물러난다
    await waitFor(() => expect(screen.getByTestId("chat-message-0")).toHaveTextContent("인사말"));
    expect(screen.getByTestId("chat-message-0")).toHaveTextContent("배포된 첫 문장입니다.");
    expect(screen.queryByTestId("chat-greeting")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-message-1")).toHaveAttribute("data-role", "user");
    await waitFor(() => expect(screen.getByTestId("chat-message-2")).toHaveTextContent("3 더하기 4를 계산하겠습니다."));
    const call = await screen.findByTestId("chat-message-3");
    expect(call).toHaveAttribute("data-role", "tool_call");
    await user.click(screen.getByTestId("chat-message-3-toggle"));
    expect(screen.getByTestId("chat-message-3-detail")).toHaveTextContent('"a": 3');
    await waitFor(() => expect(screen.getByTestId("chat-message-4")).toHaveTextContent("결과는 7 입니다."));
    // 턴 요약은 입력 가운데 캐시에서 읽은 몫을 따로 적는다. 단가가 낮아 비용을 가늠하는 데 쓴다
    expect(await screen.findByTestId("chat-message-5-usage")).toHaveTextContent("토큰 144 (입력 120 · 캐시 96 · 출력 24)");
    // 상세의 채팅은 테스트 채팅이라 ephemeral 이다 (D34: 장기 기억을 안 더럽힌다)
    expect(lastRequest("POST", "/agents/ag-support/chat")?.body).toEqual({ session_id: null, message: "3 더하기 4", ephemeral: true });

    await user.type(screen.getByTestId("chat-input"), "한 번 더");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/chat")?.body).toEqual({ session_id: "s-new", message: "한 번 더", ephemeral: true }));
    // 이어지는 턴에는 인사말이 없다. 둘째 사용자 말이 앞 턴의 마감 바로 뒤다
    await screen.findByTestId("chat-message-10-usage");
    expect(screen.getByTestId("chat-message-6")).toHaveAttribute("data-role", "user");
  });

  it("an interrupt shows the inline approval card and approve resumes the turn", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "8 나누기 2");
    const card = await screen.findByTestId("approval-card");
    expect(card).toHaveTextContent("Divide");
    // 카드는 interrupt 에서 바로 뜨지만 스트림이 닫힐 때까지 결정 버튼은 비활성이다
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    await user.click(within(card).getByTestId("decision-approve"));
    await waitFor(() => expect(card).toHaveAttribute("data-resolved", "true"));
    expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/decisions")?.body).toEqual({ decisions: [{ type: "approve" }], ephemeral: true });
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 4 입니다."));
  });

  it("chat shows guardrail blocks and server errors (413, 429 with Retry-After)", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "card 4111 1111");
    // 입력 가드레일은 422 guardrail_blocked 로 막는다. 오류 줄이 아니라 전사의 가드레일 항목으로 안내문을 보인다
    await waitFor(() => expect(screen.getByTestId("chat-message-1")).toHaveAttribute("data-role", "guardrail"));
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("가드레일 (input · pii_mask): 카드 번호 같은 민감 정보는 보낼 수 없다");
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    // 출력 가드레일은 스트림 안의 이벤트로 온다
    await sendMessage(user, "출력 차단");
    // 422 로 막힌 첫 말은 세션을 만들지 않았으므로 이 턴이 새 세션이고 인사말이 사용자 말 앞에 선다
    await waitFor(() => expect(screen.getByTestId("chat-message-4")).toHaveAttribute("data-role", "guardrail"));
    expect(screen.getByTestId("chat-message-4")).toHaveTextContent("output");
    expect(screen.getByTestId("chat-message-2")).toHaveTextContent("인사말");
    await sendMessage(user, "limit");
    // 코드가 아니라 errorText 표의 한국어 처방이 선다 (D43 첫째)
    expect(await screen.findByTestId("chat-error")).toHaveTextContent("짧은 시간에 요청을 너무 많이 보냈습니다");
    expect(screen.getByTestId("chat-error")).toHaveTextContent("30초 뒤");
  });

  it("중단은 런타임이 cancel 을 알렸으면 중지 API 를 부르고 스트림이 turn_cancelled 로 끝나기를 기다린다 (D65)", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL)];
    await startSlowTurn(user);
    await user.click(screen.getByTestId("chat-abort"));
    // 첫 턴이라 세션 id 는 응답 헤더 X-Daegam-Session-Id 에서 온다. 중지 API 는 그 세션으로 간다
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/cancel")).toBeDefined());
    // 런타임이 마감한 마지막 이벤트가 전사에 남고 화면이 끊은 쪽의 오류 줄은 서지 않는다
    const ended = await screen.findByTestId("chat-message-3");
    expect(ended).toHaveAttribute("data-role", "error");
    expect(ended).toHaveTextContent("요청을 중단했습니다. 모델 호출은 멈췄고");
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("chat-new-session")).toBeInTheDocument());
  });

  it("중단은 런타임이 cancel 을 모르면 중지 API 를 부르지 않고 곧바로 연결을 끊는다 (D65)", async () => {
    const user = userEvent.setup();
    // 기록이 없는 에이전트(첫 배포, 옛 이미지)다
    await startSlowTurn(user);
    await user.click(screen.getByTestId("chat-abort"));
    expect(await screen.findByTestId("chat-error")).toHaveTextContent("요청을 중단했습니다. 모델 호출은 멈췄고");
    expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/cancel")).toBeUndefined();
  });

  it("중단은 중지 API 의 답이 202 가 아니면 그 자리에서 연결을 끊는다 (D65)", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL)];
    // 늦게 온 중지다. 런타임에는 진행 중 턴이 없다
    server.use(
      http.post("/agents/:id/sessions/:sid/cancel", async ({ request }) => {
        await record(request);
        return apiError(409, "no_turn_in_progress", "no turn is running in this session");
      }),
    );
    await startSlowTurn(user);
    await user.click(screen.getByTestId("chat-abort"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/sessions/s-new/cancel")).toBeDefined());
    expect(await screen.findByTestId("chat-error")).toHaveTextContent("요청을 중단했습니다. 모델 호출은 멈췄고");
  });

  it("the chat is disabled until the runtime is Ready", async () => {
    agentState.items = [agentFixture({ status: { ...agentFixture().status!, phase: "Pending", chat_url: null } })];
    await openAgentChat();
    expect(screen.getByTestId("chat-input")).toBeDisabled();
    expect(screen.getByTestId("chat-send")).toBeDisabled();
  });

  it("탭을 바꿨다 돌아와도 채팅이 그대로 있다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "1+1 은?");
    // 새 세션의 인사말이 앞에 서므로 사용자 말은 둘째 항목이다
    await waitFor(() => expect(screen.getByTestId("chat-message-1")).toHaveTextContent("1+1 은?"));

    // 상태를 보러 개요 탭에 다녀온다. 대화는 패널의 로컬 state 라 언마운트되면 사라진다
    await user.click(screen.getByTestId("tab-overview"));
    await screen.findByTestId("readiness-card");
    expect(screen.getByTestId("chat-tabpanel")).not.toBeVisible();
    await user.click(screen.getByTestId("tab-chat"));
    expect(screen.getByTestId("chat-tabpanel")).toBeVisible();
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("1+1 은?");
  });
});
