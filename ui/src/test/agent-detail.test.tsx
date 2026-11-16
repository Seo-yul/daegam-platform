/** Agent 상세의 개요와 배포 동작 (D42). 배포는 화면 이동이 아니라 문서에 거는 동작이라 대화상자다. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { submitConfirm } from "./confirm";
import {
  HttpResponse,
  agentDraftFixture,
  agentFixture,
  agentState,
  agentWithRuntimeFeatures,
  agentWithSkill,
  http,
  lastRequest,
  server,
} from "./msw";
import { selectValue } from "./select";

describe("agents (detail and deployment)", () => {
  it("deploys a draft from the deployment tab with replicas, resources and namespace", async () => {
    const user = userEvent.setup();
    agentState.items = [agentDraftFixture()];
    render(<TestApp initialPath="/agents/ag-draft?tab=deployment" />);
    expect(await screen.findByTestId("deployment-empty")).toHaveTextContent("아직 배포하지 않았습니다");
    await user.click(screen.getByTestId("deployment-deploy"));

    const dialog = await screen.findByTestId("deploy-dialog");
    expect(within(dialog).getByTestId("ag-preview")).toHaveTextContent("agent-draft");
    await selectValue(user, "ag-cluster", "cl-home");
    await user.clear(within(dialog).getByTestId("ag-replicas"));
    await user.type(within(dialog).getByTestId("ag-replicas"), "2");
    await user.type(within(dialog).getByTestId("ag-req-cpu"), "250m");
    await waitFor(() => expect(screen.getByTestId("exp-namespace")).toHaveValue("daegam-default"));
    await selectValue(user, "exp-namespace", "daegam-default-edge");
    await user.click(within(dialog).getByTestId("deploy-submit"));

    await waitFor(() =>
      expect(lastRequest("POST", "/agents/ag-draft/deploy")?.body).toEqual({
        cluster_id: "cl-home",
        namespace: "daegam-default-edge",
        replicas: 2,
        resources: { requests: { cpu: "250m" } },
        exposure: { enabled: false },
        // 쓰지 않으면 0 으로 줄이는 스위치는 처음에 켜져 있다 (P44 6절)
        scale_to_zero: true,
      }),
    );
    expect(await screen.findByTestId("warning-network_policy_unenforced")).toBeInTheDocument();
  });

  it("상태 카드가 준비된 복제본 수와 conditions 표를 보인다", async () => {
    // 제일 오래 기다리는 화면이라 진짜 진척을 낼 숫자가 있어야 한다 (D43)
    agentState.items = [
      agentFixture({
        status: {
          ...agentFixture().status!,
          phase: "Rolling",
          ready_replicas: 1,
          conditions: [{ type: "Progressing", status: "True", reason: "NewReplicaSetAvailable", message: "replica set is progressing", last_transition: null }],
        },
      }),
    ];
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("status-ready-replicas")).toHaveTextContent("1 / 1");
    const table = screen.getByTestId("conditions-table");
    expect(table).toHaveTextContent("Progressing");
    expect(table).toHaveTextContent("NewReplicaSetAvailable");
    // 전이 시각이 없으면 "가동 시간" 줄을 뺀다 (D44 셋째)
    expect(screen.queryByTestId("readiness-uptime")).not.toBeInTheDocument();
    expect(screen.getByTestId("conditions-details")).toHaveTextContent("conditions 1개 전부 정상");
  });

  it("배포한 Agent 의 개요가 상태 타일과 주소·준비 상태·정의 요약으로 그래프를 요약한다", async () => {
    // 배포하지 않은 개요와 닫힌 탭은 resource-states.test.tsx 의 표가 본다
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Ready");
    const address = screen.getByTestId("address-card");
    expect(within(address).getByTestId("status-chat-url")).toHaveTextContent("/api/agents/ag-support/chat");
    expect(within(address).getByTestId("external-url")).toHaveTextContent("외부 노출 안 함");

    const readiness = screen.getByTestId("readiness-card");
    expect(within(readiness).getByTestId("status-sessions")).toHaveTextContent("2");
    expect(within(readiness).getByTestId("status-pending")).toHaveTextContent("1");

    const summary = screen.getByTestId("definition-summary");
    expect(within(summary).getByTestId("summary-models")).toHaveTextContent("openai-main");
    expect(within(summary).getByTestId("summary-tools")).toHaveTextContent("Gateway 1개");
    expect(within(summary).getByTestId("summary-memory")).toHaveTextContent("체크포인터");
    expect(within(summary).getByTestId("summary-skills")).toHaveTextContent("-");
    expect(within(summary).getByTestId("summary-replicas")).toHaveTextContent("1");
  });

  it("개요의 정의 요약이 붙인 Skill 과 고정한 리비전, 더 새 리비전을 보인다 (D64)", async () => {
    agentState.items = [agentWithSkill({ id: "sk-report", revision: 1 })];
    render(<TestApp initialPath="/agents/ag-support" />);
    const row = await screen.findByTestId("summary-skills");
    await waitFor(() => expect(within(row).getByTestId("summary-skill-sk-report-name")).toHaveTextContent("보고서 문체 · r1"));
    expect(within(row).getByTestId("summary-skill-sk-report-name")).toHaveAttribute("href", "/skills/sk-report");
    expect(within(row).getByTestId("summary-skill-sk-report-behind")).toHaveTextContent("새 리비전 r2");
  });

  it.each([
    ["init 이 기능을 알리기 전이면 보인다", false],
    ["런타임이 기능을 알렸으면 '런타임 기능 미확인' 은 서 있지 않는다", true],
  ])("재배포 응답의 런타임 기능 미확인: %s", async (_label, reported) => {
    const user = userEvent.setup();
    agentState.items = [reported ? agentWithRuntimeFeatures() : agentFixture()];
    const unknown = "runtime_features_unknown: the runtime image has not reported its features yet";
    server.use(
      http.post("/agents/:id/redeploy", () => HttpResponse.json({ ...agentState.items[0], warnings: [unknown] })),
    );
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    await user.click(screen.getByTestId("redeploy-button"));
    await user.click(await screen.findByTestId("redeploy-submit"));
    await waitFor(() => expect(screen.queryByTestId("redeploy-dialog")).not.toBeInTheDocument());
    if (reported) expect(screen.queryByTestId("redeploy-warning")).not.toBeInTheDocument();
    else expect(await screen.findByTestId("redeploy-warning")).toHaveTextContent("init 컨테이너가 확인");
  });

  it("redeploy warns about pending sessions, undeploy empties the deployment, clone copies the graph", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    await user.click(screen.getByTestId("redeploy-button"));
    expect(await screen.findByTestId("redeploy-dialog")).toBeInTheDocument();
    await user.click(screen.getByTestId("redeploy-submit"));
    const warning = await screen.findByTestId("redeploy-warning");
    expect(warning).toHaveTextContent("승인을 기다리던 세션이 있습니다");
    expect(warning).not.toHaveTextContent("pending_sessions_revision_changed");
    expect(lastRequest("POST", "/agents/ag-support/redeploy")?.body).toEqual({});

    await user.click(screen.getByTestId("undeploy-button"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("진행 중인 세션과 보류 승인은 사라집니다");
    await user.click(screen.getByTestId("confirm-submit"));
    await waitFor(() => expect(screen.getByTestId("state-badge")).toHaveTextContent("미배포"));

    await user.click(screen.getByTestId("clone-button"));
    const dialog = await screen.findByTestId("clone-dialog");
    expect(within(dialog).getByTestId("clone-name")).toHaveValue("support-copy");
    // 복제 대화상자도 노출을 함께 받는다 (D42). 다만 Agent 는 oidc 모드에서만 켤 수 있어(D27) header 모드인
    // 이 테스트에서는 안내가 뜨고 토글이 잠기므로 본문에는 꺼진 노출이 실린다
    expect(within(dialog).getByTestId("ai-exposure-oidc-only")).toBeInTheDocument();
    expect(within(dialog).getByTestId("exp-enabled")).toBeDisabled();
    await user.click(within(dialog).getByTestId("clone-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/agents/ag-support/clone")?.body).toMatchObject({
        name: "support-copy",
        exposure: { enabled: false },
      }),
    );
  });

  it("delete requires the name and returns to the list", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("agent-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user, "support");
    expect(await screen.findByTestId("agents-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/agents/ag-support")).toBeTruthy();
    expect(agentState.items).toHaveLength(0);
  });

  it("the open tab is in the URL so a shared link lands on the approval inbox", async () => {
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    await screen.findByTestId("agent-detail-page");
    expect(screen.getByTestId("tab-approvals")).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("pending-session-s-2")).toBeInTheDocument();
    // 채팅 탭은 대화를 잃지 않으려고 언마운트하지 않는다. 보이지만 않는다 (D43)
    expect(screen.getByTestId("chat-tabpanel")).not.toBeVisible();
  });
});
