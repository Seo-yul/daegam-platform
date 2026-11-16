import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentPending, AgentProposal } from "@/api/types";
import { ApprovalCard } from "@/components/ApprovalCard";

import { PROPOSAL_ADDED_LINE, PROPOSAL_DIGEST, proposalBlockFixture } from "./msw";
import { renderWithProviders } from "./render";

/** 승인 카드의 머리와 설명. 결정한 카드는 기다린다고 말하지 않고, LangChain 의 영어 기본 설명은 감춘다. */

function pending(description: string): AgentPending {
  return {
    session_id: "s-1",
    kind: "confirm",
    action_requests: [{ name: "ask_wiki_question", args: { repoName: "stacklok/toolhive" }, description }],
    review_configs: [{ action_name: "ask_wiki_question", allowed_decisions: ["approve", "reject"] }],
  } as AgentPending;
}

test("open card asks for a decision and shows the tool description", () => {
  render(<ApprovalCard pending={pending("저장소 위키에 묻는다.")} onSubmit={() => {}} />);
  expect(screen.getByText("승인 필요")).toBeInTheDocument();
  expect(screen.getByText("1개 Tool 호출이 결정을 기다립니다.")).toBeInTheDocument();
  expect(screen.getByText("저장소 위키에 묻는다.")).toBeInTheDocument();
});

test("resolved card says the decision was made", () => {
  render(<ApprovalCard pending={pending("저장소 위키에 묻는다.")} onSubmit={() => {}} resolved />);
  expect(screen.getByText("결정함")).toBeInTheDocument();
  expect(screen.getByText("1개 Tool 호출을 결정했습니다.")).toBeInTheDocument();
  expect(screen.queryByText("승인 필요")).not.toBeInTheDocument();
  expect(screen.queryByTestId("decision-approve")).not.toBeInTheDocument();
});

test("LangChain default English description is hidden", () => {
  const text = "Tool execution requires approval\n\nTool: ask_wiki_question\nArgs: {'repoName': 'stacklok/toolhive'}";
  render(<ApprovalCard pending={pending(text)} onSubmit={() => {}} />);
  expect(screen.queryByText(/Tool execution requires approval/)).not.toBeInTheDocument();
  expect(screen.getByTestId("approval-args-0")).toHaveTextContent("stacklok/toolhive");
});

test("the assistant's reserved proposal key is not shown among the arguments", () => {
  // 비서가 Agent 정의 제안의 인자에 넣는 칸이다(P48 6.1절). 결정의 지문으로만 쓰인다
  const p = pending("새 Agent 정의를 제안한다.");
  p.action_requests[0].args = { name: "support", _proposal: { id: "p-1", digest: "p48.1:abc" } };
  render(<ApprovalCard pending={p} onSubmit={() => {}} />);
  expect(screen.getByTestId("approval-args-0")).toHaveTextContent("support");
  expect(screen.getByTestId("approval-args-0")).not.toHaveTextContent("_proposal");
  expect(screen.getByTestId("approval-args-0")).not.toHaveTextContent("p48.1:abc");
});

test("an edit decision keeps the request's tool name and sends only the edited args", async () => {
  // 런타임은 이름이 다른 edit 을 422 edit_name_mismatch 로 거절한다(runtime/hitl.py). 카드는 이름을 바꿀 자리를 주지 않고
  // 요청의 이름을 그대로 싣는다
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  const p = pending("저장소 위키에 묻는다.");
  p.review_configs = [{ action_name: "ask_wiki_question", allowed_decisions: ["approve", "edit"] }];
  render(<ApprovalCard pending={p} onSubmit={onSubmit} />);
  await user.click(screen.getByTestId("decision-edit"));
  const args = screen.getByTestId("decision-edit-args");
  await user.clear(args);
  await user.type(args, '{{"repoName": "stacklok/daegam"}');
  await user.click(screen.getByTestId("decision-submit"));
  expect(onSubmit).toHaveBeenCalledWith([{ type: "edit", edited_action: { name: "ask_wiki_question", args: { repoName: "stacklok/daegam" } } }]);
});

/** 외부 내용 뒤의 이유와 기억할 수 있는 선택 (P43 4·5절). */
function divide(over: Partial<AgentPending> = {}, reason: "rule" | "outside" = "rule"): AgentPending {
  return {
    session_id: "s-1",
    action_requests: [{ name: "Divide", args: { a: 1, b: 0 }, reason }],
    review_configs: [{ action_name: "Divide", allowed_decisions: ["approve", "reject"] }],
    ...over,
  };
}

