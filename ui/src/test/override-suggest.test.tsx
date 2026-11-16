import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { POLL_MS } from "@/lib/specJobs";
import { toggleIn } from "@/lib/utils";

import { HttpResponse, http, lastRequest, mcpServerFixture, overridesJobState, platformSettings, server } from "./msw";
import { advanceClock, tickingClock } from "./timers";

/**
 * 도구 오버라이드 제안 (D37): 상세의 단추 → 제안 화면 → job → 변경 목록 채택 → apply-draft.
 *
 * 변경 목록은 `SpecDiff` 가 아니라 `OverrideChanges` 다. 채택 단위가 두 모양이라는 것(기존 항목은
 * 필드마다 하나, 없던 항목은 통째로 하나)을 화면이 그대로 보여야 한다.
 */
describe("mcp server override suggestions", () => {
  it("detail links to the suggest screen", async () => {
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await screen.findByTestId("mcp-server-detail-page");
    // 더보기 메뉴를 없앴으므로 머리에 그대로 선다
    expect(screen.getByTestId("suggest-button")).toHaveAttribute("href", "/mcp-servers/ms-calc/suggest");
  });

  /**
   * 제안 화면에서 job 을 돌려 변경 목록이 설 때까지 간다. job 을 만들 때 목이 finished 를 되돌리므로 running 을
   * 본 뒤에 끝내고, 다음 폴링(POLL_MS 뒤)은 기다리지 않고 가짜 시계를 그만큼 넘겨 바로 오게 한다.
   */
  async function runJob() {
    tickingClock();
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc/suggest" />);
    await screen.findByTestId("suggest-page");
    await user.click(screen.getByTestId("suggest-run"));
    await waitFor(() => expect(screen.getByTestId("suggest-progress")).toHaveAttribute("data-state", "running"));
    overridesJobState.finish();
    await advanceClock(POLL_MS);
    return { user, list: await screen.findByTestId("override-changes") };
  }

  it("runs a job for the current revision and shows both change shapes", async () => {
    const { list } = await runJob();
    expect(screen.getByTestId("page-title")).toHaveTextContent("calc · r2");
    // 원본은 지금 리비전이다. 프리셋은 이 모드에 뜻이 있는 둘 중 고른 것만 간다
    const body = lastRequest("POST", "/spec-generator/jobs")?.body as Record<string, unknown>;
    expect(body.mode).toBe("overrides");
    expect(body.source).toEqual({ mcp_server_id: "ms-calc", revision: 2 });
    expect(body.presets).toEqual(["enrich_descriptions"]);
    expect(screen.getByTestId("override-changes-count")).toHaveTextContent("변경 2개 · 채택 2개");

    // 이미 오버라이드가 있던 도구는 필드 하나가 한 단위다
    const existing = within(list).getByTestId("override-group-0/GetOrder");
    expect(within(existing).getByTestId("override-before-o1-name")).toHaveTextContent("get_order");
    expect(within(existing).getByTestId("override-after-o1-name")).toHaveTextContent("getOrder");

    // 없던 도구는 항목 통째가 한 단위라 체크박스가 하나뿐이고 두 필드가 함께 들어간다
    const added = within(list).getByTestId("override-group-0/CreateOrder");
    expect(within(added).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(added).getByTestId("override-after-o2-name")).toHaveTextContent("createOrder");
    expect(within(added).getByTestId("override-after-o2-description")).toHaveTextContent("주문을 만든다.");
    expect(added).toHaveTextContent("이 Tool 에는 아직 오버라이드가 없어서");

    // 적용이 곧 배포가 아니라는 것을 말한다
    expect(screen.getByTestId("suggest-deploy-notice")).toHaveTextContent("적용은 배포가 아닙니다");
  });

  it("applies only the accepted changes and returns to the detail", async () => {
    const { user } = await runJob();
    await user.click(screen.getByTestId("override-accept-o1"));
    expect(screen.getByTestId("override-changes-count")).toHaveTextContent("변경 2개 · 채택 1개");
    await user.click(screen.getByTestId("apply-suggest"));

    await waitFor(() => {
      expect(lastRequest("POST", "/mcp-servers/ms-calc/apply-draft")?.body).toEqual({
        job_id: "job-overrides-1",
        accepted_ids: ["o2"],
      });
    });
    await screen.findByTestId("mcp-server-detail-page");
  });

  it("unchecking everything disables apply", async () => {
    const { user } = await runJob();
    await user.click(screen.getByTestId("override-toggle-all"));
    expect(screen.getByTestId("override-changes-count")).toHaveTextContent("채택 0개");
    expect(screen.getByTestId("apply-suggest")).toBeDisabled();
  });

  it("keeps the overrides of an all-tools profile when the form saves an unrelated field", async () => {
    /**
     * `tools` 가 빈 것은 "안 골랐다" 가 아니라 "전부 노출한다" 는 뜻이다. 폼이 저장 본문을 만들 때
     * `b.tools` 만 돌면 그런 프로필의 오버라이드가 통째로 사라진다. 제안을 채택한 결과가 정확히
     * 이 모양이라(D37) 이 자리는 노출 집합을 돌아야 한다.
     *
     * 화면 조작이 아니라 저장 본문으로 단언한다. 오버라이드 입력칸은 체크된 도구에만 그려지므로
     * 이 프로필에서는 아예 보이지 않고, 그래서 사용자가 값이 사라지는 것을 보지도 못한다.
     */
    const stored = { GetOrder: { name: "get_order", description: null } };
    server.use(
      http.get("/mcp-servers/:id", () =>
        HttpResponse.json({
          ...mcpServerFixture(),
          backends: [{ ...mcpServerFixture().backends[0], tools: [], tool_overrides: stored }],
        }),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    await waitFor(() => expect(screen.getByTestId("mp-replicas")).toHaveValue(2));

    await user.clear(screen.getByTestId("mp-replicas"));
    await user.type(screen.getByTestId("mp-replicas"), "3");
    await user.click(screen.getByTestId("save-button"));

    await waitFor(() => {
      const body = lastRequest("PUT", "/mcp-servers/ms-calc")?.body as
        | { backends: { tools: string[]; tool_overrides: Record<string, unknown> }[]; runtime: { replicas: number } }
        | undefined;
      expect(body).toBeTruthy();
      expect(body!.runtime.replicas).toBe(3);
      expect(body!.backends[0].tools).toEqual([]);
      expect(body!.backends[0].tool_overrides).toEqual(stored);
    });
  });

  it("keeps them even when the backend version list cannot be read", async () => {
    /**
     * 위 테스트의 노출 집합은 버전의 `tool_names` 에서 온다. 그 조회가 실패하면 집합이 빈 배열이라
     * 거르는 순간 같은 유실이 되살아난다. 노출 집합을 모를 때는 거르지 않는다. 키가 낡았다면
     * 서버의 `backend_final_names` 가 422 로 말해 주므로 조용히 사라지는 것보다 낫다.
     */
    const stored = { GetOrder: { name: "get_order", description: null } };
    server.use(
      http.get("/mcp-servers/:id", () =>
        HttpResponse.json({
          ...mcpServerFixture(),
          backends: [{ ...mcpServerFixture().backends[0], tools: [], tool_overrides: stored }],
        }),
      ),
      http.get("/backend-apis/:id/versions", () => HttpResponse.json({ error: { code: "boom" } }, { status: 500 })),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc/edit" />);
    await waitFor(() => expect(screen.getByTestId("mp-replicas")).toHaveValue(2));
    await waitFor(() => expect(screen.getByTestId("save-button")).toBeEnabled());

    await user.click(screen.getByTestId("save-button"));

    await waitFor(() => {
      const body = lastRequest("PUT", "/mcp-servers/ms-calc")?.body as { backends: { tool_overrides: Record<string, unknown> }[] } | undefined;
      expect(body).toBeTruthy();
      expect(body!.backends[0].tool_overrides).toEqual(stored);
    });
  });

  it("a job the server no longer has shows the error and frees the run button", async () => {
    // 만료된 job 이나 낡은 ?job= 주소다. 목은 모르는 id 에 404 를 준다. 이 실패를 진행 상태로 오해하면
    // 배지는 queued 에 멈추고 실행 단추는 "실행 중…" 에 묶인다
    render(<TestApp initialPath="/mcp-servers/ms-calc/suggest?job=job-expired" />);
    expect(await screen.findByTestId("suggest-job-error")).toBeInTheDocument();
    expect(screen.queryByTestId("suggest-progress")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("suggest-run")).toBeEnabled());
    expect(screen.getByTestId("suggest-run")).toHaveTextContent(/^실행$/);
  });

  it("a failed job explains its code in Korean like the refine screen", async () => {
    server.use(
      http.get("/spec-generator/jobs/:id", () =>
        HttpResponse.json({
          id: "job-overrides-1",
          tenant: "default",
          state: "failed",
          provider: "openai-main",
          hints: {},
          attempt: 1,
          iterations: [],
          usage_total: {},
          error: { code: "provider_no_key", detail: "llm provider 'openai-main' has no api key" },
          report: null,
          verdict: null,
          created_at: "x",
          updated_at: "x",
          finished_at: "x",
          expires_at: null,
          mode: "overrides",
          source: { mcp_server_id: "ms-calc", revision: 2 },
        }),
      ),
    );
    render(<TestApp initialPath="/mcp-servers/ms-calc/suggest?job=job-overrides-1" />);
    const failed = await screen.findByTestId("suggest-job-failed");
    expect(failed).toHaveTextContent("provider 의 API 키를 읽지 못했습니다");
    expect(failed).toHaveTextContent("provider_no_key");
    expect(screen.getByTestId("suggest-progress")).toHaveAttribute("data-state", "failed");
  });

  it("without providers the run button is disabled and points to Platform Settings", async () => {
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, llm_providers: [], defaults: { spec_generator: null } })));
    render(<TestApp initialPath="/mcp-servers/ms-calc/suggest" />);
    const alert = await screen.findByTestId("suggest-no-provider");
    expect(alert).toHaveTextContent("등록된 provider 가 없습니다");
    expect(within(alert).getByTestId("suggest-settings-link")).toHaveAttribute("href", "/admin/settings");
    expect(screen.getByTestId("suggest-run")).toBeDisabled();
  });
});

describe("toggleIn", () => {
  it("켜면 넣고 끄면 빼며, 받은 Set 은 그대로 둔다", () => {
    const base: ReadonlySet<string> = new Set(["a"]);
    expect([...toggleIn(base, "b", true)]).toEqual(["a", "b"]);
    expect([...toggleIn(base, "a", false)]).toEqual([]);
    expect([...base]).toEqual(["a"]);
  });
});
