/**
 * 이 대화의 파일 서랍과 도구 카드의 다운로드 (P36 10절). 런타임이 `session_files` 를 알린 에이전트에서 채팅 위의 "파일"
 * 단추가 여는 서랍이 업로드한 파일과 에이전트가 만든 파일의 두 묶음을 보이고 에이전트 쪽에 텍스트 파일 안내를 다는지, 미리
 * 보기가 상한 이하 파일에만 있고 `.md` 는 마크다운·이미지는 안내로 보이는지, 다운로드 도우미가 경로를 조각마다 인코딩해 부르고
 * 헤더의 이름으로 저장하는지, 닫힌 대화가 목록 대신 파일이 지워졌다는 문구를 보이는지, 성공한 `write_file` 카드에만 다운로드가
 * 서는지, 거절을 한국어 문장으로 보이는지 본다. 알리지 않은 런타임에 단추가 없는 것은 resource-states.test 의 표가 본다.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { AgentSessionDetail } from "@/api/types";
import { dictionary } from "@/i18n";
import { RUNTIME_CANCEL, RUNTIME_SESSION_FILES } from "@/lib/runtimeFeatures";

import { openHomeSession } from "./chat";
import { stubDownloads } from "./downloads";
import { BIG_FILE, HttpResponse, PNG_BYTES, agentSessionFileState, agentSessionState, agentState, agentWithRuntimeFeatures, apiError, http, lastRequest, server } from "./msw";

const ko = dictionary("ko");

type User = ReturnType<typeof userEvent.setup>;

async function openDrawer(user: User, sessionId?: string) {
  await openHomeSession(user, sessionId);
  await user.click(await screen.findByTestId("session-files-open"));
  return screen.findByTestId("session-files");
}

/** 묶음 하나의 행 경로다. */
function rowPaths(group: HTMLElement): (string | undefined)[] {
  return within(group)
    .getAllByRole("listitem")
    .map((li) => li.dataset.testid?.replace("session-file-", ""));
}

/** s-1 의 전사를 파일 쓰기 도구 카드 둘로 바꾼다. 하나는 성공했고 하나는 실패했다. */
function useWriteTranscript() {
  const session = agentSessionState.items.find((s) => s.session_id === "s-1")!;
  const card = (id: string, path: string, status: "success" | "error") => ({
    kind: "tool_call",
    call: { id, name: "write_file", args: { file_path: path, content: "# 요약" } },
    result: { id, name: "write_file", status, content: status === "success" ? `Updated file ${path}` : "Error: permission denied" },
  });
  const detail: AgentSessionDetail = {
    session,
    messages: [],
    pending: false,
    transcript: { items: [{ kind: "user", text: "요약을 파일로 남겨 줘" }, card("call-1", "/reports/요약.md", "success"), card("call-2", "/uploads/보고서.pdf.txt", "error")], limit: 500 },
  };
  server.use(http.get("/agents/:id/sessions/s-1", () => HttpResponse.json(detail)));
}

