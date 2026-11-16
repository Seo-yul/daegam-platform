/**
 * 홈 챗 (D35, D45). 첫 화면이 인사와 입력칸 하나인지, 상대와 모델을 입력칸 안에서 고르는지, 빠른 시작
 * 칩이 입력칸을 채우는지, 첫 메시지가 대화 화면으로 이어지는지를 본다. 네 상태 가운데 "둘 다 없음" 은
 * 갓 설치한 플랫폼의 기본 경로라 따로 본다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";
import { NARROW_QUERY } from "@/lib/media";

import { openHomeChat, pickHomeTarget, startHomeChat, transcriptRows } from "./chat";
import { stubMatchMedia } from "./media";
import { HttpResponse, agentState, apiError, http, lastRequest, meDeveloper, server } from "./msw";
import { selectValue } from "./select";

/** 인스턴스를 하나도 없는 상태로. (3)·(4) 상태를 만든다. */
function noInstances() {
  agentState.items = [];
}

/** 비서를 끈 상태로. (2)·(4) 상태를 만든다. */
function noAssistant() {
  server.use(http.get("/assistant/config", () => HttpResponse.json({ enabled: false })));
}

describe("home chat", () => {
  it("(1) 첫 화면은 인사와 입력칸 하나다. 상대는 청지기이고 누구로서 도는지를 말한다", async () => {
    await openHomeChat();
    // 이름을 모르는 주체(header 모드의 /me 에는 name 이 없다)에게는 이름 없는 인사다
    expect(await screen.findByTestId("home-greeting")).toHaveTextContent("무엇을 도와드릴까요?");
    expect(screen.getByTestId("home-start-input")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("home-agent-select")).toHaveTextContent("청지기"));
    expect(screen.getByTestId("home-actor-note")).toHaveTextContent("청지기는 로그인한 사용자의 권한으로 플랫폼을 조작합니다");
    // 대화가 없으니 전사는 아직 없다. 대화 목록은 조용한 열로 옆에 남는다 (D45 첫째)
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("home-session-list")).toBeInTheDocument();
  });

  it("이름을 아는 사람은 이름으로 부른다", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    await openHomeChat();
    expect(await screen.findByTestId("home-greeting")).toHaveTextContent("김개발님, 안녕하세요");
  });

  it("빠른 시작 칩을 누르면 입력칸이 채워지고 포커스가 간다", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    await user.click(await screen.findByTestId("home-quick-mcp"));
    const input = screen.getByTestId("home-start-input");
    expect(input).toHaveValue("Petstore 스펙으로 MCP Server 를 만들어 주세요");
    expect(input).toHaveFocus();
  });

  it("첫 메시지를 보내면 같은 상대의 대화 화면으로 넘어가고 그 말이 첫 턴이 된다 (D45 다섯째)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    await startHomeChat(user, "MCP Server 가 몇 개 있어?");

    const panel = await screen.findByTestId("assistant-panel");
    expect(panel).toHaveAttribute("data-variant", "home");
    await waitFor(() => expect(screen.getByTestId("assistant-transcript")).toHaveTextContent("인스턴스가 1개 있습니다"));
    // 시작 화면은 사라지고 상대 pill 은 입력칸에 남는다
    expect(screen.queryByTestId("home-start")).not.toBeInTheDocument();
    expect(within(panel).getByTestId("home-agent-select")).toHaveTextContent("청지기");
    // 홈에서는 닫기·따라가기·세션 고르개가 없다. 왼쪽 목록이 세션을 쥔다
    expect(within(panel).queryByTestId("assistant-close")).not.toBeInTheDocument();
    expect(within(panel).queryByTestId("assistant-follow")).not.toBeInTheDocument();
    expect(within(panel).queryByTestId("assistant-session")).not.toBeInTheDocument();
    expect(screen.getByTestId("home-session-list")).toBeInTheDocument();
    expect(screen.getByTestId("home-sessions-note")).toHaveTextContent("테넌트마다 이어집니다");
  });

  it("고른 모델이 그 대화에 실려 간다 (D45 셋째)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    await screen.findByTestId("home-model-select");
    await selectValue(user, "home-model-select", "ant-main");
    await startHomeChat(user, "MCP Server 가 몇 개 있어?");

    await screen.findByTestId("assistant-panel");
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-2/turns")).toBeDefined());
    expect(lastRequest("POST", "/assistant/sessions/as-2/turns")?.body).toMatchObject({ provider: "ant-main" });
    // 왼쪽 목록이 어느 모델과 나눈 대화인지 말한다
    await waitFor(() => expect(screen.getByTestId("home-session-list")).toHaveTextContent("ant-main"));
  });

  it("상대를 에이전트로 바꾸면 안쪽과 설명이 함께 갈리고 모델은 정의가 정한 값이다", async () => {
    const user = userEvent.setup();
    await pickHomeTarget(user);

    // 상대가 Agent 면 모델은 읽기 전용이고 빠른 시작 칩은 사라진다 (D45 둘째·넷째)
    expect(await screen.findByTestId("home-model-readonly")).toHaveTextContent("openai-main");
    expect(screen.queryByTestId("home-model-select")).not.toBeInTheDocument();
    expect(screen.queryByTestId("home-quick-starts")).not.toBeInTheDocument();
    expect(screen.getByTestId("home-actor-note")).toHaveTextContent("자기 자격으로 Tool 을 부릅니다");

    await startHomeChat(user, "3 더하기 4");
    expect(await screen.findByTestId("chat-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 7 입니다."));
    // 안내문은 정의의 세션 TTL 과 플랫폼의 기록 보존 기간을 읽어 사실대로 적는다 (D46)
    expect(screen.getByTestId("home-sessions-note")).toHaveTextContent("대화는 마지막 메시지 뒤 1시간이 지나면 닫혀 이어 쓸 수 없고, 기록은 30일 동안 남습니다");
    // 세션 고르개는 왼쪽 목록 하나뿐이다. 채팅 패널의 머리줄은 끈다
    expect(screen.queryByTestId("chat-session")).not.toBeInTheDocument();
    // 첫 턴이 만든 세션이 왼쪽 목록에 바로 보인다
    expect(await screen.findByTestId("home-session-s-new")).toBeInTheDocument();
  });

  it("에이전트와의 첫 턴은 인사말이 사용자 말 앞에 선다 (D65)", async () => {
    const user = userEvent.setup();
    await pickHomeTarget(user);
    await startHomeChat(user, "3 더하기 4");
    await screen.findByTestId("chat-panel");
    // 홈 챗은 첫 말과 함께 대화 화면을 열므로 정의를 읽지 않고 런타임의 greeting 이벤트로 인사말을 받는다
    await waitFor(() => expect(screen.getByTestId("chat-message-0")).toHaveTextContent("인사말"));
    expect(screen.getByTestId("chat-message-0")).toHaveTextContent("안녕하세요. 무엇을 도와드릴까요?");
    expect(screen.getByTestId("chat-message-1")).toHaveAttribute("data-role", "user");
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("3 더하기 4");
  });

  it("고른 상대를 테넌트마다 기억한다. 새로고침해도 청지기로 돌아가지 않는다", async () => {
    const user = userEvent.setup();
    const { unmount } = await pickHomeTarget(user);
    await waitFor(() => expect(screen.getByTestId("home-actor-note")).toHaveTextContent("자기 자격으로"));
    unmount();

    await openHomeChat();
    await waitFor(() => expect(screen.getByTestId("home-actor-note")).toHaveTextContent("자기 자격으로"));
    expect(screen.queryByTestId("home-quick-starts")).not.toBeInTheDocument();
  });

  it("에이전트 대화를 고르면 저장된 전사를 원문으로 되살린다 (F1)", async () => {
    const user = userEvent.setup();
    await pickHomeTarget(user);
    await startHomeChat(user, "3 더하기 4");
    await screen.findByTestId("chat-panel");

    await user.click(await screen.findByTestId("home-session-s-1"));
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("8 나누기 2 는?"));
    expect(screen.getByTestId("chat-transcript")).toHaveTextContent("나눗셈은 승인이 필요합니다.");
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-1")).toBeDefined();
  });

  it("닫힌 대화는 배지가 붙고 열면 읽기 전용이다. 새 대화로 이어 간다 (D46)", async () => {
    const user = userEvent.setup();
    await pickHomeTarget(user);
    // 닫힌 세션(s-3)에는 목록에서 이미 배지가 붙는다. 열리기는 한다
    expect(await screen.findByTestId("home-session-closed-s-3")).toHaveTextContent("닫힘");
    expect(screen.queryByTestId("home-session-closed-s-1")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("home-session-s-3"));
    // 전사는 그대로 보이고 입력칸 자리에 닫힌 까닭과 새 대화 버튼이 온다
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("8 나누기 2 는?"));
    expect(screen.getByTestId("chat-closed")).toHaveTextContent("이 대화는 마지막 메시지 뒤 1시간이 지나 닫혔습니다. 새 대화로 이어 가 주세요.");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("chat-closed-new-session"));
    // 새 대화는 시작 화면이다. 입력칸이 다시 있고 닫힘 안내는 사라진다
    expect(await screen.findByTestId("home-start-input")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-closed")).not.toBeInTheDocument();
  });

  it("전사와 입력칸은 가운데 열에 선다 (D46)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    // 청지기와 나누는 대화도 같은 폭이다. 오른쪽 비서 서랍만 그대로 400px 다
    await startHomeChat(user, "안녕");
    expect(await screen.findByTestId("assistant-column")).toHaveClass("mx-auto", "w-full", "max-w-[820px]");

    await selectValue(user, "home-agent-select", "ag-support");
    await startHomeChat(user, "3 더하기 4");
    expect(await screen.findByTestId("chat-column")).toHaveClass("mx-auto", "w-full", "max-w-[820px]");
  });

  it("홈 챗에서는 청지기 패널과 토글이 없고 다른 화면으로 나가면 돌아온다 (F5)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    // 헤더의 토글이 없다. 화면 안에 청지기가 이미 있다
    expect(screen.queryByTestId("assistant-toggle")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("nav-mcp-hub"));
    await screen.findByTestId("hub-page");
    expect(await screen.findByTestId("assistant-toggle")).toBeInTheDocument();

    await user.click(screen.getByTestId("nav-home-chat"));
    await screen.findByTestId("home-chat-page");
    expect(screen.queryByTestId("assistant-toggle")).not.toBeInTheDocument();
  });

  it("사이드바가 접힌 채로 열리고, 나갔다 오면 편 상태가 돌아온다 (D45)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true");

    await user.click(screen.getByTestId("nav-mcp-hub"));
    await screen.findByTestId("hub-page");
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");
  });

  it("브랜드 마크는 홈으로 가고, nav 의 새 대화는 시작 화면으로 되돌린다 (D45)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    await startHomeChat(user, "안녕");
    // 첫 메시지를 보냈으니 대화 화면이다. 상대가 청지기라 비서 패널이 선다
    expect(await screen.findByTestId("assistant-column")).toBeInTheDocument();
    expect(screen.queryByTestId("home-start")).not.toBeInTheDocument();

    // 같은 경로라 라우팅이 없다. state 로 받아 시작 화면으로 돌아온다
    await user.click(screen.getByTestId("nav-home-chat"));
    expect(await screen.findByTestId("home-start")).toBeInTheDocument();

    // 브랜드 마크는 다른 화면에서 홈으로 오는 길이다
    await user.click(screen.getByTestId("nav-mcp-hub"));
    await screen.findByTestId("hub-page");
    await user.click(screen.getByTestId("brand-home"));
    expect(await screen.findByTestId("home-chat-page")).toBeInTheDocument();
  });

  it("(2) 비서가 꺼져 있으면 인스턴스가 기본 상대다", async () => {
    noAssistant();
    await openHomeChat();
    expect(await screen.findByTestId("home-assistant-off")).toBeInTheDocument();
    expect(screen.getByTestId("home-actor-note")).toHaveTextContent("자기 자격으로");
    // 비서가 꺼졌으니 고를 모델도 없다
    expect(screen.queryByTestId("home-model-select")).not.toBeInTheDocument();
  });

  it("비서 설정을 받기 전에는 꺼짐을 보이지 않는다", async () => {
    let answer: () => void = () => undefined;
    const answered = new Promise<void>((resolve) => (answer = resolve));
    server.use(
      http.get("/assistant/config", async () => {
        await answered;
        return HttpResponse.json({ enabled: false });
      }),
    );
    await openHomeChat();
    expect(screen.queryByTestId("home-assistant-off")).not.toBeInTheDocument();
    answer();
    expect(await screen.findByTestId("home-assistant-off")).toBeInTheDocument();
  });

  it("(3) 인스턴스가 없으면 청지기와만 대화한다", async () => {
    const user = userEvent.setup();
    noInstances();
    await openHomeChat();
    await startHomeChat(user, "MCP Server 가 몇 개 있어?");
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();
  });

  it("(4) 둘 다 없으면 무엇을 만들면 되는지 말하고 그리로 보낸다", async () => {
    noAssistant();
    noInstances();
    render(<TestApp initialPath="/chat" />);
    const empty = await screen.findByTestId("home-empty");
    expect(empty).toHaveTextContent("아직 대화할 상대가 없습니다");
    // 인사는 그대로 있고 입력칸 자리에 길 둘이 온다
    expect(screen.getByTestId("home-greeting")).toBeInTheDocument();
    expect(screen.queryByTestId("home-start-input")).not.toBeInTheDocument();
    expect(within(empty).getByTestId("home-empty-agent-link")).toHaveAttribute("href", "/agents");
    expect(within(empty).getByTestId("home-empty-assistant-link")).toHaveAttribute("href", "/admin/settings");
    expect(screen.queryByTestId("home-empty-error")).not.toBeInTheDocument();
  });

  it("목록을 못 읽으면 401 을 '아무것도 없음' 으로 접지 않고 그렇게 말한다", async () => {
    noAssistant();
    server.use(http.get("/agents/summary", () => apiError(401, "unauthorized", "no token")));
    render(<TestApp initialPath="/chat" />);
    const empty = await screen.findByTestId("home-empty");
    expect(within(empty).getByTestId("home-empty-error")).toHaveTextContent("Agent 목록을 읽지 못했습니다");
    // 길은 그대로 남는다
    expect(within(empty).getByTestId("home-empty-agent-link")).toBeInTheDocument();
  });

  it("목록은 Ready 라는데 상세가 아니면 이유를 말하고 입력을 막는다", async () => {
    noAssistant();
    // 목록의 phase 는 마지막으로 알려진 값이라 지금 사실이 아닐 수 있다. 그래서 고른 뒤 상세로 확인한다
    agentState.items = agentState.items.map((i) => ({ ...i, status: { ...i.status, phase: "Failed" } }) as typeof i);
    server.use(
      http.get("/agents/summary", () =>
        HttpResponse.json([{ id: "ag-support", name: "support", display_name: "고객지원 에이전트", phase: "Ready" }]),
      ),
    );
    render(<TestApp initialPath="/chat" />);
    await screen.findByTestId("home-chat-page");
    expect(await screen.findByTestId("home-agent-not-ready")).toHaveTextContent("Failed 상태라 대화할 수 없습니다");
    expect(screen.getByTestId("home-start-input")).toBeDisabled();
  });

  it("phase 를 아직 모르는 인스턴스도 고르개에 올린다 (상태 조회 전이면 null 이다)", async () => {
    noAssistant();
    server.use(
      http.get("/agents/summary", () =>
        HttpResponse.json([{ id: "ag-support", name: "support", display_name: "고객지원 에이전트", phase: null }]),
      ),
    );
    render(<TestApp initialPath="/chat" />);
    await screen.findByTestId("home-chat-page");
    expect(await screen.findByTestId("home-agent-select")).toHaveTextContent("고객지원 에이전트");
    expect(screen.queryByTestId("home-empty")).not.toBeInTheDocument();
  });

  it("viewer 가 청지기에게 쓰기를 시키면 403 을 사람이 읽는 문장으로 받는다 (완료 조건 3)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    await startHomeChat(user, "내 권한으로 되는지 해 봐");

    // 도구는 실패로 접히고 모델이 이유를 말한다. 인가는 청지기 안에서도 그대로 돈다
    const transcript = await screen.findByTestId("assistant-transcript");
    await waitFor(() => expect(transcript).toHaveTextContent("배포 권한이 필요한데 지금 계정에는 없습니다"));
    const [failed] = transcriptRows({ role: "tool_call", container: transcript });
    expect(failed).toHaveAttribute("data-status", "error");
  });

  it("좁은 폭에서는 대화 목록과 대화가 한 번에 하나만 보인다", async () => {
    const user = userEvent.setup();
    stubMatchMedia(NARROW_QUERY);
    await openHomeChat();
    await startHomeChat(user, "MCP Server 가 몇 개 있어?");
    // 접힌 채로 시작한다. 나란히 세우면 대화 쪽이 잘린다(실측)
    expect(screen.queryByTestId("home-session-list")).not.toBeInTheDocument();
    expect(await screen.findByTestId("assistant-panel")).toBeInTheDocument();

    await user.click(screen.getByTestId("home-toggle-sessions"));
    expect(await screen.findByTestId("home-session-list")).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();

    // 새 대화를 고르면 시작 화면으로 돌아온다
    await user.click(screen.getByTestId("home-new-session"));
    expect(await screen.findByTestId("home-start")).toBeInTheDocument();
    expect(screen.queryByTestId("home-session-list")).not.toBeInTheDocument();
  });

  it("청지기가 홈 챗에서 화면을 옮기지 않고 링크만 남긴다 (F5)", async () => {
    const user = userEvent.setup();
    await openHomeChat();
    // 목의 "이동|열어" 스크립트가 ui{navigate} 를 흘린다
    await startHomeChat(user, "MCP Server 화면 열어 줘");

    const link = await screen.findByRole("link", { name: /mcp-servers\/ms-calc/ });
    expect(link).toHaveAttribute("href", "/mcp-servers/ms-calc");
    // 화면은 그대로 홈 챗이다. 옮기면 스트림이 끊긴다
    expect(screen.getByTestId("home-chat-page")).toBeInTheDocument();
  });
});
