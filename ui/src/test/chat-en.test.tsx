/**
 * 영어로 그린 채팅의 언어 전환과 런타임 글자 (P49 F8a). 입력칸의 첨부 칩은 거절의 문장 대신 거절 자체를 두므로 언어를
 * 바꾸면 다시 그려지고, 첨부의 안내와 낭독기 이름에 한글이 남지 않는다. 사람과 모델이 쓴 글, 파일 이름 같은 목 데이터는
 * `FIXTURE` 로 빼고 본다. 런타임이 저장한 가드레일 문장은 사전의 기본 문장으로 바뀌는지 본다.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChatItemView } from "@/components/chat";
import { setLanguage } from "@/lib/language";

import { chip, pick, setupAttachmentAgent, waitReady } from "./attach";
import { openAgentChat } from "./chat";
import { leftKorean } from "./leftKorean";
import { renderWithProviders } from "./render";

/** 목 데이터의 한국어(대화 제목, 사람과 모델의 말, 도구 설명, 인사말, 파일 이름). 화면 언어와 상관없이 그대로 나온다. */
const FIXTURE: readonly string[] = [
  "환불 기한 문의",
  "8 나누기 2 는?",
  "나눗셈은 승인이 필요합니다.",
  "결과는 4 입니다.",
  "8을 2로 나눕니다.",
  "안녕하세요. 무엇을 도와드릴까요?",
  "고객지원 에이전트",
  "상담 에이전트",
  "보고서.pdf.txt",
  "화면.png",
  "할 일.txt",
  "요약.md",
  "큰 표.csv",
];

function pdf(name: string): File {
  return new File(["%PDF-1.7"], name, { type: "application/pdf" });
}

describe("chat attachments in English", () => {
  setupAttachmentAgent();

  it("redraws a refused chip in the new language and draws chips, notices and announcements in English", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    // 칩은 거절의 문장이 아니라 거절 자체를 들어 언어를 바꾸면 다시 그려진다
    fireEvent.drop(screen.getByTestId("chat-form"), { dataTransfer: { files: [new File(["MZ"], "setup.exe")], types: ["Files"] } });
    expect(screen.getByTestId("chat-attachments-0-error")).toHaveTextContent("첨부할 수 없는 형식입니다.");
    act(() => setLanguage("en"));
    expect(screen.getByTestId("chat-attachments-0-error")).toHaveTextContent("This file type cannot be attached. Attach a document (");
    expect(screen.getByTestId("chat-attachments-0-remove")).toHaveAccessibleName("Remove setup.exe");
    await user.click(screen.getByTestId("chat-attachments-0-remove"));

    expect(screen.getByTestId("chat-attach")).toHaveAccessibleName("Attach files");
    await pick(user, pdf("report.pdf"));
    await waitReady(0);
    expect(chip(0)).toHaveTextContent("report.pdf· 12 pages · 38,211 characters");
    expect(screen.getByTestId("chat-attachments")).toHaveAccessibleName("Attachments");
    expect(screen.getByTestId("chat-attachments-live")).toHaveTextContent("Uploaded report.pdf.");
    expect(screen.getByTestId("chat-attachments-hint")).toHaveTextContent("Type a message to send it with the attachments.");

    const many = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf", "f.pdf"].map(pdf);
    fireEvent.drop(screen.getByTestId("chat-form"), { dataTransfer: { files: many, types: ["Files"] } });
    expect(screen.getByTestId("chat-attachments-notice")).toHaveTextContent("You can attach up to 5 files at a time, including up to 4 images, so 2 files were not attached.");
    expect(leftKorean(screen.getByTestId("chat-panel"), FIXTURE)).toEqual([]);
  });
});

/** 문구 없는 가드레일 위반을 저장할 때 런타임이 채우는 문장 (agent-runtime/runtime/transcript.py GUARDRAIL_FALLBACK_MESSAGE). */
const SERVER_GUARDRAIL_FALLBACK = "가드레일에 걸렸습니다.";

describe("guardrail text the runtime stores", () => {
  it("reads the same value transcript.py writes where the stream had no message", () => {
    const src = readFileSync(resolve(process.cwd(), "../agent-runtime/runtime/transcript.py"), "utf8");
    expect(src).toContain(`GUARDRAIL_FALLBACK_MESSAGE = "${SERVER_GUARDRAIL_FALLBACK}"`);
  });

  it("draws the dictionary's default for the stored value and an empty message, and keeps a rule's own message", () => {
    setLanguage("en");
    renderWithProviders(
      <>
        <ChatItemView index={0} item={{ kind: "guardrail", action: "flag", message: SERVER_GUARDRAIL_FALLBACK, position: "output" }} />
        <ChatItemView index={1} item={{ kind: "guardrail", action: "block", message: "" }} />
        <ChatItemView index={2} item={{ kind: "guardrail", action: "block", message: "Card numbers cannot be sent." }} />
      </>,
    );
    expect(screen.getByTestId("chat-message-0")).toHaveTextContent("Guardrail (output): A guardrail rule matched.");
    expect(screen.getByTestId("chat-message-1")).toHaveTextContent("Guardrail: A guardrail rule matched.");
    expect(screen.getByTestId("chat-message-2")).toHaveTextContent("Guardrail: Card numbers cannot be sent.");
  });

  it("keeps the runtime's sentence on Korean screens", () => {
    renderWithProviders(<ChatItemView index={0} item={{ kind: "guardrail", action: "flag", message: SERVER_GUARDRAIL_FALLBACK }} />);
    expect(screen.getByTestId("chat-message-0")).toHaveTextContent(`가드레일: ${SERVER_GUARDRAIL_FALLBACK}`);
  });
});
