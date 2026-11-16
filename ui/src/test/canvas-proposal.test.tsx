/**
 * 청지기의 제안을 연 캔버스 (P48 6.5절). 카드의 "캔버스에서 보기" 가 여는 주소(proposal·session)로 제안을 읽어 기준 위의
 * 저장하지 않은 편집으로 올리고, 노드 표시·지우는 노드·바뀌는 글의 앞과 뒤를 보인다. 저장 길 둘("제안대로 저장" 과 사람의
 * 저장 뒤의 거절), 패널에서 다듬은 새 제안, 닫힌 제안과 같은 대상의 나중 제안, 기준이 지난 제안, 볼 수 없는 대상, 세 길의
 * 409 proposal_changed 를 본다. 목은 msw/assistant.ts 의 제안 대본(ag-support r2 위의 바꾸기)이다.
 */
import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter } from "react-router";

import { AppRoutes, TestApp, makeQueryClient } from "@/App";
import { canvasSaveReason, isCanvasUnsaved, setAssistantOpen, setAssistantSessionId } from "@/lib/assistant";

import { transcriptRows } from "./chat";
import {
  HttpResponse,
  PROPOSAL_ADDED_LINE,
  agentFixture,
  PROPOSAL_CALL_ID,
  PROPOSAL_DIGEST,
  agentState,
  apiError,
  assistantSessionFixture,
  assistantState,
  http,
  lastRequest,
  proposalArgs,
  proposalBlockFixture,
  proposedGraphFixture,
  record,
  server,
  tokenUsage,
} from "./msw";

const DEFINITION = "/agents/ag-support?tab=definition";

/** 제안이 기다리는 세션. 패널이 고르는 세션과 같은 테넌트다. */
function seedProposalSession(id = "as-p"): void {
  assistantState.sessions.unshift(
    assistantSessionFixture({
      id,
      title: "상담 에이전트 다듬기",
      state: "pending",
      pending: { kind: "confirm", calls: [{ id: PROPOSAL_CALL_ID, name: "update_agent", args: proposalArgs(), risk: "write" }] },
      transcript: [
        { kind: "user", text: "환불 안내를 더해 줘" },
        { kind: "interrupt", pending: { session_id: id, action_requests: [{ name: "update_agent", args: proposalArgs(), proposal: proposalBlockFixture() }], review_configs: [{ action_name: "update_agent", allowed_decisions: ["approve", "reject"] }] }, resolved: false },
      ],
      turns: 1,
      usage: tokenUsage(100, 20),
      proposals: 1,
    }),
  );
}

/** 캔버스 노드의 자리. ReactFlow 는 노드 바깥 틀의 transform 으로 놓는다. */
function nodePosition(id: string): { x: number; y: number } {
  const el = document.querySelector<HTMLElement>(`.react-flow__node[data-id="${id}"]`);
  const m = el?.style.transform.match(/translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/);
  if (!m) throw new Error(`node ${id} has no position`);
  return { x: Number(m[1]), y: Number(m[2]) };
}

/** 노드를 하나 지워 올린 제안을 고친다. 노드는 끌기를 받는 d3 가 붙어 있어 클릭 이벤트만 보낸다. */
async function deleteNode(user: ReturnType<typeof userEvent.setup>, id: string): Promise<void> {
  fireEvent.click(await screen.findByTestId(`node-${id}`));
  await waitFor(() => expect(screen.getByTestId("inspector")).toHaveAttribute("data-selected-node", id));
  await user.click(screen.getByTestId("inspector-delete"));
  await waitFor(() => expect(screen.queryByTestId(`node-${id}`)).not.toBeInTheDocument());
}

const PROPOSAL_CHANGED = "보고 있던 제안이 그 사이에 바뀌었습니다. 새 제안을 확인한 뒤에 다시 결정해 주세요.";

function decisionsAnswer409(): void {
  server.use(
    http.post("/assistant/sessions/:id/decisions", async ({ request }) => {
      await record(request);
      return apiError(409, "proposal_changed", "the proposal is not the one this decision was made on");
    }),
  );
}

