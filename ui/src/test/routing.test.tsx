import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { initials } from "@/components/AccountMenu";
import { NAV_GROUPS, NAV_ITEMS, navLabel } from "@/components/Layout";
import { dictionary } from "@/i18n";

import { http, HttpResponse, server } from "./msw";

describe("sidebar rail collapse", () => {
  it("collapses the whole sidebar to an icon rail, keeps items reachable by title, and remembers it", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("nav-mcp-hub");
    const sidebar = screen.getByTestId("sidebar");
    const toggle = screen.getByTestId("sidebar-toggle");
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("nav-agents")).toHaveTextContent("Agents");
    expect(screen.getByTestId("tenant-current")).toHaveTextContent("default");

    await user.click(toggle);
    expect(sidebar).toHaveAttribute("data-collapsed", "true");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // 레일에서는 라벨이 사라지고 title·aria-label 로만 남는다. testid 는 그대로다
    const item = screen.getByTestId("nav-agents");
    expect(item).toHaveTextContent("");
    expect(item).toHaveAttribute("title", "Agents");
    expect(item).toHaveAttribute("aria-label", "Agents");
    // 레일의 테넌트 전환은 머리글자 칸 하나이고 이름은 title 이다. 계정 줄은 아바타 하나지만 같은 메뉴를 연다
    expect(screen.queryByTestId("tenant-current")).not.toBeInTheDocument();
    expect(screen.getByTestId("tenant-select")).toHaveAttribute("title", "default");
    await user.click(screen.getByTestId("account-menu"));
    expect(screen.getByTestId("account-menu-panel")).toBeVisible();
    expect(screen.getByTestId("theme-toggle")).toBeVisible();
    // 접힌 묶음(관리)의 항목도 레일에서는 전부 보인다
    expect(screen.getByTestId("nav-clusters")).toBeInTheDocument();
    expect(window.localStorage.getItem("daegam.nav.sidebar")).toBe("collapsed");

    unmount();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("nav-mcp-hub");
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true");
    await user.click(screen.getByTestId("sidebar-toggle"));
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");
    expect(window.localStorage.getItem("daegam.nav.sidebar")).toBe("expanded");
  });
});

describe("sidebar auto-collapse on the agent canvas", () => {
  it("collapses on the standalone canvas and restores the previous state when leaving", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents" />);
    await screen.findByTestId("nav-mcp-hub");
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");

    await screen.findByTestId("agents-page");
    await user.click(screen.getByTestId("agent-new"));
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    // 자동 접힘은 저장하지 않는다
    expect(window.localStorage.getItem("daegam.nav.sidebar")).toBeNull();

    await user.click(screen.getByTestId("nav-mcp-hub"));
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false"));
  });

  it("저장된 Agent 는 정의 탭에서만 접고 다른 탭에서는 편다 (D42)", async () => {
    const user = userEvent.setup();
    // 상세를 여는 것만으로 접으면 캔버스를 보려던 것이 아닌 사람까지 접힌 화면을 받는다.
    // 정의 탭을 고르는 것은 캔버스를 보겠다는 뜻이라 그때만 든다
    render(<TestApp initialPath="/agents/ag-support" />);
    await screen.findByTestId("nav-mcp-hub");
    await screen.findByTestId("agent-detail-page");
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");

    await user.click(screen.getByTestId("tab-definition"));
    expect(await screen.findByTestId("agent-canvas-page")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    // 만들기 화면과 같이 자동 접힘은 저장하지 않는다
    expect(window.localStorage.getItem("daegam.nav.sidebar")).toBeNull();
    // 제목과 동작 줄도 접혀 세로가 캔버스로 간다
    expect(screen.queryByTestId("page-title")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("tab-overview"));
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false"));
    expect(screen.getByTestId("page-title")).toBeInTheDocument();
  });

  it("keeps the sidebar open if the user expands it on the canvas", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/new" />);
    await screen.findByTestId("nav-mcp-hub");
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    await user.click(screen.getByTestId("sidebar-toggle"));
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");
    await user.click(screen.getByTestId("nav-mcp-hub"));
    await waitFor(() => expect(screen.getByTestId("nav-mcp-hub")).toBeInTheDocument());
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false");
  });
});

