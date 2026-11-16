/**
 * 채팅 화면 테스트 도우미. Agent 상세의 채팅 탭과 홈 챗(`/chat`)을 열고, 말을 보내고, 전사의 줄을 집는다.
 *
 * 목의 기본 Agent 는 `ag-support` 이고 그 대화 목록에는 s-1(이어 쓰는 대화), s-2(승인 대기), s-3(닫힌 대화)이 있다
 * (msw/agents.ts·agent-sse.ts). 새 세션의 첫 턴은 런타임의 greeting 이벤트가 먼저 와서 전사의 0번이 인사말, 1번이 사용자 말이다.
 * 이 파일은 .ts 라 앱은 JSX 대신 createElement 로 그린다.
 */
import { render, screen, waitFor, within, type RenderResult } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { createElement } from "react";
import { expect } from "vitest";

import { TestApp } from "@/App";

import { selectValue } from "./select";

/** 목의 기본 Agent 다. */
const AGENT_ID = "ag-support";

/**
 * Agent 상세의 채팅 탭을 열고 패널이 설 때까지 기다린다. 상세의 채팅은 테스트 채팅이라 턴과 평가에 ephemeral 이 붙는다.
 * 패널이 사라질 때의 정리를 보는 테스트가 unmount 할 수 있게 그린 결과를 돌려준다.
 */
export async function openAgentChat(agentId = AGENT_ID): Promise<RenderResult> {
  const view = render(createElement(TestApp, { initialPath: `/agents/${agentId}?tab=chat` }));
  await screen.findByTestId("chat-panel");
  return view;
}

/** 홈 챗을 열고 시작 화면이 설 때까지 기다린다. 상대는 기본값이다(비서가 켜져 있으면 청지기). */
export async function openHomeChat(): Promise<RenderResult> {
  const view = render(createElement(TestApp, { initialPath: "/chat" }));
  await screen.findByTestId("home-start");
  return view;
}

/** 홈 챗을 열고 입력칸 안의 상대 pill 에서 Agent 를 고른다. 왼쪽 목록이 그 Agent 와 나눈 대화로 바뀐다. */
export async function pickHomeTarget(user: UserEvent, agentId = AGENT_ID): Promise<RenderResult> {
  const view = await openHomeChat();
  await selectValue(user, "home-agent-select", agentId);
  return view;
}

/** 홈 챗에서 기본 Agent 를 고르고 왼쪽 대화 목록이 설 때까지 기다린다. s-1 의 행을 돌려준다. */
export async function openHomeSessionList(user: UserEvent): Promise<HTMLElement> {
  await pickHomeTarget(user);
  return screen.findByTestId("home-session-s-1");
}

/** 홈 챗에서 기본 Agent 를 고르고 왼쪽 목록의 대화 하나를 열어 채팅 패널이 설 때까지 기다린다. */
export async function openHomeSession(user: UserEvent, sessionId = "s-1"): Promise<void> {
  await pickHomeTarget(user);
  await user.click(await screen.findByTestId(`home-session-${sessionId}`));
  await screen.findByTestId("chat-panel");
}

/** 홈 챗의 시작 화면에서 첫 메시지를 보낸다. 여기서부터 고른 상대와의 대화 화면이다. */
export async function startHomeChat(user: UserEvent, text: string): Promise<void> {
  await user.type(await screen.findByTestId("home-start-input"), text);
  await user.click(screen.getByTestId("home-start-send"));
}

/** 열린 채팅 패널의 입력칸에 말을 쓰고 보내기를 누른다. 턴이 끝나기를 기다리지는 않는다. */
export async function sendMessage(user: UserEvent, text: string): Promise<void> {
  await user.type(screen.getByTestId("chat-input"), text);
  await user.click(screen.getByTestId("chat-send"));
}

/** 채팅 패널 전사의 마지막 줄이 턴 마감(done)이 될 때까지 기다린다. */
export async function waitForTurnEnd(): Promise<void> {
  await waitFor(() => expect(transcriptRows().at(-1)).toHaveAttribute("data-role", "done"));
}

/** Agent 상세의 채팅 탭을 열고 첫 턴을 보내 끝나기를 기다린다. */
export async function firstTurn(user: UserEvent, text: string): Promise<void> {
  await openAgentChat();
  await sendMessage(user, text);
  await waitForTurnEnd();
}

interface RowQuery {
  /** 이 data-role 의 줄만 집는다(user·assistant·tool_call·done 등). */
  role?: string;
  /** 줄을 찾을 전사. 주지 않으면 채팅 패널의 전사다. */
  container?: HTMLElement;
}

/** 전사의 줄(말풍선·도구 카드·마감 줄)을 위에서부터 돌려준다. 줄이 하나도 없으면 실패한다. */
export function transcriptRows({ role, container = screen.getByTestId("chat-transcript") }: RowQuery = {}): HTMLElement[] {
  const rows = within(container).getAllByTestId(/^chat-message-\d+$/);
  return role ? rows.filter((el) => el.dataset.role === role) : rows;
}
