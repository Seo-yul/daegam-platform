/**
 * 테스트 채팅 서랍의 미리 보기 (P44b 11절). agent:deploy 가 있는 사람만 미리 보기를 말하고 띄우며, 저장한 리비전이 배포된 것이면
 * 서랍은 배포된 Agent 와 대화한다. 미리 보기와의 대화는 미리 보기 아래 라우트를 부르고, 옮기기 전에는 대화가 사라진다고 묻고,
 * 끝난 까닭을 말한다.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Agent, PreviewEndReason } from "@/api/types";
import { PREVIEW_OPEN_POLL_MS, PREVIEW_PENDING_POLL_MS, previewable, previewPollMs } from "@/lib/agentPreview";
import { initAuth, resetAuthState } from "@/lib/auth";
import { TestApp } from "@/App";
import { formatClock } from "@/lib/utils";
import { TestChatDrawer } from "@/pages/agents/TestChatDrawer";

import { agentFixture, agentPreviewState, agentState, apiError, countExact, holdRequest, http, HttpResponse, lastExact, meDeveloper, previewFixture, received, record, server } from "./msw";
import { renderWithProviders } from "./render";

const ID = "ag-support";
const PREVIEW = `/agents/${ID}/preview`;

/** 저장만 하고 배포한 적 없는 Agent. */
const draft = (): Agent => agentFixture({ state: "draft", revision: 1, deployed_revision: null, status: null });
/** 배포된 r2 가 Ready 이고 저장한 리비전은 그보다 앞선 Agent. */
const ahead = (revision = 3): Agent => agentFixture({ revision, revisions: Array.from({ length: revision }, (_, i) => i + 1) });

function drawer(agent: Agent | undefined, { isNew = false, dirty = false } = {}) {
  return renderWithProviders(<TestChatDrawer isNew={isNew} agent={agent} dirty={dirty} />);
}