describe("sidebar group collapse", () => {
  it("collapses and expands only the 관리 group, with its role items, and remembers it", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("nav-mcp-hub");
    // Tool 과 Agent 묶음은 접히지 않는다 (P81 5절)
    expect(screen.queryByTestId("nav-group-tool")).not.toBeInTheDocument();
    expect(screen.queryByTestId("nav-group-agent")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("sidebar")).getByText("Tool")).toBeInTheDocument();

    const toggle = screen.getByTestId("nav-group-admin");
    expect(toggle).toHaveTextContent("관리");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // 관리 묶음의 차례: 테넌트 설정, 테넌트 범위의 role 항목, Clusters, 구분선 아래 플랫폼 관리자 항목
    const sidebar = screen.getByTestId("sidebar");
    const order = ["nav-settings", "nav-audit", "nav-usage", "nav-clusters", "nav-admin-usage"].map((id) => within(sidebar).getByTestId(id));
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(within(screen.getByTestId("role-nav-platform")).getByTestId("nav-admin-settings")).toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    for (const item of ["nav-settings", "nav-clusters", "nav-audit", "nav-admin-settings"]) expect(screen.queryByTestId(item)).not.toBeInTheDocument();
    // Agent 묶음으로 옮긴 Database 는 그대로 보인다
    expect(screen.getByTestId("nav-data-services")).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem("daegam.nav.collapsed") ?? "[]")).toEqual(["Admin"]);
    await user.click(toggle);
    expect(screen.getByTestId("nav-settings")).toBeInTheDocument();
  });

  it("keeps the 관리 group open while one of its screens is active even if collapsed", async () => {
    window.localStorage.setItem("daegam.nav.collapsed", JSON.stringify(["Admin"]));
    render(<TestApp initialPath="/clusters" />);
    await screen.findByTestId("clusters-page");
    expect(screen.getByTestId("nav-clusters")).toBeInTheDocument();
    expect(screen.getByTestId("nav-group-admin")).toHaveAttribute("aria-expanded", "true");
  });
});

describe("top bar trail", () => {
  it("names the group and the page on a list, and links back from a detail with its display name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-servers-page");
    const trail = () => screen.getByTestId("trail");
    expect(trail()).toHaveTextContent("Tool/MCP Servers");
    expect(within(trail()).getByText("MCP Servers")).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByTestId("nav-agents"));
    await screen.findByTestId("agents-page");
    expect(trail()).toHaveTextContent("Agent/Agents");

    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    await screen.findByTestId("backend-api-detail-page");
    const trails = screen.getAllByTestId("trail");
    const detail = trails[trails.length - 1];
    await waitFor(() => expect(detail).toHaveTextContent("Tool/Backend APIs/Orders SOAP"));
    expect(within(detail).getByRole("link", { name: "Backend APIs" })).toHaveAttribute("href", "/backend-apis");
  });
});

