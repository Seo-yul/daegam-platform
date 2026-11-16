/** Agent 상세의 승인함·세션·메트릭·이벤트 탭. 목의 응답은 런타임이 실제로 내는 모양이다(msw/agent-chat.ts·agent-sse.ts). */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { RUNTIME_CANCEL, RUNTIME_REGENERATE } from "@/lib/runtimeFeatures";
import { transcriptRows } from "./chat";
import { HttpResponse, agentMetricsFixture, meViewer, agentState, agentWithRuntimeFeatures, http, lastRequest, server } from "./msw";
import { selectValue } from "./select";

describe("agents (approvals, sessions, metrics and events)", () => {
  it("the approval inbox lists pending sessions and rejects one", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=approvals" />);
    const row = await screen.findByTestId("pending-session-s-2");
    expect(screen.getByTestId("approval-inbox-count")).toHaveTextContent("보류 1");
    await user.click(row);
    const card = await screen.findByTestId("approval-card");
    await user.click(within(card).getByTestId("decision-reject"));
    expect(lastRequest("POST", "/agents/ag-support/sessions/s-2/decisions")?.body).toEqual({ decisions: [{ type: "reject" }] });
    await waitFor(() => expect(screen.queryByTestId("pending-session-s-2")).not.toBeInTheDocument());
  });

  it("sessions, metrics and events tabs", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    // 색인 행에는 합이 없다. 화면이 입력 + 출력을 더해 보인다
    expect(await screen.findByTestId("session-row-s-1")).toHaveTextContent("1,500");
    await selectValue(user, "sessions-status", "pending");
    await waitFor(() => expect(screen.queryByTestId("session-row-s-1")).not.toBeInTheDocument());
    await user.click(screen.getByTestId("session-row-s-2"));
    await waitFor(() => expect(screen.getByTestId("session-messages")).toHaveTextContent("8 나누기 2 는?"));

    await user.click(screen.getByTestId("tab-metrics"));
    // 기간 안의 사용량이다. 화면이 input + output 을 더한다
    expect(await screen.findByTestId("metrics-tokens")).toHaveTextContent("15,000");
    // 플랫폼이 모든 레플리카의 이벤트에서 센 완료한 턴이다
    expect(screen.getByTestId("metrics-turns")).toHaveTextContent("38");
    expect(screen.getByTestId("metrics-turns-done")).toHaveTextContent("38");
    // 멈춘 턴은 "중지" 이고 오류율에 들지 않는다
    const turnResults = screen.getByTestId("metrics-turn-results");
    expect(turnResults).toHaveTextContent("승인 대기");
    expect(screen.getByTestId("metrics-turns-cancelled")).toHaveTextContent("1");
    expect(turnResults).toHaveTextContent("중지");
    expect(screen.getByTestId("metrics-error-rate")).toHaveTextContent("2.6%");
    expect(screen.getByTestId("metrics-rejected-total")).toHaveTextContent("2");
    expect(screen.getByTestId("metrics-model-totals")).toHaveTextContent("호출 45건 · 오류 1건");
    expect(screen.getByTestId("metrics-tool-calls-totals")).toHaveTextContent("호출 57건 · 오류 2건");
    expect(screen.getByTestId("metrics-tools-count")).toHaveTextContent("2개");
    expect(screen.getByTestId("metrics-cost")).toHaveTextContent("$0.42");
    expect(screen.getByTestId("metrics-telemetry-mode")).toHaveTextContent("Agent 가 직접 보냄");
    // 예산이 없으면 null 이 온다. 0 으로 그리면 한도가 0 인 것처럼 보인다
    expect(screen.getByTestId("metrics-daily-none")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-warning-no_shared_state")).toBeInTheDocument();
    // 파드 하나의 카운터가 아니므로 파드마다 다르다는 말이 없다
    expect(screen.getByTestId("metrics-tab")).not.toHaveTextContent("파드가 뜬 뒤로");
    expect(screen.queryByTestId("metrics-redeploy")).not.toBeInTheDocument();
    await selectValue(user, "metrics-window", "24h");
    await waitFor(() => expect(lastRequest("GET", "/agents/ag-support/metrics")?.path).toContain("window=24h"));
    await user.click(screen.getByTestId("tab-events"));
    const firstEvent = await screen.findByTestId("event-row-0");
    // 이벤트 이름은 사전의 이름이다
    expect(firstEvent).toHaveTextContent("턴 종료");
    // 갈래·주체·토큰은 수집기가 옮겨 담은 이름으로 읽는다. 잘못 읽으면 칸이 조용히 빈다
    expect(firstEvent).toHaveTextContent("header-admin");
    expect(firstEvent).toHaveTextContent("500");
  });

  it("세션 탭은 런타임 모양의 전사를 채팅과 같은 말풍선과 도구 카드로 읽기 전용으로 그린다 (P35)", async () => {
    const user = userEvent.setup();
    // 런타임이 다시 생성을 알려도 남의 세션이 보이는 탭이라 다시 생성·메시지 수정 단추가 없다(P36)
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_CANCEL, RUNTIME_REGENERATE)];
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    await user.click(await screen.findByTestId("session-row-s-1"));
    // 전에는 요약을 content 와 role "user" 로 읽어 본문이 비고 모든 말이 상대 말이었다
    const conversation = await screen.findByTestId("session-messages");
    await waitFor(() => expect(transcriptRows({ container: conversation })).toHaveLength(7));
    const items = transcriptRows({ container: conversation });
    expect(items.map((el) => el.dataset.role)).toEqual(["assistant", "user", "assistant", "tool_call", "interrupt", "assistant", "done"]);
    expect(items[0]).toHaveTextContent("인사말");
    expect(items[1]).toHaveTextContent("8 나누기 2 는?");
    expect(items[3]).toHaveTextContent("Divide");
    expect(items[3]).toHaveAttribute("data-status", "success");
    expect(items[4]).toHaveTextContent("결정함");
    expect(items[5]).toHaveTextContent("결과는 4 입니다.");
    expect(screen.queryByTestId("session-pending-note")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-regenerate")).not.toBeInTheDocument();
    expect(within(conversation).queryByTestId(/-edit$/)).not.toBeInTheDocument();
    // 되살린 턴 마감 줄에 turn_id 가 있어도 남의 평가를 매길 자리가 아니라 엄지가 없다(P36 6절)
    expect(items[6]).toHaveAttribute("data-turn-id", "turn-s-1-1");
    expect(within(conversation).queryByRole("button", { name: "좋아요" })).not.toBeInTheDocument();

    // 보류 중인 세션은 살아 있는 보류로 카드를 다시 세우고, 카드가 붙든 호출은 결정을 기다린다
    await user.click(screen.getByTestId("session-row-s-2"));
    await waitFor(() => expect(within(screen.getByTestId("session-messages")).getByTestId("chat-message-3")).toHaveAttribute("data-status", "waiting"));
    expect(lastRequest("GET", "/agents/ag-support/sessions/s-2/pending")).toBeDefined();
    // 남의 세션도 보이는 탭이라 결정 콜백이 없다. 단추가 잠기고 결정은 승인함 탭에서 한다
    const card = within(screen.getByTestId("session-messages")).getByTestId("approval-card");
    for (const decision of ["approve", "edit", "reject", "respond"]) expect(within(card).getByTestId(`decision-${decision}`)).toBeDisabled();
    expect(screen.getByTestId("session-pending-note")).toHaveTextContent("결정은 승인함 탭에서 합니다.");
  });

  it("런타임에 연결할 수 없으면 런타임 몫만 비고, 로그로만 오는 에이전트에는 다시 배포하라고 한다", async () => {
    const runtimeError = { code: "runtime_unreachable", message: "no ready pod" };
    const telemetry = { mode: "log", pods: [], dropped: { cost: 0, other: 0 }, image_pushes: false };
    server.use(http.get("/agents/:id/metrics", () => HttpResponse.json({ ...agentMetricsFixture, sessions: null, tools: null, warnings: null, runtime_error: runtimeError, telemetry })));
    render(<TestApp initialPath="/agents/ag-support?tab=metrics" />);
    expect(await screen.findByTestId("metrics-runtime-error")).toHaveTextContent("런타임에 연결할 수 없어");
    expect(screen.getByTestId("metrics-sessions")).toHaveTextContent("- / - / -");
    // 저장소가 센 수는 그대로 있다
    expect(screen.getByTestId("metrics-turns")).toHaveTextContent("38");
    expect(screen.getByTestId("metrics-redeploy")).toHaveTextContent("이 Agent 를 다시 배포하면 파드 로그를 거치지 않고 이벤트를 바로 보냅니다.");
    expect(screen.getByTestId("metrics-telemetry-mode")).toHaveTextContent("파드 로그에서 읽음");
  });

  it("다시 배포하라는 안내는 로그로만 오는 옛 이미지에만 선다", async () => {
    for (const [mode, image_pushes] of [["none", false], ["log", true]] as const) {
      server.use(http.get("/agents/:id/metrics", () => HttpResponse.json({ ...agentMetricsFixture, telemetry: { ...agentMetricsFixture.telemetry, mode, image_pushes } })));
      const { unmount } = render(<TestApp initialPath="/agents/ag-support?tab=metrics" />);
      expect(await screen.findByTestId("metrics-telemetry-mode")).toBeInTheDocument();
      expect(screen.queryByTestId("metrics-redeploy")).not.toBeInTheDocument();
      unmount();
    }
  });

  it("세션의 턴 기록이 그 턴의 실행들을 트레이스 창으로 연다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=sessions" />);
    await user.click(await screen.findByTestId("session-row-s-1"));
    const row = await screen.findByTestId("session-turn-t-1");
    expect(row).toHaveTextContent("완료");
    await user.click(within(row).getByTestId("session-turn-t-1-trace"));
    const drawer = await screen.findByTestId("trace-drawer");
    // 승인 대기로 끝난 실행과 재개한 실행 사이가 승인을 기다린 시간이다
    expect(await within(drawer).findByTestId("trace-gap")).toHaveTextContent("승인 대기 3분 12초");
    const timeline = await within(drawer).findByTestId("trace-run-0-timeline");
    const retried = within(timeline).getByTestId("trace-run-0-timeline-span-2");
    expect(within(retried).getByTestId("trace-run-0-timeline-span-2-flag-retry")).toBeInTheDocument();
    expect(within(retried).getByTestId("trace-run-0-timeline-span-2-flag-cache")).toBeInTheDocument();
    expect(within(retried).getByTestId("trace-run-0-timeline-span-2-flag-gateway")).toBeInTheDocument();
    expect(within(timeline).getByTestId("trace-run-0-timeline-span-1-flag-error")).toBeInTheDocument();
    // 줄은 단추라 키보드로 연다. 본문은 글로만 그린다
    const tool = within(timeline).getByTestId("trace-run-0-timeline-span-3-toggle");
    tool.focus();
    await user.keyboard("{Enter}");
    expect(tool).toHaveAttribute("aria-expanded", "true");
    const body = within(timeline).getByTestId("trace-run-0-timeline-span-3-body");
    expect(body).toHaveTextContent("<b>7</b>");
    expect(body.querySelector("b")).toBeNull();
    expect(within(timeline).getByTestId("trace-run-0-timeline-points")).toHaveTextContent("모델 재시도");
  });

  it("이벤트 탭은 이름·세션·trace 로 거르고 본문을 접어 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/agents/ag-support?tab=events" />);
    expect(await screen.findByTestId("event-row-2")).toHaveTextContent("Tool 실행");
    await user.click(within(screen.getByTestId("event-row-2-body")).getByText("본문 보기"));
    expect(screen.getByTestId("event-row-2-body")).toHaveTextContent('"content": "7"');
    await selectValue(user, "events-event", "tool_exec");
    await user.type(screen.getByTestId("events-trace_id"), "abc");
    await user.click(screen.getByTestId("events-apply"));
    await waitFor(() => expect(lastRequest("GET", "/agents/ag-support/events")?.path).toMatch(/event=tool_exec.*trace_id=abc|trace_id=abc.*event=tool_exec/));
  });

  it("감사 읽기 권한이 없으면 이벤트 탭이 서지 않는다", async () => {
    const reader = { ...meViewer, scopes_by_tenant: { default: ["agent:read", "agent:chat"] } };
    server.use(http.get("/me", () => HttpResponse.json(reader)));
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("tab-metrics")).toBeInTheDocument();
    expect(screen.queryByTestId("tab-events")).not.toBeInTheDocument();
  });
});
