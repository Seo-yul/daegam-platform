/** MCP Server 의 배포 동작(배포·내리기·복제·재배포·삭제)과 개요의 상태·폴링·배너 (D42). */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { submitConfirm } from "./confirm";
import { HttpResponse, NOW, apiError, countExact, draftFixture, http, lastRequest, mcpServerFixture, mcpServerState, server } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

describe("mcp servers (deployment)", () => {
  it("deploys a draft from the deployment tab and shows the warnings the deploy returned", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [draftFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-draft?tab=deployment" />);
    expect(await screen.findByTestId("deployment-empty")).toHaveTextContent("아직 배포하지 않았습니다");
    await user.click(screen.getByTestId("deployment-deploy"));

    const dialog = await screen.findByTestId("deploy-dialog");
    await selectValue(user, "deploy-cluster", "cl-edge");
    await user.click(within(dialog).getByTestId("deploy-submit"));

    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-draft/deploy")?.body).toEqual({ cluster_id: "cl-edge", namespace: null, exposure: { enabled: false } }));
    expect(await screen.findByTestId("warning-network_policy_unenforced")).toBeInTheDocument();
  });

  it("배포 응답과 상태에 같은 경고가 와도 한 번만 보인다", async () => {
    // 노출을 켜고 배포하면 서버가 무인증 노출 경고를 응답 본문의 warnings 와 문서의 status.warnings 에
    // 함께 싣는다. 상세는 그 둘을 따로 그리므로 거르지 않으면 같은 문장이 두 번 선다
    const user = userEvent.setup();
    mcpServerState.items = [draftFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));

    const dialog = await screen.findByTestId("deploy-dialog");
    await selectValue(user, "deploy-cluster", "cl-edge");
    await user.click(within(dialog).getByTestId("exp-enabled"));
    await user.clear(within(dialog).getByTestId("exp-host"));
    await user.type(within(dialog).getByTestId("exp-host"), "demo.default.apps.local");
    await user.click(within(dialog).getByTestId("deploy-submit"));

    await waitFor(() => expect(screen.queryByTestId("deploy-dialog")).toBeNull());
    expect(await screen.findAllByText(/인증 없이 외부에 열립니다/)).toHaveLength(1);
  });

  it("undeploy keeps the definition and puts the document back to 미배포", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    await user.click(screen.getByTestId("undeploy-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("정의와 리비전 이력은 그대로 남아");
    await submitConfirm(user);

    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-calc/undeploy")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("state-badge")).toHaveTextContent("미배포"));
    // 안 올린 것에는 재배포·배포 해제가 없다. 배포 해제는 메뉴 안이라 열고 본다
    expect(screen.queryByTestId("undeploy-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("redeploy-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("deploy-button")).toBeInTheDocument();
  });

  it("clone copies the definition into a new draft", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    await user.click(screen.getByTestId("clone-button"));
    const dialog = await screen.findByTestId("clone-dialog");
    expect(within(dialog).getByTestId("clone-name")).toHaveValue("calc-copy");
    // 노출을 함께 정하면 그 설정이 복제 본문에 실린다. 노출용 둘째를 한 호출로 만드는 자리다 (D42)
    await user.click(within(dialog).getByTestId("exp-enabled"));
    await user.clear(within(dialog).getByTestId("exp-host"));
    await user.type(within(dialog).getByTestId("exp-host"), "calc-copy.example.com");
    await user.click(within(dialog).getByTestId("clone-submit"));

    await waitFor(() =>
      expect(lastRequest("POST", "/mcp-servers/ms-calc/clone")?.body).toMatchObject({
        name: "calc-copy",
        exposure: { enabled: true, host: "calc-copy.example.com" },
      }),
    );
    // 복제본은 배포 쪽이 비어 있다
    expect(await screen.findByTestId("overview-empty")).toBeInTheDocument();
  });

  it("Pending explains the wait, redeploy asks first and tells the screen reader it started, and delete needs the name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    // 기본 픽스처는 Pending 이다. 재배포가 phase 를 바꾸기 전에 읽는다
    expect(await screen.findByTestId("status-progress")).toHaveTextContent("5초마다 상태를 다시 확인하므로");
    expect(screen.getByTestId("status-phase-text")).toHaveTextContent("준비 중");
    // 배포를 시작하면 낭독기에게 말한다. 누르기 전에는 비어 있다
    const announce = await screen.findByTestId("deploy-announce");
    expect(announce).toHaveAttribute("role", "status");
    expect(announce).toHaveTextContent("");

    await user.click(screen.getByTestId("redeploy-button"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("정의는 r2 그대로입니다");
    expect(lastRequest("POST", "/mcp-servers/ms-calc/redeploy")).toBeUndefined();
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-calc/redeploy")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("deploy-announce")).toHaveTextContent("재배포를 시작했습니다."));

    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user, "calc");
    expect(await screen.findByTestId("mcp-servers-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/mcp-servers/ms-calc")).toBeDefined();
  });

  it("개요는 주소·준비 상태·참조·최근 기록·정의 요약 다섯 카드이고 conditions 는 접혀 있다", async () => {
    // Ready 조건의 전이 시각이 "가동 시간" 의 유일한 재료다 (D44 셋째, API 예외 하나)
    const base = mcpServerFixture();
    mcpServerState.items = [
      mcpServerFixture({
        status: {
          ...base.status!,
          phase: "Ready",
          url: "http://mcp-calc.daegam-default.svc:8080/mcp",
          message: null,
          ready_replicas: 2,
          since: NOW,
          conditions: [{ type: "Ready", status: "True", reason: "Running", message: "all replicas ready", last_transition: NOW }],
        },
      }),
    ];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");

    const address = screen.getByTestId("address-card");
    expect(within(address).getByTestId("status-url")).toHaveTextContent("mcp-calc.daegam-default.svc");
    expect(within(address).getByTestId("status-external-url")).toHaveTextContent("외부 노출 안 함");
    expect(within(address).getByTestId("address-auth")).toHaveTextContent("서비스 토큰");

    const readiness = screen.getByTestId("readiness-card");
    expect(within(readiness).getByTestId("status-phase")).toHaveTextContent("Ready");
    expect(within(readiness).getByTestId("status-ready-replicas")).toHaveTextContent("2 / 2");
    expect(within(readiness).getByTestId("readiness-uptime")).toBeInTheDocument();
    expect(within(readiness).getByTestId("status-revision")).toHaveTextContent("r2");
    expect(within(readiness).getByTestId("status-revision")).not.toHaveTextContent("정의");

    const summary = screen.getByTestId("definition-summary");
    await waitFor(() => expect(within(summary).getByTestId("summary-backends")).toHaveTextContent("orders"));
    expect(within(summary).getByTestId("summary-tools")).toHaveTextContent("2 / 3");
    expect(within(summary).getByTestId("summary-runtime")).toHaveTextContent("replicas 2");
    expect(screen.getByTestId("references-card")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("recent-card-list")).toHaveTextContent("정의 r2 저장"));

    // K8s conditions 는 접힌 상세로 내려간다. 전부 정상이면 요약 한 줄이다
    const details = screen.getByTestId("conditions-details");
    expect(details).toHaveTextContent("클러스터 상태 자세히 · conditions 1개 전부 정상");
    expect(details).not.toHaveAttribute("open");
  });

  it("polls while deployed and stops once nothing is running", async () => {
    tickingClock();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Pending");
    const before = countExact("GET", "/mcp-servers/ms-calc");

    mcpServerState.items = [mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: "Ready", url: "http://mcp-calc.daegam-default.svc:8080/mcp", message: null, conditions: [{ type: "Ready", status: "True", reason: "Running", message: "all replicas ready", last_transition: NOW }] } })];
    await advanceClock(DEPLOY_POLL_MS + 100);
    await waitFor(() => expect(screen.getByTestId("status-phase")).toHaveTextContent("Ready"));
    expect(screen.getByTestId("conditions-table")).toHaveTextContent("all replicas ready");
    expect(countExact("GET", "/mcp-servers/ms-calc")).toBeGreaterThan(before);
  });

  it("keeps the last data with a stale banner when a poll fails", async () => {
    tickingClock();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Pending");
    server.use(http.get("/mcp-servers/:id", () => HttpResponse.error()));
    await advanceClock(DEPLOY_POLL_MS + 100);
    await waitFor(() => expect(screen.getByTestId("status-stale")).toBeInTheDocument());
    expect(screen.getByTestId("status-phase")).toHaveTextContent("Pending");
  });

  it("Pending past the expected time turns into a warning and Failed says what to do next", async () => {
    const user = userEvent.setup();
    // 상세는 status.since 를 배너에 넘긴다. 예상 시간을 넘긴 Pending 은 경고로 바뀐다
    // (예상 시간 안의 Pending 안내는 재배포 테스트가 기본 픽스처로 본다)
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    mcpServerState.items = [mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: "Pending", since: fiveMinutesAgo } })];
    const slow = render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const progress = await screen.findByTestId("status-progress");
    expect(progress).toHaveTextContent("예상보다 오래 걸립니다");
    expect(progress).toHaveTextContent("파드 이벤트");

    slow.unmount();
    mcpServerState.items = [mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: "Failed", error: "ImagePullBackOff" } })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const failed = await screen.findByTestId("status-failed");
    expect(failed).toHaveTextContent("이미지 이름과 리소스 요청을 고친 뒤 다시 배포해 주세요");
    await user.click(within(failed).getByTestId("failed-manifest"));
    expect(await screen.findByTestId("advanced-editor")).toBeInTheDocument();
  });

  it("마지막 오류는 코드를 표에 태워 한국어 처방으로 보인다", async () => {
    // 서버는 last_error 를 언제나 `코드: 설명` 으로 저장한다 (services/instance_base.py)
    mcpServerState.items = [
      mcpServerFixture({ last_error: "cluster_unreachable: HTTPSConnectionPool(host='10.0.0.1', port=6443): Max retries exceeded" }),
    ];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const last = await screen.findByTestId("last-error");
    expect(last).toHaveTextContent("마지막 오류:");
    expect(last).toHaveTextContent("클러스터에 연결하지 못했습니다");
    expect(last).not.toHaveTextContent("HTTPSConnectionPool");
    expect(last).not.toHaveTextContent("last error");
  });

  it("클러스터에 닿지 못하면 배너를 세운다. 진행도 실패도 아니라 지금까지 아무 배너가 없었다", async () => {
    mcpServerState.items = [
      mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: null, error: "cluster_unreachable: Max retries exceeded" } }),
    ];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const banner = await screen.findByTestId("status-unreachable");
    expect(banner).toHaveTextContent("지금 상태를 읽지 못했습니다");
    expect(banner).toHaveTextContent("클러스터에 연결하지 못했습니다");
    expect(screen.queryByTestId("status-progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("status-failed")).not.toBeInTheDocument();
  });

  it("재배포 실패는 대화상자 안에 보인다. 뒤에 그리면 dim 에 가려 못 읽는다", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [mcpServerFixture({ state: "deployed" })];
    server.use(http.post("/mcp-servers/:id/redeploy", () => apiError(502, "cluster_unreachable", "Max retries exceeded")));
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await user.click(await screen.findByTestId("redeploy-button"));
    await user.click(await screen.findByTestId("confirm-submit"));
    const inDialog = await screen.findByTestId("confirm-error");
    expect(inDialog).toHaveTextContent("클러스터에 연결하지 못했습니다");
    // 대화상자는 열린 채이고 페이지 쪽에는 같은 오류를 겹쳐 그리지 않는다
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("redeploy-error")).not.toBeInTheDocument();
  });

  it("리비전이 앞서가면 그 배너에서 바로 재배포한다", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [mcpServerFixture({ state: "deployed", revision: 3, revisions: [1, 2, 3], deployed_revision: 2 })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const behind = await screen.findByTestId("revision-behind");
    await user.click(within(behind).getByTestId("behind-redeploy"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("재배포");
  });

  it("status warnings read as Korean sentences, not server codes", async () => {
    mcpServerState.items = [mcpServerFixture({ status: { ...mcpServerFixture().status!, warnings: ["network_policy_unenforced: no NetworkPolicy controller was detected on this cluster (D16)"] } })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const warning = await screen.findByTestId("status-warning");
    expect(warning).toHaveTextContent("아무것도 막지 않습니다");
    expect(warning).not.toHaveTextContent("network_policy_unenforced");
  });

  it("the Advanced tab of a deployed document opens the manifest editor", async () => {
    // 배포하지 않은 문서에서 닫혀 있는 것은 resource-states.test.tsx 의 표가 본다
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=advanced" />);
    expect(await screen.findByTestId("tab-advanced")).toHaveAttribute("aria-selected", "true");
    const editor = await screen.findByTestId("advanced-editor");
    expect((within(editor).getByTestId("monaco") as HTMLTextAreaElement).value).toContain("kind: MCPServer");
  });
});
