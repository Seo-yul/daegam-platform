import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { ko } from "@/i18n/ko";
import { codeEditor } from "./editor";
import { submitConfirm } from "./confirm";
import { HttpResponse, apiError, http, lastRequest, profilePets, server } from "./msw";
import { selectValue } from "./select";

describe("backend apis", () => {
  it("lists profiles with kind, latest version and tool count", async () => {
    render(<TestApp initialPath="/backend-apis" />);
    const row = await screen.findByTestId("backend-api-row-orders");
    expect(row).toHaveTextContent("soap");
    expect(row).toHaveTextContent("2");
    expect(row).toHaveTextContent("3");
    const empty = screen.getByTestId("backend-api-row-empty");
    expect(empty).toHaveTextContent("-");
  });

  it("verifies pasted text and shows verdict, items and tools", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    expect(screen.getByTestId("verify-button")).toBeDisabled();
    await user.type(await codeEditor("spec-text"), "<definitions/>");
    await user.type(screen.getByTestId("spec-filename"), "calc.wsdl");
    await user.click(screen.getByTestId("verify-button"));

    const result = await screen.findByTestId("verify-result");
    expect(within(result).getByTestId("verdict-level")).toHaveTextContent("warn");
    expect(within(result).getByTestId("verify-source-format")).toHaveTextContent("wsdl");
    expect(within(result).getByTestId("verify-kind")).toHaveTextContent("soap");
    const item = within(result).getByTestId("verdict-item-tool-description.present");
    expect(item).toHaveTextContent("2");
    expect(item).toHaveTextContent("tool Add has no description");
    expect(within(result).getByTestId("tool-list")).toHaveTextContent("Add");
    expect(within(result).getByTestId("tool-list")).toHaveTextContent("Divide");

    const req = lastRequest("POST", "/backend-apis/verify");
    expect(req?.headers["content-type"]).toContain("application/json");
    expect(req?.body).toEqual({ text: "<definitions/>", filename: "calc.wsdl" });
  });

  it("verifies an uploaded file as multipart and reports an error verdict", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    const file = new File(["broken stuff"], "broken.txt", { type: "text/plain" });
    await user.upload(screen.getByTestId("spec-file"), file);
    await user.click(screen.getByTestId("verify-button"));
    expect(await screen.findByTestId("verdict-level")).toHaveTextContent("error");
    expect(screen.getByTestId("verdict-item-unsupported-format")).toBeInTheDocument();
    const req = lastRequest("POST", "/backend-apis/verify");
    expect(req?.headers["content-type"]).toContain("multipart/form-data");
    expect((req?.body as { file: { name: string } }).file.name).toBe("broken.txt");
  });

  it("generates yaml into the editor, then saves the edited yaml as the profile spec", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "<definitions/>");
    await user.click(screen.getByTestId("generate-button"));

    const editor = await codeEditor("generated-yaml");
    expect(editor.value).toContain("openapi: 3.0.3");
    expect(screen.getByTestId("generate-verdict-level")).toHaveTextContent("warn");
    expect(lastRequest("POST", "/backend-apis/convert")?.path).toBe("/api/backend-apis/convert?format=json");

    await user.type(editor, "# edited");
    await user.type(screen.getByTestId("bp-name"), "calc");
    await user.type(screen.getByTestId("bp-endpoint"), "https://calc.example.com");
    await selectValue(user, "bp-auth", "bearer");
    await user.type(screen.getByTestId("bp-secret"), "calc-cred");
    await user.click(screen.getByTestId("save-button"));

    expect(await screen.findByTestId("backend-api-detail-page")).toBeInTheDocument();
    const req = lastRequest("POST", "/backend-apis");
    const body = req?.body as { name: string; endpoint: string; auth: string; credential_secret: string; file: { name: string; text: string } };
    expect(body.name).toBe("calc");
    expect(body.endpoint).toBe("https://calc.example.com");
    expect(body.auth).toBe("bearer");
    expect(body.credential_secret).toBe("calc-cred");
    expect(body.file.name).toBe("calc.openapi.yaml");
    expect(body.file.text).toContain("# edited");
  });

  it("shows the generate error and the verdict items that came with the 422 instead of one line", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "broken");
    await user.click(screen.getByTestId("generate-button"));
    expect(await screen.findByTestId("generate-error")).toHaveTextContent("verdict_error");
    expect(screen.queryByTestId("generate-result")).not.toBeInTheDocument();
    const verdict = await screen.findByTestId("generate-error-verdict");
    expect(within(verdict).getByTestId("verdict-item-unsupported-format")).toHaveTextContent("could not recognise the document");
  });

  it("shows the SOAP header Secret only for a verified SOAP spec and sends it with the upload", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "<definitions/>");
    expect(screen.queryByTestId("bp-soap-secret")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("verify-button"));
    await screen.findByTestId("verify-result");
    expect(screen.getByText(ko.backendApis.detail.soapHeadersHint)).toBeInTheDocument();
    await user.type(screen.getByTestId("bp-name"), "calc");
    await user.type(screen.getByTestId("bp-soap-secret"), "calc-soap");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("backend-api-detail-page");
    expect(lastRequest("POST", "/backend-apis")?.body).toMatchObject({ soap_headers_secret: "calc-soap" });
  });

  it("keeps a typed SOAP header Secret when editing the spec clears the verify result, and sends it", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    const editor = await codeEditor("spec-text");
    await user.type(editor, "<definitions/>");
    await user.click(screen.getByTestId("verify-button"));
    await screen.findByTestId("verify-result");
    await user.type(screen.getByTestId("bp-soap-secret"), " calc-soap ");
    await user.type(editor, " ");
    expect(screen.queryByTestId("verify-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("bp-soap-secret")).toHaveValue(" calc-soap ");
    await user.type(screen.getByTestId("bp-name"), "calc");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("backend-api-detail-page");
    expect(lastRequest("POST", "/backend-apis")?.body).toMatchObject({ soap_headers_secret: "calc-soap" });
  });

  it("saves pasted text directly when Generate was not used", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "{{\"swagger\": \"2.0\"}");
    await user.type(screen.getByTestId("bp-name"), "legacy");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("backend-api-detail-page");
    const body = lastRequest("POST", "/backend-apis")?.body as { file: { name: string; text: string } };
    expect(body.file.name).toBe("spec.json");
    expect(body.file.text).toBe('{"swagger": "2.0"}');
  });

  it("detail shows versions, per-check report, connection patch and tokens", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    expect(await screen.findByTestId("summary-latest-version")).toHaveTextContent("2");
    expect(await screen.findByTestId("version-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("version-row-1")).toBeInTheDocument();

    // 최신 버전이 자동 선택되고 체크별 결과가 뜬다
    const report = await screen.findByTestId("check-results");
    expect(within(report).getByTestId("check-status-document.openapi3")).toHaveTextContent("pass");
    expect(within(report).getByTestId("check-status-tool-description.present")).toHaveTextContent("warn");
    expect(within(report).getByTestId("check-status-fastmcp.roundtrip")).toHaveTextContent("skip");

    // 접속 정보 PATCH
    const endpoint = screen.getByTestId("conn-endpoint");
    await user.clear(endpoint);
    await user.type(endpoint, "https://orders-v2.example.com");
    await user.click(screen.getByTestId("connection-save"));
    await screen.findByTestId("connection-saved");
    expect(lastRequest("PATCH", "/backend-apis/bp-orders")?.body).toEqual({
      endpoint: "https://orders-v2.example.com",
      auth: "bearer",
      credential_secret: "orders-cred",
      soap_headers_secret: null,
    });

    // 토큰 목록·발급·회수
    expect(await screen.findByTestId("token-row-tok-1")).toHaveTextContent("active");
    await user.type(screen.getByTestId("token-label"), "runtime");
    await user.click(screen.getByTestId("token-issue"));
    expect(await screen.findByTestId("issued-token-value")).toHaveTextContent("secret-token-value");
    // 한 번 보이는 칸은 개인 MCP 토큰과 같은 공용 칸이라 복사 버튼이 붙는다
    expect(screen.getByTestId("issued-token")).toHaveTextContent("이 토큰은 지금 한 번만 보입니다. 복사해 주세요.");
    expect(screen.getByTestId("issued-token-copy")).toHaveAccessibleName("토큰 복사");
    expect(lastRequest("POST", "/backend-apis/bp-orders/tokens")?.body).toEqual({ label: "runtime" });
    await user.click(screen.getByTestId("token-revoke-tok-1"));
    // 요청이 나갔는지가 아니라 목록이 다시 읽혀 상태가 바뀌었는지를 본다
    await waitFor(() => expect(screen.getByTestId("token-row-tok-1")).not.toHaveTextContent("active"));
    expect(lastRequest("DELETE", "/backend-apis/bp-orders/tokens/tok-1")).toBeDefined();
  });

  it("SOAP 백엔드의 접속 정보에서 SOAP Header Secret 이름을 저장하고, 규칙에 맞지 않는 이름은 그 오류 문구를 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    const field = await screen.findByTestId("conn-soap-secret");
    expect(screen.getByText(ko.backendApis.connection.labels.soapHeadersSecret)).toBeInTheDocument();
    expect(screen.getByText(ko.backendApis.detail.soapHeadersHint)).toBeInTheDocument();
    await user.type(field, "orders-soap");
    await user.click(screen.getByTestId("connection-save"));
    await screen.findByTestId("connection-saved");
    expect(lastRequest("PATCH", "/backend-apis/bp-orders")?.body).toMatchObject({ soap_headers_secret: "orders-soap" });

    server.use(http.patch("/backend-apis/:id", () => apiError(422, "invalid_soap_headers_secret", "invalid soap_headers_secret")));
    await user.clear(field);
    await user.type(field, "Bad_Name");
    await user.click(screen.getByTestId("connection-save"));
    expect(await screen.findByTestId("connection-error")).toHaveTextContent(ko.errors.codes.invalid_soap_headers_secret);
  });

  it("REST 백엔드의 접속 정보에는 SOAP Header Secret 칸이 없고 PATCH 에도 싣지 않는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets" />);
    await screen.findByTestId("conn-endpoint");
    expect(screen.queryByTestId("conn-soap-secret")).toBeNull();
    await user.click(screen.getByTestId("connection-save"));
    await screen.findByTestId("connection-saved");
    expect(lastRequest("PATCH", "/backend-apis/bp-pets")?.body).not.toHaveProperty("soap_headers_secret");
  });

  it("REST 백엔드라도 SOAP Header Secret 이 이미 있으면 칸을 보여 지울 수 있다", async () => {
    const user = userEvent.setup();
    server.use(http.get("/backend-apis/bp-pets", () => HttpResponse.json({ ...profilePets, soap_headers_secret: "pets-soap" })));
    render(<TestApp initialPath="/backend-apis/bp-pets" />);
    const field = await screen.findByTestId("conn-soap-secret");
    expect(field).toHaveValue("pets-soap");
    await user.clear(field);
    await user.click(screen.getByTestId("connection-save"));
    await screen.findByTestId("connection-saved");
    expect(lastRequest("PATCH", "/backend-apis/bp-pets")?.body).toMatchObject({ soap_headers_secret: null });
  });

  it("warns that an existing name adds a version to that profile and overwrites its connection", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "openapi: 3.0.0");
    expect(screen.getByTestId("save-button")).toHaveTextContent("저장");

    await user.type(screen.getByTestId("bp-name"), "orders");
    const warn = await screen.findByTestId("bp-name-exists");
    expect(warn).toHaveTextContent("v3");
    expect(warn).toHaveTextContent("덮어씁니다");
    expect(within(warn).getByRole("link")).toHaveAttribute("href", "/backend-apis/bp-orders");
    expect(screen.getByTestId("save-button")).toHaveTextContent("새 버전으로 저장");
  });

  it("문서 편집은 문서만 다루고 접속 정보와 이름 칸을 두지 않는다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    await screen.findByTestId("backend-api-detail-page");
    expect(screen.getByTestId("edit-button")).toHaveTextContent("문서 편집");
    await user.click(screen.getByTestId("edit-button"));

    // 제목이 대상의 표시 이름이다. 리소스 종류만 적힌 제목은 어느 것인지 말해 주지 않는다
    expect(await screen.findByTestId("page-title")).toHaveTextContent("Orders SOAP");
    expect(screen.getByTestId("page-name")).toHaveTextContent("orders");
    // Endpoint·Auth·Credential Secret 은 상세의 "접속 정보 저장" 이 이미 PATCH 로 고친다. 두 자리에
    // 같은 값을 두면 어느 쪽이 이겼는지 알 수 없다. Name 은 생성 뒤 바꿀 수 없어 보여 줄 것도 없다
    expect(screen.queryByTestId("bp-endpoint")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bp-auth")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bp-secret")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bp-name")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bp-display-name")).not.toBeInTheDocument();

    // 만들기 쪽의 "이름이 겹친다" 경고와 뜻이 달라 testid 도 다르다
    const note = screen.getByTestId("bp-version-note");
    expect(note).toHaveTextContent("저장하면 v3이 새로 생깁니다");
    expect(screen.queryByTestId("bp-name-exists")).not.toBeInTheDocument();
    // 지금 무엇이 등록돼 있는지 보인다. 변환된 OpenAPI 가 아니라 올린 원본(WSDL)이다
    expect(await codeEditor("registered-spec-text")).toHaveValue('<definitions name="Calculator"><portType name="Calc"><operation name="Add"/></portType></definitions>');
    // 지금 등록된 버전이다. 저장하면 생길 v3 이 아니다
    expect(screen.getByTestId("registered-spec-version")).toHaveTextContent("v2");
    expect(screen.getByTestId("registered-spec-format")).toHaveTextContent("source.wsdl");
    expect(screen.getByTestId("save-button")).toHaveTextContent("새 버전 저장");

    // 한 줄짜리 WSDL 은 그대로 띄우면 읽을 수 없다. 보기만 들여쓰고 저장된 본문은 그대로다
    await user.click(screen.getByTestId("registered-spec-pretty"));
    expect(await codeEditor("registered-spec-text")).toHaveValue(
      '<definitions name="Calculator">\n  <portType name="Calc">\n    <operation name="Add"/>\n  </portType>\n</definitions>',
    );
    // 보기만 바꾼 것이라 "편집기로 가져오기" 는 저장된 원본을 그대로 넣는다
    await user.click(screen.getByTestId("use-registered-spec"));
    expect(await codeEditor("spec-text")).toHaveValue('<definitions name="Calculator"><portType name="Calc"><operation name="Add"/></portType></definitions>');
  });

  it("표시 이름은 제목 옆 연필로 그 자리에서 고치되 빈 이름은 막고 PATCH 하나로 끝나며, 편집에서 취소하면 목록이 아니라 온 상세로 돌아간다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders/edit" />);
    await screen.findByTestId("registered-spec");
    await user.click(screen.getByTestId("rename-button"));

    // 공백만 넣은 것과 아예 비운 것 둘 다 막혀야 한다. 만들기 폼의 규칙(validateDisplayName)은 빈 값을
    // "Name 을 그대로 쓴다" 로 통과시키는데, 여기서 비우는 것은 지우는 것이라 서버가 422 다
    await user.clear(screen.getByTestId("rename-input"));
    await user.type(screen.getByTestId("rename-input"), "   ");
    expect(screen.getByTestId("rename-save")).toBeDisabled();
    expect(screen.getByTestId("rename-feedback")).toHaveTextContent("공백만으로 채울 수 없습니다");

    await user.clear(screen.getByTestId("rename-input"));
    expect(screen.getByTestId("rename-input")).toHaveValue("");
    expect(screen.getByTestId("rename-save")).toBeDisabled();
    expect(screen.getByTestId("rename-feedback")).toHaveTextContent("표시 이름을 입력해 주세요");
    expect(lastRequest("PATCH", "/backend-apis/bp-orders")).toBeUndefined();

    // 비운 칸에 새 이름을 넣으면 저장된다
    await user.type(screen.getByTestId("rename-input"), "주문 SOAP");
    await user.click(screen.getByTestId("rename-save"));

    // 이름을 고치려고 스펙을 다시 올리지 않는다. 버전은 문서가 바뀔 때만 생긴다
    await waitFor(() => expect(lastRequest("PATCH", "/backend-apis/bp-orders")).toBeDefined());
    expect(lastRequest("PATCH", "/backend-apis/bp-orders")?.body).toEqual({ display_name: "주문 SOAP" });
    expect(lastRequest("POST", "/backend-apis")).toBeUndefined();
    await waitFor(() => expect(screen.queryByTestId("rename-input")).not.toBeInTheDocument());

    // 취소는 목록이 아니라 편집에 들어온 상세로 돌아간다
    await user.click(screen.getByTestId("cancel-button"));
    expect(await screen.findByTestId("backend-api-detail-page")).toBeInTheDocument();
  });

  it("등록된 문서를 편집기로 가져오면 확장자까지 따라온다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders/edit" />);
    await screen.findByTestId("registered-spec");
    await user.click(await screen.findByTestId("use-registered-spec"));

    expect(await codeEditor("spec-text")).toHaveValue('<definitions name="Calculator"><portType name="Calc"><operation name="Add"/></portType></definitions>');
    // WSDL 을 고쳤는데 YAML 로 등록되면 출처가 바뀐다
    expect(screen.getByTestId("spec-filename")).toHaveValue("spec.wsdl");
    await user.click(screen.getByTestId("save-button"));
    await screen.findByTestId("backend-api-detail-page");
    const body = lastRequest("POST", "/backend-apis")?.body as Record<string, unknown> & { file: { name: string; text: string } };
    expect(body.file.name).toBe("spec.wsdl");
    expect(body.file.text).toBe('<definitions name="Calculator"><portType name="Calc"><operation name="Add"/></portType></definitions>');
    // 문서와 이름만 보낸다. 접속 정보를 함께 보내면 상세에서 고친 값을 덮어쓴다
    expect(Object.keys(body).sort()).toEqual(["file", "name"]);
    expect(body.name).toBe("orders");
  });

  it("없는 id 로 편집에 들어오면 빈 폼이 아니라 찾을 수 없습니다다", async () => {
    render(<TestApp initialPath="/backend-apis/nope/edit" />);
    expect(await screen.findByTestId("not-found-page")).toBeInTheDocument();
    expect(screen.queryByTestId("backend-api-form")).not.toBeInTheDocument();
  });

  it("drops a stale Generate result when the spec input changes, and says what will be saved", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/new" />);
    await user.type(await codeEditor("spec-text"), "<definitions/>");
    await user.click(screen.getByTestId("generate-button"));
    await codeEditor("generated-yaml");
    expect(screen.getByTestId("save-target")).toHaveTextContent("OpenAPI 로 변환한 결과");

    // 화면에는 새 스펙이 보이는데 저장되는 것은 옛 결과였다
    await user.type(await codeEditor("spec-text"), "x");
    expect(screen.queryByTestId("generate-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("save-target")).toHaveTextContent("붙여넣은 스펙 텍스트");
  });

  it("delete explains 409 references and offers force when tokens are active", async () => {
    server.use(
      http.delete("/backend-apis/:id", ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("force") === "true") {
          return Response.json({ id: "bp-orders", deleted: true, revoked_token_ids: ["tok-1"] });
        }
        return apiError(409, "tokens_active", "backend api has active read tokens", { token_ids: ["tok-1"] });
      }),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    await screen.findByTestId("backend-api-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    expect(screen.queryByTestId("delete-force-button")).not.toBeInTheDocument();
    await submitConfirm(user);
    const err = await screen.findByTestId("confirm-error");
    expect(err).toHaveTextContent("회수하지 않은 읽기 토큰이 있어 삭제할 수 없습니다");
    // 토큰은 이름이 없어 개수만 말한다. 날 id 는 화면에 나오지 않는다 (D43 넷째)
    expect(err).toHaveTextContent("읽기 토큰 1개");
    expect(err).not.toHaveTextContent("tok-1");
    // 강제 삭제는 같은 대화상자 안에서
    await user.click(within(screen.getByTestId("confirm-dialog")).getByTestId("delete-force-button"));
    expect(await screen.findByTestId("backend-apis-page")).toBeInTheDocument();
  });
});