describe("account menu", () => {
  it("draws initials from the part before @ and from the first two parts of a name", () => {
    expect(initials("dev@example.com")).toBe("D");
    expect(initials("jane.doe@example.com")).toBe("JD");
    expect(initials("header-admin")).toBe("HA");
    expect(initials("홍길동")).toBe("홍");
    expect(initials("")).toBe("?");
  });

  it("keeps the header auth mode readable on the rail the home chat folds to", async () => {
    // 스모크가 "/" 로 들어와 홈 챗(레일)에서 login-placeholder 를 읽는다
    render(<TestApp initialPath="/" />);
    await screen.findByTestId("home-chat-page");
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true");
    expect(await screen.findByTestId("login-placeholder")).toHaveTextContent("헤더 인증 모드");
  });

  it("creates a tenant from the rail through a floating form", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);
    await screen.findByTestId("home-chat-page");
    await user.click(screen.getByTestId("tenant-add"));
    await user.type(screen.getByTestId("tenant-input"), "-bad");
    await user.click(screen.getByTestId("tenant-add-submit"));
    expect(screen.getByTestId("tenant-selector")).toHaveTextContent("이름은 영숫자로 시작하고");
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("tenant-input")).not.toBeInTheDocument();
    expect(screen.getByTestId("tenant-add")).toHaveFocus();

    await user.click(screen.getByTestId("tenant-add"));
    await user.type(screen.getByTestId("tenant-input"), "acme");
    await user.click(screen.getByTestId("tenant-add-submit"));
    expect(screen.getByTestId("tenant-select")).toHaveValue("acme");
  });

  it("closes when focus leaves the account row and the menu", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("nav-mcp-hub");
    await user.click(screen.getByTestId("account-menu"));
    expect(screen.getByTestId("account-menu-panel")).toBeVisible();
    await user.tab({ shift: true });
    expect(screen.getByTestId("account-menu-panel")).not.toBeVisible();
  });

  it("holds the account links, theme, language, API docs and versions, and closes on Escape and on navigation", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("nav-mcp-hub");
    const trigger = screen.getByTestId("account-menu");
    const panel = screen.getByTestId("account-menu-panel");
    // 머리 줄에는 계정 링크가 없고, 계정 줄이 header 모드를 말한다
    expect(within(screen.getByTestId("topbar")).queryByTestId("approval-choices-link")).not.toBeInTheDocument();
    expect(screen.getByTestId("login-placeholder")).toHaveTextContent("헤더 인증 모드");
    expect(panel).not.toBeVisible();

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(panel).toBeVisible();
    for (const id of ["approval-choices-link", "platform-api-docs", "theme-toggle", "language-switch", "version-badge"]) {
      expect(within(panel).getByTestId(id)).toBeVisible();
    }
    expect(within(panel).getByTestId("theme-current")).toHaveTextContent("Light");
    await user.keyboard("{Escape}");
    expect(panel).not.toBeVisible();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(within(panel).getByTestId("approval-choices-link"));
    await waitFor(() => expect(panel).not.toBeVisible());
  });
});

