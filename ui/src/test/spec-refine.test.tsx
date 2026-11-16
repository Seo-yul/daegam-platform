import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { POLL_MS } from "@/lib/specJobs";
import { apiError, http, lastRequest, platformSettings, refineJobState, server, HttpResponse } from "./msw";
import { selectValue } from "./select";
import { advanceClock, tickingClock } from "./timers";

/**
 * Backend API 버전 다듬기 (D25 F4): Generate 메뉴 → Refine 화면 → 규칙/LLM 실행 → 좌우 diff 채택 → apply-draft.
 * Monaco DiffEditor 는 스텁(original·modified textarea + monaco-reveal) 이다.
 */
describe("backend api refine", () => {
  const modified = () => (screen.getByTestId("monaco-modified") as HTMLTextAreaElement).value;
  const original = () => (screen.getByTestId("monaco-original") as HTMLTextAreaElement).value;

  it("version row Generate menu links to the refine screen with the chosen mode", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders" />);
    const row = await screen.findByTestId("version-row-2");
    await user.click(within(row).getByTestId("generate-menu"));
    const panel = await screen.findByTestId("generate-menu-panel");
    expect(within(panel).getByTestId("generate-minify")).toHaveAttribute("href", "/backend-apis/bp-orders/versions/2/refine?mode=minify");
    await user.click(within(panel).getByTestId("generate-llm"));
    const page = await screen.findByTestId("refine-page");
    expect(page).toHaveAttribute("data-mode", "llm");
    expect(screen.getByTestId("page-title")).toHaveTextContent("Orders SOAP · v2");
    expect(screen.getByTestId("refine-back")).toHaveAttribute("href", "/backend-apis/bp-orders");
  });

  it("minify runs immediately, groups changes per operation, rebuilds the right YAML when a change is unchecked, and jump reveals the pointer line in the original editor, falling back to the parent for added keys", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets/versions/1/refine?mode=minify" />);
    expect(await screen.findByTestId("refine-page")).toHaveAttribute("data-mode", "minify");
    await user.click(screen.getByTestId("enrich-errors"));
    await user.type(screen.getByTestId("max-description"), "120");
    await user.click(screen.getByTestId("drop-value-examples"));
    await user.click(screen.getByTestId("refine-run"));

    expect(await screen.findByTestId("spec-diff")).toBeInTheDocument();
    // Monaco 는 lazy 라 스텁도 한 틱 뒤에 뜬다
    await screen.findByTestId("monaco-diff");
    expect(lastRequest("POST", "/backend-apis/bp-pets/versions/1/minify")?.body).toEqual({
      enrich: ["errors"],
      max_description: 120,
      drop_value_examples: true,
    });
    expect(screen.getByTestId("spec-diff-summary")).toHaveTextContent("operation 4개 중 2개의 설명이 바뀌었습니다. 호출 표면은 바뀌지 않았습니다.");
    expect(screen.getByTestId("spec-diff-count")).toHaveTextContent("변경 5개 · 채택 5개");

    // operation 묶음은 문서 순서, 스키마 항목은 참조 operation 수를 보인다
    const list = screen.getByTestId("change-list");
    const groups = within(list).getAllByTestId(/^change-group-/);
    expect(groups.map((g) => g.getAttribute("data-testid"))).toEqual(["change-group-op:get:/pets", "change-group-op:get:/pets/{petId}", "change-group-schema:Error", "change-group-other:x-s2o"]);
    expect(within(screen.getByTestId("change-group-schema:Error")).getByTestId("group-subtitle")).toHaveTextContent("operation 1개가 참조");
    expect(screen.getByTestId("change-m3")).toHaveTextContent("삭제");
    expect(screen.getByTestId("change-m1")).toHaveTextContent("추가");

    // 오른쪽은 채택한 변경을 심은 YAML, 왼쪽은 원본이다
    expect(original()).toContain("x-internal-id: err-1");
    expect(original()).not.toContain("Errors: default");
    expect(modified()).toContain("Errors: default: unexpected error.");
    expect(modified()).toContain("Returns the pet. The owner is included when known.");
    expect(modified()).not.toContain("x-internal-id");
    expect(modified()).not.toContain("example: 500");
    expect(modified()).toContain("x-s2o:");

    // 채택을 풀면 그 항목만 원본으로 돌아간다
    await user.click(screen.getByTestId("change-m2-accept"));
    expect(screen.getByTestId("change-m2")).toHaveAttribute("data-accepted", "false");
    expect(modified()).toContain("The owner is included when known.");
    expect(modified()).not.toContain("Returns the pet. The owner is included when known.");
    expect(modified()).toContain("Errors: default: unexpected error.");
    expect(screen.getByTestId("spec-diff-count")).toHaveTextContent("채택 4개");
    await user.click(screen.getByTestId("accept-none"));
    expect(modified()).toBe(original());
    await user.click(screen.getByTestId("accept-all"));
    expect(screen.getByTestId("spec-diff-count")).toHaveTextContent("채택 5개");

    // 이동은 원본 편집기의 그 줄을 보인다. 채택을 바꿔도 왼쪽 원본은 그대로라 같은 diff 에서 본다
    const lines = original().split("\n");

    await user.click(screen.getByTestId("change-m2-jump"));
    const at = () => Number(screen.getByTestId("monaco-reveal").textContent?.split(":")[1]);
    expect(screen.getByTestId("monaco-reveal")).toHaveTextContent(/^original:\d+$/);
    expect(lines[at() - 1]).toContain("description: |-");

    // added 항목(/paths/~1pets/get/description) 은 원본에 없어 부모 get: 줄로 간다
    await user.click(screen.getByTestId("change-m1-jump"));
    expect(lines[at() - 1]).toMatch(/^\s+get:$/);
    // 부모도 없는 루트 추가(/x-s2o/minify) 는 조상이 없어 이동하지 않는다
    const before = at();
    await user.click(screen.getByTestId("change-m5-jump"));
    expect(at()).toBe(before);

    await user.click(screen.getByTestId("diff-layout"));
    expect(screen.getByTestId("monaco-diff")).toHaveAttribute("data-side-by-side", "false");
  });

  it("apply-draft sends the minify options with accepted_ids and returns to the detail with the new version selected", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets/versions/1/refine?mode=minify" />);
    await screen.findByTestId("refine-page");
    await user.click(screen.getByTestId("enrich-errors"));
    await user.click(screen.getByTestId("refine-run"));
    await screen.findByTestId("spec-diff");
    await user.click(screen.getByTestId("change-m4-accept"));
    await user.click(screen.getByTestId("apply-draft"));

    expect(await screen.findByTestId("backend-api-detail-page")).toBeInTheDocument();
    expect(lastRequest("POST", "/backend-apis/bp-pets/versions/1/apply-draft")?.body).toEqual({
      minify: { enrich: ["errors"], drop_value_examples: false },
      accepted_ids: ["m1", "m2", "m3", "m5"],
    });
    const row = await screen.findByTestId("version-row-2");
    expect(row).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("version-row-1")).not.toHaveAttribute("data-selected");
    expect(screen.getByTestId("summary-latest-version")).toHaveTextContent("2");
  });

  it("shows a 422 from apply-draft under the change list as a field error", async () => {
    server.use(
      http.post("/backend-apis/:id/versions/:n/apply-draft", () =>
        apiError(422, "unknown_change_id", "unknown change ids: m9", { accepted_ids: ["m9"] }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-pets/versions/1/refine?mode=minify" />);
    await screen.findByTestId("refine-page");
    await user.click(screen.getByTestId("refine-run"));
    await screen.findByTestId("spec-diff");
    await user.click(screen.getByTestId("apply-draft"));
    expect(await screen.findByTestId("refine-error-accepted_ids")).toHaveTextContent("unknown_change_id: unknown change ids: m9");
    expect(screen.queryByTestId("save-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("refine-page")).toBeInTheDocument();

    // 필드에 못 잇는 422 는 save-error 로 간다
    server.use(http.post("/backend-apis/:id/versions/:n/apply-draft", () => apiError(422, "verdict_error", "draft did not pass verification")));
    await user.click(screen.getByTestId("apply-draft"));
    expect(await screen.findByTestId("save-error")).toHaveTextContent("verdict_error");
  });

  it("picks a running llm job back up from the URL, and says it cannot be stopped", async () => {
    // job id 가 로컬 상태였을 때는 새로고침 한 번에 화면이 job 을 잃었다. 서버에서는 계속 돌고
    // 토큰도 계속 쓰는데 그 결과에 다시 붙을 길이 없었다. 이제는 주소만 있으면 이어 본다
    render(<TestApp initialPath="/backend-apis/bp-orders/versions/2/refine?mode=llm&job=job-refine-1" />);
    await screen.findByTestId("refine-page");
    expect(screen.getByTestId("refine-llm-notice")).toHaveTextContent("멈출 수 없습니다");
    // 실행을 누르지 않았는데도 그 job 을 따라간다
    // job 이 오기 전 JobProgress 는 <p>, 온 뒤에는 <div> 라 같은 testid 의 요소가 통째로 교체된다.
    // 잡아 둔 노드를 재사용하면 분리된 <p> 를 보게 되어 data-state 가 영영 안 바뀐다(느릴 때만 재현).
    await screen.findByTestId("refine-progress");
    const progress = () => screen.getByTestId("refine-progress");
    await waitFor(() => expect(progress()).toHaveAttribute("data-state", "running"));
    expect(lastRequest("GET", "/spec-generator/jobs/job-refine-1")).toBeDefined();
  });

  it("llm mode picks the default provider, polls the job to completion and warns about renamed operationIds with referencing profiles", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders/versions/2/refine?mode=llm" />);
    await screen.findByTestId("refine-page");
    await waitFor(() => expect(screen.getByTestId("refine-provider")).toHaveAttribute("value", "openai-main"));
    expect(screen.getByTestId("refine-provider")).toHaveTextContent("openai-main (기본)");
    expect(screen.getByTestId("preset-enrich_descriptions")).toBeChecked();
    await user.click(screen.getByTestId("preset-normalize_operation_ids"));
    await user.type(screen.getByTestId("refine-instruction"), "짧게 씁니다");
    // job 은 POLL_MS 마다 다시 묻는다. 그 간격을 실제로 기다리지 않도록 실행부터는 가짜 시계로 돈다
    tickingClock();
    await user.click(screen.getByTestId("refine-run"));

    expect(lastRequest("POST", "/spec-generator/jobs")?.body).toEqual({
      mode: "refine",
      source: { backend_profile_id: "bp-orders", version: 2 },
      presets: ["enrich_descriptions", "normalize_operation_ids"],
      instruction: "짧게 씁니다",
      provider: "openai-main",
    });
    // 첫 폴링: running, 반복 1/5 와 실패 항목 요약. running 은 목이 finish() 를 받을 때까지 이어진다
    await screen.findByTestId("refine-progress");
    const progress = () => screen.getByTestId("refine-progress");
    await waitFor(() => {
      expect(progress()).toHaveAttribute("data-state", "running");
      expect(screen.getByTestId("refine-iterations")).toHaveTextContent("반복 1/5");
      expect(screen.getByTestId("refine-iteration-1")).toHaveTextContent("실패 2건");
    });
    expect(screen.getByTestId("refine-iteration-1")).toHaveTextContent("surface.changed:/paths/~1pets/get/tags");
    expect(screen.getByTestId("refine-run")).toBeDisabled();
    refineJobState.finish();

    // 두 번째 폴링: done, changes 와 renamed. 시계를 다음 폴링까지 넘기면 done 이 오고 그때 diff 가 선다
    await advanceClock(POLL_MS);
    expect(await screen.findByTestId("spec-diff")).toBeInTheDocument();
    await screen.findByTestId("monaco-diff");
    expect(progress()).toHaveAttribute("data-state", "done");
    expect(screen.getByTestId("refine-iterations")).toHaveTextContent("반복 2/5");
    expect(screen.getByTestId("refine-tokens")).toHaveTextContent("토큰 3,400");
    expect(screen.getByTestId("refine-run")).toBeEnabled();
    expect(screen.getByTestId("spec-diff-count")).toHaveTextContent("변경 4개 · 채택 4개");
    expect(modified()).toContain("operationId: list_pets");
    expect(modified()).toContain("The pet's display name");

    const warning = screen.getByTestId("renamed-warning");
    expect(warning).toHaveTextContent("operationId 가 바뀝니다. 이 백엔드를 참조하는 MCP Server 는 다시 배포하기 전에 확인해 주세요.");
    expect(within(warning).getByTestId("renamed-list")).toHaveTextContent("listPets → list_pets");
    const ref = await within(warning).findByTestId("renamed-profile-calc");
    expect(ref).toHaveAttribute("href", "/mcp-servers/ms-calc");
    expect(lastRequest("GET", "/mcp-servers")?.path).toBe("/api/mcp-servers");

    // 적용 본문은 job_id 와 채택 id 다
    await user.click(screen.getByTestId("change-r2-accept"));
    await user.click(screen.getByTestId("apply-draft"));
    await screen.findByTestId("backend-api-detail-page");
    expect(lastRequest("POST", "/backend-apis/bp-orders/versions/2/apply-draft")?.body).toEqual({
      job_id: "job-refine-1",
      accepted_ids: ["r1", "r3", "r4"],
    });
  });

  it("llm mode shows provider_no_key under the provider field and a failed job as an error", async () => {
    server.use(
      http.get("/settings", () =>
        HttpResponse.json({
          ...platformSettings,
          llm_providers: [{ name: "no-key", kind: "openai", model: null, api_key_secret: null, base_url: null }],
          defaults: { spec_generator: "no-key" },
        }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders/versions/2/refine?mode=llm" />);
    await screen.findByTestId("refine-page");
    await waitFor(() => expect(screen.getByTestId("refine-provider")).toHaveAttribute("value", "no-key"));
    expect(screen.getByTestId("refine-provider")).toHaveTextContent("키 없음");
    await user.click(screen.getByTestId("refine-run"));
    // 코드가 아니라 errorText 표의 한국어 처방이 선다 (D43 첫째)
    expect(await screen.findByTestId("refine-error-provider")).toHaveTextContent("provider 의 API 키를 읽지 못했습니다");
    expect(screen.queryByTestId("refine-run-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("spec-diff")).not.toBeInTheDocument();

    // job 이 failed 로 끝나면 error 를 보인다
    server.use(
      http.post("/spec-generator/jobs", () => HttpResponse.json({ id: "job-refine-1", state: "queued", iterations: [], usage_total: {} }, { status: 202 })),
      http.get("/spec-generator/jobs/:id", () =>
        HttpResponse.json({
          id: "job-refine-1",
          tenant: "default",
          state: "failed",
          provider: "no-key",
          hints: {},
          attempt: 1,
          iterations: [{ n: 1, report_summary: null, failed_ids: ["surface.changed:/paths/~1pets/get/tags"], usage: { input_tokens: 10, output_tokens: 5 }, note: null, finished_at: "x" }],
          usage_total: { input_tokens: 10, output_tokens: 5 },
          error: { code: "max_iterations", detail: "no passing draft after 5 iterations" },
          report: null,
          verdict: null,
          created_at: "x",
          updated_at: "x",
          finished_at: "x",
          expires_at: null,
        }),
      ),
    );
    await user.click(screen.getByTestId("refine-run"));
    expect(await screen.findByTestId("refine-job-failed")).toHaveTextContent("max_iterations: no passing draft after 5 iterations");
    expect(screen.getByTestId("refine-progress")).toHaveAttribute("data-state", "failed");
    expect(screen.queryByTestId("spec-diff")).not.toBeInTheDocument();
  });

  it("llm mode without providers points to Platform Settings and disables Run", async () => {
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [], defaults: { spec_generator: null } })));
    render(<TestApp initialPath="/backend-apis/bp-orders/versions/2/refine?mode=llm" />);
    const alert = await screen.findByTestId("refine-no-provider");
    expect(alert).toHaveTextContent("등록된 provider 가 없습니다");
    expect(within(alert).getByTestId("refine-settings-link")).toHaveAttribute("href", "/admin/settings");
    expect(screen.getByTestId("refine-run")).toBeDisabled();
  });

  it("llm mode lets the user switch provider", async () => {
    server.use(
      http.get("/settings", () =>
        HttpResponse.json({
          ...platformSettings,
          llm_providers: [...platformSettings.llm_providers, { name: "anthropic-main", kind: "anthropic", model: null, api_key_secret: "anthropic-key", base_url: null }],
        }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/backend-apis/bp-orders/versions/2/refine?mode=llm" />);
    await screen.findByTestId("refine-page");
    await waitFor(() => expect(screen.getByTestId("refine-provider")).toHaveAttribute("value", "openai-main"));
    await selectValue(user, "refine-provider", "anthropic-main");
    await user.click(screen.getByTestId("refine-run"));
    await waitFor(() => expect(lastRequest("POST", "/spec-generator/jobs")).toBeDefined());
    expect((lastRequest("POST", "/spec-generator/jobs")?.body as { provider: string }).provider).toBe("anthropic-main");
  });
});
