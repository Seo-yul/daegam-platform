/** 턴의 트레이스 (P37 12절): 채팅의 턴 마감 줄이 단추를 세우고, 창은 실행 하나나 턴의 실행 전부를 그리며, 없으면 없다고 한다. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ChatItemView } from "@/components/chat";
import { applyChatEvent } from "@/components/chat/items";
import { gapText, TraceDrawer } from "@/components/trace/TraceDrawer";
import { spanFlags, TraceTimeline } from "@/components/trace/TraceTimeline";
import { en } from "@/i18n/en";
import { ko } from "@/i18n/ko";
import { runStatusName } from "@/lib/agentEvents";
import { itemsFromTranscript } from "@/lib/assistant";

import { http, HttpResponse, server, TRACE_ID, traceFixture } from "./msw";
import { renderWithProviders } from "./render";
import { TURN_FIXTURE } from "./runtimeFixtures";

describe("traces", () => {
  it("스트림과 전사에서 접은 턴 마감 항목이 같은 trace_id 를 든다", () => {
    const streamed = applyChatEvent([], { event: "done", data: TURN_FIXTURE.events.done })[0];
    const [restored] = itemsFromTranscript(TURN_FIXTURE.transcript.filter((it) => it.kind === "done"));
    const ids = { turn_id: TURN_FIXTURE.events.done.turn_id, trace_id: TURN_FIXTURE.events.done.trace_id };
    expect(streamed).toMatchObject({ kind: "done", ...ids });
    expect(restored).toMatchObject({ kind: "done", ...ids });
  });

  it("턴 마감 줄의 트레이스 단추는 여는 자리를 줄 때만 서고 턴과 실행의 id 를 넘긴다", async () => {
    const user = userEvent.setup();
    const done = { kind: "done" as const, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }, turn: 1, tool_calls: 0, turn_id: "t-1", trace_id: TRACE_ID };
    const opened: unknown[] = [];
    const { rerender } = render(<ChatItemView index={0} item={done} />);
    expect(screen.queryByTestId("chat-message-0-trace")).toBeNull();
    rerender(<ChatItemView index={0} item={done} onOpenTrace={(ids) => opened.push(ids)} />);
    await user.click(screen.getByTestId("chat-message-0-trace"));
    expect(opened).toEqual([{ turnId: "t-1", traceId: TRACE_ID }]);
  });

  it("실행 하나만 알면 그 실행을 그리고, 아무 id 도 없으면 트레이스가 없다고 한다", async () => {
    const { unmount } = renderWithProviders(<TraceDrawer agentId="ag-support" target={{ traceId: TRACE_ID }} onClose={() => {}} />);
    const drawer = await screen.findByTestId("trace-drawer");
    expect(await within(drawer).findByTestId("trace-run-0-timeline")).toBeInTheDocument();
    expect(within(drawer).queryByTestId("trace-gap")).toBeNull();
    unmount();
    renderWithProviders(<TraceDrawer agentId="ag-support" target={{}} onClose={() => {}} />);
    expect(await screen.findByTestId("trace-none")).toHaveTextContent("이 턴에는 트레이스가 없습니다.");
  });

  it("배지는 오류·재시도·캐시·게이트웨이를 span 의 칸에서 읽는다", () => {
    const [, failed, retried, tool] = traceFixture(TRACE_ID).spans;
    expect(spanFlags(failed)).toEqual(["error"]);
    expect(spanFlags(retried)).toEqual(["retry", "cache", "gateway"]);
    expect(spanFlags(tool)).toEqual([]);
  });

  it("오류로 끝난 턴의 항목에도 트레이스 단추가 선다", async () => {
    const user = userEvent.setup();
    const failed = { kind: "error" as const, code: "turn_failed", message: "boom", turn_id: "t-2", trace_id: TRACE_ID };
    const opened: unknown[] = [];
    render(<ChatItemView index={0} item={failed} onOpenTrace={(ids) => opened.push(ids)} />);
    await user.click(screen.getByTestId("chat-message-0-trace"));
    expect(opened).toEqual([{ turnId: "t-2", traceId: TRACE_ID }]);
  });

  it("막 끝난 턴의 404 는 모으는 중이라고 하며 다시 읽는다", async () => {
    let calls = 0;
    server.use(
      http.get("/agents/:id/traces/:traceId", ({ params }) => {
        calls += 1;
        if (calls === 1) return HttpResponse.json({ detail: { code: "not_found", message: "trace not found" } }, { status: 404 });
        return HttpResponse.json(traceFixture(String(params.traceId)));
      }),
    );
    renderWithProviders(<TraceDrawer agentId="ag-support" target={{ traceId: TRACE_ID }} onClose={() => {}} />);
    expect(await screen.findByTestId("trace-run-0-collecting")).toHaveTextContent("트레이스를 모으는 중입니다. 잠시 뒤에 다시 표시합니다.");
    expect(await screen.findByTestId("trace-run-0-timeline", undefined, { timeout: 5000 })).toBeInTheDocument();
  });

  it("상한에 걸린 트레이스는 걸린 상한을 말한다", () => {
    const spans = { ...traceFixture(TRACE_ID), truncated: true, truncated_by: "spans" as const, limit: 1000 };
    const { unmount } = renderWithProviders(<TraceTimeline trace={spans} />);
    expect(screen.getByTestId("trace-timeline-truncated")).toHaveTextContent("단계가 1,000개를 넘어 처음 1,000개만 표시합니다.");
    unmount();
    renderWithProviders(<TraceTimeline trace={{ ...spans, truncated_by: "events", limit: 3000 }} />);
    expect(screen.getByTestId("trace-timeline-truncated")).toHaveTextContent("기록이 많아 이 실행의 앞부분만 표시합니다.");
  });

  it("런타임이 내는 이벤트마다 두 사전에 이름이 있고 도는 실행은 실행 중이다", () => {
    const source = readFileSync(resolve(process.cwd(), "../agent-runtime/runtime/events.py"), "utf8");
    // 이벤트가 아닌 칸 이름(replaced_turn_id)은 뺀다
    const events = [...source.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)].filter(([, name]) => name !== "REPLACED_TURN_ID").map(([, , value]) => value);
    expect(events).toContain("memory_deleted");
    for (const dict of [ko, en]) expect(events.filter((e) => !(e in dict.agents.events.names))).toEqual([]);
    expect(runStatusName(ko, "running")).toBe("실행 중");
    expect(runStatusName(ko, "cancelled")).toBe(ko.agents.metrics.turnResult.cancelled);
  });

  it("실행 사이의 틈은 분 아래를 초까지 적는다", () => {
    expect(gapText(ko, 45_000)).toBe("45초");
    expect(gapText(ko, 192_000)).toBe("3분 12초");
    expect(gapText(ko, 3_720_000)).toBe("1시간 2분");
  });
});
