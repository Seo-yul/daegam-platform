import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { narrowed, normalizeAccess } from "@/lib/hubAccess";
import { HttpResponse, http, hubItemFixture, hubState, lastRequest, server } from "./msw";
import { selectValue } from "./select";

/** header 인증 entry 항목 (P86 6.1절). 값은 응답에 없다. */
function headerItem(check: "ok" | "header_refused" | "unreachable" = "ok") {
  return {
    ...hubItemFixture(),
    id: "hub-out",
    name: "outside",
    display_name: "outside",
    kind: "entry" as const,
    source: { ...hubItemFixture().source, url: "https://mcp.example.com/mcp", transport: "streamable-http" as const, instance_id: null, mcp_profile_id: null },
    k8s: { kind: null, name: "hub-outside", namespace: "daegam-default", group: null, tool_config: null },
    access: { view: { tenants: ["*"], roles: [], groups: [] }, consume: { tenants: ["*"], roles: [], groups: [] }, manage: { tenants: [], roles: [], groups: [] } },
    auth: { kind: "header", header_name: "X-API-Key", set: true, rev: "r1", created_by: "admin", check: { outcome: check, at: "2026-11-15T00:00:00Z" } },
    permissions: { view: true, consume: true, manage: true, replace_header: true, replace_secret_env: false },
  };
}

describe("hub header auth (P86)", () => {
  it("registers an entry item with a fixed header", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    await selectValue(user, "hub-kind", "entry");
    await user.type(screen.getByTestId("hub-name"), "outside");
    await user.type(screen.getByTestId("hub-url"), "https://mcp.example.com/mcp");
    await selectValue(user, "hub-transport", "streamable-http");
    await selectValue(user, "hub-auth", "header");
    await user.type(screen.getByTestId("hub-header-name"), "X-API-Key");
    await user.type(screen.getByTestId("hub-header-value"), "k-123");
    expect(screen.getByTestId("hub-header-value")).toHaveAttribute("type", "password");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("hub-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/hub")?.body).toMatchObject({
      kind: "entry",
      auth: { kind: "header", header_name: "X-API-Key", header_value: "k-123" },
    });
  });

  it("other kinds send no auth and hide the auth fields", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    expect(screen.queryByTestId("hub-auth")).not.toBeInTheDocument();
    await user.type(screen.getByTestId("hub-name"), "ext");
    await user.type(screen.getByTestId("hub-url"), "https://mcp.example.com/mcp");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("hub-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/hub")?.body).not.toHaveProperty("auth");
  });

  it("shows the header card with the last check and replaces the value", async () => {
    const user = userEvent.setup();
    hubState.items = [headerItem("header_refused")];
    let sent: unknown = null;
    server.use(http.put("/hub/:id/auth/header", async ({ request }) => {
      sent = await request.json();
      return HttpResponse.json({ ...headerItem("ok"), status: { phase: "Valid", ready: true, url: null, external_url: null, message: null, observed_at: "2026-11-15T00:00:00Z", error: null, warnings: [] } });
    }));
    render(<TestApp initialPath="/hub/hub-out" />);
    const card = await screen.findByTestId("hub-auth-card");
    expect(card).toHaveTextContent("X-API-Key");
    expect(screen.getByTestId("hub-header-check")).toHaveTextContent("header_refused");
    expect(screen.getByTestId("hub-header-refused")).toHaveTextContent("외부 서버가 이 헤더 값을 거절했습니다");
    await user.click(screen.getByTestId("replace-header-button"));
    await user.type(screen.getByTestId("replace-header-value"), "k-456");
    await user.click(screen.getByTestId("replace-header-save"));
    expect(await screen.findByText("ok")).toBeInTheDocument();
    expect(sent).toEqual({ value: "k-456" });
    expect(screen.queryByTestId("hub-header-refused")).not.toBeInTheDocument();
  });

  it("warns on the access tab that copies go to other tenants and that removed ones may have read it", async () => {
    const user = userEvent.setup();
    hubState.items = [headerItem()];
    render(<TestApp initialPath="/hub/hub-out?tab=access" />);
    expect(await screen.findByTestId("access-header-notice")).toHaveTextContent("헤더 값의 사본이 들어갑니다");
    expect(screen.queryByTestId("access-header-removed")).not.toBeInTheDocument();
    // 모든 테넌트(*)를 consume 에서 빼면 그 사이 값을 읽었을 수 있다고 함께 말한다
    await user.click(screen.getByTestId("access-consume-tenants-all"));
    expect(await screen.findByTestId("access-header-removed")).toHaveTextContent("헤더 값도 바꾸는 것이 안전합니다");
  });

  it("hides the replace button without permissions.replace_header", async () => {
    hubState.items = [{ ...headerItem(), permissions: { view: true, consume: true, manage: true, replace_header: false, replace_secret_env: false } }];
    render(<TestApp initialPath="/hub/hub-out" />);
    expect(await screen.findByTestId("hub-auth-card")).toBeInTheDocument();
    expect(screen.queryByTestId("replace-header-button")).not.toBeInTheDocument();
  });

  it("items without a header show no auth card", async () => {
    hubState.items = [hubItemFixture()];
    render(<TestApp initialPath="/hub/hub-calc" />);
    expect(await screen.findByTestId("hub-summary")).toBeInTheDocument();
    expect(screen.queryByTestId("hub-auth-card")).not.toBeInTheDocument();
  });

  it("narrowed matches the server's rule for * and the owner", () => {
    const acc = (tenants: string[]) => normalizeAccess({ consume: { tenants, roles: [], groups: [] } });
    expect(narrowed(acc(["a"]), acc([]), "default", "consume")).toBe(true);
    expect(narrowed(acc(["a"]), acc(["*"]), "default", "consume")).toBe(false);
    expect(narrowed(acc(["*"]), acc(["a"]), "default", "consume")).toBe(true);
    expect(narrowed(acc(["default"]), acc([]), "default", "consume")).toBe(false);
    expect(narrowed(acc([]), acc(["a"]), "default", "consume")).toBe(false);
  });
});