describe("session files drawer", () => {
  let saved: string[] = [];

  beforeEach(() => {
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_SESSION_FILES)];
    saved = stubDownloads();
  });

  it("업로드한 파일과 에이전트가 만든 파일을 두 묶음으로 보이고 에이전트 쪽에 텍스트 파일 안내를 단다", async () => {
    const user = userEvent.setup();
    const drawer = await openDrawer(user);
    const uploaded = await within(drawer).findByRole("region", { name: ko.chat.sessionFiles.uploaded });
    await waitFor(() => expect(rowPaths(uploaded)).toEqual(["/uploads/보고서.pdf.txt", "/uploads/화면.png"]));
    // 업로드한 파일은 뿌리를 뗀 이름이다
    expect(within(uploaded).getByText("보고서.pdf.txt")).toBeInTheDocument();

    const agent = within(drawer).getByRole("region", { name: ko.chat.sessionFiles.agent });
    expect(rowPaths(agent)).toEqual([BIG_FILE, "/notes/할 일.txt", "/reports/요약.md"]);
    expect(screen.getByTestId("session-files-agent-notice")).toHaveTextContent(ko.chat.sessionFiles.agentNotice);
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-1/files")).toBeDefined();
  });

  it("미리 보기는 상한 이하 파일에만 있고 .md 는 마크다운, 이미지는 안내로 보인다", async () => {
    const user = userEvent.setup();
    await openDrawer(user);
    await user.click(await screen.findByTestId("session-file-preview-/reports/요약.md"));
    expect(await screen.findByRole("heading", { name: "요약" })).toBeInTheDocument();
    // 미리 보는 행은 기억 서랍처럼 aria-current 로 알린다
    expect(screen.getByTestId("session-file-preview-/reports/요약.md")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("session-file-preview-/notes/할 일.txt")).not.toHaveAttribute("aria-current");
    expect(within(screen.getByTestId("session-file-content")).getByText("7일").tagName).toBe("STRONG");

    await user.click(screen.getByTestId("session-file-preview-/notes/할 일.txt"));
    await waitFor(() => expect(screen.getByTestId("session-file-content")).toHaveTextContent("고객에게 다시 연락한다."));
    await user.click(screen.getByTestId("session-file-preview-/uploads/화면.png"));
    await waitFor(() => expect(screen.getByTestId("session-file-content")).toHaveTextContent(ko.chat.files.notText));

    // 상한을 넘는 파일은 다운로드만 한다
    expect(screen.queryByTestId(`session-file-preview-${BIG_FILE}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`session-file-download-${BIG_FILE}`)).toBeInTheDocument();
  });

  it("다운로드는 경로를 조각마다 인코딩해 부르고 헤더의 이름으로 저장한다", async () => {
    const user = userEvent.setup();
    await openDrawer(user);
    await user.click(await screen.findByTestId("session-file-download-/notes/할 일.txt"));
    await waitFor(() => expect(saved).toEqual(["할 일.txt"]));
    const path = `/api/agents/ag-support/sessions/s-1/files/notes/${encodeURIComponent("할 일.txt")}`;
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-1/files/")?.path).toBe(path);
  });

  it("닫힌 대화는 목록을 부르지 않고 파일이 지워졌다고 보인다", async () => {
    const user = userEvent.setup();
    const drawer = await openDrawer(user, "s-3");
    expect(within(drawer).getByTestId("session-files-closed")).toHaveTextContent(ko.chat.sessionFiles.closed);
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-3/files")).toBeUndefined();
  });

  it("다운로드 거절은 한국어 문장이고, 그사이 대화가 닫혔으면 다시 연 서랍이 닫힌 대화 문구를 보인다", async () => {
    const user = userEvent.setup();
    server.use(http.get("/agents/:id/sessions/:sid/files/*", () => apiError(413, "file_too_large", "file is over 10485760 bytes")));
    await openDrawer(user);
    await user.click(await screen.findByTestId("session-file-download-/reports/요약.md"));
    // 실패한 까닭은 그 행의 다운로드 단추 아래에 선다. 인용 원문과 도구 카드도 같은 모양이다
    expect(await screen.findByTestId("session-file-download-/reports/요약.md-error")).toHaveTextContent(ko.errors.codes.file_too_large);
    expect(saved).toEqual([]);

    server.use(http.get("/agents/:id/sessions/:sid/files", () => apiError(410, "session_closed", "the session is closed")));
    await user.click(screen.getByTestId("session-files-close"));
    await user.click(screen.getByTestId("session-files-open"));
    expect(await screen.findByTestId("session-files-closed")).toHaveTextContent(ko.chat.sessionFiles.closed);
  });

  it("성공한 write_file 카드에만 다운로드가 서고 누르면 그 파일을 저장한다", async () => {
    const user = userEvent.setup();
    useWriteTranscript();
    await openHomeSession(user);
    const button = await screen.findByTestId("chat-message-1-download");
    expect(button).toHaveAccessibleName("요약.md 다운로드");
    expect(screen.queryByTestId("chat-message-2-download")).not.toBeInTheDocument();
    await user.click(button);
    await waitFor(() => expect(saved).toEqual(["요약.md"]));
  });

  it("코드 실행 에이전트의 작업 공간 파일은 따로 묶고 열면 코드가 도는 형식의 거절을 보인다", async () => {
    const user = userEvent.setup();
    agentSessionFileState.files["s-1"] = { ...agentSessionFileState.files["s-1"], "/workspace/chart.png": PNG_BYTES, "/workspace/tool.exe": "MZ" };
    const drawer = await openDrawer(user);
    const workspace = await within(drawer).findByRole("region", { name: ko.chat.files.madeInWorkspace });
    expect(rowPaths(workspace)).toEqual(["/workspace/chart.png", "/workspace/tool.exe"]);
    // 뿌리를 뗀 이름이다
    expect(within(workspace).getByText("chart.png")).toBeInTheDocument();
    expect(rowPaths(within(drawer).getByRole("region", { name: ko.chat.sessionFiles.agent }))).not.toContain("/workspace/chart.png");
    await user.click(screen.getByTestId("session-file-download-/workspace/tool.exe"));
    expect(await screen.findByTestId("session-file-download-/workspace/tool.exe-error")).toHaveTextContent(ko.errors.codes.file_refused_executable);
  });

  it("작업 공간 파일이 없으면 그 묶음이 없다", async () => {
    const user = userEvent.setup();
    const drawer = await openDrawer(user);
    await within(drawer).findByRole("region", { name: ko.chat.sessionFiles.uploaded });
    expect(within(drawer).queryByTestId("session-files-workspace")).not.toBeInTheDocument();
  });

  it("도구 카드의 다운로드가 거절되면 카드 아래에 까닭을 보인다", async () => {
    const user = userEvent.setup();
    useWriteTranscript();
    server.use(http.get("/agents/:id/sessions/:sid/files/*", () => apiError(404, "file_not_found", "file not found")));
    await openHomeSession(user);
    await user.click(await screen.findByTestId("chat-message-1-download"));
    expect(await screen.findByTestId("chat-message-1-download-error")).toHaveTextContent(ko.errors.codes.file_not_found);
  });
});
