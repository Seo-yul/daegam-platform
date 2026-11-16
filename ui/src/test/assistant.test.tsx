import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { ASSISTANT_OPEN_KEY, assistantSessionKey, handOffStream, setAssistantOpen, setAssistantSessionId, setCanvasUnsaved, subscribeAssistantEvents } from "@/lib/assistant";
import { transcriptRows } from "./chat";
import { DEPLOY_CALL_ID, HttpResponse, apiError, assistantState, countExact, heldSse, http, lastExact, lastRequest, record, server, sseResponse, tokenUsage } from "./msw";
import { spyScrollTo } from "./scroll";

/**
 * 플랫폼 비서 패널 (D31, phase-14 F2): 헤더 토글과 오른쪽 열, 턴 SSE(도구 호출·리소스 카드·승인 카드), 결정 id 대응,
 * 질문 카드, 따라가기 토글, 쓰기 결과 뒤 무효화, 캔버스 자동 접기, 설정 비활성. 기억한 세션의 복원·테넌트 전환·폴링은
 * assistant-sessions.test.tsx 가 본다.
 */

/** 같은 testid 의 카드가 n 개가 될 때까지 기다려 마지막 것을 돌려준다(앞 카드는 이미 결정된 것이다). */
async function nthCard(panel: HTMLElement, testId: string, n: number): Promise<HTMLElement> {
  await waitFor(() => expect(within(panel).getAllByTestId(testId)).toHaveLength(n));
  return within(panel).getAllByTestId(testId)[n - 1];
}

async function openPanel(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(await screen.findByTestId("assistant-toggle"));
  return screen.getByTestId("assistant-panel");
}

async function say(user: ReturnType<typeof userEvent.setup>, panel: HTMLElement, text: string): Promise<void> {
  await user.type(within(panel).getByTestId("assistant-input"), text);
  await user.keyboard("{Enter}");
}

function doneItems(panel: HTMLElement): HTMLElement[] {
  return transcriptRows({ role: "done", container: panel });
}

