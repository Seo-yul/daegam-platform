/**
 * MCP Server·Gateway 상세의 메트릭 탭 (P51 5절). 목의 답은 플랫폼의 `McpMetricsOut`·`GatewayMetricsOut` 모양이다(msw/mcp-metrics.ts).
 * 숫자를 못 믿을 때(기록이 남지 않는 클러스터, 꺼진 수집기)는 숫자 대신 안내가 서고, 다시 적용을 기다리거나 거슬러 세는
 * 중이면 숫자 곁에 안내가 선다. 막대는 결과 갈래마다 고정한 색이고 단계의 시작으로 열쇠를 잡는다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { DailyBars } from "@/components/charts/DailyBars";
import { dictionary } from "@/i18n";
import { formatDate, formatHour } from "@/lib/utils";
import { HttpResponse, NOW, apiError, gatewayMetricsFixture, http, lastRequest, mcpMetricsFixture, mcpOutcomes, server } from "./msw";
import { selectValue } from "./select";

const KO = dictionary("ko");
const TEXT = KO.mcpMetrics;
const SERVER = "/mcp-servers/ms-calc?tab=metrics";
const GATEWAY = "/gateways/gw-support?tab=metrics";

/** MCP Server 의 답 하나를 바꿔 끼운다. */
function answerServer(over: Parameters<typeof mcpMetricsFixture>[1]): void {
  server.use(http.get("/mcp-servers/:id/metrics", () => HttpResponse.json(mcpMetricsFixture("24h", over))));
}

function fills(chart: HTMLElement, part: string): string[] {
  return [...chart.querySelectorAll(`rect[data-part="${part}"]`)].map((r) => r.getAttribute("fill") ?? "");
}

