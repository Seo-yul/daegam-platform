import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import OpenApiDocs from "@/components/OpenApiDocs";

import { apiError, http, lastRequest, server } from "./msw";
import { fastapiDoc } from "./fastapiDoc";
import { specOrdersSoap, specPetstore } from "./petstore";

describe("OpenApiDocs viewer", () => {
  it("renders info, tag groups and the first operation by default", () => {
    render(<OpenApiDocs doc={specPetstore} />);
    const info = screen.getByTestId("api-docs-info");
    expect(info).toHaveTextContent("Petstore");
    expect(info).toHaveTextContent("1.0.0");
    expect(info).toHaveTextContent("https://petstore.example.com/v1");
    // 설명의 링크와 코드는 단순 마크다운으로 그려진다
    expect(within(info).getByRole("link", { name: "the guide" })).toHaveAttribute("href", "https://example.com/guide");
    expect(within(info).getByText("listPets", { selector: "code" })).toBeInTheDocument();

    expect(screen.getByTestId("api-docs-tag-pets")).toBeInTheDocument();
    expect(screen.getByTestId("api-docs-tag-default")).toHaveTextContent("/pets/{petId}");
    expect(screen.getByTestId("api-docs-op-listPets")).toHaveAttribute("aria-current", "true");
    const detail = screen.getByTestId("api-docs-detail");
    expect(within(detail).getByTestId("api-docs-operation-id")).toHaveTextContent("listPets");
    expect(within(detail).getByTestId("api-docs-tool-name")).toHaveTextContent("이 값이 MCP Tool 이름이 됩니다.");
    // 경로 파라미터·$ref 파라미터가 표에 있고 enum 이 칩으로 보인다
    expect(within(detail).getByTestId("api-docs-param-limit")).toHaveTextContent("integer(int32)");
    const status = within(detail).getByTestId("api-docs-param-status");
    expect(status).toHaveTextContent("Filter by status");
    expect(status).toHaveTextContent("available");
    expect(status).toHaveTextContent("sold");
    // 응답 200 은 array<Pet> 이고 allOf·$ref 가 풀린 속성 표가 있다
    const ok = within(detail).getByTestId("api-docs-response-200");
    expect(ok).toHaveTextContent("array<Pet>");
    expect(within(ok).getByTestId("api-docs-schema-row-owner")).toHaveTextContent("Owner");
    expect(within(ok).getByTestId("api-docs-schema-row-pets")).toHaveTextContent("(순환)");
    expect(within(ok).getByTestId("api-docs-schema-row-pets")).toHaveAttribute("data-depth", "1");
    expect(within(detail).getByTestId("api-docs-response-default")).toHaveTextContent("Error");
  });

  it("selects an operation from the list and searches", async () => {
    const user = userEvent.setup();
    render(<OpenApiDocs doc={specPetstore} />);
    await user.click(screen.getByTestId("api-docs-op-createPet"));
    const detail = screen.getByTestId("api-docs-detail");
    expect(within(detail).getByTestId("api-docs-operation-id")).toHaveTextContent("createPet");
    const body = within(detail).getByTestId("api-docs-request-body");
    expect(body).toHaveTextContent("required");
    expect(body).toHaveTextContent("The pet to add");
    expect(body).toHaveTextContent("NewPet");
    expect(within(body).getByTestId("api-docs-schema-row-name")).toHaveTextContent("*");
    expect(within(detail).getByTestId("api-docs-response-201")).toHaveTextContent("Created");

    await user.click(screen.getByTestId("api-docs-op-deletePet"));
    expect(within(detail).getByTestId("api-docs-deprecated")).toBeInTheDocument();
    expect(within(detail).getByTestId("api-docs-params")).toHaveTextContent("petId");

    await user.type(screen.getByTestId("api-docs-search"), "getpet");
    expect(screen.queryByTestId("api-docs-op-listPets")).not.toBeInTheDocument();
    expect(screen.getByTestId("api-docs-op-getPet")).toBeInTheDocument();
    await user.clear(screen.getByTestId("api-docs-search"));
    await user.type(screen.getByTestId("api-docs-search"), "zzz");
    expect(screen.getByText("일치하는 오퍼레이션이 없습니다.")).toBeInTheDocument();
  });

  it("opens on the initial operation and shows the SOAP contract block", () => {
    render(<OpenApiDocs doc={specOrdersSoap} initialOperationId="CreateOrder" />);
    expect(screen.getByTestId("api-docs-op-CreateOrder")).toHaveAttribute("aria-current", "true");
    const soap = screen.getByTestId("api-docs-soap");
    expect(soap).toHaveTextContent("http://example.com/orders/CreateOrder");
    expect(soap).toHaveTextContent("1.1");
    expect(soap).toHaveTextContent("CreateOrderResponse");
    expect(soap).toHaveTextContent("http://example.com/orders");
  });

  it("hides the MCP Tool name line when toolNames is off and shows FastAPI's Optional fields", () => {
    render(<OpenApiDocs doc={fastapiDoc} toolNames={false} />);
    const detail = screen.getByTestId("api-docs-detail");
    expect(within(detail).getByTestId("api-docs-operation-id")).toHaveTextContent("list_clusters_api_clusters_get");
    expect(within(detail).queryByTestId("api-docs-tool-name")).not.toBeInTheDocument();
    const state = within(detail).getByTestId("api-docs-param-state");
    expect(state).toHaveTextContent("string | null");
    expect(state).toHaveTextContent("deployed");
    const ok = within(detail).getByTestId("api-docs-response-200");
    expect(ok).toHaveTextContent("array<ClusterOut>");
    expect(within(ok).getByTestId("api-docs-schema-row-owner")).toHaveTextContent("Owner | null");
    expect(within(ok).getByTestId("api-docs-schema-row-sub")).toHaveAttribute("data-depth", "1");
    expect(within(ok).getByTestId("api-docs-schema-row-parent")).toHaveTextContent("(순환)");
    expect(screen.getByTestId("api-docs-tag-default")).toHaveTextContent("/api/version");
  });

  it("falls back to the first operation when the initial id is unknown", () => {
    render(<OpenApiDocs doc={specPetstore} initialOperationId="nope" />);
    expect(screen.getByTestId("api-docs-op-listPets")).toHaveAttribute("aria-current", "true");
  });
});

