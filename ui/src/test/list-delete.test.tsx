import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import type { BackendAPI, DataService, MCPServer } from "@/api/types";
import { submitConfirm } from "./confirm";
import { clickCardMenuItem, openCardMenu } from "./menu";
import {
  HttpResponse,
  apiError,
  dsPostgres,
  dsRedis,
  http,
  hubItemFixture,
  hubRemote,
  hubState,
  lastRequest,
  mcpServerFixture,
  profileEmpty,
  profileOrders,
  record,
  server,
} from "./msw";

/** GET 이 삭제를 반영하도록 목록을 mutable 로 두고 DELETE 가 거기서 뺀다. */
function mutableList<T extends { id: string }>(path: string, initial: T[]) {
  const state = { items: initial };
  server.use(
    http.get(path, async ({ request }) => {
      await record(request);
      return HttpResponse.json(state.items);
    }),
    http.delete(`${path}/:id`, async ({ request, params }) => {
      await record(request);
      state.items = state.items.filter((i) => i.id !== params.id);
      return HttpResponse.json({ id: params.id, deleted: true });
    }),
  );
  return state;
}

describe("list row delete", () => {
  it("backend apis: simple confirm, DELETE is sent and the row disappears", async () => {
    mutableList<BackendAPI>("/backend-apis", [profileOrders, profileEmpty]);
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis" />);
    await screen.findByTestId("backend-api-row-orders");
    const btn = screen.getByTestId("row-delete-orders");
    expect(btn).toHaveAttribute("aria-label", "orders 삭제");
    await user.click(btn);
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(within(dialog).getByTestId("confirm-target")).toHaveTextContent("orders");
    expect(dialog).toHaveTextContent("Backend API 삭제");
    expect(dialog).toHaveTextContent("이 Backend API 를 쓰는 MCP Server 가 있으면 삭제할 수 없습니다.");
    expect(within(dialog).queryByTestId("confirm-name")).not.toBeInTheDocument();
    await submitConfirm(user);
    await waitFor(() => expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument());
    expect(lastRequest("DELETE", "/backend-apis/bp-orders")).toBeDefined();
    await waitFor(() => expect(screen.queryByTestId("backend-api-row-orders")).not.toBeInTheDocument());
    expect(screen.getByTestId("backend-api-row-empty")).toBeInTheDocument();
    // 목록 페이지에 남고 포커스는 대화상자 밖으로 돌아온다
    expect(screen.getByTestId("backend-apis-page")).toBeInTheDocument();
  });

  it("mcp servers: the name must be retyped before Delete is enabled", async () => {
    const second = mcpServerFixture({ id: "ms-calc-2", name: "calc-2" });
    const state = mutableList<MCPServer>("/mcp-servers", [mcpServerFixture(), second]);
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-server-row-calc-2");
    await user.click(screen.getByTestId("row-delete-calc-2"));
    const dialog = await screen.findByTestId("confirm-dialog");
    // 정의와 배포가 한 문서라 리비전 이력과 파드가 함께 사라진다 (D42)
    expect(dialog).toHaveTextContent("리비전 이력이 함께 삭제됩니다.");
    expect(dialog).toHaveTextContent("배포된 상태면 실행 중인 파드와 클러스터 리소스가");
    const submit = within(dialog).getByTestId("confirm-submit");
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByTestId("confirm-name"), "calc");
    expect(submit).toBeDisabled();
    expect(lastRequest("DELETE", "/mcp-servers")).toBeUndefined();
    await user.clear(within(dialog).getByTestId("confirm-name"));
    await user.type(within(dialog).getByTestId("confirm-name"), "calc-2");
    expect(submit).toBeEnabled();
    await user.click(submit);
    await waitFor(() => expect(lastRequest("DELETE", "/mcp-servers/ms-calc-2")).toBeDefined());
    await waitFor(() => expect(screen.queryByTestId("mcp-server-row-calc-2")).not.toBeInTheDocument());
    expect(state.items.map((i) => i.id)).toEqual(["ms-calc"]);
    expect(screen.getByTestId("mcp-server-row-calc")).toBeInTheDocument();
  });

  it("mcp servers: a delete left to the running action is announced", async () => {
    // 다른 작업이 문서의 클레임을 쥐고 있으면 서버는 deleted: false 로 답하고 그 작업이 이어서 지운다 (P66)
    server.use(
      http.get("/mcp-servers", () => HttpResponse.json([mcpServerFixture()])),
      http.delete("/mcp-servers/:id", ({ params }) => HttpResponse.json({ id: params.id, deleted: false })),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers" />);
    await screen.findByTestId("mcp-server-row-calc");
    await user.click(screen.getByTestId("row-delete-calc"));
    const dialog = await screen.findByTestId("confirm-dialog");
    await user.type(within(dialog).getByTestId("confirm-name"), "calc");
    await user.click(within(dialog).getByTestId("confirm-submit"));
    await waitFor(() => expect(screen.getByTestId("delete-continuing")).toHaveTextContent("진행 중인 다른 작업이 끝나면 이어서 삭제합니다."));
  });

  it("409 keeps the dialog open and shows the server reason with references", async () => {
    server.use(
      http.delete("/data-services/:id", () => apiError(409, "in_use", "data service is bound by mcp servers", { mcp_server_ids: ["ms-calc"] })),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services" />);
    await screen.findByTestId("data-service-row-orders-db");
    await user.click(screen.getByTestId("row-delete-orders-db"));
    await submitConfirm(user);
    const err = await screen.findByTestId("confirm-error");
    expect(err).toHaveTextContent("다른 리소스가 사용하고 있어 삭제할 수 없습니다");
    // 목록 행에서 연 대화상자도 같은 references 를 받는다. 이름을 모르면 개수만 말한다 (D43)
    expect(err).toHaveTextContent("MCP Server 1개");
    expect(err).not.toHaveTextContent("ms-calc");
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("confirm-submit")).toBeEnabled();
    // 취소하면 닫히고 행은 그대로다
    await user.click(screen.getByTestId("confirm-cancel"));
    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("data-service-row-orders-db")).toBeInTheDocument();
    // 다시 열면 이전 오류가 남아 있지 않다
    await user.click(screen.getByTestId("row-delete-orders-db"));
    await screen.findByTestId("confirm-dialog");
    expect(screen.queryByTestId("confirm-error")).not.toBeInTheDocument();
  });

  it("clusters: the home cluster has no delete button and others require the name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await screen.findByTestId("cluster-row-home");
    expect(screen.queryByTestId("row-delete-home")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("row-delete-edge"));
    const dialog = await screen.findByTestId("confirm-dialog");
    expect(within(dialog).getByTestId("confirm-name")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Cluster 삭제");
  });

  it("hub: only externally registered items get a delete button", async () => {
    hubState.items = [
      hubItemFixture(),
      hubRemote,
      { ...hubItemFixture(), id: "hub-pub", name: "pub", kind: "published" },
      { ...hubRemote, id: "hub-img", name: "boxed", kind: "image" },
      { ...hubRemote, id: "hub-entry", name: "entry-item", kind: "entry" },
    ];
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await screen.findByTestId("hub-row-calc");
    // 카드에서 동작은 더보기 안이라, 지울 수 없는 항목에는 더보기 자체가 없다
    expect(screen.queryByTestId("card-menu-calc")).not.toBeInTheDocument();
    expect(screen.queryByTestId("card-menu-pub")).not.toBeInTheDocument();
    expect(within(await openCardMenu(user, "boxed")).getByTestId("row-delete-boxed")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(within(await openCardMenu(user, "entry-item")).getByTestId("row-delete-entry-item")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await clickCardMenuItem(user, "weather", "row-delete-weather");
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/hub/hub-weather")).toBeDefined());
    await waitFor(() => expect(screen.queryByTestId("hub-row-weather")).not.toBeInTheDocument());
  });

  it("deleting the only row on the last page moves back to the previous page", async () => {
    const many: DataService[] = Array.from({ length: 11 }, (_, i) => ({
      ...(i % 2 ? dsRedis : dsPostgres),
      id: `ds-${i}`,
      name: `svc-${String(i).padStart(2, "0")}`,
    }));
    mutableList<DataService>("/data-services", many);
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services" />);
    await screen.findByTestId("data-service-row-svc-00");
    await user.click(screen.getByTestId("page-next"));
    expect(screen.getByTestId("page-info")).toHaveTextContent("11–11 / 11");
    await user.click(screen.getByTestId("row-delete-svc-10"));
    await submitConfirm(user);
    await waitFor(() => expect(screen.queryByTestId("data-service-row-svc-10")).not.toBeInTheDocument());
    expect(screen.getByTestId("page-info")).toHaveTextContent("1–10 / 10");
    expect(screen.getByTestId("page-chip-1")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("data-service-row-svc-00")).toBeInTheDocument();
  });
});
