import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { auditEvents, http, HttpResponse, lastRequest, meDeveloper, record, server } from "./msw";

describe("audit (/audit, /audit/mcp)", () => {
  it("lists platform API events with request_id and applies filters as query params", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/audit" />);
    const row = await screen.findByTestId("audit-row-ae-1");
    expect(row).toHaveTextContent("mcp_instance.create");
    expect(row).toHaveTextContent("req-abc123");
    expect(screen.getByTestId("audit-row-ae-2")).toHaveTextContent("in_use");
    // platform-admin 은 tenant 를 안 보내 전체를 본다 (limit 만)
    expect(lastRequest("GET", "/audit")?.path).toBe("/api/audit?limit=100");

    await user.type(screen.getByTestId("filter-action"), "mcp_instance.create");
    await user.type(screen.getByTestId("filter-sub"), "dev@example.com");
    await user.type(screen.getByTestId("filter-resource"), "mcp_instance");
    await user.type(screen.getByTestId("filter-tenant"), "acme");
    await user.click(screen.getByTestId("filter-apply"));

    await waitFor(() => expect(lastRequest("GET", "/audit?")?.path).toContain("action=mcp_instance.create"));
    const path = lastRequest("GET", "/audit?")!.path;
    const params = new URL(path, "http://x").searchParams;
    expect(params.get("tenant")).toBe("acme");
    expect(params.get("sub")).toBe("dev@example.com");
    expect(params.get("resource")).toBe("mcp_instance");
    expect(params.get("limit")).toBe("100");
    expect(params.has("since")).toBe(false);
    await waitFor(() => expect(screen.queryByTestId("audit-row-ae-2")).not.toBeInTheDocument());
  });

  it("filters by via with the assistant-only check and links Steward sessions to the panel (D31)", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/audit", async ({ request }) => {
        await record(request);
        return HttpResponse.json([
          { ...auditEvents[0], id: "ae-via-1", via: "assistant/as-1" },
          { ...auditEvents[0], id: "ae-via-2", via: "assistant/as-gone" },
          { ...auditEvents[1], id: "ae-via-3", via: null },
        ]);
      }),
    );
    render(<TestApp initialPath="/audit" />);
    const row = await screen.findByTestId("audit-row-ae-via-1");
    // 내 세션 목록에 있는 세션만 패널로 여는 버튼이 된다. 없는 세션은 글자뿐이다
    expect(await within(row).findByTestId("audit-via-open-as-1")).toHaveTextContent("as-1");
    expect(within(screen.getByTestId("audit-row-ae-via-2")).getByTestId("audit-via-as-gone")).toBeInTheDocument();
    expect(within(screen.getByTestId("audit-row-ae-via-2")).queryByRole("button")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("audit-via-cell-ae-via-3"))).toBeTruthy();
    expect(screen.getByTestId("audit-via-cell-ae-via-3")).toHaveTextContent("-");

    // 체크만 하면 접두(assistant/)로 거른다
    await user.click(screen.getByTestId("filter-assistant_only"));
    await user.click(screen.getByTestId("filter-apply"));
    await waitFor(() => expect(new URL(lastRequest("GET", "/audit?")!.path, "http://x").searchParams.get("via")).toBe("assistant/"));
    // 세션 id 만 적으면 접두를 붙인다
    await user.type(screen.getByTestId("filter-via"), "as-1");
    await user.click(screen.getByTestId("filter-apply"));
    await waitFor(() => expect(new URL(lastRequest("GET", "/audit?")!.path, "http://x").searchParams.get("via")).toBe("assistant/as-1"));
    // 체크를 풀면 적은 그대로 보낸다
    await user.click(screen.getByTestId("filter-assistant_only"));
    await user.click(screen.getByTestId("filter-apply"));
    await waitFor(() => expect(new URL(lastRequest("GET", "/audit?")!.path, "http://x").searchParams.get("via")).toBe("as-1"));

    // 세션 링크는 패널에서 그 세션을 연다
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("audit-via-open-as-1"));
    const panel = await screen.findByTestId("assistant-panel");
    expect(panel).toHaveAttribute("data-session-id", "as-1");
    await waitFor(() => expect(within(panel).getByTestId("assistant-transcript")).toHaveTextContent("calc-1 (Ready)"));
  });

  it("says the list is cut at the limit and offers the next batch", async () => {
    const user = userEvent.setup();
    // limit 만큼 꽉 찬 응답이면 잘렸을 수 있다
    server.use(
      http.get("/audit", async ({ request }) => {
        await record(request);
        const limit = Number(new URL(request.url).searchParams.get("limit") ?? 100);
        return HttpResponse.json(Array.from({ length: limit }, (_, i) => ({ ...auditEvents[0], id: `ae-${i}` })));
      }),
    );
    render(<TestApp initialPath="/audit" />);
    expect(await screen.findByTestId("audit-truncated")).toHaveTextContent("최신 100건만");
    await user.click(screen.getByTestId("audit-more"));
    await waitFor(() => expect(new URL(lastRequest("GET", "/audit?")!.path, "http://x").searchParams.get("limit")).toBe("200"));
    // Limit 칸도 따라온다
    await waitFor(() => expect(screen.getByTestId("filter-limit")).toHaveValue(200));
  });

  it("non-admin gets its own tenant prefilled, and the filter is re-seeded when the header tenant changes, so the old one cannot 403", async () => {
    const user = userEvent.setup();
    server.use(http.get("/me", () => HttpResponse.json(meDeveloper)));
    render(<TestApp initialPath="/audit" />);
    await screen.findByTestId("audit-row-ae-1");
    await waitFor(() => expect(screen.getByTestId("filter-tenant")).toHaveValue("default"));

    await user.click(screen.getByTestId("tenant-add"));
    await user.type(screen.getByTestId("tenant-input"), "acme");
    await user.click(screen.getByTestId("tenant-add-submit"));
    await waitFor(() => expect(screen.getByTestId("filter-tenant")).toHaveValue("acme"));
  });

  it("MCP proxy tab lists tool calls and filters by tool/type/outcome", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/audit" />);
    await screen.findByTestId("audit-row-ae-1");
    await user.click(screen.getByTestId("audit-tab-mcp"));
    const row = await screen.findByTestId("audit-mcp-row-ma-1");
    expect(row).toHaveTextContent("CreateOrder");
    expect(row).toHaveTextContent("success");
    // Gateway(vMCP) 를 거친 호출은 부른 백엔드(Entry)를 보이고, 서버로 바로 온 호출은 비어 있다 (P50 4절)
    expect(screen.getByRole("columnheader", { name: "백엔드" })).toBeInTheDocument();
    expect(screen.getByTestId("audit-mcp-backend-ma-2")).toHaveTextContent("gw-support-calc");
    expect(screen.getByTestId("audit-mcp-backend-ma-1")).toHaveTextContent("-");
    // 사람 이름으로 위임한 호출은 경유 칸에 Agent 가 오고, 자기 이름으로 부른 호출은 비어 있다 (P53 7절)
    expect(screen.getByRole("columnheader", { name: "경유" })).toBeInTheDocument();
    expect(screen.getByTestId("audit-mcp-via-ma-2")).toHaveTextContent("svc:agent:agent-support");
    expect(screen.getByTestId("audit-mcp-via-ma-1")).toHaveTextContent("-");

    await user.type(screen.getByTestId("filter-tool"), "CreateOrder");
    await user.type(screen.getByTestId("filter-type"), "mcp_tool_call");
    await user.type(screen.getByTestId("filter-outcome"), "success");
    await user.type(screen.getByTestId("filter-sub"), "agent-1");
    await user.click(screen.getByTestId("filter-apply"));
    await waitFor(() => expect(lastRequest("GET", "/audit/mcp?")?.path).toContain("tool=CreateOrder"));
    const params = new URL(lastRequest("GET", "/audit/mcp?")!.path, "http://x").searchParams;
    expect(params.get("type")).toBe("mcp_tool_call");
    expect(params.get("outcome")).toBe("success");
    expect(params.get("sub")).toBe("agent-1");
  });
});