describe("canvas proposal view", () => {
  it("loads the proposal over r2 as unsaved edits with node marks, the removed nodes and the whole text, and keeps the panel on that session", async () => {
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    const bar = await screen.findByTestId("canvas-proposal");
    expect(bar).toHaveAttribute("data-state", "open");
    expect(bar).toHaveTextContent("청지기의 제안을 불러왔습니다. 저장하기 전에는 Agent 가 바뀌지 않습니다.");
    expect(within(bar).getByTestId("canvas-proposal-removed-guardrail-input")).toBeInTheDocument();
    // 기준 위의 저장하지 않은 편집이다
    expect(await screen.findByTestId("canvas-dirty")).toBeInTheDocument();
    expect(screen.getByTestId("node-agent-main")).toHaveAttribute("data-proposal", "changed");
    expect(screen.getByTestId("node-guardrail-output")).toHaveAttribute("data-proposal", "added");
    expect(screen.getByTestId("node-proposal-guardrail-output")).toHaveTextContent("추가");
    expect(screen.queryByTestId("node-guardrail-input")).not.toBeInTheDocument();
    expect(screen.getByTestId("node-memory")).not.toHaveAttribute("data-proposal");
    // 캔버스와 그 대화를 함께 본다. 패널은 접히지 않고 그 세션을 고른다
    const panel = await screen.findByTestId("assistant-panel");
    expect(panel).toHaveAttribute("data-session-id", "as-p");
    expect(await within(panel).findByTestId("approval-proposal-0")).toBeInTheDocument();
    // 바뀌는 글은 인스펙터가 앞과 뒤 전체를 보인다
    // 노드는 끌기를 받는 d3 가 붙어 있어 클릭 이벤트만 보낸다 (agent-canvas.test.tsx 와 같다)
    fireEvent.click(screen.getByTestId("node-agent-main"));
    const texts = await screen.findByTestId("inspector-proposal-texts");
    expect(within(texts).getByTestId("inspector-proposal-before-system_prompt")).not.toHaveTextContent(PROPOSAL_ADDED_LINE);
    expect(within(texts).getByTestId("inspector-proposal-after-system_prompt")).toHaveTextContent(PROPOSAL_ADDED_LINE);
    expect(within(texts).getByTestId("inspector-proposal-before-system_prompt")).toHaveTextContent("너는 고객 상담 에이전트다.");
    // 불러온 것만으로는 고친 것이 아니다. 청지기에게는 그 제안 위에서 고친 것만 저장하지 않은 변경으로 알린다
    expect(isCanvasUnsaved()).toBe(false);
  });

  it("follows a proposal refined in the panel, and the new card's link opens it without asking", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    expect(await screen.findByTestId("canvas-proposal")).toHaveAttribute("data-state", "open");
    // 패널에서 다듬으면 p-1 이 닫히고 같은 Agent 의 p-2 가 기다린다. 캔버스는 그 턴을 듣고 다시 읽는다
    const panel = await screen.findByTestId("assistant-panel");
    await user.type(within(panel).getByTestId("assistant-input"), "입력 가드레일은 남기는 제안을 다시 해 줘");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "closed"));
    expect(screen.getByTestId("canvas-proposal-latest")).toBeInTheDocument();
    // 새 카드의 링크는 같은 캔버스에서 p-2 를 연다. 올린 제안을 고치지 않았으므로 묻지 않는다
    const cards = await within(panel).findAllByTestId("approval-proposal-0-open-canvas");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveAttribute("href", "/agents/ag-support?tab=definition&proposal=p-2&session=as-p");
    await user.click(cards[0]);
    await waitFor(() => expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "open"));
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByTestId("node-guardrail-output")).toHaveAttribute("data-proposal", "added");
    expect(lastRequest("GET", "/assistant/sessions/as-p/proposal")).toBeDefined();
  });

  it("asks before saving as proposed over edits made on the canvas, and then tells Steward about them", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await screen.findByTestId("canvas-proposal");
    await deleteNode(user, "memory");
    expect(isCanvasUnsaved()).toBe(true);
    await user.click(screen.getByTestId("canvas-proposal-approve"));
    expect(confirm).toHaveBeenCalledWith(
      "캔버스에서 제안을 고친 내용이 있습니다. 제안대로 저장하면 고친 내용은 사라집니다. 고친 내용까지 저장하려면 취소한 뒤 저장을 눌러 주세요. 제안대로 저장할까요?",
    );
    expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")).toBeUndefined();
  });

  it("saving as proposed approves with the digest, the panel draws the turn, and the canvas reloads the saved revision", async () => {
    const user = userEvent.setup();
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await user.click(await screen.findByTestId("canvas-proposal-approve"));
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")).toBeDefined());
    expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")?.body).toEqual({ decisions: [{ id: PROPOSAL_CALL_ID, type: "approve", proposal_digest: PROPOSAL_DIGEST }] });
    const panel = screen.getByTestId("assistant-panel");
    await waitFor(() => expect(transcriptRows({ role: "done", container: panel })).toHaveLength(1));
    expect(within(panel).getByTestId("approval-card")).toHaveAttribute("data-resolved", "true");
    // 청지기의 호출이 r3 을 저장했다. 캔버스는 그것을 다시 올리고 제안의 표시를 거둔다
    await waitFor(() => expect(screen.getByTestId("canvas-notice")).toHaveTextContent("리비전 r3을 만들었습니다."));
    expect(screen.queryByTestId("canvas-proposal")).not.toBeInTheDocument();
    expect(screen.getByTestId("node-agent-main")).not.toHaveAttribute("data-proposal");
    expect(screen.queryByTestId("canvas-dirty")).not.toBeInTheDocument();
    expect(agentState.items.find((a) => a.id === "ag-support")?.revision).toBe(3);
    // 청지기의 저장은 배치를 싣지 않는다. 자리가 없는 새 노드는 격자가 아니라 저장된 노드들 아래에 놓인다
    const added = nodePosition("guardrail-output");
    for (const id of ["entry", "agent-main", "model-main", "memory"]) expect(added.y).toBeGreaterThan(nodePosition(id).y);
  });

  it("the person's own save sends the base revision and closes the call as rejected with the fixed reason", async () => {
    const user = userEvent.setup();
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await screen.findByTestId("canvas-proposal");
    const save = screen.getByTestId("canvas-save");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")).toBeDefined());
    expect(lastRequest("PUT", "/agents/ag-support")?.body).toMatchObject({ expect_revision: 2 });
    // 카드의 거절이 아니므로 canvas_saved 를 실어 플랫폼이 제 설명을 붙이고 거절 수에 세지 않게 한다
    expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")?.body).toEqual({
      decisions: [{ id: PROPOSAL_CALL_ID, type: "reject", message: canvasSaveReason({ revision: 3 }), proposal_digest: PROPOSAL_DIGEST, canvas_saved: true }],
    });
    await waitFor(() => expect(screen.queryByTestId("canvas-proposal")).not.toBeInTheDocument());
    expect(screen.getByTestId("node-agent-main")).not.toHaveAttribute("data-proposal");
  });

  it("a revision saved after the proposal says neither save goes through", async () => {
    const user = userEvent.setup();
    seedProposalSession();
    const agent = agentState.items.find((a) => a.id === "ag-support");
    if (!agent) throw new Error("ag-support fixture is missing");
    agent.revision = 3;
    agent.revisions = [1, 2, 3];
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    const bar = await screen.findByTestId("canvas-proposal");
    await waitFor(() => expect(bar).toHaveAttribute("data-state", "stale"));
    expect(within(bar).getByTestId("canvas-proposal-stale")).toHaveTextContent(
      "이 제안을 만든 뒤에 r3이 저장됐습니다. 지금 내용으로는 저장할 수 없습니다. 제안대로 저장하면 청지기가 r3을 기준으로 다시 제안합니다.",
    );
    // 앞의 글은 기준 리비전 r2 를 읽어 보인다
    fireEvent.click(screen.getByTestId("node-agent-main"));
    await waitFor(() => expect(lastRequest("GET", "/agents/ag-support/revisions/2")).toBeDefined());
    expect(await screen.findByTestId("inspector-proposal-before-system_prompt")).toHaveTextContent("너는 고객 상담 에이전트다.");
    // 사람의 저장은 기준 r2 를 실어 409 다. 청지기의 호출은 닫지 않는다
    const save = screen.getByTestId("canvas-save");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    expect(await screen.findByTestId("save-error")).toHaveTextContent("그 사이에 이 항목이 바뀌었습니다.");
    expect(lastRequest("PUT", "/agents/ag-support")?.body).toMatchObject({ expect_revision: 2 });
    expect(lastRequest("POST", "/assistant/sessions/as-p/decisions")).toBeUndefined();
  });

  it("after a failed save as proposed the canvas's own save still carries the proposal's base and overwrites nothing", async () => {
    const user = userEvent.setup();
    seedProposalSession();
    const agent = agentState.items.find((a) => a.id === "ag-support");
    if (!agent) throw new Error("ag-support fixture is missing");
    agent.revision = 3;
    agent.revisions = [1, 2, 3];
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await waitFor(() => expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "stale"));
    // 승인은 기준이 지나 revision_conflict 결과로 아무것도 저장하지 않고, 제안은 닫힌다
    await user.click(screen.getByTestId("canvas-proposal-approve"));
    await waitFor(() => expect(transcriptRows({ role: "done", container: screen.getByTestId("assistant-panel") })).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId("canvas-proposal")).not.toBeInTheDocument());
    // 캔버스에는 제안이 그대로 남아 있다. 그 저장은 제안의 기준 r2 를 실어 r3 을 덮지 않는다
    expect(screen.getByTestId("node-guardrail-output")).toBeInTheDocument();
    const save = screen.getByTestId("canvas-save");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    expect(await screen.findByTestId("save-error")).toHaveTextContent("그 사이에 이 항목이 바뀌었습니다.");
    expect(lastRequest("PUT", "/agents/ag-support")?.body).toMatchObject({ expect_revision: 2 });
    expect(agent.revision).toBe(3);
  });

  it("a 409 proposal_changed keeps the panel card open, shows on the canvas's approve, and loads nothing from the proposal route", async () => {
    const user = userEvent.setup();
    seedProposalSession();
    decisionsAnswer409();
    // 패널 카드의 결정
    setAssistantSessionId("default", "as-p");
    setAssistantOpen(true);
    const first = render(<TestApp initialPath="/hub" />);
    const panel = await screen.findByTestId("assistant-panel");
    const card = await within(panel).findByTestId("approval-card");
    await waitFor(() => expect(within(card).getByTestId("decision-approve")).toBeEnabled());
    await user.click(within(card).getByTestId("decision-approve"));
    expect(await within(panel).findByTestId("assistant-error")).toHaveTextContent(PROPOSAL_CHANGED);
    expect(card).not.toHaveAttribute("data-resolved");
    first.unmount();
    // 캔버스의 "제안대로 저장"
    const second = render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await user.click(await screen.findByTestId("canvas-proposal-approve"));
    expect(await screen.findByTestId("canvas-proposal-approve-error")).toHaveTextContent(PROPOSAL_CHANGED);
    expect(agentState.items.find((a) => a.id === "ag-support")?.revision).toBe(2);
    second.unmount();
    // 제안이 기준 위에 더는 짜 맞춰지지 않는다
    server.use(
      http.get("/assistant/sessions/:id/proposal", async ({ request }) => {
        await record(request);
        return apiError(409, "proposal_changed", "the waiting proposal no longer composes onto its base");
      }),
    );
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    expect(await screen.findByTestId("canvas-proposal-error")).toHaveTextContent(PROPOSAL_CHANGED);
    expect(screen.getByTestId("node-guardrail-input")).toBeInTheDocument();
    expect(screen.queryByTestId("node-guardrail-output")).not.toBeInTheDocument();
    expect(screen.queryByTestId("canvas-dirty")).not.toBeInTheDocument();
  });

  it("another Agent's canvas neither opens the proposal nor inherits its base or its edited state", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    seedProposalSession();
    const base = agentFixture();
    agentState.items.push(
      agentFixture({ id: "ag-other", name: "other", display_name: "다른 에이전트", revision: 5, deployed_revision: 5, revisions: [1, 2, 3, 4, 5], k8s: { name: "agent-other", namespace: base.k8s?.namespace ?? "daegam-default", token_secret: "agent-other-token" } }),
    );
    // 같은 앱 안에서 링크로 Agent 를 옮긴다. 정의 탭의 캔버스가 Agent 마다 새로 서는지 본다
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <MemoryRouter initialEntries={["/agents/ag-other?tab=definition&proposal=p-1&session=as-p"]}>
          <Link to="/agents/ag-support?tab=definition&proposal=p-1&session=as-p" data-testid="go-x">
            x
          </Link>
          <Link to="/agents/ag-other?tab=definition" data-testid="go-y">
            y
          </Link>
          <AppRoutes />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // 다른 Agent 를 고치는 제안은 이 캔버스에서 볼 수 없다
    expect(await screen.findByTestId("canvas-proposal")).toHaveAttribute("data-state", "unavailable");
    expect(screen.queryByTestId("node-guardrail-output")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("go-x"));
    await waitFor(() => expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "open"));
    expect(await screen.findByTestId("node-guardrail-output")).toBeInTheDocument();
    // 읽어 둔 다른 Agent 로 돌아가면 깨끗한 캔버스이고, 그 저장은 앞 제안의 기준을 싣지 않는다
    await user.click(screen.getByTestId("go-y"));
    await waitFor(() => expect(screen.queryByTestId("node-guardrail-output")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByTestId("canvas-dirty")).not.toBeInTheDocument());
    expect(isCanvasUnsaved()).toBe(false);
    await deleteNode(user, "guardrail-input");
    const save = screen.getByTestId("canvas-save");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(lastRequest("PUT", "/agents/ag-other")).toBeDefined());
    expect(lastRequest("PUT", "/agents/ag-other")?.body).not.toHaveProperty("expect_revision");
    await waitFor(() => expect(agentState.items.find((a) => a.id === "ag-other")?.revision).toBe(6));
  });

  it("after a failed save as proposed, a card approval of a later call with the same id does not reload the edited canvas", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    seedProposalSession();
    const agent = agentState.items.find((a) => a.id === "ag-support");
    if (!agent) throw new Error("ag-support fixture is missing");
    agent.revision = 3;
    agent.revisions = [1, 2, 3];
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    await waitFor(() => expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "stale"));
    await user.click(screen.getByTestId("canvas-proposal-approve"));
    const panel = screen.getByTestId("assistant-panel");
    await waitFor(() => expect(transcriptRows({ role: "done", container: panel })).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId("canvas-proposal")).not.toBeInTheDocument());
    // 사람이 캔버스를 고친다
    await deleteNode(user, "guardrail-output");
    // 기준이 다시 최신이 되고 청지기가 같은 호출 id 로 다시 제안한다. 패널 카드에서 승인한다
    agent.revision = 2;
    agent.revisions = [1, 2];
    await user.type(within(panel).getByTestId("assistant-input"), "다시 제안해 줘");
    await user.keyboard("{Enter}");
    const approve = await within(panel).findByTestId("decision-approve");
    await waitFor(() => expect(approve).toBeEnabled());
    confirm.mockClear();
    await user.click(approve);
    await waitFor(() => expect(transcriptRows({ role: "done", container: panel }).length).toBeGreaterThanOrEqual(2));
    expect(agent.revision).toBe(3);
    // 그 승인은 이 캔버스가 보낸 것이 아니다. 고친 캔버스를 묻지 않고 다시 올리지 않는다
    expect(screen.queryByTestId("node-guardrail-output")).not.toBeInTheDocument();
    expect(screen.getByTestId("canvas-dirty")).toBeInTheDocument();
    expect(screen.queryByTestId("canvas-notice")).not.toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("a closed proposal says so, and offers the newest proposal for the same Agent, asking first over unsaved edits", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    seedProposalSession();
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-0&session=as-p`} />);
    const bar = await screen.findByTestId("canvas-proposal");
    expect(bar).toHaveAttribute("data-state", "closed");
    expect(bar).toHaveTextContent("이 제안은 이미 결정됐거나 새 메시지로 닫혔습니다.");
    // 같은 Agent 의 나중 제안이 기다린다. 손으로 고친 것이 있으면 불러오기 전에 묻는다
    await deleteNode(user, "memory");
    expect(await screen.findByTestId("canvas-dirty")).toBeInTheDocument();
    await user.click(within(bar).getByTestId("canvas-proposal-latest"));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith("저장하지 않은 변경이 있습니다. 제안을 불러오면 지금 내용은 사라지고 되돌릴 수 없습니다. 계속할까요?"));
    expect(await screen.findByTestId("canvas-proposal-load")).toHaveTextContent("최신 제안 불러오기");
    expect(screen.queryByTestId("node-guardrail-output")).not.toBeInTheDocument();
    confirm.mockReturnValue(true);
    await user.click(screen.getByTestId("canvas-proposal-load"));
    expect(await screen.findByTestId("node-guardrail-output")).toHaveAttribute("data-proposal", "added");
    expect(screen.getByTestId("canvas-proposal")).toHaveAttribute("data-state", "open");
  });

  it("no waiting proposal is closed without an offer, and an Agent the person cannot view loads nothing", async () => {
    // 세션에 기다리는 제안이 없다
    const { unmount } = render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-1`} />);
    const bar = await screen.findByTestId("canvas-proposal");
    expect(bar).toHaveAttribute("data-state", "closed");
    expect(within(bar).queryByTestId("canvas-proposal-latest")).not.toBeInTheDocument();
    unmount();
    server.use(
      http.get("/assistant/sessions/:id/proposal", async ({ request }) => {
        await record(request);
        return apiError(403, "forbidden", "missing scope agent:read");
      }),
    );
    render(<TestApp initialPath={`${DEFINITION}&proposal=p-1&session=as-p`} />);
    const denied = await screen.findByTestId("canvas-proposal");
    expect(denied).toHaveAttribute("data-state", "unavailable");
    expect(denied).toHaveTextContent("이 Agent 를 찾을 수 없거나 볼 권한이 없어 제안을 불러올 수 없습니다.");
    expect(screen.getByTestId("node-guardrail-input")).toBeInTheDocument();
    expect(screen.queryByTestId("canvas-dirty")).not.toBeInTheDocument();
  });

  it("a creation lays out every node on the new-Agent canvas, and the person's own save names the new Agent when it closes the call", async () => {
    const user = userEvent.setup();
    const graph = proposedGraphFixture();
    const block = proposalBlockFixture({ kind: "create", agent: { id: null, name: "refunds", display_name: "환불 에이전트" }, base_revision: null, deployed_revision: null });
    const args = { ...proposalArgs(), _proposal: { id: "p-1", digest: PROPOSAL_DIGEST, base_revision: null, attention: [] } };
    assistantState.sessions.unshift(assistantSessionFixture({ id: "as-c", state: "pending", pending: { kind: "confirm", calls: [{ id: PROPOSAL_CALL_ID, name: "create_agent", args, risk: "write" }] }, transcript: [] }));
    server.use(
      http.get("/assistant/sessions/as-c/proposal", async ({ request }) => {
        await record(request);
        return HttpResponse.json({ proposal_id: "p-1", kind: "create", agent_id: null, name: "refunds", display_name: "환불 에이전트", base_revision: null, latest_revision: null, graph, service: {}, proposal: block });
      }),
    );
    render(<TestApp initialPath="/agents/new?proposal=p-1&session=as-c" />);
    expect(await screen.findByTestId("node-agent-main")).toBeInTheDocument();
    expect(screen.getByTestId("ap-name")).toHaveValue("refunds");
    // 좌표가 없는 제안이라 자동 배치로 놓는다. 격자라면 모델과 메모리가 에이전트와 같은 줄에 서지만, 자동 배치는 둘을 에이전트
    // 위에, 가드레일을 아래에, Tool 을 오른쪽에 둔다
    const agentAt = nodePosition("agent-main");
    expect(nodePosition("model-main").y).toBeLessThan(agentAt.y);
    expect(nodePosition("memory").y).toBeLessThan(agentAt.y);
    expect(nodePosition("guardrail-output").y).toBeGreaterThan(agentAt.y);
    expect(nodePosition("tools-calculator").x).toBeGreaterThan(agentAt.x);
    const save = screen.getByTestId("canvas-save");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(lastRequest("POST", "/assistant/sessions/as-c/decisions")).toBeDefined());
    const created = lastRequest("POST", "/agents");
    expect(created?.body).not.toHaveProperty("expect_revision");
    const id = agentState.items.find((a) => a.name === "refunds")?.id;
    expect(lastRequest("POST", "/assistant/sessions/as-c/decisions")?.body).toEqual({
      decisions: [{ id: PROPOSAL_CALL_ID, type: "reject", message: canvasSaveReason({ agentId: id ?? "", revision: 1 }), proposal_digest: PROPOSAL_DIGEST, canvas_saved: true }],
    });
  });
});
