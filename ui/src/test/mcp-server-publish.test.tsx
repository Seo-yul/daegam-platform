/** MCP Server 를 MCP Hub 에 게시하고 내리기 (D23). 게시는 배포된 문서에만 걸리고 참조하는 Gateway 를 이름으로 보인다. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { summarizeReferences } from "@/lib/references";
import { submitConfirm } from "./confirm";
import { HttpResponse, draftFixture, http, hubRemote, hubState, lastRequest, mcpServerFixture, mcpServerState, meViewer, publishedHubRef, record, reference, referencesState, server } from "./msw";

describe("mcp servers (hub publish)", () => {
  it("publishing needs a deployed document, then shows the badge and link", async () => {
    const user = userEvent.setup();
    // 안 올린 것에는 게시 버튼이 없다. 가리킬 엔드포인트가 없기 때문이다
    mcpServerState.items = [draftFixture()];
    const app = render(<TestApp initialPath="/mcp-servers/ms-draft" />);
    await screen.findByTestId("mcp-server-detail-page");
    expect(screen.queryByTestId("publish-button")).not.toBeInTheDocument();

    app.unmount();
    mcpServerState.items = [mcpServerFixture()];
    // 허브 픽스처에 이미 내부 항목 calc 가 있다. 같은 이름은 409 라 비우고 시작한다
    hubState.items = [hubRemote];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    // 게시는 더보기가 아니라 머리의 버튼이다 (D44 둘째의 예외. 배포 다음으로 자주 누른다)
    await user.click(await screen.findByTestId("publish-button"));
    const dialog = await screen.findByTestId("publish-dialog");
    expect(within(dialog).getByTestId("publish-name")).toHaveValue("calc");
    await user.click(within(dialog).getByTestId("publish-submit"));

    await waitFor(() => expect(lastRequest("POST", "/mcp-servers/ms-calc/publish")?.body).toEqual({ name: "calc", display_name: "calc" }));
    expect(await screen.findByTestId("published-badge")).toHaveTextContent("Published");
    expect(await screen.findByTestId("unpublish-button")).toHaveTextContent("게시 해제");
  });

  it("the delete dialog warns about the gateways that would lose this backend, and unpublish 409 in_use links them and keeps the dialog open", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef() })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("published-badge");
    await user.click(screen.getByTestId("delete-button"));
    const warning = await screen.findByTestId("delete-referenced-warning");
    expect(warning).toHaveTextContent("Gateway 1개");
    expect(within(warning).getByTestId("delete-referenced-gateway-gw-support")).toHaveAttribute("href", "/gateways/gw-support");
    // 지우지 않고 닫는다. 게시 해제의 확인 대화상자도 같은 testid 를 쓰므로 닫힌 것을 보고 넘어간다
    await user.click(screen.getByTestId("confirm-cancel"));
    await waitFor(() => expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument());

    await user.click(await screen.findByTestId("unpublish-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    await user.click(within(dialog).getByTestId("confirm-submit"));
    const err = await screen.findByTestId("unpublish-error");
    expect(err).toHaveTextContent("in_use");
    expect(within(err).getByTestId("unpublish-ref-gw-support")).toHaveAttribute("href", "/gateways/gw-support");

    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef({ gateway_ids: [] }) })];
    await user.click(within(dialog).getByTestId("confirm-submit"));
    await waitFor(() => expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument());
    expect(await screen.findByTestId("publish-button")).toBeInTheDocument();
  });

  it("the unpublish dialog lists what still uses the hub item before anything is sent", async () => {
    const user = userEvent.setup();
    const ko = dictionary("ko");
    const refs = [reference("gateway", "gw-support", { display_name: "support" })];
    referencesState["hub/hub-calc"] = refs;
    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef() })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await user.click(await screen.findByTestId("unpublish-button"));
    const dialog = await screen.findByTestId("confirm-dialog");
    const notice = await within(dialog).findByTestId("unpublish-references");
    expect(notice).toHaveTextContent(ko.detail.referencesCard.notice(summarizeReferences(ko, refs)));
    const link = within(notice).getByTestId("unpublish-references-link-gw-support");
    expect(link).toHaveTextContent("support");
    expect(link).toHaveAttribute("href", "/gateways/gw-support");
    expect(lastRequest("DELETE", "/mcp-servers/ms-calc/publish")).toBeUndefined();
  });

  it("a referenced gateway missing from the list reads as deleted, never as its id", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef({ gateway_ids: ["gw-gone"] }) })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("published-badge");
    await user.click(screen.getByTestId("delete-button"));
    const link = await screen.findByTestId("delete-referenced-gateway-gw-gone");
    await waitFor(() => expect(link).toHaveTextContent(dictionary("ko").ui.names.deleted));
    expect(screen.getByTestId("delete-referenced-warning")).not.toHaveTextContent("gw-gone");
  });

  it("a viewer without hub:write sees neither publish nor unpublish", async () => {
    server.use(
      http.get("/me", async ({ request }) => {
        await record(request);
        return HttpResponse.json(meViewer);
      }),
    );
    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef({ permissions: { view: true, consume: false, manage: false } }) })];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    expect(await screen.findByTestId("published-badge")).toBeInTheDocument();
    await waitFor(() => expect(lastRequest("GET", "/me")).toBeDefined());
    expect(screen.queryByTestId("unpublish-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("publish-button")).not.toBeInTheDocument();
  });

  it("the list row publishes and unpublishes in place", async () => {
    const user = userEvent.setup();
    mcpServerState.items = [mcpServerFixture({ hub_item: publishedHubRef({ gateway_ids: [] }) })];
    render(<TestApp initialPath="/mcp-servers" />);
    const row = await screen.findByTestId("mcp-server-row-calc");
    // 버튼의 낱말이 상태를 말한다. "게시 해제" 가 보이면 게시된 것이라 배지가 따로 없다
    expect(within(row).queryByTestId("published-badge-calc")).not.toBeInTheDocument();
    expect(within(row).getByTestId("hub-link-calc")).toBeInTheDocument();
    const unpublish = within(row).getByTestId("unpublish-button-calc");
    expect(unpublish).toHaveTextContent("게시 해제");
    await user.click(unpublish);
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/mcp-servers/ms-calc/publish")).toBeDefined());
    expect(await screen.findByTestId("publish-button-calc")).toHaveTextContent("게시");
  });
});
