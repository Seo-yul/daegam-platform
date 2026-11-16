/**
 * 영어로 그린 Agent 화면의 언어 전환 (P49 F7b-1). 배포 대화상자의 리소스 오류와 캔버스의 안내는 상태에 문장 대신 코드를
 * 두므로 언어를 바꾸면 다시 그려진다. 목 서버가 주는 표시 이름과 템플릿 이름은 데이터라 `FIXTURE` 로 지우고 본다.
 */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { setLanguage } from "@/lib/language";

import { leftKorean } from "./leftKorean";
import { agentDraftFixture, agentState } from "./msw";

/** 목 서버(msw/agents.ts, agent-templates.ts, agent-sse.ts, agent-feedback.ts)가 담은 한국어. */
const FIXTURE: readonly string[] = [
  "단일 상담 에이전트",
  "상담 에이전트",
  "고객 상담을 맡는 메인 에이전트다.",
  "안녕하세요. 무엇을 도와드릴까요?",
  "환불 기한 문의",
  "8 나누기 2 는?",
  "나눗셈은 승인이 필요합니다.",
  "8을 2로 나눕니다.",
  "결과는 4 입니다.",
  "환불 기한이 틀렸습니다.",
];

describe("Agent screens in English", () => {
  beforeEach(() => setLanguage("en"));

  it("redraws the resources error of the deploy dialog in the new language after a switch", async () => {
    setLanguage("ko");
    const user = userEvent.setup();
    agentState.items = [agentDraftFixture()];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await user.type(within(dialog).getByTestId("ag-req-mem"), "500mi");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    const error = await screen.findByTestId("form-error");
    expect(error).toHaveTextContent(/^resources: requests\.memory: .*[가-힣]/);
    act(() => setLanguage("en"));
    expect(error).toHaveTextContent(/^resources: requests\.memory: /);
    expect(leftKorean(error)).toEqual([]);
    expect(within(dialog).getByTestId("ag-req-mem")).toHaveValue("500mi");
  });

  it("draws the canvas header, the empty canvas and the test chat drawer, and redraws a notice after a switch", async () => {
    const user = userEvent.setup();
    const app = render(<TestApp initialPath="/agents/new" />);
    const header = await screen.findByTestId("canvas-header");
    const empty = await screen.findByTestId("canvas-empty");
    expect(empty).toHaveTextContent("You need three nodes to save: Chat trigger, Agent and Model.");
    expect(header).toHaveTextContent("New Agent");
    expect(screen.getByTestId("canvas-validation-idle")).toHaveTextContent("Validates once you place nodes");
    expect(screen.getByTestId("canvas-autolayout")).toHaveAccessibleName("Arrange nodes. The canvas has no nodes. Place a node from the palette first.");
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("Save to chat in a preview.");
    expect(leftKorean(header, FIXTURE)).toEqual([]);
    expect(leftKorean(empty, FIXTURE)).toEqual([]);
    expect(leftKorean(screen.getByTestId("test-chat-drawer"), FIXTURE)).toEqual([]);

    act(() => setLanguage("ko"));
    await user.click(await screen.findByTestId("canvas-empty-template-support-agent"));
    const notice = await screen.findByTestId("canvas-notice");
    expect(notice).toHaveTextContent('템플릿 "단일 상담 에이전트"를 불러왔습니다.');
    act(() => setLanguage("en"));
    expect(notice).toHaveTextContent('Loaded the template "단일 상담 에이전트". Select a provider, an MCP source and a Database, then save.');
    expect(leftKorean(notice, FIXTURE)).toEqual([]);
    expect(leftKorean(screen.getByTestId("canvas-header"), FIXTURE)).toEqual([]);

    app.unmount();
    render(<TestApp initialPath="/agents/ag-support?tab=definition" />);
    expect(await screen.findByTestId("canvas-revision")).toHaveAccessibleName("Revision");
    expect(screen.getByTestId("canvas-revision")).toHaveTextContent("r2 (current)");
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("Chatting with deployed r2");
    expect(leftKorean(screen.getByTestId("canvas-header"), FIXTURE)).toEqual([]);
    expect(leftKorean(screen.getByTestId("test-chat-drawer"), FIXTURE)).toEqual([]);
  });

  it("names the deployed revision of the test chat, and Korean prints nothing after r while it is unknown", () => {
    expect(dictionary("en").agents.testChat.talkingTo(null)).toBe("Chatting with the deployed revision");
    expect(dictionary("ko").agents.testChat.talkingTo(null)).toBe("배포된 r과 대화합니다");
    expect(dictionary("ko").agents.testChat.talkingTo(2)).toBe("배포된 r2와 대화합니다");
    expect(dictionary("ko").agents.testChat.talkingTo(1)).toBe("배포된 r1과 대화합니다");
  });
});
