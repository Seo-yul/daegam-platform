/**
 * 배포 탭의 되돌리기와 배포 기록, 배포 게이트의 거절과 평가 없이 배포하기 (P45 6절, 12절, 14절).
 *
 * 목의 리비전은 서로 다른 내용이고(`v1:r{n}`), 되돌리기가 만든 리비전만 가져온 리비전과 같은 지문이다(msw/agents.ts `digestOf`).
 * 기본 Agent 는 r1 을 배포하고 r2 를 재배포한 기록을 가진다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { canRollBackTo, digestsOf, gateApplies, lastDeployedRow } from "@/lib/evaluation";

import { selectValue } from "./select";
import {
  HttpResponse,
  agentDraftFixture,
  agentFixture,
  agentState,
  evaluationGateFixture,
  evaluationRunFixture,
  evaluationSetFixture,
  evaluationState,
  http,
  lastRequest,
  meDeveloper,
  server,
} from "./msw";

const ID = "ag-support";
const DEPLOYMENT = `/agents/${ID}?tab=deployment`;

/** 배포된 r2 앞에 저장만 한 r3 이 있는 Agent. */
const ahead = () => agentFixture({ revision: 3, revisions: [1, 2, 3] });

describe("되돌리기의 판단 (lib/evaluation.ts)", () => {
  const revisions = [1, 2, 3].map((n) => ({ revision: n, spec_digest: `v1:r${n}`, created_at: "", graph: { nodes: [], edges: [] }, service: agentFixture().service }));
  const digests = digestsOf(revisions);

  it("배포된 내용도 지금 리비전의 내용도 아닌 리비전만 되돌린다", () => {
    const agent = ahead();
    expect([1, 2, 3].filter((n) => canRollBackTo(agent, digests, n))).toEqual([1]);
    // 배포하지 않은 Agent 는 되돌릴 것이 없다
    expect(canRollBackTo({ ...agent, deployed_revision: null }, digests, 1)).toBe(false);
  });

  it("게이트는 켜져 있고 배포된 적 없는 내용에만 걸린다", () => {
    const off = agentFixture();
    expect(gateApplies(off, digests, "v1:r3")).toBe(false);
    const on = agentFixture({ evaluation_gate: evaluationGateFixture() });
    // r1 과 r2 는 기록 줄에서 deployed 다. r3 은 배포된 적 없다
    expect(gateApplies(on, digests, "v1:r1")).toBe(false);
    expect(gateApplies(on, digests, "v1:r3")).toBe(true);
    expect(lastDeployedRow(on.deploy_history, "v1:r1")?.id).toBe("dh-1");
    expect(lastDeployedRow(on.deploy_history, "v1:r3")).toBeUndefined();
  });
});

