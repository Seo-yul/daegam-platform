/**
 * 채팅 입력칸의 첨부 (P36 9절). 고르기·끌어 놓기·붙여 넣기와 업로드 칩을 화면으로 본다. 보내기와 대화에 묶이는 규칙은
 * attachments-send 에 있다. 형식 판정과 이미지 다시 굽기 같은 순수 규칙은 attachments-lib 에 있다.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { dictionary } from "@/i18n";
import { IMAGE_ACCEPT, refusalText } from "@/lib/attachments";
import { DOCUMENT_ACCEPT, MAX_DOCUMENT_BYTES } from "@/lib/knowledge";

import { chip, pdf, pick, setupAttachmentAgent, waitReady } from "./attach";
import { openAgentChat } from "./chat";
import { stubProperty } from "./globals";
import { png } from "./images";
import { agentState, agentUploadState, agentWithAttachments, apiError, holdRequest, http, lastRequest, server } from "./msw";

const ko = dictionary("ko");

const UPLOADS_PATH = "/agents/ag-support/uploads";

/** 파일을 끌어 오는 DataTransfer. */
function files(...list: File[]) {
  return { dataTransfer: { files: list, types: ["Files"] } };
}

const canvas = setupAttachmentAgent();

describe("입력칸의 첨부", () => {
  it("첨부가 웹 fetch 를 좁히는 에이전트는 칩이 있을 때 허용 목록만 가져온다고 알린다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithAttachments({ attachments_limit_fetch: true })];
    await openAgentChat();
    expect(screen.queryByTestId("chat-attachments-fetch")).toBeNull();
    await pick(user, pdf());
    await waitReady(0);
    expect(screen.getByTestId("chat-attachments-fetch")).toHaveTextContent(ko.chat.attachments.limitsFetch);
  });

  it("선택한 문서는 곧바로 업로드되고 칩이 쪽과 글자 수를 보이며 그동안 보내기가 잠긴다", async () => {
    const user = userEvent.setup();
    const held = holdRequest("post", UPLOADS_PATH);
    await openAgentChat();
    expect(screen.getByTestId("chat-attach")).toHaveAccessibleName("파일 첨부");
    expect(screen.getByTestId("chat-attach-input")).toHaveAttribute("accept", `${DOCUMENT_ACCEPT},${IMAGE_ACCEPT}`);
    await user.type(screen.getByTestId("chat-input"), "정리해 줘");
    await pick(user, pdf());
    expect(chip(0)).toHaveAttribute("data-status", "uploading");
    expect(chip(0)).toHaveTextContent(`보고서.pdf· ${ko.chat.attachments.uploading}`);
    expect(screen.getByTestId("chat-send")).toBeDisabled();
    held.release();
    await waitReady(0);
    expect(chip(0)).toHaveTextContent("보고서.pdf· 12쪽 · 38,211자");
    expect(screen.getByTestId("chat-send")).toBeEnabled();
    expect(lastRequest("POST", UPLOADS_PATH)?.body).toMatchObject({ file: { name: "보고서.pdf" } });
    // 업로드 결과는 늘 있는 알림 줄이 낭독기에 알린다
    expect(screen.getByTestId("chat-attachments-live")).toHaveTextContent("보고서.pdf를 업로드했습니다.");
  });

  it("여러 파일은 하나씩 차례로 업로드하고 50만 자에서 잘린 문서에는 일부만 배지가 붙는다", async () => {
    const user = userEvent.setup();
    let inFlight = 0;
    let most = 0;
    server.use(
      http.post(UPLOADS_PATH, async () => {
        inFlight += 1;
        most = Math.max(most, inFlight);
        await new Promise((r) => setTimeout(r, 20));
        inFlight -= 1;
        return undefined;
      }),
    );
    await openAgentChat();
    await pick(user, pdf("하나.pdf"), new File(["a,b"], "대용량.csv", { type: "text/csv" }));
    await waitReady(1);
    await waitReady(0);
    expect(most).toBe(1);
    expect(chip(1)).toHaveTextContent(ko.chat.attachments.truncated);
    expect(chip(0)).not.toHaveTextContent(ko.chat.attachments.truncated);
  });

  it("파일은 채팅 열 어디에 끌어 놓아도 첨부하고, 붙여 넣은 이미지는 다시 구워 썸네일을 그린다", async () => {
    await openAgentChat();
    // 입력칸을 빗나가 전사 위에 놓아도 받는다. 브라우저가 파일을 열지 않게 기본 동작을 막는다
    const transcript = screen.getByTestId("chat-transcript");
    fireEvent.dragEnter(transcript, files(pdf()));
    expect(screen.getByTestId("chat-column")).toHaveAttribute("data-drop-over", "true");
    expect(fireEvent.drop(transcript, files(pdf()))).toBe(false);
    expect(screen.getByTestId("chat-column")).not.toHaveAttribute("data-drop-over");
    await waitReady(0);
    fireEvent.paste(screen.getByTestId("chat-input"), { clipboardData: { files: [png()], types: ["Files"] } });
    await waitReady(1);
    expect(canvas().toBlob).toHaveBeenCalledOnce();
    expect(lastRequest("POST", UPLOADS_PATH)?.body).toMatchObject({ file: { name: "화면.jpg", text: "baked" } });
    expect(chip(1).querySelector("img")).toHaveAttribute("src", expect.stringMatching(/^data:image\/jpeg;base64,/));
    // 글을 끌어 놓으면 첨부가 아니고 기본 동작도 그대로다
    expect(fireEvent.drop(screen.getByTestId("chat-form"), { dataTransfer: { files: [], types: ["text/plain"] } })).toBe(true);
    expect(screen.getAllByTestId(/^chat-attachments-\d+$/)).toHaveLength(2);
  });

  it("클립보드에 글이 함께 있으면 글만 붙이고 그림 표현은 첨부하지 않는다", async () => {
    await openAgentChat();
    // Office 에서 복사한 셀은 글과 함께 PNG 표현을 싣는다
    fireEvent.paste(screen.getByTestId("chat-input"), { clipboardData: { files: [png("셀.png")], types: ["text/plain", "text/html", "Files"] } });
    expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument();
    expect(lastRequest("POST", UPLOADS_PATH)).toBeUndefined();
  });

  it("입력칸이 잠긴 에이전트에서는 끌어 놓은 파일을 받지 않고 브라우저가 여는 것만 막는다", async () => {
    const agent = agentWithAttachments();
    agentState.items = [{ ...agent, status: agent.status && { ...agent.status, phase: "Failed" } }];
    await openAgentChat();
    const transcript = screen.getByTestId("chat-transcript");
    fireEvent.dragEnter(transcript, files(pdf()));
    expect(screen.getByTestId("chat-column")).not.toHaveAttribute("data-drop-over");
    expect(fireEvent.drop(transcript, files(pdf()))).toBe(false);
    expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument();
  });

  it("받지 않는 형식과 종류, 상한을 넘는 문서는 업로드하지 않고 오류 칩으로 보내기를 잠그며, 삭제하면 풀린다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithAttachments({ documents: false })];
    await openAgentChat();
    expect(screen.getByTestId("chat-attach-input")).toHaveAttribute("accept", IMAGE_ACCEPT);
    await user.type(screen.getByTestId("chat-input"), "이거 봐 줘");
    fireEvent.drop(screen.getByTestId("chat-form"), files(pdf(), new File(["MZ"], "설치.exe")));
    expect(chip(0)).toHaveAttribute("data-status", "error");
    expect(screen.getByTestId("chat-attachments-0-error")).toHaveTextContent(ko.errors.codes.attachments_need_filesystem);
    expect(screen.getByTestId("chat-attachments-1-error")).toHaveTextContent(refusalText(ko, { code: "unsupported" }));
    expect(screen.getByTestId("chat-send")).toBeDisabled();
    expect(lastRequest("POST", UPLOADS_PATH)).toBeUndefined();
    await user.click(screen.getByTestId("chat-attachments-1-remove"));
    await user.click(screen.getByTestId("chat-attachments-0-remove"));
    expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-send")).toBeEnabled();
  });

  it("10MiB 를 넘는 문서는 앞 업로드를 기다리지 않고 곧바로 오류 칩이 된다", async () => {
    const user = userEvent.setup();
    holdRequest("post", UPLOADS_PATH);
    await openAgentChat();
    await pick(user, pdf("앞.pdf"), new File([new Uint8Array(MAX_DOCUMENT_BYTES + 1)], "큰.pdf", { type: "application/pdf" }));
    expect(chip(0)).toHaveAttribute("data-status", "uploading");
    expect(chip(1)).toHaveAttribute("data-status", "error");
    expect(screen.getByTestId("chat-attachments-1-error")).toHaveTextContent(refusalText(ko, { code: "too_large", kind: "document", limit: MAX_DOCUMENT_BYTES }));
  });

  it("서버가 거절한 업로드는 그 까닭을 칩에 적고 낭독기에 알린다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await pick(user, pdf("스캔본.pdf"));
    await waitFor(() => expect(chip(0)).toHaveAttribute("data-status", "error"));
    expect(screen.getByTestId("chat-attachments-0-error")).toHaveTextContent(ko.errors.codes.empty_document);
    expect(screen.getByTestId("chat-attachments-live")).toHaveTextContent(`스캔본.pdf를 업로드하지 못했습니다. ${ko.errors.codes.empty_document}`);
    server.use(http.post(UPLOADS_PATH, () => apiError(429, "upload_rate_limited", "more than 20 uploads in a minute")));
    await pick(user, pdf("둘째.pdf"));
    await waitFor(() => expect(screen.getByTestId("chat-attachments-1-error")).toHaveTextContent(ko.errors.codes.upload_rate_limited));
  });

  it("풀지 못한 이미지는 손상됐거나 너무 클 수 있다고 칩에 적는다", async () => {
    const user = userEvent.setup();
    stubProperty(window, "createImageBitmap", vi.fn(async () => Promise.reject(new DOMException("The source image could not be decoded.", "InvalidStateError"))));
    await openAgentChat();
    await pick(user, png("거대한.png"));
    await waitFor(() => expect(screen.getByTestId("chat-attachments-0-error")).toHaveTextContent(ko.chat.attachments.imageUnreadable));
    expect(lastRequest("POST", UPLOADS_PATH)).toBeUndefined();
  });

  it("한 턴 상한을 넘는 파일은 칩을 만들지 않고 안내하며 낭독기에도 알린다", async () => {
    await openAgentChat();
    const list = [pdf("1.pdf"), pdf("2.pdf"), png("1.png"), png("2.png"), png("3.png"), png("4.png"), png("5.png")];
    fireEvent.drop(screen.getByTestId("chat-form"), files(...list));
    expect(screen.getAllByTestId(/^chat-attachments-\d+$/)).toHaveLength(5);
    const notice = "한 번에 파일 5개, 그 가운데 이미지 4개까지 첨부할 수 있어 2개는 첨부하지 않았습니다.";
    expect(screen.getByTestId("chat-attachments-notice")).toHaveTextContent(notice);
    // 섰다 사라지는 줄의 role="status" 는 낭독기가 놓칠 수 있어 늘 있는 알림 줄이 알린다
    expect(screen.getByTestId("chat-attachments-notice")).not.toHaveAttribute("role");
    expect(screen.getByTestId("chat-attachments-live")).toHaveTextContent(notice);
    await waitFor(() => expect(agentUploadState.items).toHaveLength(5));
  });

  it("칩을 삭제하면 초점이 다음 칩의 삭제 단추로, 마지막이면 입력칸으로 간다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await pick(user, pdf("하나.pdf"), pdf("둘.pdf"));
    await waitReady(1);
    await user.click(screen.getByTestId("chat-attachments-0-remove"));
    // 지운 칩 뒤의 칩이 앞으로 당겨져 0 번이 된다
    expect(screen.getByTestId("chat-attachments-0-remove")).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.queryByTestId("chat-attachments")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-input")).toHaveFocus();
  });

  it("칩을 삭제하거나 패널이 사라지면 보내지 않은 업로드를 서버에서 지운다", async () => {
    const user = userEvent.setup();
    const { unmount } = await openAgentChat();
    await pick(user, pdf("하나.pdf"), pdf("둘.pdf"));
    await waitReady(1);
    const [first, second] = agentUploadState.items.map((u) => u.id);
    await user.click(screen.getByTestId("chat-attachments-0-remove"));
    await waitFor(() => expect(agentUploadState.deleted).toContain(first));
    expect(agentUploadState.deleted).not.toContain(second);
    unmount();
    await waitFor(() => expect(agentUploadState.items).toEqual([]));
  });

  it("패널이 사라질 때 업로드 중인 것은 끝난 뒤 지우고 줄 서 있던 것은 시작하지 않는다", async () => {
    const user = userEvent.setup();
    const held = holdRequest("post", UPLOADS_PATH);
    const { unmount } = await openAgentChat();
    await pick(user, pdf("하나.pdf"), pdf("둘.pdf"), pdf("셋.pdf"));
    await waitFor(() => expect(held.started()).toBe(1));
    unmount();
    held.release();
    // 업로드 중이던 하나는 끝난 뒤 지워지고, 줄의 둘·셋은 요청조차 하지 않는다
    await waitFor(() => expect(agentUploadState.deleted).toHaveLength(1));
    expect(agentUploadState.items).toEqual([]);
    expect(held.started()).toBe(1);
  });
});
