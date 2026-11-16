import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { http, HttpResponse, lastRequest, meDeveloper, server } from "./msw";
import { selectValue } from "./select";

describe("tenant settings (/tenant/settings)", () => {

  it("PATCHes only changed keys: cluster, allowed list, namespace, quota, bindings", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/tenant/settings" />);
    await screen.findByTestId("tenant-settings-form");
    expect(screen.getByTestId("page-title")).toHaveTextContent("default");

    await selectValue(user, "ts-cluster", "cl-edge");
    await user.click(screen.getByTestId("ts-allowed-restrict"));
    await user.click(await screen.findByTestId("ts-allowed-edge"));
    await user.type(screen.getByTestId("ts-ns-name"), "team-default");
    await user.type(screen.getByTestId("ts-quota"), '{{"pods": "20"}');
    await user.click(screen.getByTestId("ts-bindings-add"));
    await selectValue(user, "ts-bindings-ds-0", "ds-redis");
    await user.type(screen.getByTestId("ts-bindings-prefix-0"), "cache");
    await user.click(screen.getByTestId("save-button"));

    await waitFor(() => expect(lastRequest("PATCH", "/tenants/default/settings")).toBeDefined());
    expect(lastRequest("PATCH", "/tenants/default/settings")?.body).toEqual({
      default_cluster_id: "cl-edge",
      allowed_cluster_ids: ["cl-edge"],
      namespace: { name: "team-default", labels: null },
      quota: { pods: "20" },
      default_bindings: [{ data_service_id: "ds-redis", env_prefix: "CACHE" }],
    });
    expect(await screen.findByTestId("ts-notice")).toHaveTextContent("저장했습니다");
  });

  it("members: okta_group rows are read-only, manual rows are edited and PUT as a whole", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/tenant/settings" />);
    const card = await screen.findByTestId("members-card");
    await within(card).findByTestId("member-group-dev@example.com");
    expect(within(card).getByTestId("member-sub-0")).toHaveValue("bot-1");
    expect(within(card).getByTestId("members-save")).toBeDisabled();

    await user.click(within(card).getByTestId("member-add"));
    await user.type(within(card).getByTestId("member-sub-1"), "ops@example.com");
    await selectValue(user, within(card).getByTestId("member-role-1"), "tenant-admin");
    await user.click(within(card).getByTestId("members-save"));

    await waitFor(() => expect(lastRequest("PUT", "/tenants/default/members")).toBeDefined());
    expect(lastRequest("PUT", "/tenants/default/members")?.body).toEqual([
      { sub: "bot-1", role: "viewer" },
      { sub: "ops@example.com", role: "tenant-admin" },
    ]);

    // 삭제도 전체 교체
    await user.click(within(card).getByTestId("member-remove-0"));
    await user.click(within(card).getByTestId("members-save"));
    await waitFor(() => expect(lastRequest("PUT", "/tenants/default/members")?.body).toEqual([{ sub: "ops@example.com", role: "tenant-admin" }]));
  });

  it("바뀐 것이 없으면 저장 알림과 다른 안내로 알리고, 틀린 quota JSON 은 오류로 막으며, 둘 다 보내지 않는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/tenant/settings" />);
    await screen.findByTestId("tenant-settings-form");
    await user.click(screen.getByTestId("save-button"));
    const notice = await screen.findByTestId("ts-notice");
    expect(notice).toHaveTextContent("바뀐 것이 없어 저장하지 않았습니다.");
    expect(notice).toHaveClass("bg-info-soft");
    expect(lastRequest("PATCH", "/tenants/default/settings")).toBeUndefined();

    // 틀린 quota JSON 은 보내기 전에 막는다
    await user.type(screen.getByTestId("ts-quota"), "{{nope");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("ts-form-error")).toHaveTextContent("quota");
    expect(lastRequest("PATCH", "/tenants/default/settings")).toBeUndefined();
  });

  it("developer (no members:write) sees the page read-only without the members card", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/tenant/settings" />);
    await screen.findByTestId("tenant-settings-form");
    expect(await screen.findByTestId("readonly-notice")).toBeInTheDocument();
    expect(screen.getByTestId("save-button")).toBeDisabled();
    expect(screen.queryByTestId("members-card")).not.toBeInTheDocument();
    // 사이드바: audit 은 보이고 admin 은 숨는다
    expect(await screen.findByTestId("nav-audit")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-admin-settings")).not.toBeInTheDocument();
    expect(screen.queryByTestId("nav-tenant-settings")).not.toBeInTheDocument();
  });
});