describe("MCP Server·Gateway 메트릭 탭", () => {
  it("MCP Server 는 합계·결과 갈래·지연과 Tool 별 표를 보이고 백엔드 표는 없다", async () => {
    render(<TestApp initialPath={SERVER} />);
    const tab = await screen.findByTestId("mcp-metrics-tab");
    expect(screen.getByTestId("tab-metrics")).toHaveAttribute("aria-selected", "true");
    expect(await within(tab).findByTestId("mcp-metrics-calls")).toHaveTextContent("12");
    expect(screen.getByTestId("mcp-metrics-errors")).toHaveTextContent("3");
    expect(screen.getByTestId("mcp-metrics-outcome-success")).toHaveTextContent("9");
    expect(screen.getByTestId("mcp-metrics-outcome-application_error")).toHaveTextContent("1");
    expect(screen.getByTestId("mcp-metrics-outcome-failure")).toHaveTextContent("0");
    const facts = screen.getByTestId("mcp-metrics-facts");
    for (const name of Object.values(TEXT.outcome)) expect(facts).toHaveTextContent(name);
    expect(facts).toHaveTextContent(TEXT.duration);
    expect(screen.getByTestId("mcp-metrics-duration")).toHaveTextContent("25 / 180 / 900");
    expect(tab).toHaveTextContent(TEXT.approx);
    expect(screen.getByTestId("mcp-metrics-rolled-up")).toHaveTextContent(formatDate(KO, NOW));
    // 리소스 읽기와 프롬프트 가져오기는 0 이면 서지 않는다
    expect(screen.queryByTestId("mcp-metrics-other-calls")).not.toBeInTheDocument();

    const byTool = screen.getByTestId("mcp-metrics-by-tool");
    expect(byTool).toHaveTextContent(TEXT.byTool);
    expect(screen.getByTestId("mcp-metrics-by-tool-totals")).toHaveTextContent("호출 12건 · 오류 3건 · p95 180 ms");
    const rows = within(byTool).getAllByTestId("mcp-metrics-by-tool-row");
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["CreateOrder", "GetOrder", TEXT.otherTools]);
    expect(within(rows[0]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["CreateOrder", "8", "2", "200"]);
    expect(screen.queryByTestId("mcp-metrics-by-backend")).not.toBeInTheDocument();

    const notes = screen.getByTestId("mcp-metrics-notes");
    for (const note of Object.values(TEXT.notes)) expect(notes).toHaveTextContent(note);
    for (const id of ["audit-off", "collector-off", "audit-pending", "counting", "empty"]) expect(screen.queryByTestId(`mcp-metrics-${id}`)).not.toBeInTheDocument();
    expect(screen.getAllByTestId("mcp-metrics-bars-day")).toHaveLength(24);
    expect(screen.getByTestId("mcp-metrics-bars")).toHaveAttribute("aria-label", TEXT.chartLabel);
  });

  it("기간을 바꾸면 그 기간으로 다시 읽고, 결과 갈래의 색은 기간이 바뀌어도 같다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={SERVER} />);
    const first = await screen.findByTestId("mcp-metrics-bars");
    expect(lastRequest("GET", "/mcp-servers/ms-calc/metrics")?.path).toContain("window=24h");
    expect(lastRequest("GET", "/mcp-servers/ms-calc/metrics")?.path).toContain("tz=");
    const before = { success: fills(first, "success"), denied: fills(first, "denied"), error: fills(first, "error") };
    expect(new Set(before.success)).toEqual(new Set(["var(--ui-ok)"]));
    expect(new Set(before.error)).toEqual(new Set(["var(--ui-err)"]));
    // 오류 응답과 거부는 적록 색각 이상에서도 갈리는 두 색이다
    expect(new Set(fills(first, "application_error"))).toEqual(new Set(["var(--ui-kind-entry)"]));
    expect(new Set(before.denied)).toEqual(new Set(["var(--ui-kind-delegate)"]));

    await selectValue(user, "mcp-metrics-window", "7d");
    await waitFor(() => expect(lastRequest("GET", "/mcp-servers/ms-calc/metrics")?.path).toContain("window=7d"));
    await waitFor(() => expect(screen.getAllByTestId("mcp-metrics-bars-day")).toHaveLength(7));
    const week = screen.getByTestId("mcp-metrics-bars");
    expect(new Set(fills(week, "success"))).toEqual(new Set(before.success));
    expect(new Set(fills(week, "denied"))).toEqual(new Set(before.denied));
    expect(new Set(fills(week, "error"))).toEqual(new Set(before.error));

    await selectValue(user, "mcp-metrics-window", "30d");
    await waitFor(() => expect(screen.getAllByTestId("mcp-metrics-bars-day")).toHaveLength(30));
    expect(lastRequest("GET", "/mcp-servers/ms-calc/metrics")?.path).toContain("window=30d");
  });

  it("Gateway 는 백엔드 표를 더 보이고, 스냅샷에 없는 Entry 와 백엔드를 정하지 못한 호출이 한 줄씩이다", async () => {
    render(<TestApp initialPath={GATEWAY} />);
    const card = await screen.findByTestId("mcp-metrics-by-backend");
    expect(card).toHaveTextContent(TEXT.byBackend);
    expect(lastRequest("GET", "/gateways/gw-support/metrics")?.path).toContain("window=24h");
    const rows = within(card).getAllByTestId("mcp-metrics-by-backend-row");
    const names = rows.map((r) => within(r).getAllByRole("cell")[0]);
    expect(names[0]).toHaveTextContent("calc");
    expect(names[1]).toHaveTextContent("support-orders");
    expect(names[1]).toHaveTextContent(TEXT.removedBackend);
    expect(names[2]).toHaveTextContent(TEXT.noBackend);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual(["7", "4", "1"]);
    expect(screen.getByTestId("mcp-metrics-by-tool")).toBeInTheDocument();
  });

  it("보이는 이름이 겹치는 백엔드 줄도 Entry 이름을 열쇠로 하나씩 그린다", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const base = gatewayMetricsFixture();
    // 지금 배포의 허브 항목 이름이 스냅샷에 없는 Entry 이름과 같다
    const rows = base.by_backend.map((r) => (r.hub_item_id ? { ...r, name: "support-orders" } : r));
    server.use(http.get("/gateways/:id/metrics", () => HttpResponse.json({ ...base, by_backend: rows })));
    render(<TestApp initialPath={GATEWAY} />);
    const card = await screen.findByTestId("mcp-metrics-by-backend");
    expect(within(card).getAllByTestId("mcp-metrics-by-backend-row")).toHaveLength(3);
    expect(spy.mock.calls.flat().join(" ")).not.toContain("same key");
  });

  it("경로가 실패하면 탭 안에 오류가 서고 기간 고르개는 남는다", async () => {
    server.use(http.get("/mcp-servers/:id/metrics", () => apiError(500, "internal_error", "boom")));
    render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId("mcp-metrics-error")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-metrics-window")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-metrics-facts")).not.toBeInTheDocument();
  });

  it.each([
    ["audit off", { audit: "off" as const }, "mcp-metrics-audit-off", TEXT.auditOff],
    ["collector off", { collector: "off" as const }, "mcp-metrics-collector-off", TEXT.collectorOff],
  ])("%s 이면 숫자 대신 안내만 선다", async (_name, source, testId, sentence) => {
    answerServer({ source: { ...mcpMetricsFixture().source, ...source } });
    render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId(testId)).toHaveTextContent(sentence);
    for (const id of ["facts", "bars", "by-tool", "notes", "empty"]) expect(screen.queryByTestId(`mcp-metrics-${id}`)).not.toBeInTheDocument();
  });

  it("다시 적용을 기다리는 Gateway 는 숫자를 둔 채 곁에 안내가 선다", async () => {
    server.use(http.get("/gateways/:id/metrics", () => HttpResponse.json(gatewayMetricsFixture("24h", { source: { ...mcpMetricsFixture().source, audit: "pending" } }))));
    render(<TestApp initialPath={GATEWAY} />);
    expect(await screen.findByTestId("mcp-metrics-audit-pending")).toHaveTextContent(TEXT.auditPending);
    expect(screen.getByTestId("mcp-metrics-calls")).toHaveTextContent("12");
    expect(screen.getByTestId("mcp-metrics-by-backend")).toBeInTheDocument();
  });

  it("거슬러 세는 일이 기간 안에 있으면 언제부터 세었는지 숫자 곁에 말하고, 기간의 시작보다 앞이면 말하지 않는다", async () => {
    const since = mcpMetricsFixture().since;
    const countedFrom = new Date(Date.parse(since) + 5 * 3_600_000).toISOString();
    answerServer({ source: { ...mcpMetricsFixture().source, counted_from: countedFrom } });
    const app = render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId("mcp-metrics-counting")).toHaveTextContent(TEXT.countingFrom(formatDate(KO, countedFrom)));
    expect(screen.getByTestId("mcp-metrics-calls")).toHaveTextContent("12");
    app.unmount();

    answerServer({ source: { ...mcpMetricsFixture().source, counted_from: new Date(Date.parse(since) - 3_600_000).toISOString() } });
    render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId("mcp-metrics-calls")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-metrics-counting")).not.toBeInTheDocument();
  });

  it("기간에 호출이 없으면 빈 문장이 숫자 대신 서고 주의 사항은 남는다", async () => {
    const zero = mcpMetricsFixture();
    answerServer({
      totals: { ...zero.totals, calls: 0, errors: 0, outcomes: mcpOutcomes(), duration_ms: { avg: null, p50: null, p95: null, max: null } },
      series: zero.series.map((s) => ({ ...s, calls: 0, outcomes: mcpOutcomes() })),
      by_tool: [],
    });
    render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId("mcp-metrics-empty")).toHaveTextContent(KO.shared.callsCard.noCalls);
    expect(screen.queryByTestId("mcp-metrics-facts")).not.toBeInTheDocument();
    expect(screen.getByTestId("mcp-metrics-notes")).toBeInTheDocument();
  });

  it("리소스 읽기나 프롬프트 가져오기가 있으면 Tool 호출 밖의 수를 따로 적고, Tool 호출이 없으면 그래프 대신 문장이 선다", async () => {
    const base = mcpMetricsFixture();
    answerServer({
      totals: { ...base.totals, calls: 0, errors: 0, outcomes: mcpOutcomes(), resource_reads: 2, prompt_gets: 1 },
      series: base.series.map((s) => ({ ...s, calls: 0, outcomes: mcpOutcomes() })),
      by_tool: [],
    });
    render(<TestApp initialPath={SERVER} />);
    expect(await screen.findByTestId("mcp-metrics-other-calls")).toHaveTextContent(TEXT.otherCalls(2, 1));
    expect(screen.getByTestId("mcp-metrics-tab")).toHaveTextContent(TEXT.chartEmpty);
    expect(screen.queryByTestId("mcp-metrics-bars")).not.toBeInTheDocument();
  });
});