beforeEach(async () => {
  await initAuth();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("테스트 채팅 서랍의 미리 보기", () => {
  it("agent:deploy 가 없으면 지금의 말 그대로이고 미리 보기를 읽지 않는다", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    resetAuthState();
    await initAuth();
    const { unmount } = drawer(draft());
    expect(await screen.findByTestId("test-chat-empty")).toHaveTextContent("배포 탭에서 배포하면 여기서 바로 대화할 수 있습니다.");
    unmount();
    drawer(undefined, { isNew: true });
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("저장하고 배포한 뒤에 쓸 수 있습니다.");
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();
    expect(countExact("GET", PREVIEW)).toBe(0);
  });

  it("agent:deploy 가 있으면 새 캔버스, 저장하지 않은 변경, draft 가 미리 보기를 말한다", async () => {
    const first = drawer(undefined, { isNew: true });
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("저장하면 미리 보기로 대화할 수 있습니다.");
    first.unmount();
    const dirty = drawer(draft(), { dirty: true });
    expect(screen.getByTestId("test-chat-empty")).toHaveTextContent("저장하면 여기서 미리 보기로 대화할 수 있습니다.");
    // 저장해야 띄울 수 있다
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();
    dirty.unmount();
    drawer(draft());
    expect(screen.getByTestId("test-chat-empty")).toHaveTextContent("미리 보기를 시작하거나 배포 탭에서 배포하면 여기서 대화할 수 있습니다.");
    expect(await screen.findByTestId("preview-start")).toHaveTextContent("r1 미리 보기");
  });

  it("배포된 리비전이 저장한 것이면 배포된 Agent 와 대화하고 미리 보기를 읽지 않는다", async () => {
    const user = userEvent.setup();
    drawer(agentFixture());
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("배포된 r2와 대화합니다");
    await user.click(screen.getByTestId("test-chat-toggle"));
    expect(screen.getByTestId("chat-input")).toBeEnabled();
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();
    expect(countExact("GET", PREVIEW)).toBe(0);
  });

  it("draft 를 미리 보기로 띄우는 동안 진행을 말하고, 뜬 뒤 미리 보기 아래 라우트로 대화하고, 끝낸다", async () => {
    const user = userEvent.setup();
    agentState.items = [draft()];
    agentPreviewState.readyAfterMs = 60_000;
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    expect(lastExact("POST", PREVIEW)).toBeDefined();
    expect(await screen.findByTestId("preview-status")).toHaveTextContent("미리 보기를 준비하는 중입니다. 파드가 뜨면 대화할 수 있습니다.");
    expect(screen.getByTestId("preview-tools")).toHaveTextContent("미리 보기도 배포된 Agent 처럼 실제 Tool 을 부르므로 쓰기 Tool 은 실제 시스템을 바꿉니다.");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();

    // 파드가 뜨면 다음 다시 읽기가 대화를 연다
    for (const id of Object.keys(agentPreviewState.readyAt)) agentPreviewState.readyAt[id] = 0;
    await waitFor(() => expect(screen.getByTestId("preview-status")).toHaveTextContent("미리 보기 r1과 대화합니다. 장기 기억을 사용하지 않고, 미리 보기를 끝내면 이 대화도 사라집니다."), { timeout: 6000 });
    await user.type(screen.getByTestId("chat-input"), "3 더하기 4");
    await user.click(screen.getByTestId("chat-send"));
    expect(await screen.findByText("결과는 7 입니다.", { exact: false })).toBeInTheDocument();
    expect(lastExact("POST", `${PREVIEW}/chat`)?.body).toMatchObject({ message: "3 더하기 4", ephemeral: true });
    expect(countExact("POST", `/agents/${ID}/chat`)).toBe(0);
    // 미리 보기에는 답변 피드백이 없다
    expect(screen.queryByTestId("feedback-up")).not.toBeInTheDocument();
    expect(received.filter((r) => r.path.includes("/feedback"))).toEqual([]);

    await user.click(screen.getByTestId("preview-end"));
    await waitFor(() => expect(lastExact("DELETE", PREVIEW)).toBeDefined());
    expect(await screen.findByTestId("test-chat-empty")).toHaveTextContent("미리 보기를 시작하거나 배포 탭에서 배포하면 여기서 대화할 수 있습니다.");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
    // 내가 끝낸 것은 까닭을 말하지 않는다
    expect(screen.queryByTestId("preview-ended")).not.toBeInTheDocument();
  });

  it("배포된 것과 대화하다 미리 보기로 바꾸고, 저장한 최신 리비전으로 옮기기 전에 대화가 사라진다고 묻는다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead(3)];
    const view = drawer(ahead(3));
    expect(screen.getByTestId("test-chat-drawer")).toHaveTextContent("배포된 r2와 대화합니다");
    await user.click(await screen.findByTestId("preview-start"));
    expect(await screen.findByTestId("preview-status")).toHaveTextContent("미리 보기 r3과 대화합니다.");
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();

    // r4 를 저장했다
    agentState.items = [ahead(4)];
    view.rerender(<TestChatDrawer isNew={false} agent={ahead(4)} dirty={false} />);
    await user.click(screen.getByTestId("preview-start"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("저장한 최신 리비전으로 미리 보기를 바꾸면 파드를 다시 띄우므로 지금 대화는 사라집니다.");
    await user.click(within(dialog).getByTestId("confirm-cancel"));
    expect(countExact("POST", PREVIEW)).toBe(1);

    await user.click(screen.getByTestId("preview-start"));
    await user.click(within(await screen.findByRole("dialog")).getByTestId("confirm-submit"));
    await waitFor(() => expect(screen.getByTestId("preview-status")).toHaveTextContent("미리 보기 r4와 대화합니다."));
    expect(countExact("POST", PREVIEW)).toBe(2);
  });

  it.each<[PreviewEndReason, string | null]>([
    ["idle", "미리 보기를 30분 동안 사용하지 않아 끝냈습니다. 다시 시작해 주세요."],
    ["max_age", "미리 보기는 처음 시작하고 2시간이 지나면 끝납니다. 다시 시작해 주세요."],
    ["ended_by_other", "다른 사람이 이 미리 보기를 끝냈습니다. 다시 시작해 주세요."],
    ["access_lost", "이 테넌트에서 Agent 를 배포할 권한이 없어져 미리 보기를 끝냈습니다."],
    ["disabled", "플랫폼에서 미리 보기를 꺼서 이 미리 보기를 끝냈습니다."],
    ["ended", null],
    ["source_deleted", null],
  ])("끝난 까닭 %s 를 말한다", async (reason, said) => {
    const user = userEvent.setup();
    agentPreviewState.ended[`${ID}:${agentPreviewState.me}`] = { reason, at: "2026-11-12T01:00:00Z", by: "dev@example.com" };
    drawer(draft());
    await user.click(screen.getByTestId("test-chat-toggle"));
    await screen.findByTestId("preview-start");
    await waitFor(() => expect(countExact("GET", PREVIEW)).toBeGreaterThan(0));
    if (said) expect(await screen.findByTestId("preview-ended")).toHaveTextContent(said);
    else expect(screen.queryByTestId("preview-ended")).not.toBeInTheDocument();
  });

  it("다른 사람이 끝내면 다음 다시 읽기에서 대화를 닫고 그 까닭을 말한다", async () => {
    // 다시 읽기의 간격을 앞당겨 보도록 서랍이 그리기 전에 시계를 바꾼다
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    agentState.items = [draft()];
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    expect(await screen.findByTestId("chat-input")).toBeInTheDocument();
    const p = agentPreviewState.items[0]!;
    agentPreviewState.items = [];
    agentPreviewState.ended[`${ID}:${p.owner}`] = { reason: "ended_by_other", at: "2026-11-12T01:00:00Z", by: "dev@example.com" };
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_OPEN_POLL_MS + 1_000));
    expect(await screen.findByTestId("preview-ended")).toHaveTextContent("다른 사람이 이 미리 보기를 끝냈습니다.");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
  });

  it("내 미리 보기를 읽기 전에는 띄우는 단추가 없다", async () => {
    const reading = holdRequest("get", PREVIEW);
    drawer(draft());
    await waitFor(() => expect(reading.started()).toBe(1));
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();
    reading.release();
    expect(await screen.findByTestId("preview-start")).toBeInTheDocument();
  });

  it("범위를 잃어 읽기가 거절되면 쥐고 있던 미리 보기를 놓고 그 까닭을 보인다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    agentState.items = [draft()];
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    expect(await screen.findByTestId("chat-input")).toBeInTheDocument();
    server.use(http.get(PREVIEW, () => apiError(403, "forbidden", "missing scope agent:deploy")));
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_OPEN_POLL_MS + 1_000));
    expect(await screen.findByTestId("preview-error")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
    expect(screen.queryByTestId("preview-status")).not.toBeInTheDocument();
    // 읽기를 마치지 못했으니 띄우는 단추도 없다
    expect(screen.queryByTestId("preview-start")).not.toBeInTheDocument();
  });

  it("상태 읽기가 phase 를 한 번 놓쳐도 보이던 대화를 닫지 않는다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    agentState.items = [draft()];
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    expect(await screen.findByTestId("chat-input")).toBeInTheDocument();
    const p = agentPreviewState.items[0]!;
    server.use(
      http.get(PREVIEW, async ({ request }) => {
        await record(request);
        return HttpResponse.json({ ...p, state: "deployed", phase: null });
      }),
    );
    await act(() => vi.advanceTimersByTimeAsync(PREVIEW_OPEN_POLL_MS + 1_000));
    await waitFor(() => expect(countExact("GET", PREVIEW)).toBeGreaterThan(1));
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    expect(screen.getByTestId("preview-status")).toHaveTextContent("미리 보기 r1과 대화합니다.");
  });

  it("뜨지 못한 미리 보기는 준비하는 중이 아니라 시작하지 못했다고 말한다", async () => {
    const user = userEvent.setup();
    agentPreviewState.items = [previewFixture({ id: "pv-mine", owner: agentPreviewState.me, source_id: ID, revision: 1, state: "deployed", phase: "Failed" })];
    drawer(draft());
    await user.click(screen.getByTestId("test-chat-toggle"));
    expect(await screen.findByTestId("preview-status")).toHaveTextContent("미리 보기를 시작하지 못했습니다. 미리 보기를 끝낸 뒤 다시 시작해 주세요.");
    expect(screen.queryByTestId("chat-input")).not.toBeInTheDocument();
    expect(screen.getByTestId("preview-end")).toBeInTheDocument();
  });

  it("끝난 미리 보기에 보낸 채팅이 404 면 다음 다시 읽기를 기다리지 않고 끝난 까닭을 말한다", async () => {
    const user = userEvent.setup();
    agentState.items = [draft()];
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    const input = await screen.findByTestId("chat-input");
    const p = agentPreviewState.items[0]!;
    agentPreviewState.items = [];
    agentPreviewState.ended[`${ID}:${p.owner}`] = { reason: "idle", at: "2026-11-12T01:00:00Z", by: null };
    const reads = countExact("GET", PREVIEW);
    await user.type(input, "3 더하기 4");
    await user.click(screen.getByTestId("chat-send"));
    expect(await screen.findByTestId("preview-ended")).toHaveTextContent("미리 보기를 30분 동안 사용하지 않아 끝냈습니다. 다시 시작해 주세요.");
    expect(countExact("GET", PREVIEW)).toBe(reads + 1);
  });

  it("시작이 거절되면 그 까닭을 보인다", async () => {
    const user = userEvent.setup();
    server.use(http.post(PREVIEW, () => apiError(409, "preview_limit", "previews are limited to 3 per tenant and none is free", { limit: 3 })));
    drawer(draft());
    await user.click(await screen.findByTestId("preview-start"));
    expect(await screen.findByTestId("preview-error")).toHaveTextContent("이 테넌트에서 동시에 열 수 있는 미리 보기 수를 넘었습니다. 다른 미리 보기를 끝낸 뒤 다시 시도해 주세요.");
  });
});

