import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

import { TestApp } from "@/App";
import { stubPlainHttpCopy } from "./clipboard";
import { submitConfirm } from "./confirm";
import { DEPLOY_POLL_MS } from "@/lib/deployment";
import { HttpResponse, apiError, countExact, http, hubItemFixture, hubSharedReadOnly, hubState, lastRequest, meDeveloper, reference, referencesState, resetReferencesState, server } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

/**
 * 허브 상세는 탭 넷이다 (D48). 개요가 아닌 탭의 것을 보려면 먼저 연다.
 * 탭은 `?tab=` 에 남으므로 처음부터 그 주소로 들어가도 되지만, 사람이 하는 대로 눌러서 연다.
 */
async function openTab(user: UserEvent, tab: "tools" | "access" | "manifest"): Promise<void> {
  await user.click(await screen.findByTestId(`tab-${tab}`));
}

/** 서버가 지금 답하는 도구가 Add 하나인 `/hub/:id/tools` 응답. 상한에 걸려 잘렸는지만 고른다. */
function oneLiveTool(truncated: boolean) {
  return http.get("/hub/:id/tools", () =>
    HttpResponse.json({ tools: [{ name: "Add", title: null, description: null, input_schema: null, schema_omitted: true, output_schema: false }], listed: true, reason: null, truncated }),
  );
}