describe("assistant panel", () => {
  it("opens from the header toggle, streams a scripted turn and approves the deploy with the pending call id", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("status-phase");
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    const panel = await openPanel(user);
    expect(panel.className).toContain("nokey");
    expect(within(panel).getByTestId("assistant-wordmark")).toHaveTextContent("청지기");
    expect(screen.getByTestId("assistant-toggle")).toHaveAttribute("aria-expanded", "true");
    expect(window.localStorage.getItem(ASSISTANT_OPEN_KEY)).toBe("true");
    expect(within(panel).getByTestId("assistant-empty")).toBeInTheDocument();
    // 열면 바로 쓸 수 있게 입력에 포커스가 간다
    expect(within(panel).getByTestId("assistant-input")).toHaveFocus();

    await say(user, panel, "calc 프로필로 배포해 줘");
    // 첫 메시지가 세션을 만들고(제목은 메시지 앞부분), 턴에는 현재 화면의 문맥이 실린다
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/turns")).toBeDefined());
    expect(lastExact("POST", "/api/assistant/sessions")?.body).toEqual({ title: "calc 프로필로 배포해 줘" });
    // provider 는 모델 chip 이 보이는 값 그대로다. 고르지 않았으면 설정의 기본 이름이 실린다 (D45 셋째)
    expect(lastRequest("POST", "/assistant/sessions/as-2/turns")?.body).toEqual({
      message: "calc 프로필로 배포해 줘",
      provider: "openai-main",
      context: { route: "/mcp-servers/ms-calc", resource: { kind: "mcp-server", id: "ms-calc", name: "calc" } },
    });
    expect(window.localStorage.getItem(assistantSessionKey("default"))).toBe("as-2");

    expect(within(panel).getByTestId("chat-message-0")).toHaveAttribute("data-role", "user");
    const call = await within(panel).findByTestId("chat-message-2");
    expect(call).toHaveAttribute("data-role", "tool_call");
    await waitFor(() => expect(call).toHaveAttribute("data-status", "success"));
    expect(call).toHaveTextContent("get_mcp_server");
    const resource = await within(panel).findByTestId("chat-message-3");
    expect(resource).toHaveAttribute("data-role", "resource");
    expect(within(resource).getByTestId("resource-card")).toHaveAttribute("href", "/mcp-servers/ms-calc");
    expect(within(resource).getByTestId("resource-card")).toHaveAttribute("data-kind", "mcp-server");

    const card = await within(panel).findByTestId("approval-card");
    expect(card).toHaveTextContent("deploy_mcp_server");
    // 비서의 결정은 approve·reject 뿐이다
    expect(within(card).queryByTestId("decision-edit")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("decision-respond")).not.toBeInTheDocument();
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    const sessionReads = countExact("GET", "/assistant/sessions/as-2");
    await user.click(within(card).getByTestId("decision-approve"));
    // interrupt data 에 id 가 없으므로 세션을 다시 읽어 pending.calls[0].id 를 쓴다
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/decisions")?.body).toEqual({ decisions: [{ id: DEPLOY_CALL_ID, type: "approve" }] }));
    expect(countExact("GET", "/assistant/sessions/as-2")).toBe(sessionReads + 1);
    await waitFor(() => expect(card).toHaveAttribute("data-resolved", "true"));
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("calc-2 를 만들었습니다"));
    const created = transcriptRows({ role: "resource", container: panel }).at(-1)!;
    expect(within(created).getByTestId("resource-card")).toHaveAttribute("href", "/mcp-servers/ms-new");
    // 표시 이름이 먼저 서고, 다른 K8s 이름은 곁에, id 는 앞 여덟 자만 적는다(전체는 title)
    expect(within(created).getByTestId("resource-card-name")).toHaveTextContent("계산기 두 번째");
    expect(within(created).getByTestId("resource-card-k8s-name")).toHaveTextContent("calc-2");
    expect(within(created).getByTestId("resource-card-id")).toHaveTextContent(/^ms-new-0$/);
    expect(within(created).getByTestId("resource-card-id")).toHaveAttribute("title", "ms-new-0123456789");
    await waitFor(() => expect(doneItems(panel).at(-1)).toHaveTextContent("턴 2"));
    // 스트림이 닫히면 중단 버튼이 보내기로 돌아온다
    await waitFor(() => expect(within(panel).getByTestId("assistant-send")).toBeInTheDocument());
  });

  it("rejecting sends the reason with the call id; a decision count mismatch is reported as 422", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    await say(user, panel, "deploy calc");
    const card = await within(panel).findByTestId("approval-card");
    await waitFor(() => expect(within(card).getByTestId("decision-reject")).toBeEnabled());
    await user.click(within(card).getByTestId("decision-reject"));
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/decisions")?.body).toEqual({ decisions: [{ id: DEPLOY_CALL_ID, type: "reject" }] }));
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("배포를 취소했습니다."));

    // 서버가 개수 불일치를 돌려주면 패널이 오류로 보인다
    await say(user, panel, "deploy again");
    const card2 = await nthCard(panel, "approval-card", 2);
    server.use(http.post("/assistant/sessions/:id/decisions", () => apiError(422, "decision_count_mismatch", "expected 1 decision(s), got 0")));
    await waitFor(() => expect(within(card2).getByTestId("decision-approve")).toBeEnabled());
    await user.click(within(card2).getByTestId("decision-approve"));
    // 코드가 아니라 errorText 표의 한국어 처방이 선다 (D43 첫째)
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent("대기 중인 호출 수와 보낸 결정 수가 다릅니다");
    // 서버에 가지 못한 결정이라 카드는 열린 채다
    expect(card2).not.toHaveAttribute("data-resolved");
  });

  it("aborting the first turn of a new session shows the notice and starts polling the running session", async () => {
    const user = userEvent.setup();
    // 끝나지 않는 스트림. 서버 쪽 턴은 이어지므로 세션은 running 이다
    server.use(
      http.post("/assistant/sessions/:id/turns", async ({ request, params }) => {
        await record(request);
        const s = assistantState.sessions.find((x) => x.id === params.id)!;
        s.state = "running";
        s.transcript = [...s.transcript, { kind: "user", text: "느린 배포" }, { kind: "assistant", text: "천천히 진행합니다. ", streaming: false }];
        return heldSse([{ event: "token", data: { text: "천천히 진행합니다. " } }]).response;
      }),
    );
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    await say(user, panel, "느린 배포");
    await waitFor(() => expect(within(panel).getByTestId("chat-message-1")).toHaveTextContent("천천히 진행합니다."));
    expect(window.localStorage.getItem(assistantSessionKey("default"))).toBe("as-2");
    const reads = countExact("GET", "/assistant/sessions/as-2");
    // 보내기 자리의 같은 버튼이 중단이 된다
    await user.click(within(panel).getByTestId("assistant-abort"));
    // 중단은 화면의 reader 만 끊는다. 서버 턴은 그대로 돌아 토큰을 쓰므로 그 사실을 말한다 (D43)
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent("요청을 중단했습니다. 서버 쪽 처리는 끝날 때까지 이어집니다.");
    // 새 세션이라 ref 가 아니라 스트림의 세션 id 로 다시 읽는다. running 이면 폴링 안내가 뜬다
    expect(await within(panel).findByTestId("assistant-running")).toBeInTheDocument();
    expect(countExact("GET", "/assistant/sessions/as-2")).toBe(reads + 1);
    expect(within(panel).getByTestId("assistant-send")).toBeInTheDocument();
    expect(within(panel).getByTestId("chat-message-1")).toHaveTextContent("천천히 진행합니다.");
  });

  it("keeps the input and leaves no bubble when the turn cannot start (session creation failure, 409)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    const input = within(panel).getByTestId("assistant-input");
    server.use(http.post("/assistant/sessions", () => apiError(503, "unavailable", "session store is down")));
    await say(user, panel, "첫 메시지");
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent("unavailable");
    expect(input).toHaveValue("첫 메시지");
    expect(within(panel).queryByTestId("chat-message-0")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(assistantSessionKey("default"))).toBeNull();

    server.resetHandlers();
    await user.clear(input);
    await say(user, panel, "busy");
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent("아직 답변이 끝나지 않았습니다");
    expect(input).toHaveValue("busy");
    expect(within(panel).queryByTestId("chat-message-0")).not.toBeInTheDocument();

    // 그 뒤 정상 메시지는 그대로 간다
    await user.clear(input);
    await say(user, panel, "목록 보여 줘");
    await waitFor(() => expect(doneItems(panel)).toHaveLength(1));
    expect(within(panel).getByTestId("chat-message-0")).toHaveTextContent("목록 보여 줘");
    expect(within(panel).queryByTestId("assistant-error")).not.toBeInTheDocument();
  });

  it("question interrupt shows the AskCard and an option answer becomes the next turn message", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    await say(user, panel, "클러스터를 물어봐");
    const ask = await within(panel).findByTestId("ask-card");
    expect(ask).toHaveTextContent("어느 클러스터에 배포할까요?");
    expect(within(ask).getByTestId("ask-card-field")).toHaveTextContent("cluster_id");
    expect(within(panel).queryByTestId("approval-card")).not.toBeInTheDocument();
    await waitFor(() => expect(within(ask).getByTestId("ask-option-1")).toBeEnabled());
    await user.click(within(ask).getByTestId("ask-option-1"));
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/turns")?.body).toMatchObject({ message: "cl-edge" }));
    // 답한 뒤 포커스는 입력으로 돌아온다
    await waitFor(() => expect(within(panel).getByTestId("assistant-input")).toHaveFocus());
    await waitFor(() => expect(ask).toHaveAttribute("data-resolved", "true"));
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("cl-edge 로 진행하겠습니다."));

    // 자유 입력으로도 답한다
    await say(user, panel, "다시 물어봐");
    const ask2 = await nthCard(panel, "ask-card", 2);
    await waitFor(() => expect(within(ask2).getByTestId("ask-input")).toBeEnabled());
    await user.type(within(ask2).getByTestId("ask-input"), "cl-home 으로");
    await user.click(within(ask2).getByTestId("ask-submit"));
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/turns")?.body).toMatchObject({ message: "cl-home 으로" }));
  });

  it("keeps following the bottom while one bubble grows token by token", async () => {
    const scroll = spyScrollTo();
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    await say(user, panel, "MCP Server 가 뭔지 길게 설명해 줘");

    // 말풍선이 생긴 뒤(항목 수가 마지막으로 늘어난 시점) 세어 둔다.
    const transcript = within(panel).getByTestId("assistant-transcript");
    await waitFor(() => expect(transcript).toHaveTextContent("MCP Server 는"));
    const afterFirstToken = scroll.count();

    // 같은 말풍선에 토큰이 더 붙는 동안 항목 수는 그대로다. 그래도 바닥을 따라가야 한다.
    await waitFor(() => expect(transcript).toHaveTextContent("Ready 면 호출할 수 있습니다."));
    expect(scroll.count()).toBeGreaterThan(afterFirstToken);
  });

  it("ui.navigate moves the screen only while the follow toggle is on", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    expect(panel).toHaveAttribute("data-follow", "true");
    await say(user, panel, "calc-1 화면으로 이동해 줘");
    expect(await screen.findByTestId("mcp-server-detail-page")).toBeInTheDocument();
    await waitFor(() => expect(doneItems(panel)).toHaveLength(1));

    await user.click(within(panel).getByTestId("assistant-follow"));
    expect(panel).toHaveAttribute("data-follow", "false");
    expect(window.localStorage.getItem("daegam.assistant.follow")).toBe("false");
    await user.click(screen.getByTestId("nav-mcp-hub"));
    await screen.findByTestId("hub-page");
    await say(user, panel, "다시 이동해 줘");
    await waitFor(() => expect(doneItems(panel)).toHaveLength(2));
    expect(screen.getByTestId("hub-page")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-server-detail-page")).not.toBeInTheDocument();
  });

  it("invalidates the resource's queries after a write tool result so the open list refetches", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-servers-page");
    await waitFor(() => expect(countExact("GET", "/mcp-servers")).toBeGreaterThan(0));
    const panel = await openPanel(user);
    await say(user, panel, "calc 프로필로 배포해 줘");
    const card = await within(panel).findByTestId("approval-card");
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    const before = countExact("GET", "/mcp-servers");
    await user.click(within(card).getByTestId("decision-approve"));
    await waitFor(() => expect(doneItems(panel)).toHaveLength(1));
    // 성공한 deploy_mcp_server 결과가 규칙표의 mcp-servers 키를 무효화해 목록이 다시 온다
    await waitFor(() => expect(countExact("GET", "/mcp-servers")).toBeGreaterThan(before));
  });

  it("collapses on the agent canvas without saving and comes back when leaving", async () => {
    const user = userEvent.setup();
    setAssistantOpen(true);
    render(<TestApp initialPath="/agents" />);
    await screen.findByTestId("agents-page");
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();
    await user.click(screen.getByTestId("agent-new"));
    await waitFor(() => expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument());
    expect(window.localStorage.getItem(ASSISTANT_OPEN_KEY)).toBe("true");
    await user.click(screen.getByTestId("nav-mcp-hub"));
    await waitFor(() => expect(screen.getByTestId("assistant-panel")).toBeInTheDocument());
    // 패널의 닫기 버튼은 저장한다
    await user.click(screen.getByTestId("assistant-close"));
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(ASSISTANT_OPEN_KEY)).toBe("false");
  });

  it("stays open on a canvas that opened Steward's proposal (P48 6.5)", async () => {
    setAssistantOpen(true);
    render(<TestApp initialPath="/agents/new?proposal=p-1&session=as-1" />);
    await screen.findByTestId("agent-canvas-page");
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();
  });

  it("draws a decision stream the canvas hands off in the open session, passes its events on, and sends the canvas's unsaved flag", async () => {
    // 캔버스의 제안 보기가 보낸 결정은 옆 패널의 대화가 이어 그린다 (P48 6.5절)
    const user = userEvent.setup();
    setAssistantSessionId("default", "as-1");
    setAssistantOpen(true);
    render(<TestApp initialPath="/hub" />);
    const panel = await screen.findByTestId("assistant-panel");
    await waitFor(() => expect(doneItems(panel)).toHaveLength(1));
    const heard: string[] = [];
    const stop = subscribeAssistantEvents((sid, ev) => heard.push(`${sid}:${ev?.event ?? "cut"}`));
    expect(handOffStream("as-other", sseResponse([]))).toBe(false);
    const script = [
      { event: "tool_call", data: { id: "c-1", name: "update_agent", args: {} } },
      { event: "tool_result", data: { id: "c-1", name: "update_agent", status: "success", content: '{"id":"ag-support","revision":3}' } },
      { event: "done", data: { session_id: "as-1", turn: 2, usage: tokenUsage(10, 2), tool_calls: 1, text: "" } },
    ];
    act(() => {
      expect(handOffStream("as-1", sseResponse(script))).toBe(true);
    });
    await waitFor(() => expect(doneItems(panel)).toHaveLength(2));
    // 읽는 쪽은 끝 이벤트를 건넨 뒤 스트림을 그만 읽으며 null 을 건넨다. 그 null 은 렌더와 따로 오므로 넷을 기다린다
    await waitFor(() => expect(heard).toEqual(["as-1:tool_call", "as-1:tool_result", "as-1:done", "as-1:cut"]));
    stop();
    // 캔버스에 저장하지 않은 변경이 있으면 턴의 문맥이 그렇게 말한다
    setCanvasUnsaved(true);
    await say(user, panel, "MCP Server 가 몇 개야?");
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-1/turns")).toBeDefined());
    expect(lastRequest("POST", "/assistant/sessions/as-1/turns")?.body).toMatchObject({ context: { route: "/hub", unsaved_edits: true } });
  });

  it("renders neither the toggle nor the panel when the config is disabled", async () => {
    server.use(
      http.get("/assistant/config", async ({ request }) => {
        await record(request);
        return HttpResponse.json({ enabled: false });
      }),
    );
    setAssistantOpen(true);
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    await waitFor(() => expect(lastRequest("GET", "/assistant/config")).toBeDefined());
    expect(screen.queryByTestId("assistant-toggle")).not.toBeInTheDocument();
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
  });

  it("shows stream errors inline and reports a busy session from the server", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-page");
    const panel = await openPanel(user);
    await say(user, panel, "오류를 내 봐");
    const err = await within(panel).findByTestId("chat-message-1");
    expect(err).toHaveAttribute("data-role", "error");
    expect(err).toHaveTextContent("turn_failed");
    await say(user, panel, "busy");
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent("아직 답변이 끝나지 않았습니다");
  });
});

describe("assistant approval provenance", () => {
  it("shows the fetched document and its source URL on the approval card", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis" />);
    await user.click(await screen.findByTestId("assistant-toggle"));
    await user.type(await screen.findByTestId("assistant-input"), "배포해 줘");
    await user.click(screen.getByTestId("assistant-send"));
    const card = await screen.findByTestId("approval-card");
    const files = within(card).getByTestId("approval-files-0");
    expect(files).toHaveTextContent("외부에서 가져온 문서를 사용합니다.");
    expect(files).toHaveTextContent("fetched-1.yaml");
    expect(files).toHaveTextContent("https://api.example.com/spec.yaml");
  });
});