test("a call asked because of outside content says so and that a new chat starts clean", () => {
  render(<ApprovalCard pending={divide({}, "outside")} onSubmit={() => {}} />);
  const reason = screen.getByTestId("approval-reason-0");
  expect(reason).toHaveTextContent("이 대화에 외부 내용이 들어와 승인이 필요합니다.");
  expect(reason).toHaveTextContent("새 대화는 외부 내용 없이 시작합니다.");
  render(<ApprovalCard pending={divide()} onSubmit={() => {}} testId="rule-card" />);
  expect(within(screen.getByTestId("rule-card")).queryByTestId("approval-reason-0")).not.toBeInTheDocument();
});

test("remember buttons appear only for the choices the platform offers and send remember", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  render(<ApprovalCard pending={divide({ remember_options: [["approve", "reject"]], remember_days: 30 })} onSubmit={onSubmit} />);
  expect(screen.getByTestId("approval-remember-hint")).toHaveTextContent("기억한 선택은 30일 동안");
  await user.click(screen.getByTestId("decision-remember-approve"));
  expect(onSubmit).toHaveBeenCalledWith([{ type: "approve", remember: "approve" }]);
});

test("a remembered reject and a plain approve keep their own shapes", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  const { unmount } = render(<ApprovalCard pending={divide({ remember_options: [["reject"]], remember_days: 7 })} onSubmit={onSubmit} />);
  expect(screen.queryByTestId("decision-remember-approve")).not.toBeInTheDocument();
  await user.click(screen.getByTestId("decision-remember-reject"));
  expect(onSubmit).toHaveBeenLastCalledWith([{ type: "reject", remember: "reject" }]);
  unmount();
  render(<ApprovalCard pending={divide({ remember_options: [["reject"]], remember_days: 7 })} onSubmit={onSubmit} />);
  await user.click(screen.getByTestId("decision-approve"));
  expect(onSubmit).toHaveBeenLastCalledWith([{ type: "approve" }]);
});

test("without remember options nothing is offered", () => {
  render(<ApprovalCard pending={divide()} onSubmit={() => {}} />);
  expect(screen.queryByTestId("decision-remember-approve")).not.toBeInTheDocument();
  expect(screen.queryByTestId("approval-remember-hint")).not.toBeInTheDocument();
});

test("with several requests each row remembers on its own and the same tool cannot be remembered both ways", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  const two = divide({
    action_requests: [
      { name: "Divide", args: { a: 1, b: 0 }, reason: "rule" },
      { name: "Divide", args: { a: 2, b: 0 }, reason: "rule" },
    ],
    remember_options: [
      ["approve", "reject"],
      ["approve", "reject"],
    ],
    remember_days: 30,
  });
  render(<ApprovalCard pending={two} onSubmit={onSubmit} />);
  await user.click(screen.getByTestId("decision-remember-approve-0"));
  expect(screen.getByTestId("decision-remember-approve-0")).toHaveAttribute("aria-pressed", "true");
  // 같은 Tool 을 반대로 기억하면 플랫폼이 거절하므로 둘째 줄의 기억한 거부를 막는다
  expect(screen.getByTestId("decision-remember-reject-1")).toBeDisabled();
  await user.click(screen.getByTestId("decision-reject-1"));
  await user.click(screen.getByTestId("decision-submit"));
  expect(onSubmit).toHaveBeenCalledWith([{ type: "approve", remember: "approve" }, { type: "reject" }]);
});

/** 청지기의 Agent 정의 제안 (P48 6.4절). 카드는 플랫폼이 비교한 덩어리를 그리고 인자 JSON 은 보이지 않는다. */
function proposalPending(proposal: AgentProposal): AgentPending {
  return {
    session_id: "as-9",
    action_requests: [{ name: "update_agent", args: { agent_id: "ag-support", _proposal: { id: proposal.proposal_id, digest: PROPOSAL_DIGEST } }, proposal }],
    review_configs: [{ action_name: "update_agent", allowed_decisions: ["approve", "reject"] }],
  };
}