describe("배포 탭의 리비전 표와 되돌리기", () => {
  it("배포된 것도 지금 것도 아닌 리비전에 되돌리기가 서고, 대화상자가 새 리비전 번호와 마지막 배포를 말한 뒤 되돌린다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead()];
    render(<TestApp initialPath={DEPLOYMENT} />);
    const table = await screen.findByTestId("revisions-table");
    await waitFor(() => expect(within(table).getByTestId("revision-rollback-1")).toBeInTheDocument());
    expect(within(table).queryByTestId("revision-rollback-2")).not.toBeInTheDocument();
    expect(within(table).queryByTestId("revision-rollback-3")).not.toBeInTheDocument();
    expect(within(table).getByTestId("revision-row-2")).toHaveTextContent("배포됨");
    expect(within(table).getByTestId("revision-row-3")).toHaveTextContent("현재");

    await user.click(within(table).getByTestId("revision-rollback-1"));
    const dialog = await screen.findByTestId("rollback-dialog");
    expect(dialog).toHaveTextContent("r1로 되돌리기");
    expect(within(dialog).getByTestId("rollback-body")).toHaveTextContent(
      "r1의 정의를 새 리비전 r4로 저장하고 배포합니다. 런타임 이미지와 provider 설정, Tool 목록은 현재 상태를 따르므로 r1이 배포됐던 때와 다르게 동작할 수 있습니다.",
    );
    expect(within(dialog).getByTestId("rollback-last-deployed")).toHaveTextContent("daegam-agent-runtime:0.1.0 이미지로 마지막으로 배포됐습니다.");
    // 게이트가 꺼져 있으면 평가 없이 배포하는 칸이 없다
    expect(within(dialog).queryByTestId("rollback-skip")).not.toBeInTheDocument();
    await user.click(within(dialog).getByTestId("rollback-submit"));

    await waitFor(() => expect(screen.queryByTestId("rollback-dialog")).not.toBeInTheDocument());
    expect(lastRequest("POST", `/agents/${ID}/rollback`)?.body).toEqual({ revision: 1 });
    // 기록 카드에 되돌린 줄이 새것부터 선다
    const history = await screen.findByTestId("deploy-history-table");
    await waitFor(() => expect(within(history).getAllByRole("row")[1]).toHaveTextContent("r1로 되돌림"));
    expect(within(history).getAllByRole("row")[1]).toHaveTextContent("r4");
  });

  it("배포된 내용으로 되돌리는 거절은 대화상자 안에 문장으로 선다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead()];
    server.use(http.post("/agents/:id/rollback", () => HttpResponse.json({ detail: { code: "nothing_to_roll_back", detail: "same" } }, { status: 409 })));
    render(<TestApp initialPath={DEPLOYMENT} />);
    await user.click(await screen.findByTestId("revision-rollback-1"));
    await user.click(within(await screen.findByTestId("rollback-dialog")).getByTestId("rollback-submit"));
    expect(await screen.findByTestId("rollback-error")).toHaveTextContent("선택한 리비전은 지금 배포된 정의와 같아 되돌릴 것이 없습니다. 다른 리비전을 선택해 주세요.");
  });

  it("되돌릴 정의가 지금 플랫폼에서 검증되지 않으면 그 리비전을 고치라고 말한다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead()];
    server.use(http.post("/agents/:id/rollback", () => HttpResponse.json({ detail: { code: "invalid_graph", detail: "graph has 1 error(s)", issues: [] } }, { status: 422 })));
    render(<TestApp initialPath={DEPLOYMENT} />);
    await user.click(await screen.findByTestId("revision-rollback-1"));
    await user.click(within(await screen.findByTestId("rollback-dialog")).getByTestId("rollback-submit"));
    expect(await screen.findByTestId("rollback-error")).toHaveTextContent(
      "r1의 정의가 지금 플랫폼의 검증을 통과하지 못해 되돌리지 않았습니다. 정의 탭에서 r1을 불러와 고친 뒤 저장하고 배포해 주세요.",
    );
  });

  it("게이트가 걸리는 되돌리기는 처음부터 평가 없이 배포하는 칸을 내고, 까닭을 적어야 보낸다", async () => {
    const user = userEvent.setup();
    // r1 의 배포 기록이 없어 r1 의 내용은 배포된 적 없는 새 내용이다
    const base = ahead();
    agentState.items = [{ ...base, evaluation_gate: evaluationGateFixture(), deploy_history: base.deploy_history.filter((r) => r.revision !== 1) }];
    evaluationState.sets = [evaluationSetFixture()];
    render(<TestApp initialPath={DEPLOYMENT} />);
    await user.click(await screen.findByTestId("revision-rollback-1"));
    const dialog = await screen.findByTestId("rollback-dialog");
    expect(within(dialog).queryByTestId("rollback-last-deployed")).not.toBeInTheDocument();
    const skip = within(dialog).getByTestId("rollback-skip");
    await user.click(within(skip).getByTestId("rollback-skip-toggle"));
    expect(within(dialog).getByTestId("rollback-submit")).toBeDisabled();
    await user.type(within(skip).getByTestId("rollback-skip-reason"), "장애 복구");
    await user.click(within(dialog).getByTestId("rollback-submit"));
    await waitFor(() => expect(lastRequest("POST", `/agents/${ID}/rollback`)?.body).toEqual({ revision: 1, skip_evaluation: { reason: "장애 복구" } }));
    const history = await screen.findByTestId("deploy-history-table");
    await waitFor(() => expect(within(history).getAllByRole("row")[1]).toHaveTextContent("평가 없이 배포함: 장애 복구"));
  });

  it("agent:write 가 없으면 되돌리기가 없다", async () => {
    agentState.items = [ahead()];
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:deploy", "agent:approve"] } })));
    render(<TestApp initialPath={DEPLOYMENT} />);
    expect(await screen.findByTestId("revision-row-1")).toBeInTheDocument();
    expect(screen.queryByTestId("revision-rollback-1")).not.toBeInTheDocument();
  });
});

