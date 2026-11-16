/** IdP 그룹을 테넌트·역할에 잇는 그룹 매핑 화면 (/admin/group-mappings). 저장은 목록 전체를 바꾼다. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { HttpResponse, http, lastRequest, meDeveloper, server } from "./msw";
import { selectValue } from "./select";

describe("group mappings (/admin/group-mappings)", () => {
  it("edits the table and PUTs the whole list", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/group-mappings" />);
    await waitFor(() => expect(screen.getByTestId("gm-group-0")).toHaveValue("daegam-admins"));
    expect(screen.getByTestId("save-button")).toBeDisabled();

    await user.click(screen.getByTestId("gm-add"));
    await user.type(screen.getByTestId("gm-group-2"), "daegam-acme-admins");
    await user.type(screen.getByTestId("gm-tenant-2"), "acme");
    await selectValue(user, "gm-role-2", "tenant-admin");
    await user.click(screen.getByTestId("gm-remove-1"));
    await user.click(screen.getByTestId("save-button"));

    // 전체 교체는 되돌릴 수 없어 확인을 한 번 받는다. platform-admin 이 남아 있으므로 붉은 경고는 없다
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("전체를 교체합니다");
    expect(screen.queryByTestId("gm-loses-admin")).not.toBeInTheDocument();
    expect(lastRequest("PUT", "/group-mappings")).toBeUndefined();
    await user.click(screen.getByTestId("confirm-submit"));

    await waitFor(() => expect(lastRequest("PUT", "/group-mappings")).toBeDefined());
    expect(lastRequest("PUT", "/group-mappings")?.body).toEqual([
      { group: "daegam-admins", tenant: "*", role: "platform-admin" },
      { group: "daegam-acme-admins", tenant: "acme", role: "tenant-admin" },
    ]);
  });

  it("warns before saving a list that keeps no platform-admin mapping", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/group-mappings" />);
    // platform-admin 행(0번)을 지우면 관리자 매핑이 하나도 안 남는다
    await user.click(await screen.findByTestId("gm-remove-0"));
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("gm-loses-admin")).toHaveTextContent("다시 들어오지 못할 수 있습니다");
    await user.click(screen.getByTestId("confirm-cancel"));
    expect(lastRequest("PUT", "/group-mappings")).toBeUndefined();
  });

  it("blocks platform-admin without tenant '*' before sending", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/group-mappings" />);
    await waitFor(() => expect(screen.getByTestId("gm-group-0")).toHaveValue("daegam-admins"));
    await selectValue(user, "gm-role-1", "platform-admin");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("gm-form-error")).toHaveTextContent("platform-admin 은 테넌트 * 에만");
    expect(lastRequest("PUT", "/group-mappings")).toBeUndefined();
  });

  it("tells a non-admin that the page is read-only and locks the add button with the reason", async () => {
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    const readOnly = dictionary("ko").admin.readOnly;
    render(<TestApp initialPath="/admin/group-mappings" />);
    expect(await screen.findByTestId("readonly-notice")).toHaveTextContent(readOnly);
    expect(screen.getByTestId("gm-add")).toBeDisabled();
    expect(screen.getByTestId("gm-add")).toHaveAttribute("title", readOnly);
  });
});
