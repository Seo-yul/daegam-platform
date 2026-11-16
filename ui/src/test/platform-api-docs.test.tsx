import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { makeQueryClient, TestApp } from "@/App";

import { fastapiDoc } from "./fastapiDoc";
import { apiError, http, HttpResponse, lastRequest, server } from "./msw";

/**
 * 플랫폼 자신의 API 문서 팝업 (P59). token 모드에서 /api/docs 는 새 탭이 토큰을 들고 있지 않아 401 이므로, 사이드바
 * 아래 단추가 화면 안의 팝업으로 /api/openapi.json 을 그 사람의 토큰으로 받아 그린다. 목은 FastAPI 꼴의 픽스처
 * (fastapiDoc)를 낸다.
 */

describe("platform API docs dialog", () => {
  it("opens from the account menu, fetches the platform document and hides the MCP Tool name line", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    // API 문서는 계정 메뉴 안에 있다 (P81 5절)
    await user.click(await screen.findByTestId("account-menu"));
    await user.click(screen.getByTestId("platform-api-docs"));
    const dialog = await screen.findByTestId("platform-api-docs-dialog");
    expect(dialog).toHaveTextContent("대감 API 문서");
    expect(dialog).toHaveTextContent("0.1.0");
    await within(dialog).findByTestId("api-docs-op-list_clusters_api_clusters_get");
    expect(within(dialog).queryByTestId("api-docs-tool-name")).not.toBeInTheDocument();
    // 다른 API 호출처럼 테넌트 헤더(token 모드면 토큰도)가 붙는다
    expect(lastRequest("GET", "/openapi.json")?.headers["x-daegam-tenant"]).toBe("default");

    await user.click(within(dialog).getByTestId("platform-api-docs-close"));
    await waitFor(() => expect(screen.queryByTestId("platform-api-docs-dialog")).not.toBeInTheDocument());
  });

  it("keeps the document under the platform build for as long as the tab lives", async () => {
    const user = userEvent.setup();
    const client = makeQueryClient();
    render(<TestApp initialPath="/clusters" client={client} />);
    // API 문서는 계정 메뉴 안에 있다 (P81 5절)
    await user.click(await screen.findByTestId("account-menu"));
    await user.click(screen.getByTestId("platform-api-docs"));
    const dialog = await screen.findByTestId("platform-api-docs-dialog");
    await within(dialog).findByTestId("api-docs-op-list_clusters_api_clusters_get");
    // 키는 GET /version 의 api_build 다. 새 빌드가 배포되면 키가 바뀌어 새 문서를 받는다
    const key = ["platform-openapi", "0.1.0+test-api"];
    expect(client.getQueryData(key)).toEqual(fastapiDoc);
    expect(client.getQueryCache().find({ queryKey: key })?.gcTime).toBe(Infinity);
  });

  it("waits for GET /version before it fetches the document", async () => {
    const user = userEvent.setup();
    let answer: () => void = () => undefined;
    const answered = new Promise<void>((resolve) => (answer = resolve));
    server.use(
      http.get("/version", async () => {
        await answered;
        return HttpResponse.json({ api_version: "0.1.0", api_build: "0.1.0+test-api" });
      }),
    );
    render(<TestApp initialPath="/clusters" />);
    // API 문서는 계정 메뉴 안에 있다 (P81 5절)
    await user.click(await screen.findByTestId("account-menu"));
    await user.click(screen.getByTestId("platform-api-docs"));
    const dialog = await screen.findByTestId("platform-api-docs-dialog");
    expect(within(dialog).getByTestId("api-docs-loading")).toBeInTheDocument();
    expect(lastRequest("GET", "/openapi.json")).toBeUndefined();
    answer();
    await within(dialog).findByTestId("api-docs-op-list_clusters_api_clusters_get");
    expect(lastRequest("GET", "/openapi.json")).toBeDefined();
  });

  it("shows the API error inside the dialog", async () => {
    const user = userEvent.setup();
    server.use(http.get("/openapi.json", () => apiError(401, "unauthorized", "missing bearer token")));
    render(<TestApp initialPath="/clusters" />);
    // API 문서는 계정 메뉴 안에 있다 (P81 5절)
    await user.click(await screen.findByTestId("account-menu"));
    await user.click(screen.getByTestId("platform-api-docs"));
    const dialog = await screen.findByTestId("platform-api-docs-dialog");
    expect(await within(dialog).findByTestId("api-docs-error")).toBeInTheDocument();
  });

  it("opens from the account menu on the collapsed rail too, and stays open when the menu closes", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await user.click(await screen.findByTestId("sidebar-toggle"));
    await user.click(screen.getByTestId("account-menu"));
    const button = screen.getByTestId("platform-api-docs");
    expect(button).toHaveTextContent("API 문서");
    await user.click(button);
    const dialog = await screen.findByTestId("platform-api-docs-dialog");
    // 위에 뜬 대화상자를 누르는 것은 메뉴의 바깥 누름이 아니다(hasModalAbove). 메뉴가 남아야 대화상자가 닫힐 때
    // 포커스가 감춰진 단추로 돌아가 body 로 떨어지지 않는다
    await user.click(dialog);
    expect(screen.getByTestId("account-menu-panel")).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("platform-api-docs-dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("account-menu-panel")).toBeVisible();
    expect(screen.getByTestId("platform-api-docs")).toHaveFocus();
  });
});