describe("backend api detail: API docs dialog", () => {

  it("opens the docs dialog for the selected version, traps focus, closes on Escape, the close button and the backdrop, and a tool tag opens it with that operation selected", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets" />);
    const open = await screen.findByTestId("open-api-docs");
    await waitFor(() => expect(open).toBeEnabled());
    expect(screen.queryByTestId("api-docs-dialog")).not.toBeInTheDocument();
    await user.click(open);

    const dialog = await screen.findByTestId("api-docs-dialog");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect(dialog).toHaveTextContent("pets API 문서");
    expect(dialog).toHaveTextContent("v1");
    expect(screen.getByTestId("api-docs-close")).toHaveFocus();
    expect(await within(dialog).findByTestId("api-docs-detail")).toHaveTextContent("listPets");
    // Backend API 의 operationId 는 MCP Tool 이름이 되므로 안내가 보인다
    expect(within(dialog).getByTestId("api-docs-tool-name")).toBeInTheDocument();
    expect(lastRequest("GET", "/backend-apis/bp-pets/versions/1/spec")).toBeDefined();

    // Tab 순환: 마지막 요소에서 Tab 은 닫기 버튼으로 돌아온다
    const focusables = dialog.querySelectorAll<HTMLElement>("button, input, a[href]");
    focusables[focusables.length - 1].focus();
    await user.tab();
    expect(screen.getByTestId("api-docs-close")).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("api-docs-dialog")).not.toBeInTheDocument();
    expect(open).toHaveFocus();

    // 닫기 버튼과 배경으로도 닫힌다
    await user.click(open);
    await screen.findByTestId("api-docs-dialog");
    await user.click(screen.getByTestId("api-docs-close"));
    expect(screen.queryByTestId("api-docs-dialog")).not.toBeInTheDocument();

    await user.click(open);
    await screen.findByTestId("api-docs-dialog");
    await user.click(screen.getByTestId("api-docs-dialog-backdrop"));
    expect(screen.queryByTestId("api-docs-dialog")).not.toBeInTheDocument();

    // Tool 태그를 누르면 그 오퍼레이션을 고른 채 열린다
    await user.click(await screen.findByTestId("tool-getPet"));
    const toolDialog = await screen.findByTestId("api-docs-dialog");
    expect(await within(toolDialog).findByTestId("api-docs-op-getPet")).toHaveAttribute("aria-current", "true");
    expect(within(toolDialog).getByTestId("api-docs-operation-id")).toHaveTextContent("getPet");
    expect(within(toolDialog).getByTestId("api-docs-param-petId")).toHaveTextContent("The id of the pet");
  });

  it("uses the version selected in the Versions table", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    await user.click(await screen.findByTestId("version-row-1"));
    await user.click(screen.getByTestId("open-api-docs"));
    const dialog = await screen.findByTestId("api-docs-dialog");
    expect(dialog).toHaveTextContent("v1");
    await within(dialog).findByTestId("api-docs-detail");
    expect(lastRequest("GET", "/backend-apis/bp-orders/versions/1/spec")).toBeDefined();
  });

  it("shows the API error inside the dialog", async () => {
    server.use(
      http.get("/backend-apis/:id/versions/:n/spec", () => apiError(500, "integrity_error", "version 1 body missing")),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets" />);
    const open = await screen.findByTestId("open-api-docs");
    await waitFor(() => expect(open).toBeEnabled());
    await user.click(open);
    expect(await screen.findByTestId("api-docs-error")).toHaveTextContent("저장된 데이터가 서로 맞지 않습니다");
  });

  it("disables the button when the profile has no version", async () => {
    render(<TestApp initialPath="/backend-apis/bp-empty" />);
    expect(await screen.findByTestId("open-api-docs")).toBeDisabled();
  });
});

