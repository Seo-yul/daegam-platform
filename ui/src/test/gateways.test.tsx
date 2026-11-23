/**
 * Gateway (D42) 화면 테스트다.
 *
 * Gateway 는 허브 항목 여럿을 vMCP 하나로 묶는다. 정의 쪽 `backends` 와 배포 쪽 `deployed_backends` 가
 * 따로 있어, "무엇을 묶기로 했는가" 와 "지금 무엇이 살아 있는가" 를 갈라 보는 것이 이 파일의 중심이다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { advertisedTools } from "@/lib/gateway";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { authBoundary } from "@/lib/auth";
import { splitAccessRevoked } from "@/pages/gateways/Detail";
import { submitConfirm } from "./confirm";
import { signInLocal } from "./localSignIn";
import { HttpResponse, authConfigState, oidcAuthConfig, gatewayDraftFixture, gatewayFixture, gatewayState, http, hubItemFixture, hubRemote, hubSharedReadOnly, hubState, lastRequest, meViewer, server } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

describe("gateways (pure helpers)", () => {
  it("advertisedTools reads the deployed snapshot, falls back to the filter and applies renames", () => {
    const base = gatewayFixture();
    const gw = gatewayFixture({
      deployed_backends: [
        { ...base.deployed_backends[0], tools: ["Add"], tool_names: ["Add", "Divide"] },
        { ...base.deployed_backends[1], tools: ["GetWeather"], tool_names: [] },
      ],
      tool_overrides: { "gw-support-weather": { GetWeather: "gw-support-weather_GetWeather" } },
    });
    const out = advertisedTools(gw);
    expect(out[0].tools).toEqual([{ name: "Add", original: "Add" }]);
    expect(out[1].tools).toEqual([{ name: "gw-support-weather_GetWeather", original: "GetWeather" }]);
    // 필터 없음 + 이름 미상 → 아무것도 광고하지 못한다
    expect(advertisedTools({ deployed_backends: [{ ...base.deployed_backends[1], tools: [], tool_names: [] }], deployed_conflict_resolution: "priority", tool_overrides: {} })[0].tools).toEqual([]);
  });

  it("advertisedTools puts the workload in front of every name in prefix mode, like vMCP", () => {
    const out = advertisedTools(gatewayFixture({ deployed_conflict_resolution: "prefix", tool_overrides: { "gw-support-calc": { Add: "plus" } } }));
    expect(out[0].tools[0]).toEqual({ name: "gw-support-calc_Add", original: "Add" });
  });

  it("splitAccessRevoked separates hub_access_revoked warnings from the rest", () => {
    expect(splitAccessRevoked(["prefix mode", "hub_access_revoked: docs", "hub_access_revoked:  weather"])).toEqual({
      revoked: ["docs", "weather"],
      rest: ["prefix mode"],
    });
  });

  it("authBoundary follows the platform auth mode", () => {
    expect(authBoundary("oidc")).toBe("oidc");
    expect(authBoundary("local")).toBe("local");
    // Gateway 응답의 auth 를 그대로 넣어도 같은 값이 나온다 (연결 카드)
    expect(authBoundary("anonymous")).toBe("anonymous");
    expect(authBoundary("header")).toBe("anonymous");
    expect(authBoundary("unknown")).toBe("anonymous");
  });
});

describe("gateways (list)", () => {
  it("a row shows the state, the phase, the backend counts and 미배포 for a draft, and the summary counts 미배포 apart from the deployed buckets", async () => {
    gatewayState.items = [gatewayFixture(), gatewayDraftFixture()];
    render(<TestApp initialPath="/gateways" />);
    const row = await screen.findByTestId("gateway-row-support");
    expect(within(row).getByTestId("gateway-state-support")).toHaveTextContent("배포");
    // 행의 phase 는 색 점이다. 낱말은 위 요약 줄이 이미 세고 있어 겹친다
    expect(within(row).getByTestId("gateway-phase-support")).toHaveAttribute("data-phase", "Degraded");
    expect(within(row).getByTestId("gateway-badge-support")).toHaveTextContent("일부 백엔드 불가");
    expect(within(row).getByTestId("gateway-backends-support")).toHaveTextContent("1/2");
    expect(row).toHaveTextContent("http://vmcp-gw-support.daegam-default.svc.cluster.local:4483/mcp");

    // 안 올린 것은 묶어 둔 개수만 뜻이 있다
    const draft = screen.getByTestId("gateway-row-draft");
    expect(within(draft).getByTestId("gateway-state-draft")).toHaveTextContent("미배포");
    expect(within(draft).getByTestId("gateway-backends-draft")).toHaveTextContent("2");
    expect(within(draft).getByTestId("deploy-draft")).toHaveAttribute("href", "/gateways/gw-draft?tab=deployment");

    await waitFor(() => expect(screen.getByTestId("gw-count-total")).toHaveTextContent("2"));
    expect(screen.getByTestId("gw-count-draft")).toHaveTextContent("1");
    // Degraded 는 Ready 도 Failed 도 아니라 진행 중으로 센다
    expect(screen.getByTestId("gw-count-progressing")).toHaveTextContent("1");
  });

  it("배포 권한이 없으면 목록에 배포 버튼이 없다", async () => {
    // 목의 기본 사용자가 platform-admin 이라 viewer 로 바꿔야 scope 검사가 실제로 돈다
    server.use(http.get("/me", () => HttpResponse.json(meViewer)));
    gatewayState.items = [gatewayDraftFixture()];
    render(<TestApp initialPath="/gateways" />);
    await screen.findByTestId("gateway-row-draft");
    expect(screen.queryByTestId("deploy-draft")).not.toBeInTheDocument();
  });

  it("list row trash requires the name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways" />);
    await screen.findByTestId("gateway-row-support");
    await user.click(screen.getByTestId("row-delete-support"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("Gateway 와 그룹·Entry 클러스터 리소스가 함께 사라지고");
    expect(within(dialog).getByTestId("confirm-submit")).toBeDisabled();
    await submitConfirm(user, "support");
    await waitFor(() => expect(lastRequest("DELETE", "/gateways/gw-support")).toBeDefined());
  });
});

describe("gateways (definition)", () => {
  it("creating saves a draft: hub items with per-item tools, conflict/failure/authz/runtime", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/new" />);
    await user.type(screen.getByTestId("gp-name"), "helper");
    expect(screen.getByTestId("gp-auth")).toHaveTextContent("anonymous");

    await user.click(await screen.findByTestId("backend-calc"));
    await user.click(screen.getByTestId("backend-calc-tool-Add"));
    expect(screen.getByTestId("backend-calc-priority")).toHaveTextContent("#1");
    await user.click(screen.getByTestId("backend-weather"));
    expect(screen.getByTestId("backend-weather-no-tools")).toHaveTextContent("Tool 새로고침을 먼저 실행해 주세요");

    await selectValue(user, "gp-conflict", "priority");
    expect(screen.getByTestId("gp-conflict-warning")).toHaveTextContent("weather");
    await selectValue(user, "gp-conflict", "prefix");
    expect(screen.queryByTestId("gp-conflict-warning")).not.toBeInTheDocument();

    await selectValue(user, "gp-failure", "fail");
    // 백엔드마다 호출 타임아웃을 정한다. 비운 백엔드는 배포가 정한다 (P50 7절)
    await user.type(screen.getByTestId("backend-calc-timeout"), "45");
    // 새 Gateway 는 resources·prompts 를 둘 다 끈 채로 시작한다 (P50 13절)
    expect(screen.getByTestId("gp-surface-resources")).not.toBeChecked();
    expect(screen.getByTestId("gp-surface-prompts")).not.toBeChecked();
    await user.click(screen.getByTestId("gp-surface-resources"));
    await user.click(screen.getByTestId("save-button"));

    expect(await screen.findByTestId("gateway-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/gateways")?.body).toMatchObject({
      name: "helper",
      backends: [
        { hub_item_id: "hub-calc", tools: ["Add"], timeout_seconds: 45 },
        { hub_item_id: "hub-weather", tools: [] },
      ],
      conflict_resolution: "prefix",
      failure_mode: "fail",
      surface: { resources: true, prompts: false },
    });
    // 만든 것은 아직 클러스터에 없다
    expect(await screen.findByTestId("overview-empty")).toHaveTextContent("아직 배포하지 않았습니다");
  });

  it("백엔드 목록은 표시 이름을 먼저 보이고, 다를 때만 K8s 이름을 곁에 적는다", async () => {
    hubState.items = [hubItemFixture({ display_name: "계산기" }), hubRemote];
    render(<TestApp initialPath="/gateways/new" />);
    await screen.findByTestId("backend-calc");
    const list = screen.getByTestId("backend-list");
    expect(within(list).getByText("계산기")).toBeInTheDocument();
    expect(within(list).getByText("calc")).toHaveClass("font-mono");
    // 표시 이름이 name 과 같은 항목은 이름 하나만 보인다
    expect(within(list).getAllByText("weather")).toHaveLength(1);
  });

  it("while Gateways accept only IdP tokens, published hub items are blocked, and items without consume are disabled with the reason", async () => {
    server.use(http.get("/auth/config", () => HttpResponse.json({ auth_mode: "oidc", issuer: "https://issuer.example.com", client_id: "daegam-ui", scopes: ["openid"] })));
    hubState.items = [hubItemFixture(), { ...hubItemFixture(), id: "hub-pub", name: "pub", kind: "published" }, hubSharedReadOnly];
    render(<TestApp initialPath="/gateways/new" />);
    const pub = await screen.findByTestId("backend-pub");
    await waitFor(() => expect(pub).toBeDisabled());
    expect(screen.getByTestId("backend-pub-oidc-blocked")).toHaveTextContent("Gateway 가 아직 IdP 토큰만 받는 동안에는 게시된 Gateway 를 백엔드로 사용할 수 없습니다");
    expect(screen.getByTestId("backend-docs")).toBeDisabled();
    expect(screen.getByTestId("backend-docs-not-consumable")).toHaveTextContent("이 항목을 백엔드로 선택할 권한이 없습니다");
    expect(screen.getByTestId("backend-calc")).toBeEnabled();
  });

  it("under platform tokens a published Gateway is a backend without its script tool, and a proxy-mode one is blocked (P57 6.1·6.2)", async () => {
    authConfigState.mode = "local";
    const user = userEvent.setup();
    const published = { ...hubItemFixture(), id: "hub-pub", name: "pub", kind: "published" as const, tool_names: ["Add", "execute_tool_script"] };
    const proxied = { ...published, id: "hub-proxy", name: "proxied", k8s: { ...published.k8s, kind: "MCPRemoteProxy" } };
    hubState.items = [hubItemFixture(), published, proxied];
    render(<TestApp initialPath="/gateways/new" />);
    await signInLocal(user);
    const pub = await screen.findByTestId("backend-pub");
    expect(pub).toBeEnabled();
    expect(screen.queryByTestId("backend-pub-oidc-blocked")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("backend-proxied")).toBeDisabled());
    expect(screen.getByTestId("backend-proxied-proxy-blocked")).toHaveTextContent("proxy 모드로 게시된 Gateway 는 백엔드로 사용할 수 없습니다");
    await user.click(pub);
    expect(screen.getByTestId("backend-pub-tool-Add")).toBeInTheDocument();
    expect(screen.queryByTestId("backend-pub-tool-execute_tool_script")).not.toBeInTheDocument();
  });

  it("oidc under platform tokens allows published Gateways and blocks only proxy mode (P57 6.1)", async () => {
    server.use(http.get("/auth/config", () => HttpResponse.json(oidcAuthConfig)));
    const published = { ...hubItemFixture(), id: "hub-pub", name: "pub", kind: "published" as const };
    const proxied = { ...published, id: "hub-proxy", name: "proxied", k8s: { ...published.k8s, kind: "MCPRemoteProxy" } };
    hubState.items = [hubItemFixture(), published, proxied];
    render(<TestApp initialPath="/gateways/new" />);
    await waitFor(() => expect(screen.getByTestId("backend-proxied")).toBeDisabled());
    expect(screen.getByTestId("backend-pub")).toBeEnabled();
    expect(screen.queryByTestId("backend-pub-oidc-blocked")).not.toBeInTheDocument();
    expect(screen.getByTestId("backend-proxied-proxy-blocked")).toHaveTextContent("원래 Gateway 를 entry 모드로 게시한 항목을 선택해 주세요.");
  });

  it("under platform tokens a published Gateway without Tool names is blocked, and counts show only selectable Tools", async () => {
    authConfigState.mode = "local";
    const user = userEvent.setup();
    const unknown = { ...hubItemFixture(), id: "hub-unknown", name: "unknown", kind: "published" as const, tool_names: [] };
    const scripted = { ...hubItemFixture(), id: "hub-script", name: "scripted", kind: "published" as const, tool_names: ["Add", "execute_tool_script"] };
    hubState.items = [hubItemFixture(), unknown, scripted];
    render(<TestApp initialPath="/gateways/new" />);
    await signInLocal(user);
    await waitFor(() => expect(screen.getByTestId("backend-unknown")).toBeDisabled());
    expect(screen.getByTestId("backend-unknown-names-unknown")).toHaveTextContent("이 게시된 Gateway 의 Tool 이름을 알 수 없어 선택할 수 없습니다.");
    const row = screen.getByTestId("backend-scripted").closest("li") as HTMLElement;
    expect(within(row).getByText("Tool 1개")).toBeInTheDocument();
  });

  it("a refused published backend is named in the save error, with the source Gateway for a forbid (P57 6.2)", async () => {
    authConfigState.mode = "local";
    const user = userEvent.setup();
    const published = { ...hubItemFixture(), id: "hub-pub", name: "orders-pub", kind: "published" as const };
    hubState.items = [hubItemFixture(), published];
    server.use(
      http.post("/gateways", () =>
        HttpResponse.json(
          { detail: { code: "published_owner_forbid", detail: "orders-pub: forbid", hub_item_id: "hub-pub", gateway: "orders" } },
          { status: 422 },
        ),
      ),
    );
    render(<TestApp initialPath="/gateways/new" />);
    await signInLocal(user);
    await user.type(await screen.findByTestId("gp-display-name"), "front");
    await user.click(await screen.findByTestId("backend-orders-pub"));
    await user.click(screen.getByTestId("save-button"));
    const alert = await screen.findByTestId("backend-error");
    expect(alert).toHaveTextContent(/^orders-pub: 이 게시된 Gateway 는 Authz policy \(Cedar\) 에 forbid 문장이 있어/);
    expect(alert).toHaveTextContent("원래 Gateway 는 orders 입니다.");
  });

  it("the definition tab shows the summary, revisions with a diff and the document bundle preview", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    await screen.findByTestId("gateway-detail-page");
    expect(screen.getByTestId("gw-revision")).toHaveTextContent("r2");
    expect(screen.getByTestId("gw-conflict")).toHaveTextContent("auto");
    expect(screen.getByTestId("gw-authz")).toHaveTextContent("(테넌트 기본값)");
    await waitFor(() => expect(screen.getByTestId("gw-backends")).toHaveTextContent("calc"));

    const diff = await screen.findByTestId("revision-diff");
    expect(within(diff).getByTestId("diff-conflict_resolution")).toHaveTextContent("priority");

    await user.click(screen.getByTestId("preview-button"));
    const yaml = await screen.findByTestId("preview-yaml");
    expect(yaml).toHaveTextContent("kind: VirtualMCPServer");
    expect(yaml).toHaveTextContent("kind: MCPGroup");
    expect(screen.getByTestId("preview-conflict")).toHaveTextContent("prefix");
    expect(screen.getByTestId("preview-warning")).toHaveTextContent("prefix mode");
  });

  it("editing makes a new revision", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support/edit" />);
    expect(await screen.findByTestId("revision-notice")).toHaveTextContent("r3");
    await waitFor(() => expect(screen.getByTestId("backend-calc")).toBeChecked());
    await user.click(screen.getByTestId("backend-calc-tool-Divide"));
    expect(screen.getByTestId("gp-surface-resources")).not.toBeChecked();
    expect(screen.getByTestId("backend-calc-timeout")).toHaveValue(90);
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("gateway-detail-page");
    const body = lastRequest("PUT", "/gateways/gw-support")?.body as { backends: { tools: string[]; timeout_seconds?: number | null }[] };
    expect(body.backends[0].tools).toEqual(["Add", "Divide"]);
    // 손대지 않은 백엔드 타임아웃은 저장된 값 그대로 간다 (P50 7절)
    expect(body.backends.map((b) => b.timeout_seconds)).toEqual([90, undefined]);
    expect((lastRequest("PUT", "/gateways/gw-support")?.body as { surface: unknown }).surface).toEqual({ resources: false, prompts: false });
  });

  it("the Code mode switch sends code_mode, shows the audit note while on and loads the stored value (P56 6.2·6.6)", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<TestApp initialPath="/gateways/new" />);
    await user.type(screen.getByTestId("gp-name"), "reader");
    await user.click(await screen.findByTestId("backend-calc"));
    const toggle = screen.getByTestId("gp-code-mode");
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/execute_tool_script Tool 을 함께 제공합니다/);
    expect(screen.queryByTestId("gp-code-mode-audit")).not.toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByTestId("gp-code-mode-audit")).toHaveTextContent("스크립트 하나는 이 Gateway 의 감사 기록과 메트릭에 execute_tool_script 호출 한 건으로 남습니다.");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("gateway-detail-page");
    expect(lastRequest("POST", "/gateways")?.body).toMatchObject({ name: "reader", code_mode: true });
    unmount();

    gatewayState.items = [gatewayFixture({ code_mode: true })];
    render(<TestApp initialPath="/gateways/gw-support/edit" />);
    await waitFor(() => expect(screen.getByTestId("gp-code-mode")).toBeChecked());
    await user.click(screen.getByTestId("gp-code-mode"));
    expect(screen.queryByTestId("gp-code-mode-audit")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("gateway-detail-page");
    expect((lastRequest("PUT", "/gateways/gw-support")?.body as { code_mode: unknown }).code_mode).toBe(false);
  });

  it("a refused Code mode save names the Tools that are not read-only (P56 6.2)", async () => {
    const user = userEvent.setup();
    server.use(
      http.put("/gateways/:id", () =>
        HttpResponse.json(
          { detail: { code: "code_mode_not_read_only", detail: "code mode needs read-only tools", names: { create_order: "write", web_fetch: "web" } } },
          { status: 422 },
        ),
      ),
    );
    render(<TestApp initialPath="/gateways/gw-support/edit" />);
    await waitFor(() => expect(screen.getByTestId("backend-calc")).toBeChecked());
    await user.click(screen.getByTestId("gp-code-mode"));
    await user.click(screen.getByTestId("save-button"));
    const alert = await screen.findByTestId("save-error");
    expect(alert).toHaveTextContent("이 Gateway 가 제공하는 Tool 가운데 읽기 전용이 아닌 Tool 이 있어 Code mode 를 켤 수 없습니다.");
    expect(within(alert).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["create_order", "web_fetch"]);
  });

  it("the definition shows code mode, and the metrics tab of a code-mode Gateway explains how scripts are counted (P56 6.6)", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    expect(await screen.findByTestId("gw-code-mode")).toHaveTextContent("no");
    await user.click(screen.getByTestId("tab-metrics"));
    expect(await screen.findByTestId("mcp-metrics-notes")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-metrics-note")).not.toBeInTheDocument();
    unmount();
    gatewayState.items = [gatewayFixture({ code_mode: true })];
    render(<TestApp initialPath="/gateways/gw-support?tab=definition" />);
    expect(await screen.findByTestId("gw-code-mode")).toHaveTextContent("yes");
    await user.click(screen.getByTestId("tab-metrics"));
    expect(await screen.findByTestId("mcp-metrics-note")).toHaveTextContent("스크립트 안의 Tool 호출은 각 백엔드의 기록에 남고, 그 기록에는 부른 사람 대신 이 Gateway 가 남습니다.");
  });

  it("a save that closes consumers names them on the detail page (consumer_notice, P57 6.2)", async () => {
    const user = userEvent.setup();
    server.use(
      http.put("/gateways/:id", async () => HttpResponse.json({ ...gatewayFixture(), warnings: [], rules_notice: null, consumer_notice: { closed: ["front", "ops"] } })),
    );
    render(<TestApp initialPath="/gateways/gw-support/edit" />);
    await waitFor(() => expect(screen.getByTestId("backend-calc")).toBeChecked());
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("consumer-notice")).toHaveTextContent(
      "다시 배포하면 이 Gateway 를 백엔드로 사용하는 Gateway(front, ops)의 호출을 모두 거절합니다. Authz policy (Cedar) 에 forbid 문장이 있기 때문입니다.",
    );
  });
});

describe("gateways (deployment)", () => {
  it("deploy refuses while backends are not ready and shows the report, then a cycle shows the path", async () => {
    const user = userEvent.setup();
    gatewayState.items = [gatewayDraftFixture({ id: "gw-nr", name: "not-ready" })];
    const app = render(<TestApp initialPath="/gateways/gw-nr?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    await user.click(within(await screen.findByTestId("deploy-dialog")).getByTestId("deploy-submit"));
    const report = await screen.findByTestId("backends-not-ready");
    expect(within(report).getByTestId("not-ready-weather")).toHaveTextContent("unreachable");

    app.unmount();
    gatewayState.items = [gatewayDraftFixture({ id: "gw-loop", name: "loop" })];
    render(<TestApp initialPath="/gateways/gw-loop?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    await user.click(within(await screen.findByTestId("deploy-dialog")).getByTestId("deploy-submit"));
    expect(await screen.findByTestId("cycle-path")).toHaveTextContent("gw-support → hub-pub-bot → gw-support");
  });

  it("deploys a draft and shows the warnings the deploy returned", async () => {
    const user = userEvent.setup();
    gatewayState.items = [gatewayDraftFixture()];
    render(<TestApp initialPath="/gateways/gw-draft?tab=deployment" />);
    await user.click(await screen.findByTestId("deployment-deploy"));
    await selectValue(user, "deploy-cluster", "cl-home");
    await user.click(within(await screen.findByTestId("deploy-dialog")).getByTestId("deploy-submit"));
    await waitFor(() => expect(lastRequest("POST", "/gateways/gw-draft/deploy")?.body).toEqual({ cluster_id: "cl-home", namespace: null, exposure: { enabled: false } }));
    expect(await screen.findByTestId("warning-network_policy_unenforced")).toBeInTheDocument();
  });

  it("the overview shows per-backend rows, conditions and the endpoint copy button", async () => {
    render(<TestApp initialPath="/gateways/gw-support" />);
    await screen.findByTestId("gateway-detail-page");
    expect(screen.getByTestId("status-phase")).toHaveTextContent("Degraded");
    expect(screen.getByTestId("status-badge")).toHaveTextContent("일부 백엔드 불가");
    expect(screen.getByTestId("status-backend-count")).toHaveTextContent("1/2");
    expect(screen.getByTestId("copy-url")).toBeInTheDocument();
    const rows = screen.getByTestId("backend-rows");
    expect(within(rows).getByTestId("backend-state-calc")).toHaveTextContent("ready");
    const weather = within(rows).getByTestId("backend-row-weather");
    expect(within(weather).getByTestId("backend-state-weather")).toHaveTextContent("unavailable");
    expect(weather).toHaveTextContent("connection refused");
    expect(within(screen.getByTestId("conditions-table")).getByTestId("condition-Ready")).toHaveTextContent("BackendUnavailable");
    // 문제인 condition 이 있으면 접힌 상세를 펼친 채로 연다 (D44 셋째)
    const details = screen.getByTestId("conditions-details");
    expect(details).toHaveTextContent("conditions 주의 1개");
    expect(details).toHaveAttribute("open");
    // 정의 요약은 백엔드 수와 충돌·실패 규칙, 런타임이다
    const summary = screen.getByTestId("definition-summary");
    expect(within(summary).getByTestId("status-backend-count")).toHaveTextContent("1/2");
    expect(within(summary).getByTestId("summary-conflict")).toHaveTextContent("auto");
    expect(within(summary).getByTestId("summary-failure")).toHaveTextContent("best_effort");
    expect(within(screen.getByTestId("address-card")).getByTestId("address-auth-badge")).toHaveTextContent("anonymous");
  });

  it("without tool names in the deployed snapshot the tools card gives way to the prefix warning, and hub_access_revoked becomes a badge", async () => {
    // 이름이 있는 기본 스냅샷의 Tool 카드는 게시 테스트가 같은 배포 탭에서 본다
    const base = gatewayFixture();
    gatewayState.items = [
      gatewayFixture({
        deployed_backends: base.deployed_backends.map((b) => ({ ...b, tools: [], tool_names: [] })),
        // 서버가 실제로 내는 문장이다. 코드 접두사가 없는 경고라 문장 조각으로 알아본다
        status: { ...base.status!, warnings: ["some backends have no tool_names; falling back to prefix mode (tool names become <workload>_<tool>)", "hub_access_revoked: weather"] },
      }),
    ];
    render(<TestApp initialPath="/gateways/gw-support?tab=deployment" />);
    await screen.findByTestId("gateway-detail-page");
    expect(screen.queryByTestId("tools-card")).not.toBeInTheDocument();
    expect(screen.getByTestId("status-warning")).toHaveTextContent("prefix 모드로 전환됩니다");
    expect(screen.getByTestId("hub-access-revoked-badge-weather")).toHaveTextContent("접근 회수됨: weather");
  });

  it("says in words that a backend's image was refused (P55 6절)", async () => {
    const base = gatewayFixture();
    gatewayState.items = [gatewayFixture({ status: { ...base.status!, warnings: ["hub_backend_refused: weather"] } })];
    render(<TestApp initialPath="/gateways/gw-support?tab=deployment" />);
    expect(await screen.findByTestId("status-warning")).toHaveTextContent("플랫폼 관리자가 이미지를 거부한 허브 항목이 있습니다.");
  });

  it("undeploy keeps the definition and clone copies it into a new draft", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    await screen.findByTestId("gateway-detail-page");
    await user.click(screen.getByTestId("undeploy-button"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("다른 테넌트가 공유한 허브 항목의 복제본은 바로 삭제하지 않고, 사용하는 Gateway 가 없어지면 잠시 뒤 정리합니다");
    await submitConfirm(user);
    await waitFor(() => expect(screen.getByTestId("state-badge")).toHaveTextContent("미배포"));

    await user.click(screen.getByTestId("clone-button"));
    const dialog = await screen.findByTestId("clone-dialog");
    expect(within(dialog).getByTestId("clone-name")).toHaveValue("support-copy");
    // 노출을 함께 정하면 그 설정이 복제 본문에 실린다. 노출용 둘째를 한 호출로 만드는 자리다 (D42)
    await user.click(within(dialog).getByTestId("exp-enabled"));
    await user.clear(within(dialog).getByTestId("exp-host"));
    await user.type(within(dialog).getByTestId("exp-host"), "support-copy.example.com");
    await user.click(within(dialog).getByTestId("clone-submit"));
    await waitFor(() =>
      expect(lastRequest("POST", "/gateways/gw-support/clone")?.body).toMatchObject({
        name: "support-copy",
        exposure: { enabled: true, host: "support-copy.example.com" },
      }),
    );
  });

  it("the tools card reads the deployed snapshot; publish posts mode and name, links the item, and unpublish deletes it from the hub", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support?tab=deployment" />);
    const tools = await screen.findByTestId("tools-card");
    expect(within(tools).getByTestId("tools-count")).toHaveTextContent("Tool 1개");
    // 광고 이름을 정하는 것은 배포할 때 풀린 값이다. 정의 쪽은 auto 라 그것을 적으면 무엇으로 광고하는지 모른다
    expect(within(tools).getByTestId("tools-count")).toHaveTextContent("충돌 해결 priority");
    expect(within(tools).getByTestId("tool-Add")).toBeInTheDocument();
    expect(within(tools).queryByTestId("tool-Divide")).not.toBeInTheDocument();

    const card = await screen.findByTestId("publish-card");
    expect(within(card).getByTestId("published-empty")).toBeInTheDocument();
    // header 모드의 Gateway 에는 소비자가 규칙을 지킨다는 안내가 없다 (P57 6.2절)
    expect(within(card).queryByTestId("publish-consumer-hint")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("publish-mode")).not.toBeInTheDocument();
    await user.clear(screen.getByTestId("publish-display-name"));
    await user.type(screen.getByTestId("publish-display-name"), "Bot Tools");
    expect(screen.getByTestId("publish-name")).toHaveValue("bot-tools");
    await user.click(screen.getByTestId("publish-button"));
    await waitFor(() => expect(lastRequest("POST", "/gateways/gw-support/publish")?.body).toEqual({ mode: "entry", name: "bot-tools", display_name: "Bot Tools" }));
    expect(await screen.findByTestId("published-link-bot-tools")).toHaveAttribute("href", "/hub/hub-pub-bot-tools");

    await user.click(screen.getByTestId("unpublish-bot-tools"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("Hub item 게시 해제");
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/hub/hub-pub-bot-tools")).toBeDefined());
  });

  it("under platform tokens the publish card says that consumers enforce this Gateway's Tool rules", async () => {
    authConfigState.mode = "local";
    gatewayState.items = [gatewayFixture({ auth: "local" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support?tab=deployment" />);
    await signInLocal(user);
    const card = await screen.findByTestId("publish-card");
    expect(within(card).getByTestId("publish-consumer-hint")).toHaveTextContent(
      "다른 Gateway 가 이 Gateway 를 백엔드로 사용하면 그 Gateway 가 이 Gateway 의 Tool 규칙을 함께 지킵니다.",
    );
  });

  it("delete is refused while the gateway is published, and says which item blocks it", async () => {
    const user = userEvent.setup();
    hubState.items = [...hubState.items, { ...hubItemFixture(), id: "hub-pub-bot", name: "bot", kind: "published", source: { ...hubItemFixture().source, instance_id: null, gateway_instance_id: "gw-support" } }];
    render(<TestApp initialPath="/gateways/gw-support" />);
    await screen.findByTestId("gateway-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    // 누르기 전에 무엇이 막는지 이름 링크로 미리 보여 준다
    const blocked = await within(dialog).findByTestId("delete-published-warning");
    expect(within(blocked).getByTestId("delete-published-item-bot")).toHaveAttribute("href", "/hub/hub-pub-bot");
    await submitConfirm(user, "support");
    expect(await screen.findByTestId("confirm-error")).toHaveTextContent("허브에서 게시를 먼저 해제해 주세요");

    hubState.items = [hubItemFixture(), hubRemote];
    await submitConfirm(user, "support");
    expect(await screen.findByTestId("gateways-page")).toBeInTheDocument();
  });

  it("the Advanced tab shows the three-document bundle and saves vMCP overrides only", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support?tab=advanced" />);
    const editor = await screen.findByTestId("advanced-editor");
    expect(within(editor).getByTestId("doc-tab-MCPGroup")).toBeInTheDocument();
    expect(within(editor).getByTestId("doc-tab-VirtualMCPServer")).toBeInTheDocument();

    const monaco = within(editor).getByTestId("monaco") as HTMLTextAreaElement;
    await user.type(monaco, "\nx: 1");
    await user.click(within(editor).getByTestId("save-button"));
    expect(await within(editor).findByTestId("error-panel")).toHaveTextContent("VirtualMCPServer 문서에만");

    await user.click(within(editor).getByTestId("doc-tab-VirtualMCPServer"));
    const doc = screen.getByTestId("advanced-doc-VirtualMCPServer");
    const vmcp = within(doc).getByTestId("monaco") as HTMLTextAreaElement;
    await user.clear(vmcp);
    await user.type(vmcp, "kind: VirtualMCPServer\nspec:\n  replicas: 2\n");
    await user.click(within(doc).getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PUT", "/gateways/gw-support/overrides")?.body).toEqual({ yaml: expect.stringContaining("replicas: 2") }));
  });

  it("polls while deployed and keeps last data through transient failures", async () => {
    tickingClock();
    render(<TestApp initialPath="/gateways/gw-support" />);
    expect(await screen.findByTestId("status-phase")).toHaveTextContent("Degraded");
    gatewayState.items = [gatewayFixture({ status: { ...gatewayFixture().status!, phase: "Ready", badge: null } })];
    await advanceClock(DEPLOY_POLL_MS + 100);
    await waitFor(() => expect(screen.getByTestId("status-phase")).toHaveTextContent("Ready"));

    server.use(http.get("/gateways/:id", () => HttpResponse.error()));
    await advanceClock(DEPLOY_POLL_MS + 100);
    await waitFor(() => expect(screen.getByTestId("status-stale")).toBeInTheDocument());
    expect(screen.getByTestId("status-phase")).toHaveTextContent("Ready");
  });

  it("hub detail links a published item back to its gateway", async () => {
    hubState.items = [...hubState.items, { ...hubItemFixture(), id: "hub-pub-bot", name: "bot", kind: "published", source: { ...hubItemFixture().source, instance_id: null, gateway_instance_id: "gw-support" } }];
    render(<TestApp initialPath="/hub/hub-pub-bot" />);
    await screen.findByTestId("hub-detail-page");
    expect(screen.getByTestId("origin-gateway-link")).toHaveAttribute("href", "/gateways/gw-support");
  });
});