test("a proposal card puts the changes to check first, then the change list and the text before and after, and links to the canvas", () => {
  renderWithProviders(<ApprovalCard pending={proposalPending(proposalBlockFixture())} onSubmit={() => {}} />);
  const block = screen.getByTestId("approval-proposal-0");
  expect(screen.queryByTestId("approval-args-0")).not.toBeInTheDocument();
  expect(within(block).getByTestId("approval-proposal-0-title")).toHaveTextContent("상담 에이전트 변경 제안 (r2 기준)");
  expect(within(block).getByTestId("approval-proposal-0-deployed")).toHaveTextContent("지금 배포된 리비전은 r2입니다.");
  // 먼저 볼 것이 바뀌는 것 목록보다 앞이다. 가드레일은 캔버스의 위치 낱말로 가리킨다
  const attention = within(block).getByTestId("approval-proposal-0-attention");
  expect(attention).toHaveTextContent("저장하기 전에 확인할 점");
  expect(attention).toHaveTextContent("Agent의 시스템 프롬프트나 설명을 새로 쓰거나 바꿉니다");
  expect(attention).toHaveTextContent("입력 가드레일을 없애거나 완화합니다");
  expect(attention.compareDocumentPosition(within(block).getByTestId("approval-proposal-0-changes")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(block).getByTestId("approval-proposal-0-summary")).toHaveTextContent("노드 1개 추가, 1개 변경, 1개 삭제");
  expect(within(block).getByTestId("approval-proposal-0-changed-agent-main")).toHaveTextContent("변경에이전트agent-mainAgentsystem_prompt");
  expect(within(block).getByTestId("approval-proposal-0-added-guardrail-output")).toHaveTextContent("추가가드레일guardrail-output");
  expect(within(block).getByTestId("approval-proposal-0-removed")).toHaveTextContent("삭제되는 노드");
  expect(within(block).getByTestId("approval-proposal-0-text-agent-main-system_prompt")).toHaveTextContent(`+ ${PROPOSAL_ADDED_LINE}`);
  expect(within(block).queryByTestId("approval-proposal-0-text-cut")).not.toBeInTheDocument();
  expect(within(block).getByTestId("approval-proposal-0-open-canvas")).toHaveAttribute("href", "/agents/ag-support?tab=definition&proposal=p-1&session=as-9");
  expect(screen.getByTestId("decision-approve")).toHaveTextContent("제안대로 저장");
});

test("a creation links to the new-Agent canvas, a cut text diff says so, and the decided card drops the link", () => {
  const create = proposalBlockFixture({
    kind: "create",
    agent: { id: null, name: "petstore", display_name: "펫스토어" },
    base_revision: null,
    deployed_revision: null,
    attention: [{ code: "tool_source_added", node_id: "tools-1", source: { kind: "mcp_instance", id: "ms-1", name: "펫스토어 MCP" } }, { code: "write_without_approval", node_id: "tools-1", tool: "addPet" }, { code: "future_code" }],
  });
  create.changes.texts[0].cut = true;
  const { unmount } = renderWithProviders(<ApprovalCard pending={proposalPending(create)} onSubmit={() => {}} />);
  expect(screen.getByTestId("approval-proposal-0-title")).toHaveTextContent("새 Agent 제안: 펫스토어");
  expect(screen.queryByTestId("approval-proposal-0-deployed")).not.toBeInTheDocument();
  const attention = screen.getByTestId("approval-proposal-0-attention");
  expect(attention).toHaveTextContent("Tool 소스 펫스토어 MCP를 연결합니다");
  expect(attention).toHaveTextContent("쓰기 Tool addPet가 승인 없이 실행됩니다");
  // 이 화면이 모르는 code 는 code 그대로다
  expect(attention).toHaveTextContent("future_code");
  expect(screen.getByTestId("approval-proposal-0-text-cut")).toHaveTextContent("바뀐 곳이 많아 일부만 보입니다.");
  expect(screen.getByTestId("approval-proposal-0-open-canvas")).toHaveAttribute("href", "/agents/new?proposal=p-1&session=as-9");
  unmount();
  renderWithProviders(<ApprovalCard pending={proposalPending(create)} onSubmit={() => {}} resolved />);
  expect(screen.queryByTestId("approval-proposal-0-open-canvas")).not.toBeInTheDocument();
});

test("rejecting a proposal sends what to change from the open message box", async () => {
  // 제안을 다듬는 길은 고칠 점을 적은 거부다(P48 6.6절). 사유 칸은 단추를 누르기 전부터 열려 있다
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  renderWithProviders(<ApprovalCard pending={proposalPending(proposalBlockFixture())} onSubmit={onSubmit} />);
  const box = screen.getByTestId("decision-message");
  expect(box).toHaveAttribute("placeholder", "고칠 점 (선택)");
  await user.type(box, "입력 가드레일은 남겨 줘");
  await user.click(screen.getByTestId("decision-reject"));
  expect(onSubmit).toHaveBeenCalledWith([{ type: "reject", message: "입력 가드레일은 남겨 줘" }]);
});
