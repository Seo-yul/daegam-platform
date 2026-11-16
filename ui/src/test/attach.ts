/**
 * 채팅 입력칸의 첨부 테스트 두 파일이 함께 쓰는 도우미. 한 파일이 스위트 전체 시간을 붙잡지 않도록 둘로 나눴다.
 * attachments 는 입력칸의 첨부를, attachments-send 는 보내기와 대화에 묶이는 규칙을 본다.
 */
import { screen, waitFor } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { beforeEach, expect } from "vitest";

import { stubCanvas } from "./images";
import { agentState, agentWithAttachments } from "./msw";

export function pdf(name = "보고서.pdf"): File {
  return new File(["%PDF-1.7"], name, { type: "application/pdf" });
}

export function chip(i: number): HTMLElement {
  return screen.getByTestId(`chat-attachments-${i}`);
}

export async function pick(user: UserEvent, ...list: File[]) {
  await user.upload(screen.getByTestId("chat-attach-input"), list);
}

export async function waitReady(i: number) {
  await waitFor(() => expect(chip(i)).toHaveAttribute("data-status", "ready"));
}

/** 테스트마다 첨부를 받는 Agent 하나와 이미지 다시 굽기용 canvas 스텁을 깐다. 스텁은 돌려준 함수로 꺼낸다. */
export function setupAttachmentAgent(): () => ReturnType<typeof stubCanvas> {
  let canvas: ReturnType<typeof stubCanvas>;
  beforeEach(() => {
    agentState.items = [agentWithAttachments()];
    canvas = stubCanvas();
  });
  return () => canvas;
}