describe("미리 보기의 화면 규칙", () => {
  it("저장한 리비전이 배포된 것이 아니거나 draft 일 때만 띄울 수 있다", () => {
    expect(previewable(draft())).toBe(true);
    expect(previewable(ahead(3))).toBe(true);
    expect(previewable(agentFixture())).toBe(false);
  });

  it("파드가 뜨거나 끝나는 동안은 짧게, 뜬 뒤와 실패한 뒤에는 길게 다시 읽는다", () => {
    expect(previewPollMs(null)).toBe(false);
    expect(previewPollMs(previewFixture({ state: "deploying", phase: null }))).toBe(PREVIEW_PENDING_POLL_MS);
    expect(previewPollMs(previewFixture({ state: "deleting" }))).toBe(PREVIEW_PENDING_POLL_MS);
    expect(previewPollMs(previewFixture())).toBe(PREVIEW_OPEN_POLL_MS);
    expect(previewPollMs(previewFixture({ state: "failed", phase: null }))).toBe(PREVIEW_OPEN_POLL_MS);
    expect(previewPollMs(previewFixture({ state: "deployed", phase: "Failed" }))).toBe(PREVIEW_OPEN_POLL_MS);
  });
});

describe("개요의 열린 미리 보기", () => {
  it("agent:deploy 가 있는 사람에게 열린 미리 보기마다 시작한 사람, 리비전, 끝나는 시각을 보이고 누구의 것이든 끝낸다", async () => {
    const user = userEvent.setup();
    const other = previewFixture();
    const own = previewFixture({ id: "pv-own", owner: agentPreviewState.me, revision: 2 });
    agentPreviewState.items = [other, own];
    render(<TestApp initialPath={`/agents/${ID}`} />);
    const card = await screen.findByTestId("open-previews");
    expect(card).toHaveTextContent("열린 미리 보기");
    expect(within(card).getByTestId("open-preview-pv-other")).toHaveTextContent(`dev@example.com 님의 r3 미리 보기, ${formatClock(other.idle_ends_at)}에 끝납니다`);
    expect(within(card).getByTestId("open-preview-pv-own")).toHaveTextContent(`header-admin 님의 r2 미리 보기, ${formatClock(own.idle_ends_at)}에 끝납니다`);

    // 그 사람의 대화가 사라지므로 그 줄을 보이며 먼저 묻는다
    await user.click(within(card).getByTestId("open-preview-end-pv-other"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("미리 보기 끝내기");
    expect(dialog).toHaveTextContent(`dev@example.com 님의 r3 미리 보기, ${formatClock(other.idle_ends_at)}에 끝납니다`);
    await user.click(within(dialog).getByTestId("confirm-cancel"));
    expect(countExact("DELETE", `/agents/${ID}/previews/pv-other`)).toBe(0);
    await user.click(within(card).getByTestId("open-preview-end-pv-other"));
    await user.click(within(await screen.findByRole("dialog")).getByTestId("confirm-submit"));
    await waitFor(() => expect(within(card).queryByTestId("open-preview-pv-other")).not.toBeInTheDocument());
    expect(lastExact("DELETE", `/agents/${ID}/previews/pv-other`)).toBeDefined();
    expect(within(card).getByTestId("open-preview-pv-own")).toBeInTheDocument();
  });

  it("열린 미리 보기가 없으면 카드가 없고, agent:deploy 가 없으면 목록을 읽지 않는다", async () => {
    const first = render(<TestApp initialPath={`/agents/${ID}`} />);
    await screen.findByTestId("status-revision-tile");
    await waitFor(() => expect(countExact("GET", `/agents/${ID}/previews`)).toBe(1));
    expect(screen.queryByTestId("open-previews")).not.toBeInTheDocument();
    first.unmount();

    agentPreviewState.items = [previewFixture()];
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:chat"] } })));
    resetAuthState();
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await screen.findByTestId("status-revision-tile");
    // 같은 개요가 그리는 참조 카드가 읽은 뒤에 본다. 카드가 섰다면 그 목록 읽기도 그때 나갔다
    await waitFor(() => expect(countExact("GET", `/agents/${ID}/references`)).toBe(2));
    expect(screen.queryByTestId("open-previews")).not.toBeInTheDocument();
    expect(countExact("GET", `/agents/${ID}/previews`)).toBe(1);
  });
});
