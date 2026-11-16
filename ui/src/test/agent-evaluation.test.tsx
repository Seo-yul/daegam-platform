/**
 * 평가 탭 (P45 7절~12절, 14절). 세트와 사례의 편집기와 개인정보 경고, 대화에서 복사하는 창과 그 자리표, 시작 창의 확인할 사례,
 * 실행의 판정과 시작한 사람만 보는 내용, 배포 전 평가 카드, 승인 권한 없는 사람의 탭을 본다.
 *
 * 목의 실행은 시작하면 바로 끝나고(`evaluationState.finishAfterMs` 0), 첫 질문에 FAIL 이 든 사례는 확인 항목이 지나지 못한다
 * (msw/agent-evaluation.ts).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { caseBodyOf, draftFieldOf, emptyCase, graderChoices } from "@/lib/evaluation";

import {
  HttpResponse,
  agentFeedbackState,
  agentFixture,
  agentPreviewState,
  agentState,
  evaluationCaseFixture,
  evaluationGateFixture,
  evaluationRunFixture,
  evaluationSetFixture,
  evaluationState,
  feedbackFixture,
  http,
  countExact,
  lastExact,
  lastRequest,
  meDeveloper,
  previewFixture,
  server,
} from "./msw";
import { selectValue } from "./select";

const ID = "ag-support";
const TAB = `/agents/${ID}?tab=evaluation`;

describe("사례 본문 (lib/evaluation.ts)", () => {
  it("빈 줄을 빼고 확인 항목은 종류에 맞는 칸만 싣는다", () => {
    const draft = { ...emptyCase(), messages: ["질문", "  "], criteria: ["", "기한을 말한다"] };
    draft.checks = [
      { kind: "tool_called", tool: " Add ", args: '{"a": 1}', text: "무시" },
      { kind: "answer_contains", tool: "무시", args: "", text: "7일" },
    ];
    draft.toolResults = [
      { tool: "Add", result: "7" },
      { tool: " ", result: "버림" },
    ];
    expect(caseBodyOf(draft)).toEqual({
      content: {
        messages: ["질문"],
        reference: null,
        criteria: ["기한을 말한다"],
        checks: [
          { kind: "tool_called", tool: "Add", args: { a: 1 } },
          { kind: "answer_contains", text: "7일" },
        ],
        expect_done: true,
        tool_results: { Add: "7" },
      },
    });
    expect(caseBodyOf({ ...draft, checks: [{ kind: "tool_called", tool: "Add", args: "[1]", text: "" }] })).toEqual({ problem: { kind: "args", index: 0 } });
  });

  it("채점 모델의 provider 는 메인 모델과 그 폴백, 서브에이전트의 모델이고 떠 있는 모델 노드는 넣지 않는다", () => {
    const graph = {
      nodes: [
        { id: "main", type: "agent", data: { role: "main" } },
        { id: "sub", type: "agent", data: { role: "sub" } },
        { id: "m-main", type: "model", data: { provider: "openai-main", model: null, fallback: { provider: "claude", model: "claude-x" } } },
        { id: "m-sub", type: "model", data: { provider: "local-stub", model: "stub" } },
        { id: "m-floating", type: "model", data: { provider: "floating", model: "f" } },
      ],
      edges: [
        { source: "main", target: "m-main", kind: "model" },
        { source: "sub", target: "m-sub", kind: "model" },
      ],
    };
    expect(graderChoices(graph)).toEqual([
      { provider: "openai-main", model: null },
      { provider: "claude", model: "claude-x" },
      { provider: "local-stub", model: "stub" },
    ]);
  });

  it("메인이 붙인 가드레일의 판정 모델도 채점 모델의 provider 이고, 붙지 않은 가드레일의 것은 넣지 않는다", () => {
    const judge = (id: string, model: string) => ({ id, type: "guardrail", data: { position: "tool", rules: [{ kind: "injection_check", value: { model, max_chars: 8000, max_calls: 20 } }] } });
    const graph = {
      nodes: [
        { id: "main", type: "agent", data: { role: "main" } },
        { id: "m-main", type: "model", data: { provider: "openai-main", model: null } },
        { id: "m-judge", type: "model", data: { provider: "claude", model: "claude-haiku-5-5" } },
        { id: "m-loose", type: "model", data: { provider: "loose", model: "x" } },
        judge("g-judge", "m-judge"),
        judge("g-loose", "m-loose"),
      ],
      edges: [
        { source: "main", target: "m-main", kind: "model" },
        { source: "main", target: "g-judge", kind: "guardrail" },
      ],
    };
    expect(graderChoices(graph)).toEqual([
      { provider: "openai-main", model: null },
      { provider: "claude", model: "claude-haiku-5-5" },
    ]);
  });

  it("개인정보 검사의 자리를 편집기의 칸으로 옮긴다. 빈 줄은 저장 본문에서 빠지므로 그만큼 밀린다", () => {
    const draft = { ...emptyCase(), messages: ["", "제 번호"], toolResults: [{ tool: " ", result: "" }, { tool: "Add", result: "7" }] };
    expect(draftFieldOf("messages[0]", draft)).toEqual({ field: "message", index: 1 });
    expect(draftFieldOf("tool_results.names[0]", draft)).toEqual({ field: "toolResultName", index: 1 });
    expect(draftFieldOf('tool_results["Add"]', draft)).toEqual({ field: "toolResult", index: 1 });
    expect(draftFieldOf("reference", draft)).toEqual({ field: "reference" });
    expect(draftFieldOf("messages[3]", draft)).toBeNull();
  });
});

describe("평가 탭", () => {
  it("승인 권한이 없으면 탭이 없다", async () => {
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:write", "agent:deploy"] } })));
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await screen.findByTestId("status-revision-tile");
    expect(screen.queryByRole("tab", { name: "평가" })).not.toBeInTheDocument();
  });

  it("세트가 없으면 만들라고 말하고, 만든 세트를 고른다", async () => {
    const user = userEvent.setup();
    // 템플릿으로 만든 Agent 는 모델 노드의 모델이 비어 있다. provider 를 고르고 모델 이름을 적는다
    render(<TestApp initialPath={TAB} />);
    expect(await screen.findByTestId("evaluation-sets-empty")).toHaveTextContent("평가 세트가 없습니다. 세트를 만들고 사례를 추가해 주세요.");
    await user.click(screen.getByTestId("evaluation-set-create"));
    const dialog = await screen.findByTestId("evaluation-set-dialog");
    await user.type(within(dialog).getByTestId("evaluation-set-name"), "  환불  ");
    // 채점 모델의 선택지는 이 Agent 의 모델 노드가 쓰는 provider 와 모델이다
    await selectValue(user, "evaluation-set-grader", "openai-main");
    const model = within(dialog).getByTestId("evaluation-set-model");
    expect(model).toHaveValue("");
    await user.type(model, "gpt-4o-mini");
    await selectValue(user, "evaluation-set-repeats", "2");
    await user.click(within(dialog).getByTestId("evaluation-set-save"));
    await waitFor(() => expect(screen.queryByTestId("evaluation-set-dialog")).not.toBeInTheDocument());
    const body = lastRequest("POST", `/agents/${ID}/evaluation-sets`)?.body as { name: string; grader: { provider: string } | null; repeats: number };
    expect(body.name).toBe("환불");
    expect(body.grader).toEqual({ provider: "openai-main", model: "gpt-4o-mini" });
    expect(body.repeats).toBe(2);
    expect(await screen.findByTestId("evaluation-set-view")).toHaveTextContent("환불");
  });

  it("손으로 쓴 사례의 개인정보 경고는 확인란을 선택해야 저장되고, 확인을 실어 보낸다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-case-add"));
    const editor = await screen.findByTestId("case-editor");
    await user.type(within(editor).getByTestId("case-message-0"), "제 번호는 010-1234-5678 입니다");
    await user.click(within(editor).getByTestId("case-check-add"));
    await user.type(within(editor).getByTestId("case-check-0-text"), "기한");
    // 쓰기 Tool 의 가상 결과가 무엇을 하는지 칸 위에 적는다
    expect(editor).toHaveTextContent("평가 실행은 쓰기 Tool 을 실제로 부르지 않습니다.");
    await user.click(within(editor).getByTestId("case-save"));

    expect(await within(editor).findByTestId("case-pii-warning")).toHaveTextContent("실제 개인정보가 아니면 아래 확인란을 선택한 뒤 저장해 주세요.");
    expect(within(editor).getByTestId("case-pii-matches")).toHaveTextContent("질문 1 · 전화번호");
    expect(within(editor).getByTestId("case-message-0")).toHaveAttribute("aria-invalid", "true");
    expect(within(editor).getByTestId("case-check-0-text")).not.toHaveAttribute("aria-invalid");
    expect(within(editor).getByTestId("case-save")).toBeDisabled();
    await user.click(within(editor).getByTestId("case-pii-acknowledge"));
    await user.click(within(editor).getByTestId("case-save"));
    await waitFor(() => expect(screen.queryByTestId("case-editor")).not.toBeInTheDocument());
    const body = lastExact("POST", `/agents/${ID}/evaluation-sets/es-1/cases`)?.body as { pii_acknowledged: boolean; content: { checks: unknown[] } };
    expect(body.pii_acknowledged).toBe(true);
    expect(body.content.checks).toEqual([{ kind: "answer_contains", text: "기한" }]);
    expect(await screen.findByTestId("evaluation-cases")).toHaveTextContent("직접 작성");
  });

  it("확인 항목의 인자가 JSON 객체가 아니면 보내지 않고 그 칸에 까닭을 단다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-case-add"));
    const editor = await screen.findByTestId("case-editor");
    await user.type(within(editor).getByTestId("case-message-0"), "더해 주세요");
    await user.click(within(editor).getByTestId("case-check-add"));
    await selectValue(user, "case-check-0-kind", "tool_called");
    await user.type(within(editor).getByTestId("case-check-0-tool"), "Add");
    await user.type(within(editor).getByTestId("case-check-0-args"), "[[1]");
    await user.click(within(editor).getByTestId("case-save"));
    expect(await within(editor).findByText("1번째 확인 항목의 인자는 JSON 객체여야 합니다. 고친 뒤 다시 저장해 주세요.")).toBeInTheDocument();
    expect(lastExact("POST", `/agents/${ID}/evaluation-sets/es-1/cases`)).toBeUndefined();
  });

  it("시작 창은 다른 사람이 쓴 사례를 확인하게 하고, 본 지문을 실어 시작한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [evaluationCaseFixture()];
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-run-start"));
    const dialog = await screen.findByTestId("evaluation-start-dialog");
    expect(dialog).toHaveTextContent("평가 실행은 미리 보기 파드 하나를 띄워 사례마다 새 대화를 엽니다.");
    // header 모드라 Tool 을 Agent 의 권한으로 부른다고 말한다
    expect(within(dialog).getByTestId("evaluation-start-header-mode")).toBeInTheDocument();
    expect(within(dialog).getByTestId("evaluation-start-review")).toHaveTextContent("다른 사람이 작성하거나 바꾼 사례가 1건 있습니다.");
    expect(within(dialog).getByTestId("evaluation-start-review-ec-1")).toHaveTextContent("환불 기한이 언제인가요?");
    await user.click(within(dialog).getByTestId("evaluation-start-submit"));
    await waitFor(() => expect(screen.queryByTestId("evaluation-start-dialog")).not.toBeInTheDocument());
    const body = lastRequest("POST", `/agents/${ID}/evaluation-sets/es-1/runs`)?.body as { set_digest: string; revision: number };
    expect(body.revision).toBe(2);
    expect(body.set_digest).toMatch(/^sd:/);
    // 목의 실행은 바로 끝난다
    expect(await screen.findByTestId("evaluation-run-er-1-result")).toHaveTextContent("사례 1건 가운데 1건 통과 (100%)");
  });

  it("시작한 사람은 판정과 답변, Tool 호출의 답한 꼴을 보고 다른 사람은 통과 여부만 본다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [
      evaluationCaseFixture(),
      evaluationCaseFixture({ id: "ec-2", content: { messages: ["FAIL 더하기"], reference: null, criteria: ["7을 말한다"], checks: [{ kind: "answer_contains", tool: null, args: {}, text: "7" }], expect_done: true, tool_results: { Add: "7" } } }),
    ];
    evaluationState.me = "header-admin";
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-run-start"));
    await user.click(within(await screen.findByTestId("evaluation-start-dialog")).getByTestId("evaluation-start-submit"));
    await user.click(await screen.findByTestId("evaluation-run-er-1-open"));
    const detail = await screen.findByTestId("evaluation-run-dialog");
    expect(await within(detail).findByTestId("evaluation-run-summary")).toHaveTextContent("사례 2건 가운데 1건 통과 (50%), 배포된 리비전보다 나빠진 사례 0건");
    expect(within(detail).getByTestId("evaluation-verdict-ec-2-0")).toHaveTextContent("실패");
    expect(within(detail).getByTestId("evaluation-criteria-advisory")).toHaveTextContent("채점 기준의 판단은 참고용입니다.");
    const result = within(detail).getByTestId("evaluation-result-ec-2-0");
    expect(within(result).getByTestId("evaluation-call-ec-2-0-0")).toHaveTextContent("Add");
    expect(within(result).getByTestId("evaluation-call-ec-2-0-0")).toHaveTextContent("가상 결과를 돌려줌");
    expect(within(detail).queryByTestId("evaluation-content-hidden")).not.toBeInTheDocument();
  });

  it("다른 사람이 시작한 실행의 내용은 그 사람만 볼 수 있다고 말하고, 도는 동안 바뀐 사례는 세지 않았다고 말한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [evaluationCaseFixture()];
    evaluationState.runs = [
      evaluationRunFixture({
        verdicts: [
          { case_id: "ec-1", case_digest: "", repeat: 0, state: "done", counted: true, checks_passed: false, criteria: 0, criteria_passed: null, grader_error: false },
          { case_id: "ec-gone", case_digest: "", repeat: 0, state: "changed", counted: false, checks_passed: null, criteria: 0, criteria_passed: null, grader_error: false },
        ],
        totals: { cases: 1, counted: 1, passed: 0, tokens: 0, cost_usd: null },
        baseline: { worse: ["ec-1"], missing: [] },
      }),
    ];
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-run-er-1-open"));
    const detail = await screen.findByTestId("evaluation-run-dialog");
    expect(await within(detail).findByTestId("evaluation-content-hidden")).toHaveTextContent(
      "답변과 Tool 결과는 이 평가를 실행한 dev@example.com 님만 볼 수 있습니다. 그 사람의 권한으로 읽은 내용이기 때문입니다.",
    );
    expect(within(detail).getByTestId("evaluation-worse-ec-1")).toHaveTextContent("나빠짐");
    expect(within(detail).getByTestId("evaluation-verdict-ec-gone-0")).toHaveTextContent("실행하는 동안 바뀌거나 지워져서 실행하지 않았고 결과에도 포함하지 않았습니다.");
    expect(within(detail).getByTestId("evaluation-verdict-ec-gone-0")).toHaveTextContent("지운 사례");
    expect(within(detail).queryByTestId("evaluation-results")).not.toBeInTheDocument();
  });

  it("세트가 그 사이 바뀌어 시작이 거절되면 지문과 확인할 사례를 다시 읽고, 다시 누르면 새 지문으로 시작한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [evaluationCaseFixture()];
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-run-start"));
    const dialog = await screen.findByTestId("evaluation-start-dialog");
    // 창을 연 뒤 다른 사람이 사례를 더했다
    evaluationState.cases = [...evaluationState.cases, evaluationCaseFixture({ id: "ec-new", content: { messages: ["새 질문"], reference: null, criteria: [], checks: [], expect_done: true, tool_results: {} } })];
    await user.click(within(dialog).getByTestId("evaluation-start-submit"));
    expect(await within(dialog).findByTestId("evaluation-start-error")).toHaveTextContent("확인한 뒤에 평가 세트가 바뀌었습니다.");
    expect(await within(dialog).findByTestId("evaluation-start-review-ec-new")).toHaveTextContent("새 질문");
    await user.click(within(dialog).getByTestId("evaluation-start-submit"));
    await waitFor(() => expect(screen.queryByTestId("evaluation-start-dialog")).not.toBeInTheDocument());
  });

  it("결과 창은 감사에 남는 실행 읽기를 되풀이하지 않고, 목록에서 실행이 바뀔 때만 다시 읽는다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [evaluationCaseFixture()];
    evaluationState.runs = [evaluationRunFixture({ state: "running", done_steps: 0, verdicts: [], ended_at: null })];
    evaluationState.finishAt = { "er-1": Date.now() + 60_000 };
    const RUN = `/agents/${ID}/evaluation-sets/es-1/runs/er-1`;
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-run-er-1-open"));
    await screen.findByTestId("evaluation-run-dialog");
    await waitFor(() => expect(countExact("GET", RUN)).toBe(1));
    // 목록이 한 번 더 읽혀도 실행이 그대로면 결과 창은 다시 읽지 않는다
    const listed = countExact("GET", `/agents/${ID}/evaluation-sets/es-1/runs`);
    await waitFor(() => expect(countExact("GET", `/agents/${ID}/evaluation-sets/es-1/runs`)).toBeGreaterThan(listed), { timeout: 5_000 });
    expect(countExact("GET", RUN)).toBe(1);
    // 실행이 끝나 목록의 줄이 바뀌면 다시 읽는다
    evaluationState.finishAt = { "er-1": Date.now() };
    await waitFor(() => expect(countExact("GET", RUN)).toBe(2), { timeout: 5_000 });
    expect(await screen.findByTestId("evaluation-run-summary")).toBeInTheDocument();
  }, 15_000);

  it("채점 중인 실행은 채점을 마친 판정의 수를 보인다", async () => {
    evaluationState.sets = [evaluationSetFixture()];
    const graded = { case_digest: "", state: "done", counted: true, checks_passed: true, criteria: 1, criteria_passed: null, grader_error: false };
    evaluationState.runs = [
      evaluationRunFixture({ state: "grading", graded_steps: 1, ended_at: null, verdicts: [{ ...graded, case_id: "ec-1", repeat: 0 }, { ...graded, case_id: "ec-2", repeat: 0 }] }),
    ];
    render(<TestApp initialPath={TAB} />);
    expect(await screen.findByTestId("evaluation-run-er-1-result")).toHaveTextContent("2개 가운데 1개 채점");
  });

  it("배포 권한이 없으면 주소가 가리키는 시작 창을 열지 않는다", async () => {
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:write", "agent:approve"] } })));
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.cases = [evaluationCaseFixture()];
    render(<TestApp initialPath={`${TAB}&set=es-1&revision=2`} />);
    expect(await screen.findByTestId("evaluation-runs-table")).toBeInTheDocument();
    expect(screen.queryByTestId("evaluation-run-start")).not.toBeInTheDocument();
    expect(screen.queryByTestId("evaluation-start-dialog")).not.toBeInTheDocument();
  });

  it("실행이 오류로 끝난 까닭을 문장으로 보인다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.runs = [evaluationRunFixture({ state: "error", reason: "evaluation_tools_unknown", verdicts: [] })];
    render(<TestApp initialPath={TAB} />);
    expect(await screen.findByTestId("evaluation-run-er-1-result")).toHaveTextContent("Tool 이름을 알 수 없는 Tool 소스가 있어 평가를 실행하지 않았습니다.");
    await user.click(screen.getByTestId("evaluation-run-er-1-open"));
    expect(await screen.findByTestId("evaluation-run-reason")).toHaveTextContent("그 소스에서 사용할 Tool 을 선택한 뒤 다시 실행해 주세요.");
  });

  it("배포 전 평가 카드가 게이트를 켜고 끈다. 반복이 하나인 세트는 나빠진 사례를 막지 못한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    render(<TestApp initialPath={TAB} />);
    const card = await screen.findByTestId("evaluation-gate");
    expect(within(card).getByTestId("evaluation-gate-state")).toHaveTextContent("꺼짐");
    expect(within(card).getByTestId("evaluation-gate-hint")).toHaveTextContent("평가 결과는 7일 동안 유효합니다.");
    expect(within(card).getByTestId("evaluation-gate-no-worse")).toBeDisabled();
    await user.clear(within(card).getByTestId("evaluation-gate-rate"));
    await user.type(within(card).getByTestId("evaluation-gate-rate"), "90");
    await user.click(within(card).getByTestId("evaluation-gate-save"));
    await waitFor(() => expect(lastRequest("PUT", `/agents/${ID}/evaluation-gate`)?.body).toEqual({ set_id: "es-1", min_pass_rate: 0.9, no_worse_cases: false, max_age_days: 7 }));
    await waitFor(() => expect(within(card).getByTestId("evaluation-gate-state")).toHaveTextContent("켜짐"));
    await user.click(within(card).getByTestId("evaluation-gate-off"));
    await waitFor(() => expect(within(card).getByTestId("evaluation-gate-state")).toHaveTextContent("꺼짐"));
    expect(lastRequest("PUT", `/agents/${ID}/evaluation-gate`)?.body).toBeNull();
  });

  it("게이트가 쓰는 세트를 지우려 하면 그 까닭을 확인 창에 보인다", async () => {
    const user = userEvent.setup();
    agentState.items = [agentFixture({ evaluation_gate: evaluationGateFixture() })];
    evaluationState.sets = [evaluationSetFixture()];
    server.use(http.delete("/agents/:id/evaluation-sets/:sid", () => HttpResponse.json({ detail: { code: "evaluation_set_in_gate", detail: "in gate" } }, { status: 409 })));
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("evaluation-set-delete"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("환불 문의 세트와 그 사례를 모두 지웁니다.");
    await user.click(within(dialog).getByTestId("confirm-submit"));
    expect(await within(dialog).findByTestId("evaluation-set-delete-error")).toHaveTextContent("배포 전 평가가 이 세트를 사용하고 있어 지울 수 없습니다.");
  });
});

describe("대화에서 평가 사례로 복사", () => {
  it("동의한 평가의 줄에만 복사가 서고, 자리표로 바꾼 복사본을 출처와 함께 저장한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", evaluation_use: true }), feedbackFixture({ id: "fb-2", turn_id: "turn-s-1-2", evaluation_use: false })];
    render(<TestApp initialPath={`/agents/${ID}?tab=feedback`} />);
    await screen.findByTestId("feedback-row-fb-2");
    expect(screen.queryByTestId("feedback-copy-fb-2")).not.toBeInTheDocument();
    await user.click(await screen.findByTestId("feedback-copy-fb-1"));

    const dialog = await screen.findByTestId("copy-case-dialog");
    expect(await within(dialog).findByTestId("case-copy-hint")).toHaveTextContent("좋아요나 아쉬워요를 받은 질문과 그 답변만 사례로 복사합니다.");
    expect(within(dialog).getByTestId("case-copy-replaced")).toHaveTextContent("전화번호 1건을 바꿔 두었습니다.");
    expect(within(dialog).getByTestId("case-message-0")).toHaveValue("제 번호는 [phone] 입니다. 환불해 주세요.");
    // 복사한 사례의 참조 답변은 아쉬워요를 받았으므로 좋은 답변의 예가 아니다
    expect(within(dialog).getByTestId("case-reference-good")).not.toBeChecked();
    await user.click(within(dialog).getByTestId("case-save"));
    await waitFor(() => expect(screen.queryByTestId("copy-case-dialog")).not.toBeInTheDocument());
    expect(lastExact("POST", `/agents/${ID}/evaluation-sets/es-1/cases`)?.body).toMatchObject({ origin: { rating_id: "fb-1", session_id: "s-1", turn_id: "turn-s-1-1" } });
    expect(evaluationState.cases).toHaveLength(1);
    // 복사는 감사에 남는 쓰기라 저장 뒤의 무효화가 다시 보내지 않는다
    expect(countExact("POST", `/agents/${ID}/evaluation-sets/es-1/cases/from-turn`)).toBe(1);
  });

  it("복사한 사례에 개인정보가 남으면 확인란 없이 오류 문장만 선다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", evaluation_use: true })];
    render(<TestApp initialPath={`/agents/${ID}?tab=feedback`} />);
    await user.click(await screen.findByTestId("feedback-copy-fb-1"));
    const dialog = await screen.findByTestId("copy-case-dialog");
    const message = await within(dialog).findByTestId("case-message-0");
    await user.clear(message);
    await user.type(message, "제 번호는 010-9999-8888 입니다");
    await user.click(within(dialog).getByTestId("case-save"));
    expect(await within(dialog).findByTestId("case-pii-error")).toHaveTextContent("표시한 부분을 지우거나 바꾼 뒤 다시 저장해 주세요.");
    expect(within(dialog).queryByTestId("case-pii-acknowledge")).not.toBeInTheDocument();
  });

  it("동의하지 않은 턴의 복사는 서버가 막고 그 까닭을 말한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", evaluation_use: true })];
    server.use(http.post("/agents/:id/evaluation-sets/:sid/cases/from-turn", () => HttpResponse.json({ detail: { code: "evaluation_consent_missing", detail: "no" } }, { status: 409 })));
    render(<TestApp initialPath={`/agents/${ID}?tab=feedback`} />);
    await user.click(await screen.findByTestId("feedback-copy-fb-1"));
    expect(await screen.findByTestId("copy-case-error")).toHaveTextContent("대화한 사람이 이 대화를 평가 사례로 사용하는 데 동의하지 않았습니다. 사례를 직접 작성해 주세요.");
  });

  it("다시 생성됐거나 보관 기간이 지난 턴은 복사할 수 없다고 말한다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", evaluation_use: true })];
    server.use(http.post("/agents/:id/evaluation-sets/:sid/cases/from-turn", () => HttpResponse.json({ detail: { code: "turn_not_found", detail: "gone" } }, { status: 404 })));
    render(<TestApp initialPath={`/agents/${ID}?tab=feedback`} />);
    await user.click(await screen.findByTestId("feedback-copy-fb-1"));
    expect(await screen.findByTestId("copy-case-error")).toHaveTextContent(
      "이 답변은 다시 생성됐거나 대화 기록의 보관 기간이 지나 사례로 복사할 수 없습니다. 사례를 직접 작성해 주세요.",
    );
  });

  it("세트가 없으면 먼저 만들라고 말한다", async () => {
    const user = userEvent.setup();
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", evaluation_use: true })];
    render(<TestApp initialPath={`/agents/${ID}?tab=feedback`} />);
    await user.click(await screen.findByTestId("feedback-copy-fb-1"));
    expect(await screen.findByTestId("copy-case-no-sets")).toHaveTextContent("평가 세트가 없습니다. 평가 탭에서 세트를 먼저 만들어 주세요.");
  });

  it("세션 탭의 턴 기록은 동의한 평가가 있는 턴에만 복사를 세운다", async () => {
    const user = userEvent.setup();
    evaluationState.sets = [evaluationSetFixture()];
    // 목의 턴 기록은 턴 t-1 하나다
    agentFeedbackState.items = [feedbackFixture({ id: "fb-1", session_id: "s-1", turn_id: "t-1", evaluation_use: true })];
    render(<TestApp initialPath={`/agents/${ID}?tab=sessions`} />);
    await user.click(await screen.findByTestId("session-row-s-1"));
    await user.click(await screen.findByTestId("session-turn-t-1-copy"));
    expect(await screen.findByTestId("copy-case-dialog")).toBeInTheDocument();
  });
});

describe("열린 미리 보기의 평가 실행", () => {
  it("평가 실행이 띄운 미리 보기는 그 실행의 것이라고 적고, 끝내기 전에 실행도 취소한다고 묻는다", async () => {
    const user = userEvent.setup();
    const preview = previewFixture({ id: "pv-eval", run: "er-1", revision: 2 });
    agentPreviewState.items = [preview, previewFixture({ id: "pv-own", revision: 2 })];
    render(<TestApp initialPath={`/agents/${ID}`} />);
    const card = await screen.findByTestId("open-previews");
    expect(within(card).getByTestId("open-preview-row-pv-eval")).toHaveTextContent(/^dev@example\.com 님의 r2 평가 실행, \d{2}:\d{2}에 끝납니다$/);
    await user.click(within(card).getByTestId("open-preview-end-pv-eval"));
    expect(await screen.findByTestId("open-preview-end-evaluation")).toHaveTextContent("미리 보기를 끝내면 이 평가 실행도 취소합니다.");
    await user.click(screen.getByTestId("confirm-cancel"));
    // 사람의 미리 보기에는 그 줄이 없다
    await user.click(within(card).getByTestId("open-preview-end-pv-own"));
    await screen.findByTestId("confirm-dialog");
    expect(screen.queryByTestId("open-preview-end-evaluation")).not.toBeInTheDocument();
  });
});