describe("formatHour", () => {
  it("오프셋에 분이 있는 시간대에서는 분까지 적고, 정시 오프셋에서는 시만 적는다", () => {
    const zone = process.env.TZ;
    const en = dictionary("en");
    try {
      process.env.TZ = "Asia/Kolkata";
      expect(formatHour(en, "2026-09-02T09:00:00Z")).toBe("9/2, 2:30 PM");
      process.env.TZ = "UTC";
      expect(formatHour(en, "2026-09-02T09:00:00Z")).toBe("9/2, 9 AM");
    } finally {
      if (zone === undefined) delete process.env.TZ;
      else process.env.TZ = zone;
    }
  });
});

describe("DailyBars (P51 5절)", () => {
  const parts = (a: number, b: number) => [
    { key: "success", label: "ok", value: a },
    { key: "error", label: "bad", value: b },
  ];

  it("이름표가 겹쳐도 단계의 열쇠로 막대를 하나씩 그린다 (서머타임이 끝나는 날)", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <DailyBars
        days={[
          { key: "2026-11-01T05:00:00Z", label: "1 AM", parts: parts(1, 0) },
          { key: "2026-11-01T06:00:00Z", label: "1 AM", parts: parts(2, 1) },
        ]}
        format={String}
        label="calls"
        empty="none"
      />,
    );
    expect(screen.getAllByTestId("daily-bars-day")).toHaveLength(2);
    expect(spy).not.toHaveBeenCalled();
  });

  it("고정한 색을 주면 차례와 상관없이 열쇠의 색이고, 표에 없는 열쇠는 나머지 색이다", () => {
    const colors = { success: "green", error: "red" };
    const { unmount } = render(<DailyBars days={[{ key: "a", label: "a", parts: parts(1, 1) }]} format={String} label="calls" empty="none" colors={colors} />);
    expect(fills(screen.getByTestId("daily-bars"), "success")).toEqual(["green"]);
    unmount();
    render(
      <DailyBars
        days={[{ key: "a", label: "a", parts: [{ key: "error", label: "bad", value: 1 }, { key: "unseen", label: "?", value: 1 }, ...parts(1, 0).slice(0, 1)] }]}
        format={String}
        label="calls"
        empty="none"
        colors={colors}
      />,
    );
    const chart = screen.getByTestId("daily-bars");
    expect(fills(chart, "error")).toEqual(["red"]);
    expect(fills(chart, "success")).toEqual(["green"]);
    expect(fills(chart, "unseen")).toEqual(["var(--ui-dim)"]);
  });

  it("그릴 값이 없으면 받은 빈 문장을 쓴다", () => {
    render(<DailyBars days={[{ key: "a", label: "a", parts: parts(0, 0) }]} format={String} label="calls" empty="nothing to draw" />);
    expect(screen.getByText("nothing to draw")).toBeInTheDocument();
    expect(screen.queryByTestId("daily-bars")).not.toBeInTheDocument();
  });
});
