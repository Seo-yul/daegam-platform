/**
 * 홈 챗의 장기 기억 서랍 (P36 8절). 도는 리비전이 장기 기억을 쓰고 런타임이 `memory_files` 를 알릴 때만 대화 목록 아래에
 * 단추가 서는지(저장만 한 정의는 보지 않는다), 서랍이 안내와 파일 목록, 마크다운·글자 미리 보기를 보이는지, 하나 삭제가 경로를 조각마다 인코딩해 부르는지,
 * 모두 삭제가 D34 라우트에 내 sub 를 넣는지, 서버 거절을 한국어 문장으로 보이는지 본다. 런타임이 `memory_files` 를 알리지
 * 않은 경우는 resource-states.test 의 표가 본다.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { dictionary } from "@/i18n";
import { RUNTIME_CANCEL, RUNTIME_MEMORY_FILES } from "@/lib/runtimeFeatures";

import { openHomeSessionList } from "./chat";
import { agentMemoryState, agentState, agentWithLongTerm, agentWithRuntimeFeatures, apiError, http, lastRequest, record, server } from "./msw";

const ko = dictionary("ko");

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  await openHomeSessionList(user);
  await user.click(await screen.findByTestId("memory-drawer-open"));
  return screen.findByTestId("memory-drawer");
}

describe("memory drawer", () => {
  beforeEach(() => {
    agentState.items = [agentWithLongTerm(RUNTIME_CANCEL, RUNTIME_MEMORY_FILES)];
  });

  it("장기 기억이 꺼졌거나 런타임이 memory_files 를 알리지 않으면 단추가 없다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_MEMORY_FILES)];
    await openHomeSessionList(user);
    expect(screen.queryByTestId("memory-drawer-open")).not.toBeInTheDocument();
  });

  it("단추는 저장만 한 정의가 아니라 도는 리비전을 따른다", async () => {
    const user = userEvent.setup();
    // 정의에서 장기 기억을 껐지만 아직 재배포하지 않아 도는 리비전은 기억한다
    const deployed = agentWithLongTerm(RUNTIME_CANCEL, RUNTIME_MEMORY_FILES);
    const draftOff = agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_MEMORY_FILES);
    agentState.items = [{ ...deployed, graph: draftOff.graph }];
    await openHomeSessionList(user);
    expect(await screen.findByTestId("memory-drawer-open")).toBeInTheDocument();
  });

  it("서랍은 안내와 보존 기간, 파일 목록을 보이고 .md 는 마크다운으로, 그 밖은 글자로 미리 본다", async () => {
    const user = userEvent.setup();
    const drawer = await openDrawer(user);
    expect(within(drawer).getByText(ko.chat.memory.title)).toBeInTheDocument();
    expect(screen.getByTestId("memory-notice")).toHaveTextContent(ko.chat.memory.notice);
    expect(await screen.findByTestId("memory-retention")).toHaveTextContent("30일");
    expect(screen.getByTestId("memory-file-prefs.md")).toBeInTheDocument();

    await user.click(screen.getByTestId("memory-file-prefs.md"));
    const markdown = await screen.findByRole("heading", { name: "취향" });
    expect(within(screen.getByTestId("memory-preview")).getByText("아메리카노").tagName).toBe("STRONG");
    expect(markdown).toBeInTheDocument();

    await user.click(screen.getByTestId("memory-file-notes/today.txt"));
    await waitFor(() => expect(screen.getByTestId("memory-preview")).toHaveTextContent("오늘 환불 문의를 남겼다."));
    expect(screen.queryByRole("heading", { name: "취향" })).not.toBeInTheDocument();
  });

  it("하나 삭제는 확인 뒤 경로를 조각마다 인코딩해 부르고 목록과 미리 보기에서 사라진다", async () => {
    const user = userEvent.setup();
    await openDrawer(user);
    await user.click(await screen.findByTestId("memory-file-notes/today.txt"));
    await user.click(screen.getByTestId("memory-delete-notes/today.txt"));
    const confirm = await screen.findByRole("dialog", { name: "이 기억을 삭제할까요?" });
    expect(confirm).toHaveTextContent("notes/today.txt 파일이 지워지고 되돌릴 수 없습니다.");
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));

    await waitFor(() => expect(screen.queryByTestId("memory-file-notes/today.txt")).not.toBeInTheDocument());
    expect(lastRequest("DELETE", "/agents/ag-support/memories/me/files/")?.path).toBe("/api/agents/ag-support/memories/me/files/notes/today.txt");
    expect(screen.getByTestId("memory-preview")).toHaveTextContent("목록에서 파일을 선택하면");
    expect(Object.keys(agentMemoryState.files)).toEqual(["/memories/prefs.md"]);
  });

  it("모두 삭제는 확인 뒤 D34 라우트에 내 sub 를 넣어 부르고 빈 목록 안내를 보인다", async () => {
    const user = userEvent.setup();
    await openDrawer(user);
    await user.click(await screen.findByTestId("memory-delete-all"));
    const confirm = await screen.findByRole("dialog", { name: "기억을 모두 삭제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "모두 삭제" }));

    expect(await screen.findByTestId("memory-empty")).toHaveTextContent("아직 기억한 내용이 없습니다.");
    expect(lastRequest("DELETE", "/agents/ag-support/memories/header-admin")).toBeDefined();
    expect(screen.getByTestId("memory-delete-all")).toBeDisabled();
  });

  it("목록을 읽지 못하면 오류만 보이고 빈 목록 안내는 보이지 않는다", async () => {
    const user = userEvent.setup();
    server.use(http.get("/agents/:id/memories/me", () => apiError(409, "runtime_rolling", "a runtime pod without the 'memory_files' capability answered")));
    await openDrawer(user);
    expect(await screen.findByTestId("memory-list-error")).toHaveTextContent(ko.errors.codes.runtime_rolling);
    expect(screen.queryByTestId("memory-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("memory-delete-all")).toBeDisabled();
  });

  it("열 수 없는 파일과 거절된 삭제는 까닭을 한국어 문장으로 보인다", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/agents/:id/memories/me/files/*", async ({ request }) => {
        await record(request);
        return apiError(413, "file_too_large", "memory file exceeds 1048576 bytes");
      }),
      http.delete("/agents/:id/memories/me/files/*", async ({ request }) => {
        await record(request);
        return apiError(404, "memory_not_found", "memory not found");
      }),
    );
    await openDrawer(user);
    await user.click(await screen.findByTestId("memory-file-prefs.md"));
    // 기억 서랍은 다운로드가 없고 기억을 삭제할 수 있어 세션 파일과 다른 문장이다
    expect(await screen.findByTestId("memory-preview-error")).toHaveTextContent(ko.chat.memory.tooLarge);
    expect(ko.chat.memory.tooLarge).not.toBe(ko.errors.codes.file_too_large);

    await user.click(screen.getByTestId("memory-delete-prefs.md"));
    const confirm = await screen.findByRole("dialog", { name: "이 기억을 삭제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));
    expect(await within(confirm).findByText(ko.errors.codes.memory_not_found)).toBeInTheDocument();
  });
});
