import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { apiError, http, lastRequest, mcpServerFixture, server } from "./msw";
import { selectValue } from "./select";
import { codeEditor } from "./editor";

/*
 * 폼 11개에 들어간 이름 두 칸 (D24). 칸 자체의 동작은 name-fields.test, 이름 규칙은 names.test 가 맡는다.
 * 여기서는 제출 본문에 두 필드가 들어가는지, 편집 폼이 Name 을 잠그는지, 서버 422 가 칸 아래로 오는지,
 * 목록이 표시 이름을 주로 보이고 다를 때만 name 을 곁에 적는지를 본다.
 */
describe("name fields in forms (D24)", () => {
  it("backend api upload sends display_name and the slugged name as multipart fields", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "<definitions/>");
    await user.type(screen.getByTestId("bp-display-name"), "계산기 SOAP");
    expect(screen.getByTestId("bp-name")).toHaveValue("soap");
    expect(screen.getByTestId("bp-name-fields")).toHaveTextContent("32자 이내입니다.");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("backend-api-detail-page");
    const body = lastRequest("POST", "/backend-apis")?.body as { name: string; display_name: string };
    expect(body.name).toBe("soap");
    expect(body.display_name).toBe("계산기 SOAP");
  });

  it("mcp server create posts both names; edit locks Name and still sends the stored one", async () => {
    const user = userEvent.setup();
    const app = render(<TestApp initialPath="/mcp-servers/new" />);
    await user.type(screen.getByTestId("mp-display-name"), "주문 Orders");
    expect(screen.getByTestId("mp-name")).toHaveValue("orders");
    await selectValue(user, "mp-backend", "bp-orders");
    await selectValue(user, "mp-version", "2");
    await user.click(await screen.findByTestId("tool-CreateOrder"));
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("mcp-server-detail-page");
    expect(lastRequest("POST", "/mcp-servers")?.body).toMatchObject({ name: "orders", display_name: "주문 Orders" });

    app.unmount();
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    await waitFor(() => expect(screen.getByTestId("mp-name")).toHaveValue("calc"));
    expect(screen.getByTestId("mp-name")).toHaveAttribute("readonly");
    expect(screen.getByTestId("mp-display-name")).toHaveValue("calc");
    await user.clear(screen.getByTestId("mp-display-name"));
    await user.type(screen.getByTestId("mp-display-name"), "계산기");
    // 읽기 전용 Name 은 Display name 을 따라가지 않는다
    expect(screen.getByTestId("mp-name")).toHaveValue("calc");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PUT", "/mcp-servers/ms-calc")?.body).toMatchObject({ name: "calc", display_name: "계산기" }));
  });

  it("gateway edit locks Name", async () => {
    render(<TestApp initialPath="/gateways/gw-support/edit" />);
    await waitFor(() => expect(screen.getByTestId("gp-name")).toHaveValue("support"));
    expect(screen.getByTestId("gp-name")).toHaveAttribute("readonly");
  });

  it("hub form posts both names and previews hub-<name> only for kinds that create a CR", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub/new" />);
    await user.type(screen.getByTestId("hub-display-name"), "날씨 Weather");
    expect(screen.getByTestId("hub-name")).toHaveValue("weather");
    // entry 는 URL 만이라 미리보기가 없고, image 는 MCPServer 를 만드니 hub-<name> 을 미리 보인다
    expect(screen.queryByTestId("hub-name-preview")).not.toBeInTheDocument();
    await selectValue(user, "hub-kind", "image");
    expect(screen.getByTestId("hub-name-preview")).toHaveTextContent("hub-weather");
    await selectValue(user, "hub-kind", "entry");
    await selectValue(user, "hub-transport", "streamable-http");
    await user.type(screen.getByTestId("hub-url"), "https://w.example.com/mcp");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("hub-detail-page");
    expect(lastRequest("POST", "/hub")?.body).toMatchObject({ name: "weather", display_name: "날씨 Weather" });
  });

  it("data service form posts both names", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    await user.type(screen.getByTestId("ds-display-name"), "Orders DB v2");
    expect(screen.getByTestId("ds-name")).toHaveValue("orders-db-v2");
    await user.type(screen.getByTestId("ds-host"), "h");
    await user.type(screen.getByTestId("ds-password"), "p");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("data-service-detail-page");
    expect(lastRequest("POST", "/data-services")?.body).toMatchObject({ name: "orders-db-v2", display_name: "Orders DB v2" });
  });

  it("cluster form keeps the home reservation hint and posts both names", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters/new" />);
    // 폼은 cluster:write 를 확인한 뒤에 그려진다
    expect(await screen.findByTestId("cl-name-fields")).toHaveTextContent("'home'은 플랫폼이 자동 등록하는 이름이라 사용할 수 없습니다.");
    await user.type(screen.getByTestId("cl-display-name"), "Edge 2");
    expect(screen.getByTestId("cl-name")).toHaveValue("edge-2");
    await user.type(screen.getByTestId("cl-platform-url"), "http://p:9000");
    await user.type(screen.getByTestId("cl-kubeconfig"), "apiVersion: v1");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("cluster-detail-page");
    expect(lastRequest("POST", "/clusters")?.body).toMatchObject({ name: "edge-2", display_name: "Edge 2" });
  });

  it("data service detail locks Name and PATCHes display_name only when it changed", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/ds-pg" />);
    await waitFor(() => expect(screen.getByTestId("ds-name")).toHaveValue("orders-db"));
    expect(screen.getByTestId("ds-name")).toHaveAttribute("readonly");
    expect(screen.getByTestId("ds-display-name")).toHaveValue("Orders DB");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Orders DB");
    expect(screen.getByTestId("page-name")).toHaveTextContent("orders-db");
    await user.clear(screen.getByTestId("ds-display-name"));
    await user.type(screen.getByTestId("ds-display-name"), "주문 DB");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("saved");
    expect(lastRequest("PATCH", "/data-services/ds-pg")?.body).toEqual({ display_name: "주문 DB" });
  });

  it("maps 422 invalid_name, invalid_display_name and name_immutable under the matching field, not the common error box", async () => {
    server.use(http.post("/data-services", () => apiError(422, "invalid_name", "name must be 1-32 lowercase letters, digits and single '-'")));
    const user = userEvent.setup();
    render(<TestApp initialPath="/data-services/new" />);
    await user.type(screen.getByTestId("ds-name"), "orders");
    await user.type(screen.getByTestId("ds-host"), "h");
    await user.type(screen.getByTestId("ds-password"), "p");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("ds-name-error")).toHaveTextContent("lowercase letters");
    expect(screen.queryByTestId("save-error")).not.toBeInTheDocument();

    server.use(http.post("/data-services", () => apiError(422, "invalid_display_name", "display_name must be 1-80 characters")));
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("ds-display-name-error")).toHaveTextContent("1-80 characters");

    // 다른 코드는 지금처럼 공통 오류 상자로 간다
    server.use(http.post("/data-services", () => apiError(409, "name_conflict", "data service 'orders' already exists")));
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("name_conflict");
    expect(screen.queryByTestId("ds-name-error")).not.toBeInTheDocument();
  });

  it("maps 422 name_immutable from an edit form under the locked Name", async () => {
    server.use(http.put("/mcp-servers/:id", () => apiError(422, "name_immutable", "name cannot be changed after creation")));
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    await waitFor(() => expect(screen.getByTestId("mp-name")).toHaveValue(mcpServerFixture().name));
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("mp-name-error")).toHaveTextContent("cannot be changed");
  });

  it("lists show display_name as the link with name as a mono aside, and search matches the display name as well as the name", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis" />);
    const orders = await screen.findByTestId("backend-api-row-orders");
    expect(within(orders).getByRole("link", { name: "Orders SOAP" })).toHaveAttribute("href", "/backend-apis/bp-orders");
    expect(within(orders).getByText("orders")).toHaveClass("font-mono");
    // 표시 이름이 name 과 같으면 보조 이름을 겹쳐 보이지 않는다
    const pets = screen.getByTestId("backend-api-row-pets");
    expect(within(pets).getByRole("link", { name: "pets" })).toBeInTheDocument();
    expect(within(pets).queryByText("pets", { selector: "span" })).not.toBeInTheDocument();

    // 검색은 표시 이름도 name 도 맞춘다
    await user.type(screen.getByTestId("list-search"), "soap");
    expect(screen.getByTestId("backend-api-row-orders")).toBeInTheDocument();
    expect(screen.queryByTestId("backend-api-row-pets")).not.toBeInTheDocument();
    await user.clear(screen.getByTestId("list-search"));
    await user.type(screen.getByTestId("list-search"), "orders");
    expect(screen.getByTestId("backend-api-row-orders")).toBeInTheDocument();
  });
});
