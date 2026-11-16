import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api, rawJson } from "@/api/client";
import { TestApp } from "@/App";
import { getRecentTenants, getTenant, setTenant } from "@/lib/tenant";
import { lastRequest } from "./msw";

describe("tenant header injection", () => {

  it("defaults to 'default' and sends it on openapi-fetch requests", async () => {
    expect(getTenant()).toBe("default");
    await api.GET("/backend-apis");
    expect(lastRequest("GET", "/backend-apis")?.headers["x-daegam-tenant"]).toBe("default");
  });

  it("sends the selected tenant on both the typed client and rawJson", async () => {
    setTenant("acme");
    await api.GET("/data-services");
    expect(lastRequest("GET", "/data-services")?.headers["x-daegam-tenant"]).toBe("acme");
    await rawJson("/version");
    expect(lastRequest("GET", "/version")?.headers["x-daegam-tenant"]).toBe("acme");
  });

  it("persists the selection in localStorage and keeps a recent list", () => {
    setTenant("acme");
    setTenant("beta");
    expect(window.localStorage.getItem("daegam.tenant")).toBe("beta");
    expect(getRecentTenants()).toEqual(["beta", "acme", "default"]);
    expect(() => setTenant("bad name!")).toThrow();
  });

  it("switching tenant in the selector refetches with the new header", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis" />);
    await screen.findByTestId("backend-api-row-orders");
    expect(lastRequest("GET", "/backend-apis")?.headers["x-daegam-tenant"]).toBe("default");

    await user.click(screen.getByTestId("tenant-add"));
    await user.type(screen.getByTestId("tenant-input"), "acme");
    await user.click(screen.getByTestId("tenant-add-submit"));

    await waitFor(() => {
      expect(lastRequest("GET", "/backend-apis")?.headers["x-daegam-tenant"]).toBe("acme");
    });
    expect(screen.getByTestId("tenant-select")).toHaveValue("acme");
  });
});
