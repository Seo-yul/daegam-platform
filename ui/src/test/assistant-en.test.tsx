/**
 * 영어로 그린 청지기 패널의 언어 전환 (P49 F8a). 중단 안내는 문장이 아니라 코드로 남아 언어를 바꾸면 다시 그려지고, 다른
 * 곳의 턴을 따라가는 줄도 영어로 그린다. 모의 비서가 쓴 말과 도구 설명은 `FIXTURE` 로 빼고 본다.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { setLanguage } from "@/lib/language";

import { leftKorean } from "./leftKorean";
import { assistantState, heldSse, http, record, server } from "./msw";

/** 모의 비서가 쓴 말과 도구 설명(msw/assistant.ts). 화면 언어와 상관없이 그대로 나온다. */
const FIXTURE: readonly string[] = [
  "calc 프로필로 MCP Server 를 만들겠습니다.",
  "배포는 승인이 필요합니다.",
  "MCP Server 를 만듭니다 (deploy 등급).",
  "calc 두 번째",
  "클러스터를 정해야 합니다.",
  "어느 클러스터에 배포할까요?",
  "천천히 진행합니다.",
];

type User = ReturnType<typeof userEvent.setup>;

async function openPanel(user: User): Promise<HTMLElement> {
  render(<TestApp initialPath="/clusters" />);
  await screen.findByTestId("clusters-page");
  await user.click(await screen.findByTestId("assistant-toggle"));
  return screen.getByTestId("assistant-panel");
}

async function say(user: User, panel: HTMLElement, text: string): Promise<void> {
  await user.type(within(panel).getByTestId("assistant-input"), text);
  await user.keyboard("{Enter}");
}

describe("assistant panel in English", () => {
  beforeEach(() => setLanguage("en"));

  it("keeps the stop notice as a code, so it follows a language switch, and follows a turn running elsewhere in English", async () => {
    const user = userEvent.setup();
    // 끝나지 않는 스트림. 서버 쪽 턴은 이어지므로 세션은 running 이다
    server.use(
      http.post("/assistant/sessions/:id/turns", async ({ request, params }) => {
        await record(request);
        const s = assistantState.sessions.find((x) => x.id === params.id)!;
        s.state = "running";
        s.transcript = [...s.transcript, { kind: "user", text: "slow deploy" }, { kind: "assistant", text: "천천히 진행합니다. ", streaming: false }];
        return heldSse([{ event: "token", data: { text: "천천히 진행합니다. " } }]).response;
      }),
    );
    const panel = await openPanel(user);
    await say(user, panel, "slow deploy");
    await waitFor(() => expect(within(panel).getByTestId("chat-message-1")).toHaveTextContent("천천히 진행합니다."));
    await user.click(within(panel).getByTestId("assistant-abort"));
    const error = await within(panel).findByTestId("assistant-error");
    expect(error).toHaveTextContent("You stopped the request. The server keeps working on it until it finishes.");
    const running = await within(panel).findByTestId("assistant-running");
    expect(running).toHaveTextContent("In progress");
    expect(running).toHaveTextContent("A turn started elsewhere is running. Checking every 3 seconds.");
    expect(leftKorean(panel, FIXTURE)).toEqual([]);

    act(() => setLanguage("ko"));
    expect(within(panel).getByTestId("assistant-error")).toHaveTextContent("요청을 중단했습니다. 서버 쪽 처리는 끝날 때까지 이어집니다.");
  });
});
