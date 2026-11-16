import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PriceRule } from "@/api/types";
import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { formatUsd } from "@/lib/usage";
import { csvCell, toCsv } from "@/lib/csv";
import { HttpResponse, http, lastRequest, server, usageReport, usageState } from "./msw";
import { selectValue } from "./select";

const USAGE = dictionary("ko").usage;

describe("CSV (P37 13절)", () => {
  it("prefixes text that a spreadsheet would run as a formula and quotes separators", () => {
    expect(csvCell("=SUM(A1)")).toBe("'=SUM(A1)");
    expect(csvCell("+1")).toBe("'+1");
    expect(csvCell("-x")).toBe("'-x");
    expect(csvCell("@a")).toBe("'@a");
    expect(csvCell("\tx")).toBe("'\tx");
    expect(csvCell("a,b")).toBe('"a,b"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell(-3)).toBe("-3");
    expect(csvCell(null)).toBe("");
    expect(toCsv(["a", "b"], [["=x", 1]])).toBe("a,b\r\n'=x,1\r\n");
  });
});

describe("usage screens (/usage, /admin/usage)", () => {
  it("shows totals, a daily bar and rows, and regroups on apply", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/usage" />);
    const totals = await screen.findByTestId("usage-totals");
    expect(totals).toHaveTextContent(formatUsd(dictionary("ko"), 5));
    expect(screen.getAllByTestId("daily-bars-day")).toHaveLength(1);
    // 그림의 이름은 사용량의 말이고, 칸 색은 처음 나온 차례로 돌며 나머지는 흐린 색이다
    const chart = screen.getByTestId("daily-bars");
    expect(chart).toHaveAttribute("aria-label", USAGE.chart.label);
    expect([...chart.querySelectorAll("rect")].map((r) => r.getAttribute("fill"))).toEqual(["var(--ui-accent)", "var(--ui-dim)"]);
    expect(screen.getAllByTestId("usage-row")).toHaveLength(1);
    await selectValue(user, "usage-group", "user");
    await user.type(screen.getByTestId("usage-until"), "2026-10-20");
    await user.click(screen.getByTestId("usage-apply"));
    await waitFor(() => expect(lastRequest("GET", "/usage")?.path).toContain("group=user"));
    // 끝은 고른 날까지라 서버에는 그 다음 날 0시가 간다(끝을 들지 않는다)
    expect(decodeURIComponent(lastRequest("GET", "/usage")?.path ?? "")).toContain(`until=${new Date("2026-10-21T00:00:00").toISOString()}`);
    const rows = await screen.findAllByTestId("usage-row");
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["kim (u1)", USAGE.unjoined]);
    expect(lastRequest("GET", "/usage")?.path).toContain("tz=");
  });

  it("says when the server cut the rows to the most expensive ones", async () => {
    server.use(http.get("/usage", () => HttpResponse.json({ ...usageReport("day"), truncated: true })));
    render(<TestApp initialPath="/usage" />);
    expect(await screen.findByTestId("usage-truncated")).toHaveTextContent(USAGE.truncated("1"));
  });

  it("warns on the platform screen when a ledger health count is not 0 and lists unpriced models", async () => {
    usageState.health = { ...usageState.health, writer_conflicts: 2, agents_without_telemetry: 3 };
    render(<TestApp initialPath="/admin/usage" />);
    const warning = await screen.findByTestId("usage-health-warning");
    expect(warning).toHaveTextContent(`${USAGE.health.names.writer_conflicts} 2`);
    expect(warning).toHaveTextContent(USAGE.health.oldAgents(3));
    const unpriced = screen.getByTestId("usage-unpriced");
    expect(unpriced).toHaveTextContent("p · mystery-model");
    // 웹 검색은 provider 이름이 모델 칸에도 있어 계열을 함께 보인다
    expect(unpriced).toHaveTextContent("web_search · brave");
    expect(unpriced).not.toHaveTextContent("brave · brave");
  });
});

