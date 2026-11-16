/**
 * 첨부를 보내기와 첨부가 대화에 묶이는 규칙 (P36 9절). 입력칸의 첨부는 attachments 에 있다.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { dictionary } from "@/i18n";
import { RUNTIME_REGENERATE } from "@/lib/runtimeFeatures";

import { chip, pdf, pick, setupAttachmentAgent, waitReady } from "./attach";
import { openAgentChat, openHomeSession, pickHomeTarget, sendMessage, startHomeChat, waitForTurnEnd } from "./chat";
import { png } from "./images";
import { agentState, agentUploadState, agentWithAttachments, apiError, holdRequest, lastRequest } from "./msw";

const ko = dictionary("ko");

const CHAT_PATH = "/agents/ag-support/chat";

setupAttachmentAgent();

describe("첨부를 보내기", () => {
  it("업로드 id 만 싣고, 스트림이 열리면 칩을 비우며 보낸 말풍선 아래에 새로고침한 뒤와 같은 칩이 남는다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await pick(user, pdf(), png());
    await waitReady(1);
    const ids = agentUploadState.items.map((u) => u.id);
    // 첨부만으로는 보낼 수 없다는 것을 알린다
    expect(screen.getByTestId("chat-attachments-hint")).toHaveTextContent(ko.chat.attachments.needsText);
    await user.type(screen.getByTestId("chat-input"), "정리해 줘");
    expect(screen.queryByTestId("chat-attachments-hint")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("chat-send"));
    await waitFor(() => expect(lastRequest("POST", CHAT_PATH)).toBeDefined());
    expect(lastRequest("POST", CHAT_PATH)?.body).toEqual({ session_id: null, message: "정리해 줘", ephemeral: true, attachments: ids });
    await waitFor(() => expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument());
    const sent = screen.getByTestId("chat-message-1-attachments");
    expect(within(sent).getAllByRole("listitem")).toHaveLength(2);
    // 전사에는 글자 수가 없다. 보낸 직후에도 새로고침한 뒤와 같은 칸만 보인다
    expect(within(sent).getAllByRole("listitem")[0]).toHaveTextContent(/^보고서\.pdf· 12쪽$/);
    // 턴 마감 줄까지 기다린다. 전달 확인은 done 에서 한다
    await waitForTurnEnd();
    expect(screen.queryByTestId("chat-error")).not.toBeInTheDocument();
    // 칩을 비운 것이지 업로드를 지운 것이 아니다. 플랫폼이 이미 지웠다
    expect(agentUploadState.deleted.filter((id) => ids.includes(id))).toEqual([]);
  });

  it("스트림이 열리기 전에는 실은 칩을 삭제할 수 없고, 입구에서 거절되면 칩과 쓴 글이 돌아오며 말풍선은 걷힌다", async () => {
    const user = userEvent.setup();
    const chat = holdRequest("post", CHAT_PATH, () => apiError(409, "runtime_rolling", "the agent is Pending, not Ready"));
    await openAgentChat();
    await pick(user, pdf());
    await waitReady(0);
    await sendMessage(user, "정리해 줘");
    await waitFor(() => expect(chat.started()).toBe(1));
    expect(screen.queryByTestId("chat-attachments-0-remove")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-input")).toHaveValue("");
    chat.release();
    await waitFor(() => expect(screen.getByTestId("chat-error")).toHaveTextContent(ko.errors.codes.runtime_rolling));
    expect(chip(0)).toHaveAttribute("data-status", "ready");
    expect(screen.getByTestId("chat-attachments-0-remove")).toBeInTheDocument();
    expect(screen.getByTestId("chat-input")).toHaveValue("정리해 줘");
    expect(screen.queryByTestId("chat-message-1")).not.toBeInTheDocument();
    expect(agentUploadState.deleted).not.toContain(agentUploadState.items[0].id);
  });

  it("런타임이 받은 수가 보낸 수와 다르면 첨부가 전달되지 않았다고 알린다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await pick(user, pdf());
    await waitReady(0);
    await sendMessage(user, "옛 런타임에 보낸다");
    await waitFor(() => expect(screen.getByTestId("chat-error")).toHaveTextContent(ko.chat.notice.attachmentsNotDelivered));
  });

  it("첨부가 없는 턴은 그 칸을 싣지 않는다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "안녕");
    await waitFor(() => expect(lastRequest("POST", CHAT_PATH)).toBeDefined());
    expect(lastRequest("POST", CHAT_PATH)?.body).toEqual({ session_id: null, message: "안녕", ephemeral: true });
  });

  it("메시지를 수정하는 동안 그 말의 첨부가 읽기 전용 칩으로 남는다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithAttachments({}, RUNTIME_REGENERATE)];
    await openAgentChat();
    await pick(user, pdf());
    await waitReady(0);
    await sendMessage(user, "정리해 줘");
    await user.click(await screen.findByTestId("chat-message-1-edit"));
    const kept = await screen.findByTestId("chat-edit-attachments");
    expect(kept).toHaveTextContent("보고서.pdf");
    expect(within(kept).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("첨부는 대화에 묶인다", () => {
  it("새 세션을 시작하면 보내지 않은 첨부를 서버에서 지우고 칩을 비운다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "안녕");
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 7 입니다."));
    await pick(user, pdf());
    await waitReady(0);
    const [id] = agentUploadState.items.map((u) => u.id);
    await user.click(screen.getByTestId("chat-new-session"));
    expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument();
    await waitFor(() => expect(agentUploadState.deleted).toEqual([id]));
  });

  it("넓은 화면의 대화 목록에서 다른 대화를 고르면 첨부가 따라가지 않는다", async () => {
    const user = userEvent.setup();
    await openHomeSession(user);
    await pick(user, pdf());
    await waitReady(0);
    const [id] = agentUploadState.items.map((u) => u.id);
    await user.click(screen.getByTestId("home-session-s-2"));
    await waitFor(() => expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument());
    await waitFor(() => expect(agentUploadState.deleted).toEqual([id]));
  });

  it("첫 턴이 만든 세션을 목록이 받아 돌려줘도 같은 대화라 그사이 첨부한 칩은 남는다", async () => {
    const user = userEvent.setup();
    const chat = holdRequest("post", CHAT_PATH);
    await pickHomeTarget(user);
    await startHomeChat(user, "3 더하기 4");
    await screen.findByTestId("chat-panel");
    await pick(user, pdf());
    await waitReady(0);
    const [id] = agentUploadState.items.map((u) => u.id);
    chat.release();
    await waitFor(() => expect(screen.getByTestId("chat-transcript")).toHaveTextContent("결과는 7 입니다."));
    await screen.findByTestId("home-session-s-new");
    expect(chip(0)).toHaveAttribute("data-status", "ready");
    expect(agentUploadState.deleted).not.toContain(id);
  });
});