describe("mcp hub", () => {
  it("lists items with kind badge, status, tool count and scope", async () => {
    render(<TestApp initialPath="/hub" />);
    const internal = await screen.findByTestId("hub-row-calc");
    expect(internal).toHaveTextContent("internal");
    expect(within(internal).getByTestId("hub-status-calc")).toHaveTextContent("Ready");
    expect(within(internal).getByTestId("hub-tools-calc")).toHaveTextContent("2");
    expect(internal).toHaveTextContent("tenant");
    const remote = screen.getByTestId("hub-row-weather");
    expect(within(remote).getByTestId("hub-status-weather")).toHaveTextContent("Pending");
    expect(within(remote).getByTestId("hub-tools-weather")).toHaveTextContent("아직 모름");
  });

  it("registers a remote item with url, and an image item with port and tools_filter", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    await user.type(screen.getByTestId("hub-name"), "ext");
    await user.type(screen.getByTestId("hub-url"), "https://mcp.example.com/mcp");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("hub-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "ext",
      kind: "remote",
      cluster_id: null,
      url: "https://mcp.example.com/mcp",
    });
  });

  it("image kind swaps url for image/port/tools_filter fields", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    await selectValue(user, "hub-kind", "image");
    expect(screen.queryByTestId("hub-url")).not.toBeInTheDocument();
    await user.type(screen.getByTestId("hub-name"), "boxed");
    await user.type(screen.getByTestId("hub-image"), "ghcr.io/acme/mcp:1");
    await user.type(screen.getByTestId("hub-port"), "9000");
    await user.type(screen.getByTestId("hub-tools-filter"), "Add, Divide");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "boxed",
      kind: "image",
      cluster_id: null,
      image: "ghcr.io/acme/mcp:1",
      port: 9000,
      tools_filter: ["Add", "Divide"],
    });
  });

  it("detail shows status, backend url and manifest YAML; refresh-tools updates the tool list", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-weather" />);
    await screen.findByTestId("hub-detail-page");
    expect(screen.getByTestId("hub-kind")).toHaveTextContent("remote");
    expect(screen.getByTestId("hub-ready")).toHaveTextContent("no");
    // 남의 서버를 가리키는 종류는 도구 새로고침 버튼이 그대로 있다. internal 에는 없다
    expect(await screen.findByTestId("refresh-tools-button")).toBeInTheDocument();
    await openTab(user, "tools");
    expect(screen.getByTestId("tools-unknown")).toHaveTextContent("Tool 새로고침");
    await openTab(user, "manifest");
    expect(await screen.findByTestId("hub-manifest-MCPRemoteProxy")).toHaveTextContent("kind: MCPRemoteProxy");

    // 도구 새로고침은 머리의 버튼이라 어느 탭에서든 누를 수 있다
    await user.click(screen.getByTestId("refresh-tools-button"));
    await waitFor(() => expect(lastRequest("POST", "/hub/hub-weather/refresh-tools")).toBeDefined());
    await openTab(user, "tools");
    expect(await screen.findByTestId("tool-list")).toHaveTextContent("GetWeather");
    expect(screen.queryByTestId("tools-unknown")).not.toBeInTheDocument();
  });

  it("아직 Ready 가 아닌 항목은 준비될 때까지 다시 묻고, 준비되면 멈춘다", async () => {
    tickingClock();
    render(<TestApp initialPath="/hub/hub-weather" />);
    expect(await screen.findByTestId("hub-ready")).toHaveTextContent("no");
    const gets = () => countExact("GET", "/hub/hub-weather");
    hubState.items = hubState.items.map((i) => (i.id === "hub-weather" ? { ...i, status: { ...i.status!, phase: "Ready", ready: true, message: null } } : i));
    await advanceClock(DEPLOY_POLL_MS + 100);
    await waitFor(() => expect(screen.getByTestId("hub-ready")).toHaveTextContent("yes"));
    const settled = gets();
    await advanceClock(DEPLOY_POLL_MS * 3);
    expect(gets()).toBe(settled);
  });

  it("internal 항목은 원본 MCP Server 로 이어지고 자동 등록 안내와 도구 새로고침 버튼이 없으며, 사람이 쓰는 MCP 주소를 복사하고 게시 해제할 수 있다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-calc" />);
    await screen.findByTestId("hub-detail-page");
    expect(screen.getByTestId("origin-instance-link")).toHaveAttribute("href", "/mcp-servers/ms-calc");
    expect(screen.getByTestId("goto-instance-button")).toHaveAttribute("href", "/mcp-servers/ms-calc");
    expect(screen.queryByTestId("internal-hint")).not.toBeInTheDocument();
    // internal 의 도구 이름은 우리 리비전에서 계산한다 (D49). 라이브 값을 저장할 자리가 없어 새로고침이
    // 할 일이 없고, 라이브는 도구 탭이 저장 없이 보인다 (D48)
    expect(screen.queryByTestId("refresh-tools-button")).not.toBeInTheDocument();

    const row = await screen.findByTestId("hub-mcp-url");
    expect(row).toHaveTextContent("https://calc.example.com/mcp");
    // 평문 http 에서는 navigator.clipboard 가 없다. 그때도 폴백으로 복사돼야 한다
    const exec = stubPlainHttpCopy(true);
    await user.click(screen.getByTestId("copy-mcp-url"));
    await waitFor(() => expect(exec).toHaveBeenCalledWith("copy"));
    expect(await screen.findByTitle("복사됨")).toBeInTheDocument();
    // Gateway 가 묶을 때 쓰는 클러스터 안 주소는 따로 남는다
    expect(screen.getByTestId("hub-backend-url")).toHaveTextContent("svc:8080/mcp");

    // 게시는 수동이므로 internal 도 해제할 수 있다 (D23). 해제는 DELETE /hub/{id} 다
    expect(screen.getByTestId("delete-button")).toHaveTextContent("게시 해제");
    await user.click(screen.getByTestId("delete-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("MCP Server 자체는 그대로 남습니다");
    await submitConfirm(user);
    expect(await screen.findByTestId("hub-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/hub/hub-calc")).toBeDefined();
  });

  it("도구 탭은 그 자리에서 물어본 스키마를 접었다 펴 보이고, 못 물어보면 저장된 이름으로 떨어진다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-calc?tab=tools" />);
    // calc 는 Ready 라 라이브 조회가 성공한다
    await screen.findByTestId("tool-schemas");
    const add = screen.getByTestId("tool-detail-Add");
    expect(add).toHaveTextContent("두 수 더하기");
    // 서버가 말하는 readOnlyHint 는 보이지 않는다. 믿을 수 없는 서버의 힌트라 명세가 판단에 쓰지
    // 말라고 못박는데, 배지로 달면 사람이 보증으로 읽는다
    expect(within(add).queryByText("읽기 전용")).not.toBeInTheDocument();
    // 접힌 줄에도 설명 첫 줄이 보인다. jsdom 은 details 를 접어도 내용이 DOM 에 남으므로
    // "감춰졌다" 는 텍스트가 아니라 open 속성으로 단정한다
    expect(add).toHaveTextContent("a 와 b 를 더한다.");
    expect(add).not.toHaveAttribute("open");
    await user.click(within(add).getByText("Add"));
    expect(add).toHaveAttribute("open");
    const args = within(add).getByTestId("tool-args-Add");
    expect(args).toHaveTextContent("a");
    expect(args).toHaveTextContent("integer");
    expect(args).toHaveTextContent("왼쪽 값");
    // 배열은 원소 타입까지 적어야 무엇을 넣는지 안다
    expect(args).toHaveTextContent("string[]");
    // 필수는 색이 아니라 낱말로도 뜻이 선다
    expect(within(args).getByText("필수")).toBeInTheDocument();

    // weather 는 아직 Ready 가 아니라 못 물어본다. 오류가 아니라 저장된 이름으로 떨어진다
    render(<TestApp initialPath="/hub/hub-weather?tab=tools" />);
    expect(await screen.findByTestId("tools-not-listed")).toHaveTextContent("마지막으로 저장된 이름만 보입니다");
    expect(screen.getByTestId("tools-unknown")).toBeInTheDocument();
  });

  it("consume 이 없으면 도구를 물어보지 않고 이름만 보인다", async () => {
    hubState.items = [hubSharedReadOnly];
    render(<TestApp initialPath="/hub/hub-shared-docs?tab=tools" />);
    expect(await screen.findByTestId("tools-need-consume")).toHaveTextContent("consume 권한이 필요합니다");
    expect(screen.queryByTestId("tool-schemas")).not.toBeInTheDocument();
  });

  it("저장된 도구 이름이 방금 본 것과 다르면 어느 쪽에 무엇이 있는지와 고칠 길을 말한다", async () => {
    // image 종류는 등록 때의 tools_filter 가 그대로 tool_names 라 새로고침 전에는 실제와 다르다
    hubState.items = [{ ...hubItemFixture(), id: "hub-img", name: "boxed", kind: "image", tool_names: ["Add"] }];
    render(<TestApp initialPath="/hub/hub-img?tab=tools" />);
    const note = await screen.findByTestId("tools-stale");
    expect(note).toHaveTextContent("서버에만 Extra가 있습니다");
    expect(note).toHaveTextContent("Tool 새로고침으로 맞춰 주세요");
    // 이름표의 수는 저장된 1 이 아니라 방금 본 것이다
    expect(screen.getByTestId("tab-tools")).toHaveTextContent("2");
  });

  it("조회 자체가 실패하면 '도구가 없다' 가 아니라 실패했다고 말한다", async () => {
    // q.data 가 없으면 아래 갈래가 모두 조용해 "도구 목록이 아직 없습니다" 로 보였다. 그것은
    // 서버가 도구를 안 준 것이고, 여기는 우리가 못 물어본 것이라 서로 다른 사실이다
    server.use(http.get("/hub/:id/tools", () => apiError(502, "cluster_unreachable", "Max retries exceeded")));
    render(<TestApp initialPath="/hub/hub-calc?tab=tools" />);
    expect(await screen.findByTestId("tools-error")).toBeInTheDocument();
  });

  it("응답 상한에 걸려 잘린 목록은 전부가 아니라고 말한다", async () => {
    server.use(oneLiveTool(true));
    render(<TestApp initialPath="/hub/hub-calc?tab=tools" />);
    expect(await screen.findByTestId("tools-truncated")).toHaveTextContent("앞의 1개만 보입니다");
  });

  it("파생 항목의 어긋남은 개수가 같아도 이름이 바뀐 것을 잡고, 없는 새로고침 버튼 대신 원본을 종류대로 짚어 다시 배포하라고 말한다", async () => {
    // internal·published 에는 도구 새로고침이 없다 (D49). 그런데도 누르라고 하면 없는 버튼을 찾게 된다
    hubState.items = [{ ...hubItemFixture(), tool_names: ["add_numbers", "Divide"] }];
    const app = render(<TestApp initialPath="/hub/hub-calc?tab=tools" />);
    const note = await screen.findByTestId("tools-stale");
    // 재배포는 성공했는데 허브 사본 갱신이 실패한 모양이다(_hub_sync 가 예외를 삼킨다). 오버라이드로
    // 이름만 바뀌면 개수가 그대로라, 개수만 보면 바로 이 경우를 놓친다
    expect(note).toHaveTextContent("저장된 쪽에만 add_numbers");
    expect(note).toHaveTextContent("서버에만 Add");
    expect(note).toHaveTextContent("이름은 이 항목을 게시한 MCP Server 에서 옵니다. 그 MCP Server 상세에서 재배포하면 맞춰집니다");
    expect(note).not.toHaveTextContent("Tool 새로고침으로 맞춰 주세요");
    app.unmount();

    // published 의 원본은 Gateway 다. MCP Server 를 다시 배포하라고 하면 없는 서버를 찾게 된다
    const source = { ...hubItemFixture().source, instance_id: null, mcp_profile_id: null, gateway_instance_id: "gw-bot" };
    hubState.items = [{ ...hubItemFixture(), id: "hub-pub", name: "bot", kind: "published", source, tool_names: ["add_numbers"] }];
    server.use(oneLiveTool(false));
    render(<TestApp initialPath="/hub/hub-pub?tab=tools" />);
    const published = await screen.findByTestId("tools-stale");
    expect(published).toHaveTextContent("이름은 이 항목을 게시한 Gateway 에서 옵니다. 그 Gateway 상세에서 재배포하면 맞춰집니다");
    expect(published).not.toHaveTextContent("MCP Server");
  });

  it("마지막 오류는 다른 상세와 같은 한국어 레이블로 표의 처방을 보인다", async () => {
    // 허브 항목도 last_error 를 `코드: 설명` 으로 저장한다 (services/hub.py)
    hubState.items = [hubItemFixture({ last_error: "cluster_unreachable: HTTPSConnectionPool(host='10.0.0.1', port=6443): Max retries exceeded" })];
    render(<TestApp initialPath="/hub/hub-calc" />);
    const last = await screen.findByTestId("hub-last-error");
    expect(last).toHaveTextContent("마지막 오류:");
    expect(last).toHaveTextContent("클러스터에 연결하지 못했습니다");
    expect(last).not.toHaveTextContent("HTTPSConnectionPool");
    expect(last).not.toHaveTextContent("last error");
  });

  it("hides unpublish, refresh and the policy form when the subject lacks manage, and dims rows without consume and says why in words", async () => {
    hubState.items = [hubItemFixture(), hubSharedReadOnly];
    const app = render(<TestApp initialPath="/hub" />);
    const row = await screen.findByTestId("hub-row-docs");
    expect(row).toHaveAttribute("data-consumable", "false");
    expect(row).toHaveAttribute("title", "백엔드로 선택할 수 없음");
    // consume 이 없는 행은 흐림 말고 글자로도 이유를 말한다
    expect(within(row).getByTestId("hub-not-consumable-docs")).toHaveTextContent("consume 권한 없음");
    expect(within(row).getByTestId("hub-scope-docs")).toHaveTextContent("shared");
    // manage 가 없으면 지울 수 없어 카드에 더보기가 서지 않는다
    expect(within(row).queryByTestId("card-menu-docs")).not.toBeInTheDocument();
    expect(screen.getByTestId("hub-row-calc")).toHaveAttribute("data-consumable", "true");
    expect(within(screen.getByTestId("hub-row-calc")).queryByTestId("hub-not-consumable-calc")).not.toBeInTheDocument();

    app.unmount();
    render(<TestApp initialPath="/hub/hub-shared-docs?tab=access" />);
    await screen.findByTestId("hub-access-card");
    expect(screen.queryByTestId("delete-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("refresh-tools-button")).toBeDisabled();
    expect(screen.queryByTestId("access-form")).not.toBeInTheDocument();
    expect(screen.getByTestId("access-readonly-consume")).toHaveTextContent("테넌트 billing");
    expect(screen.getByTestId("hub-permission-consume")).toBeInTheDocument();
  });

  it("policy form says in plain words what the current values open, assembles roles: [value], tenants and groups, PUTs /hub/{id}/access and refreshes scope", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-weather?tab=access" />);
    const form = await screen.findByTestId("access-form");
    expect(screen.getByTestId("hub-access-scope")).toHaveTextContent("tenant");

    // 정책을 바꾸는 사람에게도 지금 값이 무엇을 여는지 평문으로 보인다.
    // 비어 있는 정책의 기본값을 펴 보이지 않고 그대로 적는다
    expect(screen.getByTestId("access-preview-view")).toHaveTextContent("게시한 테넌트 · viewer 이상");
    expect(screen.getByTestId("access-preview-consume")).toHaveTextContent("게시한 테넌트 · developer 이상");
    // 입력을 고치면 문장이 따라 바뀐다
    await selectValue(user, "access-consume-role", "tenant-admin");
    await user.type(within(form).getByTestId("access-consume-groups"), "g-pay{enter}");
    await waitFor(() => expect(screen.getByTestId("access-preview-consume")).toHaveTextContent("role tenant-admin 이상 · 그룹 g-pay"));
    // scope 도 뜻이 옆에 있다
    expect(screen.getByTestId("hub-access-scope-hint")).toHaveTextContent("게시한 테넌트 안에서만 보임");

    // consume: developer 이상 또는 Okta 그룹 g-pay. view: 모든 테넌트 (platform-admin 이라 * 가 산다).
    // 그룹 g-pay 는 위에서 넣은 것을 그대로 쓰고 role 만 developer 로 고친다
    await selectValue(user, "access-consume-role", "developer");
    expect(within(form).getByTestId("access-consume-groups-chip-g-pay")).toBeInTheDocument();
    await user.click(within(form).getByTestId("access-view-tenants-all"));
    await user.type(within(form).getByTestId("access-manage-tenants"), "billing,crm{enter}");
    await user.click(within(form).getByTestId("access-manage-tenants-remove-crm"));
    await user.click(within(form).getByTestId("access-save"));

    expect(await screen.findByTestId("access-saved")).toHaveTextContent("shared");
    expect(lastRequest("PUT", "/hub/hub-weather/access")?.body).toEqual({
      view: { tenants: ["*"], roles: [], groups: [] },
      consume: { tenants: [], roles: ["developer"], groups: ["g-pay"] },
      manage: { tenants: ["billing"], roles: [], groups: [] },
    });
    // 저장 응답으로 scope 와 갱신 정보가 바뀐다
    await waitFor(() => expect(screen.getByTestId("hub-access-scope")).toHaveTextContent("shared"));
    expect(screen.getByTestId("hub-access-updated")).toHaveTextContent("header-admin");
    // 개요의 scope 도 같은 응답을 본다. 다른 탭이라 열어서 확인한다
    await user.click(screen.getByTestId("tab-overview"));
    expect(screen.getByTestId("hub-scope")).toHaveTextContent("shared");
  });

  it("policy form shows 403 forbidden from the server in place", async () => {
    const user = userEvent.setup();
    server.use(http.put("/hub/:id/access", () => apiError(403, "forbidden", "manage is required", { action: "manage" })));
    render(<TestApp initialPath="/hub/hub-weather?tab=access" />);
    await screen.findByTestId("access-form");
    await user.click(screen.getByTestId("access-save"));
    expect(await screen.findByTestId("access-error")).toHaveTextContent("forbidden");
    expect(screen.getByTestId("access-form")).toBeInTheDocument();
  });

  it("non platform-admin cannot widen the boundary: * and tenant input are disabled with the hub:share reason", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/hub/hub-weather?tab=access" />);
    const form = await screen.findByTestId("access-form");
    await waitFor(() => expect(within(form).getByTestId("access-view-tenants-all")).toBeDisabled());
    expect(within(form).getByTestId("access-view-tenants")).toBeDisabled();
    expect(within(form).getByTestId("access-share-reason")).toHaveTextContent("hub:share 권한이 필요합니다");
    // role 과 그룹은 manage 만으로 바꿀 수 있다
    expect(within(form).getByTestId("access-consume-role")).toBeEnabled();
    expect(within(form).getByTestId("access-consume-groups")).toBeEnabled();
  });

  it("internal item cannot be opened to other tenants: the tenant row is locked", async () => {
    render(<TestApp initialPath="/hub/hub-calc?tab=access" />);
    const form = await screen.findByTestId("access-form");
    expect(within(form).getByTestId("access-view-tenants-all")).toBeDisabled();
    expect(within(form).getByTestId("access-view-tenants")).toBeDisabled();
    expect(within(form).getByTestId("access-cross-tenant-reason")).toHaveTextContent("다른 테넌트에 열 수 없습니다");
    expect(within(form).getByTestId("access-view-role")).toBeEnabled();
  });

  it("register form has the policy editor collapsed and sends access only when set", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    expect(screen.getByTestId("access-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("access-fields")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("access-toggle"));
    await selectValue(user, "access-view-role", "developer");
    await user.type(screen.getByTestId("hub-name"), "ext");
    await user.type(screen.getByTestId("hub-url"), "https://mcp.example.com/mcp");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toEqual({
      name: "ext",
      kind: "remote",
      cluster_id: null,
      url: "https://mcp.example.com/mcp",
      access: {
        view: { tenants: [], roles: ["developer"], groups: [] },
        consume: { tenants: [], roles: [], groups: [] },
        manage: { tenants: [], roles: [], groups: [] },
      },
    });
  });

  it("external item delete explains 409s in the dialog and retries in place", async () => {
    // 상세의 "참조" 카드가 먼저 받아 두고 대화상자가 같은 캐시를 본다. 그 이름으로
    // 409 의 id 를 바꿔 보인다 (D43). 그래서 화면을 그리기 전에 넣는다
    referencesState["hub/hub-weather"] = [reference("gateway", "gw-support", { display_name: "지원 Gateway" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-weather?tab=access" />);
    await screen.findByTestId("hub-detail-page");

    server.use(http.delete("/hub/:id", () => apiError(409, "in_use", "hub item is used by gateways", { gateway_ids: ["gw-support"] })));
    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user);
    await waitFor(() => expect(screen.getByTestId("confirm-error")).toHaveTextContent("다른 리소스가 사용하고 있어 삭제할 수 없습니다"));
    expect(screen.getByTestId("confirm-error")).toHaveTextContent("Gateway 지원 Gateway");
    expect(screen.getByTestId("confirm-error")).not.toHaveTextContent("gw-support");
    resetReferencesState();

    // 대화상자는 열린 채다. 같은 자리에서 다시 시도한다.
    server.resetHandlers();
    await submitConfirm(user);
    expect(await screen.findByTestId("hub-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/hub/hub-weather")).toBeDefined();
  });

  it("published item is unpublished, not deleted", async () => {
    hubState.items = [...hubState.items, { ...hubItemFixture(), id: "hub-pub", name: "bot", kind: "published" }];
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/hub-pub" />);
    await screen.findByTestId("hub-detail-page");
    expect(screen.getByTestId("delete-button")).toHaveTextContent("게시 해제");
    await user.click(screen.getByTestId("delete-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(dialog).toHaveTextContent("Hub item 게시 해제");
    // 서버는 이 항목을 쓰는 Gateway 가 있으면 409 로 막는다. Gateway 상세의 게시 카드와 같은 문장이어야 한다
    expect(dialog).toHaveTextContent("이 항목을 쓰는 다른 Gateway가 있으면 해제할 수 없습니다");
    expect(within(dialog).getByTestId("confirm-submit")).toHaveTextContent("게시 해제");
    await submitConfirm(user);
    expect(await screen.findByTestId("hub-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/hub/hub-pub")).toBeDefined();
  });
});
