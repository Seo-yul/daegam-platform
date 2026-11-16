import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { knowledgeErrorText } from "@/lib/errorText";
import { submitConfirm } from "./confirm";
import { HttpResponse, apiError, dsPostgres, dsRedis, http, lastRequest, reference, referencesState, server } from "./msw";
import { openSelect, selectValue } from "./select";

const ko = dictionary("ko");

/**
 * Tenant › Knowledge (D32). 목록·생성·상세(문서 표·업로드·URL·다시 인덱싱·삭제)·chunking 편집·삭제 대화상자.
 * 픽스처는 kb-faq(ready, 문서 셋: indexed·pending·failed) 와 kb-orphan(orphaned_table, deleting_at) 이다.
 */
describe("knowledge bases", () => {
  it("lists knowledge bases with Database, embedding, document counts and state chips", async () => {
    render(<TestApp initialPath="/knowledge-bases" />);
    const row = await screen.findByTestId("knowledge-base-row-faq");
    expect(row).toHaveTextContent("제품 FAQ");
    expect(row).toHaveTextContent("faq");
    await waitFor(() => expect(row).toHaveTextContent("Orders DB"));
    expect(row).toHaveTextContent("openai-main · text-embedding-3-small · 1536");
    expect(screen.getByTestId("knowledge-base-docs-faq")).toHaveTextContent("1/3");
    expect(screen.getByTestId("knowledge-base-docs-faq")).toHaveTextContent("실패 1");
    expect(screen.getByTestId("knowledge-base-state-faq")).toHaveTextContent("ready");
    // DROP 이 실패해 남은 것은 orphaned_table 칩과 테이블 이름이 같이 보인다
    expect(screen.getByTestId("knowledge-base-state-old-notes")).toHaveTextContent("orphaned_table");
    expect(screen.getByTestId("knowledge-base-row-old-notes")).toHaveTextContent("daegam_kb_fedcba9876543210fedcba9876543210");
    expect(screen.getByTestId("knowledge-base-new")).toHaveAttribute("href", "/knowledge-bases/new");
    expect(screen.getByTestId("nav-knowledge-bases")).toHaveTextContent("Knowledge Base");
  });

  it("creates a knowledge base: postgres Database only with a warning for one never verified, embedding providers only, model and dimension placeholders, POST body", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/new" />);
    await user.type(await screen.findByTestId("kb-name"), "product-faq");
    await user.type(screen.getByTestId("kb-display-name"), "제품 FAQ 2");
    // redis 는 보이지 않는다
    const dsList = await openSelect(user, "kb-data-service");
    expect(within(dsList).queryByRole("option", { name: /cache/ })).not.toBeInTheDocument();
    await user.click(within(dsList).getByRole("option", { name: /Orders DB/ }));
    // ds-pg 는 capabilities 가 비어 있다. verify 를 한 번도 안 돌린 상태라 경고하고 확인할 자리로 보낸다
    expect(await screen.findByTestId("kb-unverified-warning")).toHaveTextContent("아직 연결을 확인하지 않았습니다");
    expect(screen.getByTestId("kb-unverified-link")).toHaveAttribute("href", "/data-services/ds-pg");
    await selectValue(user, "kb-provider", "openai-main");
    expect(screen.getByTestId("kb-model")).toHaveAttribute("placeholder", "text-embedding-3-small");
    expect(screen.getByTestId("kb-dimensions")).toHaveAttribute("placeholder", "1536");
    expect(screen.getByTestId("kb-dimensions")).not.toBeRequired();
    expect(screen.getByTestId("kb-chunk-size")).toHaveValue(1200);
    expect(screen.getByTestId("kb-chunk-overlap")).toHaveValue(150);
    expect(screen.getByTestId("kb-max-chunks")).toHaveValue(100000);
    // 모르는 모델이면 차원이 필수다 (422 dimensions_required 를 미리 막는다)
    await user.type(screen.getByTestId("kb-model"), "my-embed");
    expect(screen.getByTestId("kb-dimensions")).toBeRequired();
    expect(screen.getByText("모르는 모델입니다. 차원을 입력해야 합니다.")).toBeInTheDocument();
    await user.clear(screen.getByTestId("kb-model"));
    await user.type(screen.getByTestId("kb-model"), "text-embedding-3-large");
    expect(screen.getByTestId("kb-dimensions")).toHaveAttribute("placeholder", "3072");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("knowledge-base-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/knowledge-bases")?.body).toEqual({
      name: "product-faq",
      display_name: "제품 FAQ 2",
      data_service_id: "ds-pg",
      embedding: { provider: "openai-main", model: "text-embedding-3-large", dimensions: null },
      chunking: { size: 1200, overlap: 150 },
      limits: { max_chunks: 100000 },
    });
    expect(screen.getByTestId("page-title")).toHaveTextContent("제품 FAQ 2");
    expect(screen.getByTestId("kb-embedding")).toHaveTextContent("openai-main · text-embedding-3-large · 3072");
  });

  it("warns when the Database lacks pgvector and shows a 422 code with a human hint", async () => {
    server.use(
      http.get("/data-services", () => HttpResponse.json([{ ...dsPostgres, capabilities: { pgvector: "missing" } }, dsRedis])),
      http.post("/knowledge-bases", () => apiError(422, "pgvector_missing", "CREATE EXTENSION vector failed: must be superuser to create this extension")),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/new" />);
    await user.type(await screen.findByTestId("kb-name"), "x");
    await selectValue(user, "kb-data-service", "ds-pg");
    expect(await screen.findByTestId("kb-pgvector-warning")).toHaveTextContent("pgvector 확장을 찾지 못했습니다");
    await selectValue(user, "kb-provider", "openai-main");
    await user.click(screen.getByTestId("save-button"));
    const err = await screen.findByTestId("save-error");
    expect(err).toHaveTextContent("pgvector_missing");
    expect(screen.getByTestId("save-error-hint")).toHaveTextContent("슈퍼유저에게 CREATE EXTENSION vector 를 먼저 실행해 달라고 요청해 주세요.");
    expect(screen.getByTestId("knowledge-base-new-page")).toBeInTheDocument();
  });

  it("detail shows the header, the document table with state chips, error codes with human text and no auto retry, and row actions", async () => {
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-base-detail-page");
    expect(screen.getByTestId("page-title")).toHaveTextContent("제품 FAQ");
    expect(screen.getByTestId("page-name")).toHaveTextContent("faq");
    expect(screen.getByTestId("kb-state")).toHaveTextContent("ready");
    expect(screen.getByTestId("kb-table")).toHaveTextContent("daegam_kb_0123456789abcdef0123456789abcdef");
    expect(screen.getByTestId("kb-embedding")).toHaveTextContent("openai-main · text-embedding-3-small · 1536");
    await waitFor(() => expect(screen.getByTestId("kb-data-service")).toHaveTextContent("Orders DB"));
    expect(screen.getByTestId("kb-documents")).toHaveTextContent("3개 중 1개 인덱싱됨");
    expect(screen.getByTestId("kb-tokens")).toHaveTextContent("3400 (추정)");

    const r0 = await screen.findByTestId("knowledge-doc-0");
    expect(r0).toHaveTextContent("guide.md");
    expect(r0).toHaveTextContent("upload");
    expect(r0).toHaveTextContent("2.0 KB");
    expect(screen.getByTestId("knowledge-doc-state-0")).toHaveTextContent("indexed");
    expect(within(r0).getAllByRole("cell")[4]).toHaveTextContent("12");
    expect(within(r0).getAllByRole("cell")[5]).toHaveTextContent("3400");
    const r1 = screen.getByTestId("knowledge-doc-1");
    expect(r1).toHaveTextContent("url");
    expect(r1).toHaveTextContent("https://example.com/faq.html");
    expect(screen.getByTestId("knowledge-doc-state-1")).toHaveTextContent("pending");
    expect(screen.getByTestId("knowledge-doc-state-2")).toHaveTextContent("failed");
    expect(screen.getByTestId("knowledge-doc-error-2")).toHaveTextContent("extract_failed");
    expect(screen.getByTestId("knowledge-doc-error-2")).toHaveAttribute("title", expect.stringContaining("PdfReadError"));
    // 실패한 문서는 코드 아래에 사람 문구를 펴고 자동 재시도가 없다는 것을 말한다
    expect(screen.getByTestId("knowledge-doc-error-text-2")).toHaveTextContent("문서에서 글을 읽어 내지 못했습니다");
    expect(screen.getByTestId("documents-failed-notice")).toHaveTextContent("자동으로 다시 시도하지 않습니다");
    // pending 문서가 있으므로 폴링이 돈다는 것도 적혀 있다
    expect(screen.getByTestId("docs-polling")).toHaveTextContent("5초마다");
    // 실패한 문서는 "다시 삭제", 나머지는 "삭제"
    expect(screen.getByTestId("knowledge-doc-delete-2")).toHaveTextContent("다시 삭제");
    expect(screen.getByTestId("knowledge-doc-delete-0")).toHaveTextContent("삭제");
    expect(screen.getByTestId("knowledge-doc-reindex-0")).toBeEnabled();
  });

  it("uploads on file pick, adds a URL, reindexes all and one, deletes a document and shows the deleting chip", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-doc-0");

    // 파일을 고르면 바로 multipart 로 올라가고 pending 행이 생긴다
    const file = new File(["# 재고 안내\n\ninventory is counted nightly."], "notes.md", { type: "text/markdown" });
    await user.upload(screen.getByTestId("knowledge-upload"), file);
    await waitFor(() => expect(lastRequest("POST", "/knowledge-bases/kb-faq/documents")).toBeDefined());
    const up = lastRequest("POST", "/knowledge-bases/kb-faq/documents")?.body as { file: { name: string; text: string } };
    expect(up.file.name).toBe("notes.md");
    expect(up.file.text).toContain("inventory");
    expect(await screen.findByTestId("knowledge-doc-3")).toHaveTextContent("notes.md");
    expect(screen.getByTestId("knowledge-doc-state-3")).toHaveTextContent("pending");
    expect(screen.getByTestId("kb-notice")).toHaveTextContent("업로드했습니다");

    // URL 추가. 이름을 비우면 null 로 보내고 서버가 마지막 조각을 이름으로 쓴다
    expect(screen.getByTestId("knowledge-url-add")).toBeDisabled();
    await user.type(screen.getByTestId("knowledge-url"), "https://example.com/docs/manual.html");
    await user.click(screen.getByTestId("knowledge-url-add"));
    await waitFor(() => expect(lastRequest("POST", "/knowledge-bases/kb-faq/documents/url")?.body).toEqual({ url: "https://example.com/docs/manual.html", name: null }));
    expect(await screen.findByTestId("knowledge-doc-4")).toHaveTextContent("manual.html");
    expect(screen.getByTestId("knowledge-url")).toHaveValue("");

    // 전체 다시 인덱싱을 누르면 모든 문서가 pending 이 된다
    await user.click(screen.getByTestId("knowledge-reindex-all"));
    await waitFor(() => expect(lastRequest("POST", "/knowledge-bases/kb-faq/reindex")).toBeDefined());
    expect(await screen.findByText("5개 문서를 다시 인덱싱합니다.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("knowledge-doc-state-0")).toHaveTextContent("pending"));

    // 문서 하나 다시 인덱싱
    await user.click(screen.getByTestId("knowledge-doc-reindex-2"));
    await waitFor(() => expect(lastRequest("POST", "/knowledge-bases/kb-faq/documents/doc-broken/reindex")).toBeDefined());

    // 문서 삭제는 확인 뒤 DELETE 이고 워커가 지울 때까지 deleting 칩이다
    await user.click(screen.getByTestId("knowledge-doc-delete-0"));
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/knowledge-bases/kb-faq/documents/doc-guide")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("knowledge-doc-state-0")).toHaveTextContent("deleting"));
    expect(screen.getByTestId("knowledge-doc-delete-0")).toBeDisabled();
    expect(screen.getByTestId("knowledge-doc-reindex-0")).toBeDisabled();
  });

  it("accepts office and hwpx documents, turns a file over 10MB back before sending, and explains the document parser's codes (D61)", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-doc-0");
    expect(screen.getByTestId("kb-documents-card")).toHaveTextContent("docx · pptx · xlsx · hwpx");
    expect(screen.getByTestId("knowledge-upload")).toHaveAttribute("accept", expect.stringContaining(".hwpx"));

    // 10MB 를 넘는 파일은 보내기 전에 고른 자리에서 되돌린다. 아래 hwpx 업로드가 POST 를 남기므로 그보다 먼저 본다
    const big = new File(["x"], "huge.pdf", { type: "application/pdf" });
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    await user.upload(screen.getByTestId("knowledge-upload"), big);
    expect(await screen.findByTestId("upload-too-large")).toHaveTextContent("11.0 MB");
    expect(screen.getByTestId("upload-too-large")).toHaveTextContent("10MB 까지라 올리지 않았습니다");
    expect(lastRequest("POST", "/knowledge-bases/kb-faq/documents")).toBeUndefined();

    await user.upload(screen.getByTestId("knowledge-upload"), new File(["PK"], "공문.hwpx", { type: "application/hwp+zip" }));
    expect(await screen.findByTestId("knowledge-doc-3")).toHaveTextContent("공문.hwpx");
    expect(knowledgeErrorText(ko, "encrypted_document")).toBe(ko.errors.codes.encrypted_document);
    expect(knowledgeErrorText(ko, "extract_unavailable")).toBe(ko.errors.codes.extract_unavailable);
    // URL 문서의 실패 코드는 접두로 묶어 한 문장이다
    expect(knowledgeErrorText(ko, "fetch_status")).toBe(ko.errors.codes.url_unreachable);
  });

  it("shows upload 413/422 codes with a hint and keeps the page", async () => {
    server.use(http.post("/knowledge-bases/:id/documents", () => apiError(413, "too_large", "document exceeds 10485760 bytes")));
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-doc-0");
    await user.upload(screen.getByTestId("knowledge-upload"), new File(["x"], "big.pdf", { type: "application/pdf" }));
    const err = await screen.findByTestId("upload-error");
    expect(err).toHaveTextContent("too_large");
    expect(screen.getByTestId("upload-error-hint")).toHaveTextContent(ko.errors.codes.too_large);
  });

  it("delete shows the bound agent on 409 and stays on the page", async () => {
    referencesState["knowledge-bases/kb-faq"] = [reference("agent", "ag-support", { display_name: "상담 에이전트", state: "deployed", revision: "both" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-base-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user);
    const err = await screen.findByTestId("confirm-error");
    expect(err).toHaveTextContent("다른 리소스가 사용하고 있어 삭제할 수 없습니다");
    expect(err).toHaveTextContent("Agent 상담 에이전트");
    expect(err).not.toHaveTextContent("ag-support");
    // 409 를 기다리지 않고 열 때부터 보인다. 정의와 배포본 어느 쪽이 붙드는지도 적는다
    const notice = screen.getByTestId("delete-references");
    expect(notice).toHaveTextContent("상담 에이전트");
    expect(notice).toHaveTextContent("정의와 배포본이 참조");
    expect(screen.getByTestId("knowledge-base-detail-page")).toBeInTheDocument();
  });

  it("orphaned_table detail locks writes and can be deleted again, returning to the list", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-orphan" />);
    await screen.findByTestId("knowledge-base-detail-page");
    expect(screen.getByTestId("kb-state")).toHaveTextContent("orphaned_table");
    expect(screen.getByTestId("kb-deleting-notice")).toHaveTextContent("삭제 중입니다");
    expect(screen.getByTestId("knowledge-upload")).toBeDisabled();
    expect(screen.getByTestId("knowledge-url-add")).toBeDisabled();
    expect(screen.getByTestId("knowledge-reindex-all")).toBeDisabled();
    await user.click(screen.getByTestId("delete-button"));
    await submitConfirm(user);
    expect(await screen.findByTestId("knowledge-base-list-page")).toBeInTheDocument();
    expect(lastRequest("DELETE", "/knowledge-bases/kb-orphan")).toBeDefined();
    await screen.findByTestId("knowledge-base-row-faq");
    expect(screen.queryByTestId("knowledge-base-row-old-notes")).not.toBeInTheDocument();
  });

  it("edits chunking with a reindex notice and PATCHes only the changed fields", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-base-detail-page");
    await waitFor(() => expect(screen.getByTestId("kb-chunk-size")).toHaveValue(1200));
    // 바뀐 것이 없으면 서버로 가지 않는다. 알림은 누른 버튼과 같은 폼 안에 뜬다
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("kb-form-notice")).toHaveTextContent("바뀐 것이 없어 저장하지 않았습니다.");
    expect(lastRequest("PATCH", "/knowledge-bases/kb-faq")).toBeUndefined();

    const size = screen.getByTestId("kb-chunk-size");
    await user.clear(size);
    await user.type(size, "800");
    expect(screen.getByTestId("kb-chunking-notice")).toHaveTextContent("모든 문서를 다시 인덱싱합니다");
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/knowledge-bases/kb-faq")?.body).toEqual({ chunking: { size: 800, overlap: 150 } }));
    expect(await screen.findByTestId("kb-form-notice")).toHaveTextContent("모든 문서를 다시 인덱싱합니다");
    await waitFor(() => expect(screen.getByTestId("knowledge-doc-state-0")).toHaveTextContent("pending"));
    expect(screen.queryByTestId("kb-chunking-notice")).not.toBeInTheDocument();
  });

  it("테이블을 못 지우면 대화상자를 닫지 않고 무엇이 남았는지 말한다", async () => {
    // 서버는 DROP TABLE 이 안 되면 200 으로 {deleted: false, warning: "table_not_dropped"} 를 준다.
    // 이것을 성공으로 읽으면 지웠다고 믿은 Knowledge Base 가 목록에 그대로 남는다
    server.use(http.delete("/knowledge-bases/:id", () => HttpResponse.json({ id: "kb-orphan", deleted: false, table_dropped: false, warning: "table_not_dropped" })));
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/kb-orphan" />);
    await screen.findByTestId("knowledge-base-detail-page");
    await user.click(screen.getByTestId("delete-button"));
    // 벡터 테이블 이름을 확인 문구가 미리 말한다
    expect(await screen.findByTestId("confirm-table")).toHaveTextContent("daegam_kb_fedcba9876543210fedcba9876543210");
    await submitConfirm(user);
    const warning = await screen.findByTestId("confirm-table-not-dropped");
    expect(warning).toHaveTextContent("벡터 테이블을 지우지 못해 Knowledge Base 가 남았습니다");
    expect(warning).toHaveTextContent("table_not_dropped");
    // 목록으로 튕기지 않는다
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-base-detail-page")).toBeInTheDocument();
  });

  it("목록에서 지울 때도 실패를 삼키지 않는다", async () => {
    server.use(http.delete("/knowledge-bases/:id", () => HttpResponse.json({ id: "kb-orphan", deleted: false, table_dropped: false, warning: "table_not_dropped" })));
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases" />);
    await screen.findByTestId("knowledge-base-row-old-notes");
    await user.click(screen.getByTestId("row-delete-old-notes"));
    await submitConfirm(user);
    expect(await screen.findByTestId("confirm-table-not-dropped")).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-base-row-old-notes")).toBeInTheDocument();
  });

  it("문서 목록을 못 불러오면 '아직 문서가 없습니다' 대신 오류와 다시 시도를 보인다", async () => {
    server.use(http.get("/knowledge-bases/:id/documents", () => apiError(502, "upstream_error", "indexer is unreachable")));
    render(<TestApp initialPath="/knowledge-bases/kb-faq" />);
    await screen.findByTestId("knowledge-base-detail-page");
    expect(await screen.findByTestId("state-error")).toBeInTheDocument();
    expect(screen.getByTestId("state-retry")).toBeInTheDocument();
    expect(screen.queryByText(/아직 문서가 없습니다/)).not.toBeInTheDocument();
  });

  it("pgvector 가 확인된 Database 에는 경고가 하나도 뜨지 않는다", async () => {
    server.use(http.get("/data-services", () => HttpResponse.json([{ ...dsPostgres, capabilities: { pgvector: "installed" } }, dsRedis])));
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/new" />);
    await selectValue(user, "kb-data-service", "ds-pg");
    await waitFor(() => expect(screen.queryByTestId("kb-unverified-warning")).not.toBeInTheDocument());
    expect(screen.queryByTestId("kb-pgvector-warning")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-pgvector-available")).not.toBeInTheDocument();
  });

  it("pgvector 를 설치할 수는 있는 Database 는 CREATE EXTENSION 이 실패할 수 있다고 말한다", async () => {
    server.use(http.get("/data-services", () => HttpResponse.json([{ ...dsPostgres, capabilities: { pgvector: "available" } }, dsRedis])));
    const user = userEvent.setup();
    render(<TestApp initialPath="/knowledge-bases/new" />);
    await selectValue(user, "kb-data-service", "ds-pg");
    expect(await screen.findByTestId("kb-pgvector-available")).toHaveTextContent("슈퍼유저가 아니면 그때 실패합니다");
  });

  it("만들기 화면의 두 셀렉트가 못 불러왔을 때 침묵하지 않는다", async () => {
    server.use(
      http.get("/data-services", () => apiError(503, "unavailable", "upstream is down")),
      http.get("/settings", () => apiError(503, "unavailable", "upstream is down")),
    );
    render(<TestApp initialPath="/knowledge-bases/new" />);
    expect(await screen.findByTestId("kb-data-services-error")).toHaveTextContent("Database 목록을 불러오지 못했습니다");
    expect(await screen.findByTestId("kb-settings-error")).toHaveTextContent("provider 목록을 불러오지 못했습니다");
    // "없습니다" 로는 말하지 않는다
    expect(screen.queryByTestId("kb-no-postgres")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-no-provider")).not.toBeInTheDocument();
  });
});