describe("routing", () => {
  it("renders the grouped sidebar and redirects / to the home chat", async () => {
    // 홈 챗은 사이드바를 자동으로 접으므로(D45) 라벨은 글자가 아니라 aria-label 로 있다
    const home = render(<TestApp initialPath="/" />);
    expect(await screen.findByTestId("home-chat-page")).toBeInTheDocument();
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true");
    for (const item of NAV_ITEMS) {
      expect(within(screen.getByTestId("sidebar")).getByTestId(item.testId)).toHaveAttribute("aria-label", navLabel(dictionary("ko"), item));
    }
    home.unmount();

    // 펼친 사이드바에서는 같은 라벨이 글자로 보인다
    render(<TestApp initialPath="/hub" />);
    const sidebar = screen.getByTestId("sidebar");
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    for (const item of NAV_ITEMS) {
      expect(within(sidebar).getByTestId(item.testId)).toHaveTextContent(navLabel(dictionary("ko"), item));
    }
    // 리소스 넷이 정의와 배포를 함께 가지면서 nav 도 여덟에서 넷으로 줄었다 (D42).
    // Gateway 는 vMCP 이고 외부 클라이언트도 부르므로 Tool 묶음에 든다
    expect(NAV_GROUPS.map((g) => g.id)).toEqual([null, "Tool", "Agent", "Admin"]);
    const ids = NAV_ITEMS.map((i) => i.testId);
    // 맨 위는 "새 대화" 하나이고 MCP Hub 는 Tool 묶음의 첫 항목이다 (P81 5절)
    expect(ids.slice(0, 2)).toEqual(["nav-home-chat", "nav-mcp-hub"]);
    expect(NAV_GROUPS[0].items.map((i) => i.testId)).toEqual(["nav-home-chat"]);
    expect(ids).toContain("nav-backend-apis");
    expect(ids).toContain("nav-mcp-servers");
    expect(ids).toContain("nav-gateways");
    expect(ids).toContain("nav-agents");
    expect(ids.indexOf("nav-backend-apis")).toBeLessThan(ids.indexOf("nav-mcp-servers"));
    expect(ids.indexOf("nav-gateways")).toBeLessThan(ids.indexOf("nav-agents"));
    expect(ids.indexOf("nav-agents")).toBeLessThan(ids.indexOf("nav-data-services"));
  });

  it("navigates between sections through the sidebar", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis" />);
    await user.click(screen.getByTestId("nav-data-services"));
    expect(await screen.findByTestId("data-services-page")).toBeInTheDocument();
    await user.click(screen.getByTestId("nav-clusters"));
    expect(await screen.findByTestId("clusters-page")).toBeInTheDocument();
    await user.click(screen.getByTestId("nav-mcp-servers"));
    expect(await screen.findByTestId("mcp-servers-page")).toBeInTheDocument();
    expect(screen.getByTestId("page-title")).toHaveTextContent("MCP Servers");
    await user.click(screen.getByTestId("nav-mcp-hub"));
    expect(await screen.findByTestId("hub-page")).toBeInTheDocument();
    await user.click(screen.getByTestId("nav-gateways"));
    expect(await screen.findByTestId("gateways-page")).toBeInTheDocument();
    await user.click(screen.getByTestId("nav-agents"));
    expect(await screen.findByTestId("agents-page")).toBeInTheDocument();
  });

  it("serves one screen per resource, with the canvas standing alone only for a new agent", async () => {
    const cases: [string, string][] = [
      ["/backend-apis", "backend-apis-page"],
      ["/backend-apis/bp-orders", "backend-api-detail-page"],
      ["/mcp-servers", "mcp-servers-page"],
      ["/mcp-servers/ms-calc", "mcp-server-detail-page"],
      ["/mcp-servers/ms-calc/edit", "mcp-server-form-page"],
      ["/gateways", "gateways-page"],
      ["/gateways/gw-support", "gateway-detail-page"],
      ["/gateways/gw-support/edit", "gateway-form-page"],
      ["/agents", "agents-page"],
      ["/agents/new", "agent-canvas-page"],
      ["/agents/ag-support", "agent-detail-page"],
    ];
    for (const [path, testId] of cases) {
      const { unmount } = render(<TestApp initialPath={path} />);
      expect(await screen.findByTestId(testId)).toBeInTheDocument();
      unmount();
    }
  });

  it("serves deep links without leaving the layout", async () => {
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    expect(await screen.findByTestId("backend-api-detail-page")).toBeInTheDocument();
    // 헤더는 표시 이름이고 K8s 이름은 고정폭 보조로 그 아래에 온다 (D24)
    expect(screen.getByTestId("page-title")).toHaveTextContent("Orders SOAP");
    expect(screen.getByTestId("page-name")).toHaveTextContent("orders");
  });

  it("shows the API build from GET /version and flags a tab whose bundle is not the served UI build", async () => {
    render(<TestApp initialPath="/clusters" />);
    // API 와 UI 는 따로 배포된다(D52). API 빌드는 /api/version, 서빙 중인 UI 빌드는 UI 컨테이너의 /build.json 이다
    await waitFor(() => expect(screen.getByTestId("api-version")).toHaveTextContent("0.1.0+test-api"));
    expect(screen.getByTestId("ui-build").textContent).not.toBe("");
    expect(await screen.findByTestId("ui-build-stale")).toHaveTextContent("test");
    // 릴리스로 구운 이미지가 아니면 릴리스 줄이 없다
    expect(screen.queryByTestId("release")).not.toBeInTheDocument();
  });

  it("shows the release tag the platform image was built for", async () => {
    server.use(
      http.get("/version", () =>
        HttpResponse.json({ api_version: "0.1.0", api_build: "0.1.0+test-api", release: "1.0.0-rc3" }),
      ),
    );
    render(<TestApp initialPath="/clusters" />);
    expect(await screen.findByTestId("release")).toHaveTextContent("1.0.0-rc3");
  });
});