describe("배포 기록 카드", () => {
  it("동작과 결과, 게이트가 받아들인 실행의 통과율과 건너뛴 까닭을 새것부터 보인다", async () => {
    const base = agentFixture();
    agentState.items = [
      {
        ...base,
        evaluation_gate: evaluationGateFixture(),
        deploy_history: [
          ...base.deploy_history,
          { ...base.deploy_history[1], id: "dh-3", revision: 3, spec_digest: "v1:r3", evaluation_run: "er-1", outcome: "failed" },
          { ...base.deploy_history[1], id: "dh-4", action: "rollback", revision: 4, restored_from: 1, spec_digest: "v1:r1", skipped_reason: "장애 복구", outcome: "pending" },
        ],
      },
    ];
    evaluationState.sets = [evaluationSetFixture()];
    evaluationState.runs = [evaluationRunFixture({ totals: { cases: 5, counted: 4, passed: 3, tokens: 0, cost_usd: null } })];
    render(<TestApp initialPath={DEPLOYMENT} />);
    const table = await screen.findByTestId("deploy-history-table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.dataset.testid)).toEqual(["deploy-history-dh-4", "deploy-history-dh-3", "deploy-history-dh-2", "deploy-history-dh-1"]);
    expect(rows[0]).toHaveTextContent("r1로 되돌림");
    expect(rows[0]).toHaveTextContent("진행 중");
    expect(within(rows[0]).getByTestId("deploy-history-dh-4-evaluation")).toHaveTextContent("평가 없이 배포함: 장애 복구");
    expect(rows[1]).toHaveTextContent("실패");
    await waitFor(() => expect(within(rows[1]).getByTestId("deploy-history-dh-3-evaluation")).toHaveTextContent("평가를 통과함 (75%)"));
    expect(rows[2]).toHaveTextContent("재배포");
    expect(rows[3]).toHaveTextContent("배포");
  });
});

describe("배포 게이트의 거절", () => {
  it("첫 배포가 막히면 까닭을 보이고, 까닭을 적어 평가 없이 배포한다", async () => {
    const user = userEvent.setup();
    // 처음 배포하는 Agent 라 배포 기록이 없다. 그 내용은 배포된 적 없는 새 내용이다
    agentState.items = [agentDraftFixture({ evaluation_gate: evaluationGateFixture(), deploy_history: [] })];
    evaluationState.sets = [evaluationSetFixture()];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    const dialog = await screen.findByTestId("deploy-dialog");
    await selectValue(user, "ag-cluster", "cl-home");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    expect(await within(dialog).findByTestId("evaluation-refusal-reason")).toHaveTextContent("이 정의를 평가한 결과가 없습니다.");
    await user.click(within(dialog).getByTestId("deploy-skip-toggle"));
    expect(within(dialog).getByTestId("deploy-submit")).toBeDisabled();
    await user.type(within(dialog).getByTestId("deploy-skip-reason"), "첫 배포");
    await user.click(within(dialog).getByTestId("deploy-submit"));
    await waitFor(() => expect(screen.queryByTestId("deploy-dialog")).not.toBeInTheDocument());
    expect(lastRequest("POST", "/agents/ag-draft/deploy")?.body).toMatchObject({ skip_evaluation: { reason: "첫 배포" } });
  });

  it("재배포가 막히면 까닭과 실행의 수를 보이고, 평가 없이 배포하면 까닭을 실어 다시 보낸다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead()];
    server.use(
      http.post(
        "/agents/:id/redeploy",
        () =>
          HttpResponse.json(
            { detail: { code: "evaluation_required", detail: "pass rate", reason: "pass_rate", pass_rate: 0.5, min_pass_rate: 0.8, run: { id: "er-1", state: "finished", totals: { cases: 2, counted: 2, passed: 1, tokens: 0 } }, judged: { id: "er-1", state: "finished", totals: { cases: 2, counted: 2, passed: 1, tokens: 0 } } } },
            { status: 409 },
          ),
        { once: true },
      ),
    );
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await user.click(await screen.findByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    expect(await within(dialog).findByTestId("redeploy-error")).toHaveTextContent("이 Agent 는 배포 전에 평가를 통과해야 합니다.");
    expect(within(dialog).getByTestId("evaluation-refusal-reason")).toHaveTextContent("통과율 50%가 기준 80%에 미치지 못합니다.");
    expect(within(dialog).getByTestId("evaluation-refusal-run")).toHaveTextContent("사례 2건 가운데 1건 통과 (50%)");

    await user.click(within(dialog).getByTestId("redeploy-skip-toggle"));
    await user.type(within(dialog).getByTestId("redeploy-skip-reason"), "급한 수정");
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    await waitFor(() => expect(screen.queryByTestId("redeploy-dialog")).not.toBeInTheDocument());
    expect(lastRequest("POST", `/agents/${ID}/redeploy`)?.body).toEqual({ skip_evaluation: { reason: "급한 수정" } });
  });

  it("기준선이 없는 거절은 배포된 리비전을 평가하는 길로 평가 탭의 시작 창을 연다", async () => {
    const user = userEvent.setup();
    agentState.items = [{ ...ahead(), evaluation_gate: evaluationGateFixture({ no_worse_cases: true }) }];
    evaluationState.sets = [evaluationSetFixture({ repeats: 2 })];
    server.use(
      http.post(
        "/agents/:id/redeploy",
        () => HttpResponse.json({ detail: { code: "evaluation_required", detail: "baseline", reason: "baseline_missing", cases: ["ec-1"], offer: { revision: 2 }, run: null, judged: null } }, { status: 409 }),
        { once: true },
      ),
    );
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await user.click(await screen.findByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    expect(await within(dialog).findByTestId("evaluation-refusal-reason")).toHaveTextContent(
      "배포된 리비전에서 평가한 결과가 없는 사례가 1건 있습니다. 배포된 리비전도 이 세트로 평가해 주세요.",
    );
    await user.click(within(dialog).getByTestId("evaluation-run-deployed"));
    // 대화상자가 닫히고 평가 탭이 r2 로 시작 창을 연다. 승인 권한이 없는 사람에게는 이 단추가 없다(아래)
    await waitFor(() => expect(screen.queryByTestId("redeploy-dialog")).not.toBeInTheDocument());
    const start = await screen.findByTestId("evaluation-start-dialog");
    expect(start).toHaveTextContent("r2 평가 실행");
  });

  it("승인 권한이 없으면 배포된 리비전을 평가하는 단추가 없다", async () => {
    const user = userEvent.setup();
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:write", "agent:deploy"] } })));
    agentState.items = [{ ...ahead(), evaluation_gate: evaluationGateFixture({ no_worse_cases: true }) }];
    server.use(
      http.post("/agents/:id/redeploy", () => HttpResponse.json({ detail: { code: "evaluation_required", detail: "baseline", reason: "baseline_missing", cases: ["ec-1"], offer: { revision: 2 }, run: null, judged: null } }, { status: 409 })),
    );
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await user.click(await screen.findByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    expect(await within(dialog).findByTestId("evaluation-refusal-reason")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("evaluation-run-deployed")).not.toBeInTheDocument();
  });

  it("통과율의 거절은 나빠진 사례의 수를 말하지 않는다", async () => {
    const user = userEvent.setup();
    agentState.items = [ahead()];
    const judged = { id: "er-1", state: "finished", totals: { cases: 2, counted: 2, passed: 1, tokens: 0 } };
    server.use(
      http.post("/agents/:id/redeploy", () => HttpResponse.json({ detail: { code: "evaluation_required", detail: "pass rate", reason: "pass_rate", pass_rate: 0.5, min_pass_rate: 0.8, run: judged, judged } }, { status: 409 })),
    );
    render(<TestApp initialPath={`/agents/${ID}`} />);
    await user.click(await screen.findByTestId("redeploy-button"));
    const dialog = await screen.findByTestId("redeploy-dialog");
    await user.click(within(dialog).getByTestId("redeploy-submit"));
    expect(await within(dialog).findByTestId("evaluation-refusal-run")).toHaveTextContent(/^사례 2건 가운데 1건 통과 \(50%\)$/);
  });
});