describe("backend api detail: Verify report folding", () => {
  it("is collapsed by default when the report has no warn or fail, and toggles", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets" />);
    const toggle = await screen.findByTestId("verify-report-toggle");
    await within(toggle).findByTestId("verify-report-counts");
    expect(toggle).toHaveTextContent("Verify report of v1");
    expect(toggle).toHaveTextContent("pass 4");
    expect(toggle).toHaveTextContent("warn 0");
    expect(toggle).toHaveTextContent("fail 0");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("verify-report-body")).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(screen.getByTestId("verify-report-body")).getByTestId("check-results")).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.queryByTestId("verify-report-body")).not.toBeInTheDocument();
  });

  it("경고가 있으면 펼친 채로 열리고, 접은 상태는 버전이 바뀌어도 그대로다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    const body = await screen.findByTestId("verify-report-body");
    expect(within(body).getByTestId("check-results")).toBeInTheDocument();
    const toggle = screen.getByTestId("verify-report-toggle");
    expect(toggle).toHaveTextContent("warn 1");
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.click(toggle);
    expect(screen.queryByTestId("verify-report-body")).not.toBeInTheDocument();

    // 접힘은 버전마다가 아니라 화면 하나의 상태다. 버전을 갈아 가며 볼 때 접어 둔 것이 버전마다
    // 따로 기억되면 다음 버전에서 또 접어야 한다
    await user.click(screen.getByTestId("version-row-1"));
    expect(screen.getByTestId("verify-report-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("verify-report-body")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("version-row-2"));
    expect(screen.getByTestId("verify-report-toggle")).toHaveAttribute("aria-expanded", "false");

    // 다시 펴면 그것도 따라온다
    await user.click(screen.getByTestId("verify-report-toggle"));
    expect(await screen.findByTestId("verify-report-body")).toBeInTheDocument();
    await user.click(screen.getByTestId("version-row-1"));
    expect(screen.getByTestId("verify-report-toggle")).toHaveAttribute("aria-expanded", "true");
  });
});
