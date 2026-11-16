import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { pageWindow } from "@/components/ListControls";
import { phaseVariant } from "@/components/PhaseBadge";
import { dictionary } from "@/i18n";
import { countSummary, stateTone, type StateCounts } from "@/lib/deployment";
import { listModeKey } from "@/lib/storage";
import { openCardMenu } from "./menu";
import { agentDraftFixture, agentFixture, agentState, http, HttpResponse, hubItemFixture, server } from "./msw";

describe("pageWindow", () => {
  it("keeps up to 7 pages flat and collapses the rest around the current page", () => {
    expect(pageWindow(1, 1)).toEqual([1]);
    expect(pageWindow(2, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(pageWindow(1, 9)).toEqual([1, 2, null, 9]);
    expect(pageWindow(5, 9)).toEqual([1, null, 4, 5, 6, null, 9]);
    expect(pageWindow(9, 9)).toEqual([1, null, 8, 9]);
  });
});

describe("list search + pagination (hub list as the representative)", () => {
  beforeEach(() => {
    const items = Array.from({ length: 23 }, (_, i) =>
      hubItemFixture({ id: `hub-${i}`, name: i === 3 ? "special-calc" : `svc-${String(i).padStart(2, "0")}` }),
    );
    server.use(http.get("/hub", () => HttpResponse.json(items)));
  });

  it("paginates 10 rows per page with info text and chips", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await waitFor(() => expect(screen.getByTestId("page-info")).toHaveTextContent("1–10 / 23"));
    expect(screen.getAllByTestId(/^hub-row-/)).toHaveLength(10);
    expect(screen.getByTestId("page-chip-1")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("page-prev")).toBeDisabled();

    await user.click(screen.getByTestId("page-next"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("11–20 / 23");
    await user.click(screen.getByTestId("page-chip-3"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("21–23 / 23");
    expect(screen.getAllByTestId(/^hub-row-/)).toHaveLength(3);
    expect(screen.getByTestId("page-next")).toBeDisabled();
    await user.click(screen.getByTestId("page-prev"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("11–20 / 23");
  });

  it("filters by name and resets to page 1; empty result says no matches", async () => {
    const user = userEvent.setup();
    // 빈 결과가 표 안에 서는지 보는 테스트라 표로 바꿔 놓고 시작한다 (D47. 기본은 카드다)
    window.localStorage.setItem(listModeKey("hub"), "table");
    render(<TestApp initialPath="/hub" />);
    await waitFor(() => expect(screen.getByTestId("page-info")).toHaveTextContent("1–10 / 23"));
    await user.click(screen.getByTestId("page-next"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("11–20 / 23");

    await user.type(screen.getByTestId("list-search"), "SPECIAL");
    expect(screen.getByTestId("page-info")).toHaveTextContent("1–1 / 1");
    expect(screen.getByTestId("hub-row-special-calc")).toBeInTheDocument();
    expect(screen.getAllByTestId(/^hub-row-/)).toHaveLength(1);

    await user.clear(screen.getByTestId("list-search"));
    await user.type(screen.getByTestId("list-search"), "nothing-here");
    expect(screen.getByTestId("page-info")).toHaveTextContent("0–0 / 0");
    const table = screen.getByTestId("hub-table");
    expect(within(table).getByText(/와 맞는 것이 없습니다/)).toBeInTheDocument();
  });

  it("small lists still show the bar without extra chips", async () => {
    server.use(http.get("/hub", () => HttpResponse.json([hubItemFixture()])));
    render(<TestApp initialPath="/hub" />);
    await waitFor(() => expect(screen.getByTestId("page-info")).toHaveTextContent("1–1 / 1"));
    expect(screen.queryByTestId("page-chip-2")).not.toBeInTheDocument();
  });
});

/**
 * 목록의 두 모양 (D47). 기본은 카드이고 고른 것은 목록마다 따로 남는다.
 */
describe("ViewToggle + useListMode", () => {
  it("기본은 카드이고 허브는 두 모양이 같은 testid 로 같은 값을 보인다. 표로 바꾸면 그 목록에만 남고 다시 열어도 표다", async () => {
    const user = userEvent.setup();
    const app = render(<TestApp initialPath="/hub" />);
    const card = await screen.findByTestId("hub-row-calc");
    expect(screen.getByTestId("view-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-card")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("view-toggle-table")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("hub-cards")).toBeInTheDocument();
    expect(screen.queryByTestId("hub-table")).not.toBeInTheDocument();
    expect(screen.getByTestId("hub-cards")).toContainElement(card);
    expect(within(card).getByRole("link", { name: "calc" })).toHaveAttribute("href", "/hub/hub-calc");
    expect(card).toHaveTextContent("internal");
    expect(within(card).getByTestId("hub-status-calc")).toHaveTextContent("Ready");
    expect(within(card).getByTestId("hub-tools-calc")).toHaveTextContent("2");
    expect(within(card).getByTestId("hub-scope-calc")).toHaveTextContent("tenant");
    // 카드에서 동작은 더보기 안이다. 지울 수 없는 항목(calc)에는 더보기 자체가 없다
    expect(within(card).queryByTestId("card-menu-calc")).not.toBeInTheDocument();
    expect(within(await openCardMenu(user, "weather")).getByTestId("row-delete-weather")).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.click(screen.getByTestId("view-toggle-table"));
    expect(screen.getByTestId("hub-table")).toBeInTheDocument();
    expect(screen.queryByTestId("hub-cards")).not.toBeInTheDocument();
    expect(screen.getByTestId("view-toggle-table")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("view-toggle-card")).toHaveAttribute("aria-pressed", "false");
    expect(window.localStorage.getItem(listModeKey("hub"))).toBe("table");
    // 목록마다 따로다. Agents 는 아무것도 고르지 않았으므로 그대로 카드다
    expect(window.localStorage.getItem(listModeKey("agents"))).toBeNull();
    const row = screen.getByTestId("hub-row-calc");
    expect(screen.getByTestId("hub-table")).toContainElement(row);
    expect(within(row).getByRole("link", { name: "calc" })).toHaveAttribute("href", "/hub/hub-calc");
    expect(row).toHaveTextContent("internal");
    expect(within(row).getByTestId("hub-status-calc")).toHaveTextContent("Ready");
    expect(within(row).getByTestId("hub-tools-calc")).toHaveTextContent("2");
    expect(within(row).getByTestId("hub-scope-calc")).toHaveTextContent("tenant");
    // 표에서는 행 오른쪽 끝의 버튼 그대로다
    expect(within(screen.getByTestId("hub-row-weather")).getByTestId("row-delete-weather")).toBeInTheDocument();

    app.unmount();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-row-calc");
    expect(screen.getByTestId("hub-table")).toBeInTheDocument();
    await user.click(screen.getByTestId("view-toggle-card"));
    expect(screen.getByTestId("hub-cards")).toBeInTheDocument();
    expect(window.localStorage.getItem(listModeKey("hub"))).toBe("card");
  });

  it("Agent 는 두 모양이 같은 testid 로 같은 값을 보인다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture({ deployed_revision: 1 }), agentDraftFixture()];
    render(<TestApp initialPath="/agents" />);
    const card = await screen.findByTestId("agent-row-support");
    expect(screen.getByTestId("agent-cards")).toContainElement(card);
    expect(card).toHaveTextContent("상담 에이전트");
    expect(card).toHaveTextContent("openai-main");
    expect(within(card).getByTestId("agent-state-support")).toHaveTextContent("배포");
    expect(within(card).getByTestId("agent-phase-support")).toHaveTextContent("Ready");
    expect(within(card).getByTestId("agent-behind-support")).toHaveTextContent("배포 r1");
    // 카드는 무엇을 하는 Agent 인지(메인 에이전트 설명)와 손볼 일을 보이고, 클러스터는 표에만 둔다
    expect(within(card).getByTestId("agent-description-support")).toHaveTextContent("고객 상담을 맡는 메인 에이전트다.");
    // 무엇이 붙었는지는 캔버스 색의 태그로 보인다. 모델 이름이 비면 provider 다
    expect(within(card).getByTestId("agent-tags-support")).toHaveTextContent("openai-main");
    expect(within(card).getByTestId("agent-tags-support")).toHaveTextContent("Tool 소스 1");
    // 붙은 갈래만 태그가 된다. 이 그래프에는 메모리와 가드레일이 있고 지식은 없다
    expect(within(card).getByTestId("agent-tags-support")).toHaveTextContent(/메모리.*가드레일/);
    expect(within(card).getByTestId("agent-tags-support")).not.toHaveTextContent("지식");
    expect(within(card).getByTestId("agent-pending-support")).toHaveTextContent("승인 대기 세션 1개");
    expect(within(card).queryByTestId("agent-cluster-support")).not.toBeInTheDocument();
    expect(within(await openCardMenu(user, "support")).getByTestId("row-delete-support")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(within(await openCardMenu(user, "draft")).getByTestId("deploy-draft")).toHaveAttribute("href", "/agents/ag-draft?tab=deployment");
    await user.keyboard("{Escape}");

    await user.click(screen.getByTestId("view-toggle-table"));
    const row = screen.getByTestId("agent-row-support");
    expect(screen.getByTestId("agents-table")).toContainElement(row);
    expect(row).toHaveTextContent("상담 에이전트");
    expect(row).toHaveTextContent("openai-main");
    expect(within(row).getByTestId("agent-state-support")).toHaveTextContent("배포");
    expect(within(row).getByTestId("agent-phase-support")).toHaveTextContent("Ready");
    expect(within(row).getByTestId("agent-behind-support")).toHaveTextContent("배포 r1");
    expect(within(row).getByTestId("agent-cluster-support")).toHaveTextContent("home");
    expect(within(row).getByTestId("agent-pending-support")).toHaveTextContent("1");
    expect(within(row).getByTestId("row-delete-support")).toBeInTheDocument();
    expect(within(screen.getByTestId("agent-row-draft")).getByTestId("deploy-draft")).toHaveAttribute("href", "/agents/ag-draft?tab=deployment");
  });
});

/**
 * 카운트 카드 다섯을 대신하는 두 자리 (D44 넷째). 제목 옆 한 줄은 판정이고 점 요약 줄은 갈래별 수다.
 */
describe("countSummary", () => {
  const zero: StateCounts = { total: 0, draft: 0, ready: 0, progressing: 0, failed: 0 };
  const ko = dictionary("ko");

  it("나쁜 쪽을 먼저 말한다", () => {
    expect(countSummary(ko, zero)).toBe("0개");
    expect(countSummary(ko, { ...zero, total: 2, ready: 2 })).toBe("2개 · 모두 Ready");
    expect(countSummary(ko, { ...zero, total: 3, ready: 1, draft: 1, failed: 1 })).toBe("3개 · Failed 1");
    // Failed 가 없을 때만 진행 중을 말한다
    expect(countSummary(ko, { ...zero, total: 3, ready: 1, draft: 1, progressing: 1 })).toBe("3개 · Progressing 1");
    expect(countSummary(ko, { ...zero, total: 2, draft: 2 })).toBe("2개 · 모두 미배포");
    expect(countSummary(ko, { ...zero, total: 3, ready: 2, draft: 1 })).toBe("3개 · Ready 2 · 미배포 1");
  });
});

/** 행의 점·배지와 카드의 상태·요약 줄은 같은 phase 를 같은 갈래로 본다. 따로 적었을 때 Degraded 가 회색 점이었다. */
describe("phase tone", () => {
  it.each([
    ["Ready", "ok"],
    ["Pending", "warn"],
    ["Rolling", "warn"],
    ["Degraded", "warn"],
    ["Terminating", "warn"],
    ["Failed", "error"],
    ["Missing", "error"],
    // 잠든 Agent 는 배포된 것이고 깨우는 중은 뜨는 중이다 (P44 10절)
    ["Sleeping", "ok"],
    ["Waking", "warn"],
  ])("%s 는 점과 상태 갈래가 같은 색이다", (phase, tone) => {
    expect(phaseVariant(phase)).toBe(tone);
    expect(stateTone({ state: "deployed", status: { phase } })).toBe(tone);
  });

  it("모르는 phase 는 점이 회색, 상태 갈래는 진행 중이다", () => {
    expect(phaseVariant("Deleted")).toBe("muted");
    expect(phaseVariant(null)).toBe("muted");
    expect(stateTone({ state: "deployed", status: { phase: "Deleted" } })).toBe("warn");
    expect(stateTone({ state: "deployed", status: null })).toBe("warn");
  });
});