describe("price table card (P37 13절)", () => {
  it("copies a default, saves the whole table under the read version, and reports a conflict", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("price-table");
    await within(card).findByTestId("pricing-defaults");
    expect(within(card).getByTestId("pricing-default-0")).toHaveTextContent("2026-06-01");
    // 웹 검색 기본 가격은 요청당 값을 보인다
    expect(within(card).getByTestId("pricing-default-2")).toHaveTextContent("0.005");
    await user.click(within(card).getByTestId("pricing-copy-1"));
    const input = within(card).getByTestId("pricing-input-0");
    await user.clear(input);
    await user.type(input, "2.5");
    await user.click(within(card).getByTestId("pricing-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/pricing")).toBeDefined());
    expect(lastRequest("PUT", "/settings/pricing")?.body).toEqual({
      expected_version: 0,
      rules: [
        {
          id: null,
          provider_kind: "anthropic",
          provider_name: null,
          model: "claude-sonnet-4*",
          input: 2.5,
          output: 15,
          cache_read: 0.3,
          cache_write: 3.75,
          request: null,
          effective_from: "2024-01-01",
          note: "https://www.anthropic.com/pricing#api",
        },
      ],
    });
    await waitFor(() => expect(usageState.pricing.version).toBe(1));
    // 다른 사람이 그사이 저장했다
    usageState.pricing.version = 5;
    await user.click(within(card).getByTestId("pricing-save"));
    expect(await within(card).findByTestId("pricing-save-error")).toHaveTextContent(dictionary("ko").errors.codes.revision_conflict);
  });

  it("keeps a rule's tier when a default is copied and when a saved rule is edited", async () => {
    const user = userEvent.setup();
    const tier = { above_input_tokens: 100000, input: 0.5, output: 2.5, cache_read: 0.05, cache_write: 0.625 };
    render(<TestApp initialPath="/admin/settings" />);
    const card = await screen.findByTestId("price-table");
    await within(card).findByTestId("pricing-defaults");
    // Haiku 5.5 기본 가격을 복사해 값만 고친다. 화면에 칸이 없는 tier 도 함께 간다
    await user.click(within(card).getByTestId("pricing-copy-3"));
    const input = within(card).getByTestId("pricing-input-0");
    await user.clear(input);
    await user.type(input, "0.09");
    await user.click(within(card).getByTestId("pricing-save"));
    await waitFor(() => expect(lastRequest("PUT", "/settings/pricing")).toBeDefined());
    const copied = (lastRequest("PUT", "/settings/pricing")?.body as { rules: PriceRule[] }).rules[0];
    expect(copied).toMatchObject({ model: "claude-haiku-5-5*", input: 0.09, tier });
    await waitFor(() => expect(usageState.pricing.version).toBe(1));
    // 저장된 규칙을 다시 고쳐도 tier 는 남는다
    const output = within(card).getByTestId("pricing-output-0");
    await user.clear(output);
    await user.type(output, "0.45");
    await user.click(within(card).getByTestId("pricing-save"));
    await waitFor(() => expect(usageState.pricing.version).toBe(2));
    const edited = (lastRequest("PUT", "/settings/pricing")?.body as { rules: PriceRule[] }).rules[0];
    expect(edited).toMatchObject({ model: "claude-haiku-5-5*", input: 0.09, output: 0.45, tier });
  });

  it("starts a reprice over a period and follows its progress", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      render(<TestApp initialPath="/admin/settings" />);
      const card = await screen.findByTestId("price-table");
      await user.click(await within(card).findByTestId("pricing-reprice"));
      const dialog = await screen.findByTestId("reprice-dialog");
      await user.type(within(dialog).getByTestId("reprice-since"), "2026-10-01");
      await user.type(within(dialog).getByTestId("reprice-until"), "2026-10-20");
      await user.click(within(dialog).getByTestId("reprice-start"));
      await waitFor(() => expect(lastRequest("POST", "/usage/reprice")).toBeDefined());
      // 끝은 고른 날까지라 그 다음 날 0시를 보낸다
      expect(lastRequest("POST", "/usage/reprice")?.body).toEqual({
        since: new Date("2026-10-01T00:00:00").toISOString(),
        until: new Date("2026-10-21T00:00:00").toISOString(),
      });
      // 진행은 2초마다 다시 읽는다. 목은 둘째 읽기에서 끝난다
      expect(await within(dialog).findByTestId("reprice-progress")).toHaveTextContent(USAGE.pricing.reprice.progress("0", "4", null));
      await vi.advanceTimersByTimeAsync(2000);
      await waitFor(() => expect(within(dialog).getByTestId("reprice-progress")).toHaveTextContent(USAGE.pricing.reprice.done("4")));
    } finally {
      vi.useRealTimers();
    }
  });

  it("turns streaming usage off for an OpenAI-compatible provider", async () => {
    const user = userEvent.setup();
    server.use(http.get("/settings/pricing", () => HttpResponse.json({ version: 0, rules: [], defaults: [], updated_at: null, updated_by: null })));
    render(<TestApp initialPath="/admin/settings" />);
    const box = await screen.findByTestId("ps-provider-stream-usage-0");
    expect(box).toBeChecked();
    await user.click(box);
    await user.click(screen.getByTestId("save-button"));
    await waitFor(() => expect(lastRequest("PATCH", "/settings")).toBeDefined());
    const body = lastRequest("PATCH", "/settings")?.body as { llm_providers: { stream_usage: boolean }[] };
    expect(body.llm_providers[0].stream_usage).toBe(false);
  });
});
