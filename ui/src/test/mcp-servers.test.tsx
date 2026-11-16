/**
 * MCP Server (D42) 화면 테스트는 주제마다 파일 하나다. 여기는 목록과 정의이고, 배포 동작과 상태는
 * mcp-server-deployment, Hub 게시는 mcp-server-publish 에 있다.
 *
 * 정의와 배포가 한 문서라 화면도 하나다. 그래서 "만들면 저장만 된다", "배포는 문서에 거는 동작이다",
 * "정의가 배포보다 앞서면 그것을 말한다" 셋이 이 화면들의 중심이다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { stubClipboard } from "./clipboard";
import { HttpResponse, draftFixture, http, lastRequest, mcpServerFixture, mcpServerState, meViewer, server, versions } from "./msw";
import { selectValue } from "./select";

describe("mcp servers (list)", () => {
  it("counts 미배포 apart from the deployed buckets", async () => {
    const mk = (name: string, phase: string) => mcpServerFixture({ id: `ms-${name}`, name, status: { ...mcpServerFixture().status!, phase } });
    mcpServerState.items = [mk("a", "Ready"), mk("b", "Ready"), mk("c", "Rolling"), mk("d", "Failed"), draftFixture({ id: "ms-e", name: "e" })];
    render(<TestApp initialPath="/mcp-servers" />);
    await waitFor(() => expect(screen.getByTestId("ms-count-total")).toHaveTextContent("5"));
    expect(screen.getByTestId("ms-count-draft")).toHaveTextContent("1");
    expect(screen.getByTestId("ms-count-ready")).toHaveTextContent("2");
    expect(screen.getByTestId("ms-count-progressing")).toHaveTextContent("1");
    expect(screen.getByTestId("ms-count-failed")).toHaveTextContent("1");
    // 제목 옆 한 줄은 나쁜 쪽을 먼저 말한다 (D44 넷째)
    expect(screen.getByText("5개 · Failed 1")).toBeInTheDocument();
  });

  it("행의 백엔드와 클러스터는 링크이고 주소에는 복사가 붙는다", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    const url = "http://mcp-calc.daegam-default.svc:8080/mcp";
    mcpServerState.items = [mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: "Ready", url } })];
    render(<TestApp initialPath="/mcp-servers" />);
    const row = await screen.findByTestId("mcp-server-row-calc");
    // 개수만 보이던 칸이 이름과 길을 준다 (D44 넷째)
    await waitFor(() => expect(within(row).getByTestId("mcp-server-backends-calc")).toHaveTextContent("Orders SOAP"));
    expect(within(row).getByRole("link", { name: "Orders SOAP" })).toHaveAttribute("href", "/backend-apis/bp-orders");
    expect(within(row).getByTestId("mcp-server-cluster-calc")).toHaveAttribute("href", "/clusters/cl-home");
    await user.click(within(row).getByTestId("copy-url-calc"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(url));
  });

  it("'배포된 것만' 은 올라가 있거나 올라가는 중인 것만 센다", async () => {
    const user = userEvent.setup();
    const mk = (name: string, state: "deployed" | "deploying" | "failed" | "undeploying") => mcpServerFixture({ id: `ms-${name}`, name, state });
    mcpServerState.items = [mk("up", "deployed"), mk("going", "deploying"), mk("broken", "failed"), mk("leaving", "undeploying"), draftFixture({ id: "ms-new", name: "new" })];
    render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-server-row-up");
    await user.click(screen.getByTestId("deployed-only"));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-row-new")).not.toBeInTheDocument());
    expect(screen.getByTestId("mcp-server-row-up")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-server-row-going")).toBeInTheDocument();
    // 올리다 실패한 것과 내리는 중인 것은 "배포된 것" 이 아니다
    expect(screen.queryByTestId("mcp-server-row-broken")).not.toBeInTheDocument();
    expect(screen.queryByTestId("mcp-server-row-leaving")).not.toBeInTheDocument();
  });

  it("a row shows the document state next to the CR phase, and a draft has no phase", async () => {
    mcpServerState.items = [
      mcpServerFixture({ status: { ...mcpServerFixture().status!, phase: "Ready", url: "http://mcp-calc.daegam-default.svc:8080/mcp" } }),
      draftFixture(),
    ];
    render(<TestApp initialPath="/mcp-servers" />);
    const row = await screen.findByTestId("mcp-server-row-calc");
    expect(within(row).getByTestId("mcp-server-state-calc")).toHaveTextContent("배포");
    expect(within(row).getByTestId("mcp-server-phase-calc")).toHaveTextContent("Ready");
    expect(row).toHaveTextContent("http://mcp-calc.daegam-default.svc:8080/mcp");

    // 안 올린 문서는 phase 가 없다. 대신 배포로 가는 길을 준다
    const draft = screen.getByTestId("mcp-server-row-draft");
    expect(within(draft).getByTestId("mcp-server-state-draft")).toHaveTextContent("미배포");
    expect(within(draft).queryByTestId("mcp-server-phase-draft")).not.toBeInTheDocument();
    expect(within(draft).getByTestId("deploy-draft")).toHaveAttribute("href", "/mcp-servers/ms-draft?tab=deployment");
  });

  it("배포 권한이 없으면 목록에도 상세에도 배포 버튼이 없다", async () => {
    // 목의 기본 사용자는 platform-admin 이라 scope 검사가 첫 줄에서 참이 된다. viewer 로 바꿔야
    // 검사가 실제로 도는지 보인다
    server.use(http.get("/me", () => HttpResponse.json(meViewer)));
    mcpServerState.items = [draftFixture()];
    const app = render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-server-row-draft");
    expect(screen.queryByTestId("deploy-draft")).not.toBeInTheDocument();

    // 상세도 같은 검사를 건다. 한쪽만 걸면 목록에서 못 보던 버튼이 상세에서 보인다
    app.unmount();
    const detail = render(<TestApp initialPath="/mcp-servers/ms-draft" />);
    await screen.findByTestId("page-title");
    expect(screen.queryByTestId("deploy-button")).not.toBeInTheDocument();

    // 배포 탭도 같이 막는다. 개요만 막으면 탭 하나 옆에서 같은 버튼이 그대로 보인다
    detail.unmount();
    render(<TestApp initialPath="/mcp-servers/ms-draft?tab=deployment" />);
    await screen.findByTestId("deployment-empty");
    expect(screen.queryByTestId("deployment-deploy")).not.toBeInTheDocument();
  });

  it("says when the definition is ahead of what is running", async () => {
    mcpServerState.items = [mcpServerFixture({ revision: 3, deployed_revision: 2 })];
    render(<TestApp initialPath="/mcp-servers" />);
    const row = await screen.findByTestId("mcp-server-row-calc");
    expect(within(row).getByTestId("mcp-server-behind-calc")).toHaveTextContent("배포 r2");
  });
});

describe("mcp servers (definition)", () => {
  it("creating saves a draft instead of deploying", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/new" />);
    await user.type(screen.getByTestId("mp-name"), "calc2");
    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder"));
    await user.click(screen.getByTestId("save-button"));

    expect(await screen.findByTestId("mcp-server-detail-page")).toBeInTheDocument();
    // 타임아웃을 비운 backend 는 보내지 않아 서버의 기본값(30초)이 된다 (P50 7절)
    const created = lastRequest("POST", "/mcp-servers")?.body as { name: string; backends: Record<string, unknown>[] };
    expect(created.name).toBe("calc2");
    expect(created.backends[0]).not.toHaveProperty("timeout_seconds");
    // 만든 것은 아직 클러스터에 없다. 개요가 그것을 말하고 배포 탭으로 보낸다
    expect(await screen.findByTestId("overview-empty")).toHaveTextContent("아직 배포하지 않았습니다");
  });

  it("editing shows the stored backend timeout, keeps it, and sends a changed one", async () => {
    const user = userEvent.setup();
    const fixture = mcpServerFixture();
    fixture.backends[0].timeout_seconds = 90;
    mcpServerState.items = [fixture];
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    await screen.findByTestId("revision-notice");
    await waitFor(() => expect(screen.getByTestId("mp-timeout")).toHaveValue(90));
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("mcp-server-detail-page");
    let body = lastRequest("PUT", "/mcp-servers/ms-calc")?.body as { backends: { timeout_seconds: number }[] };
    expect(body.backends[0].timeout_seconds).toBe(90);

    await user.click(screen.getByTestId("edit-button"));
    const field = await screen.findByTestId("mp-timeout");
    // 상한은 300초다 (P50 11절의 실측)
    expect(field).toHaveAttribute("max", "300");
    await user.clear(field);
    await user.type(field, "300");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("mcp-server-detail-page");
    body = lastRequest("PUT", "/mcp-servers/ms-calc")?.body as { backends: { timeout_seconds: number }[] };
    expect(body.backends[0].timeout_seconds).toBe(300);
  });

  it("the definition tab shows revisions, a diff and the CR preview", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc?tab=definition" />);
    await screen.findByTestId("mcp-server-detail-page");
    expect(screen.getByTestId("ms-revision")).toHaveTextContent("r2");
    expect(await screen.findByTestId("revision-row-2")).toHaveTextContent("current");
    expect(screen.getByTestId("revision-row-2")).toHaveTextContent("deployed");

    const diff = await screen.findByTestId("revision-diff");
    expect(diff).toHaveTextContent("r1 → r2");
    expect(within(diff).getByTestId("diff-runtime.replicas")).toHaveTextContent("1");

    await selectValue(user, "preview-cluster", "cl-edge");
    await user.click(screen.getByTestId("preview-button"));
    const yaml = await screen.findByTestId("preview-yaml");
    expect(yaml).toHaveTextContent("kind: MCPServer");
    expect(screen.getByTestId("preview-namespace")).toHaveTextContent("daegam-default");
    // 미리보기 클러스터는 쿼리로 간다 (본문 없는 POST)
    expect(lastRequest("POST", "/mcp-servers/ms-calc/revisions/2/preview?cluster_id=cl-edge")).toBeDefined();
  });

  it("editing makes a new revision and the detail says the running one is behind", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    expect(await screen.findByTestId("revision-notice")).toHaveTextContent("r3");
    await waitFor(() => expect(screen.getByTestId("mp-name")).toHaveValue("calc"));
    await user.click(await screen.findByTestId("tool-CancelOrder"));
    await user.click(screen.getByTestId("save-button"));

    await screen.findByTestId("mcp-server-detail-page");
    // 정의만 올라갔으므로 돌고 있는 것은 아직 r2 다
    expect(await screen.findByTestId("revision-behind")).toHaveTextContent("재배포해야");
  });

  it("keeps the tools a new spec version still has, and says which ones it dropped", async () => {
    server.use(
      http.get("/backend-apis/:id/versions", () =>
        HttpResponse.json([
          { ...versions[0], tool_names: ["CreateOrder", "GetOrder"], tool_details: versions[0].tool_details.slice(0, 2), tool_count: 2 },
          versions[1],
        ]),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/new" />);
    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder"));
    await user.click(screen.getByTestId("tool-CancelOrder"));

    await selectValue(user, "mp-version", "1");
    expect(await screen.findByTestId("tools-dropped")).toHaveTextContent("CancelOrder");
    expect(screen.getByTestId("tool-CreateOrder")).toBeChecked();
  });

  it("names the clashing tool before saving instead of after a 422", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/new" />);
    await user.type(screen.getByTestId("mp-name"), "clash");
    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder"));
    await user.click(screen.getByTestId("backend-add"));
    await selectValue(user, "mp-backend-1", "bp-orders");
    await selectValue(user, "mp-version-1", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder-1"));

    const alert = await screen.findByTestId("tool-name-clash");
    expect(alert).toHaveTextContent("CreateOrder");
    expect(alert).toHaveTextContent("Backend API #1");
    expect(screen.getByTestId("save-button")).toBeDisabled();
  });
});
