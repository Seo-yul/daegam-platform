/**
 * Agent 상세의 자동 실행 탭 (P41 11절). 일정·트리거·실행 기록, 한 번만 보이는 서명 비밀, 권한에 따른 대화 보기와 중단, 지금 실행,
 * 자동 실행이 꺼진 플랫폼에서 탭이 사라지는 것이다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";

import { HttpResponse, agentDraftFixture, agentState, http, lastRequest, meViewer, runsState, server } from "./msw";

const TAB = "/agents/ag-support?tab=runs";

describe("자동 실행 탭", () => {
  it("일정, 트리거, 실행 기록을 보이고 까닭과 토큰을 사전의 말로 적는다", async () => {
    render(<TestApp initialPath={TAB} />);
    expect(await screen.findByTestId("tab-runs")).toHaveTextContent("자동 실행");
    const daily = await screen.findByTestId("schedule-sc-daily");
    expect(daily).toHaveTextContent("0 9 * * 1-5");
    expect(daily).toHaveTextContent("Asia/Seoul");
    expect(within(daily).getByTestId("schedule-sc-daily-state")).toHaveTextContent("사용 중");
    // 다른 사람의 배포로 멈춘 일정은 까닭을 단다
    expect(screen.getByTestId("schedule-sc-weekly-reason")).toHaveTextContent("다른 사람이 Agent 를 다시 배포해 멈췄습니다.");
    expect(screen.getByTestId("schedule-sc-weekly")).toHaveTextContent("dev 의 이름으로");

    const trigger = await screen.findByTestId("trigger-tr-ci");
    expect(within(trigger).getByTestId("trigger-tr-ci-url")).toHaveTextContent(`${window.location.origin}/api/hooks/tr-ci`);

    expect(await screen.findByTestId("run-run-1-tokens")).toHaveTextContent("1,500");
    expect(screen.getByTestId("run-run-1-status")).toHaveTextContent("완료");
    expect(screen.getByTestId("run-run-2-status")).toHaveTextContent("승인 대기");
    expect(screen.getByTestId("run-run-3-status")).toHaveTextContent("건너뜀 3번");
    expect(screen.getByTestId("run-run-3-reason")).toHaveTextContent("앞 실행이 아직 끝나지 않았습니다.");
    expect(screen.getByTestId("run-run-4-reason")).toHaveTextContent("메시지가 이 Agent 의 입력 한도보다 깁니다.");
    expect(screen.getByTestId("run-run-4")).toHaveTextContent("Agent 의 이름으로");
    expect(screen.getByTestId("run-run-4")).toHaveTextContent("웹훅");
  });

  it("트리거의 서명 비밀은 만든 답에서 한 번만 보이고 비밀을 새로 만들면 새 값이 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("trigger-add"));
    await user.type(screen.getByTestId("trigger-name"), "deploys");
    await user.click(screen.getByTestId("trigger-save"));
    const secret = await screen.findByTestId("trigger-secret");
    expect(secret).toHaveTextContent("이 서명 비밀값은 지금 한 번만 보입니다.");
    expect(within(secret).getByTestId("trigger-secret-value")).toHaveTextContent(/^whsec_/);
    expect(secret).toHaveTextContent(`${window.location.origin}/api/hooks/tr-2`);
    expect(within(secret).getByTestId("trigger-secret-copy")).toHaveAccessibleName("Signing secret 복사");
    expect(lastRequest("POST", "/agents/ag-support/triggers")?.body).toEqual({ name: "deploys", template: "{{payload}}", expires_in_days: 90 });
    // 다시 읽은 목록에는 비밀이 없다
    const row = await screen.findByTestId("trigger-tr-2");
    expect(row).not.toHaveTextContent("whsec_");

    await user.click(within(row).getByTestId("trigger-tr-2-rotate"));
    await waitFor(() => expect(screen.getByTestId("trigger-secret-value")).toHaveTextContent(/-rotated$/));
    // 닫으면 비밀은 다시 볼 수 없다
    await user.click(screen.getByTestId("trigger-secret-close"));
    expect(screen.queryByTestId("trigger-secret")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-triggers")).not.toHaveTextContent("whsec_");
  });

  it("트리거의 이름만 바꾸면 이름만 보낸다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("trigger-tr-ci-edit"));
    const name = screen.getByTestId("trigger-name");
    await user.clear(name);
    await user.type(name, "ci-main");
    await user.click(screen.getByTestId("trigger-save"));
    await waitFor(() => expect(lastRequest("PATCH", "/agents/ag-support/triggers/tr-ci")?.body).toEqual({ name: "ci-main" }));
  });

  it("일정을 더할 때 서버가 거절하면 그 까닭을 대화상자 안에 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("schedule-add"));
    await user.type(screen.getByTestId("schedule-cron"), "* * * * *");
    await user.type(screen.getByTestId("schedule-message"), "check");
    await user.click(screen.getByTestId("schedule-save"));
    expect(await screen.findByTestId("schedule-save-error")).toHaveTextContent("일정은 5분보다 자주 실행할 수 없습니다.");
  });

  it("일정을 멈추고 지운다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("schedule-sc-daily-toggle"));
    await waitFor(() => expect(screen.getByTestId("schedule-sc-daily-state")).toHaveTextContent("일시 정지됨"));
    expect(lastRequest("POST", "/agents/ag-support/schedules/sc-daily/pause")).toBeDefined();
    await user.click(screen.getByTestId("schedule-sc-daily-delete"));
    expect(await screen.findByTestId("confirm-dialog")).toHaveTextContent("지우면 열린 실행이 멈추고 되살릴 수 없습니다.");
    await user.click(screen.getByTestId("confirm-submit"));
    await waitFor(() => expect(screen.queryByTestId("schedule-sc-daily")).not.toBeInTheDocument());
  });

  it("지금 실행은 메시지를 보내고, 실행 한도에 걸리면 한도 문장을 보인다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    await user.click(await screen.findByTestId("run-now"));
    await user.type(screen.getByTestId("run-now-message"), "limit");
    await user.click(screen.getByTestId("run-now-send"));
    expect(await screen.findByTestId("run-now-error")).toHaveTextContent("이 Agent 의 실행 한도에 걸렸습니다.");
    const box = screen.getByTestId("run-now-message");
    await user.clear(box);
    await user.type(box, "check the queue");
    await user.click(screen.getByTestId("run-now-send"));
    await waitFor(() => expect(screen.queryByTestId("run-now-dialog")).not.toBeInTheDocument());
    expect(lastRequest("POST", "/agents/ag-support/runs")?.body).toEqual({ message: "check the queue" });
    expect(await screen.findByTestId("run-run-5-status")).toHaveTextContent("시작 중");
  });

  it("승인자는 모든 실행의 대화를 보고 멈추며, 대화는 세션 탭의 상세로 연다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath={TAB} />);
    const link = await screen.findByTestId("run-run-2-conversation");
    expect(screen.getByTestId("run-run-1-conversation")).toBeInTheDocument();
    // 끝난 실행은 멈출 것이 없다
    expect(screen.queryByTestId("run-run-1-stop")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("run-run-2-stop"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/runs/run-2/cancel")).toBeDefined());
    await user.click(link);
    expect(await screen.findByTestId("session-detail")).toHaveTextContent("s-run-2");
  });

  it("묶인 사람은 자기 실행의 대화만 보고, 쓰기 권한이 없으면 고치는 단추가 없다", async () => {
    const reader = { ...meViewer, sub: "dev@example.com", scopes_by_tenant: { default: ["agent:read", "agent:chat"] } };
    server.use(http.get("/me", () => HttpResponse.json(reader)));
    render(<TestApp initialPath={TAB} />);
    expect(await screen.findByTestId("run-run-2-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("run-run-2-stop")).toBeInTheDocument();
    expect(screen.queryByTestId("run-run-1-conversation")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-now")).not.toBeInTheDocument();
    expect(screen.queryByTestId("schedule-add")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trigger-tr-ci-rotate")).not.toBeInTheDocument();
  });

  it("기록이 없으면 빈 카드의 문장이다", async () => {
    runsState.runs = [];
    render(<TestApp initialPath={TAB} />);
    expect(await within(await screen.findByTestId("run-history")).findByText("아직 자동 실행 기록이 없습니다.")).toBeInTheDocument();
  });

  it("턴을 받지 않는 Agent 에는 묶는 단추와 대화 보기가 없고 멈춤과 지우기는 남는다", async () => {
    agentState.items = [...agentState.items, agentDraftFixture()];
    runsState.runs = runsState.runs.map((r) => ({ ...r, agent_id: "ag-draft" }));
    render(<TestApp initialPath="/agents/ag-draft?tab=runs" />);
    expect(await screen.findByTestId("schedule-sc-daily-toggle")).toHaveTextContent("일시 정지");
    expect(screen.getByTestId("schedule-sc-daily-delete")).toBeInTheDocument();
    expect(screen.getByTestId("trigger-tr-ci-toggle")).toBeInTheDocument();
    // 멈춘 일정의 다시 시작, 편집, 더하기, 비밀 새로 만들기, 지금 실행은 409 not_deployed 라 서지 않는다
    expect(screen.queryByTestId("schedule-sc-weekly-toggle")).not.toBeInTheDocument();
    expect(screen.queryByTestId("schedule-sc-daily-edit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("schedule-add")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trigger-add")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trigger-tr-ci-rotate")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-now")).not.toBeInTheDocument();
    // 세션 탭이 없으니 대화 보기도 없다. 움직이는 실행의 중단은 남는다
    expect(await screen.findByTestId("run-run-2-stop")).toBeInTheDocument();
    expect(screen.queryByTestId("run-run-2-conversation")).not.toBeInTheDocument();
  });

  it("자동 실행이 꺼진 플랫폼에서는 탭이 서지 않는다", async () => {
    runsState.enabled = false;
    render(<TestApp initialPath="/agents/ag-support" />);
    expect(await screen.findByTestId("tab-sessions")).toBeInTheDocument();
    await waitFor(() => expect(lastRequest("GET", "/me/runs-as-me")).toBeDefined());
    await waitFor(() => expect(screen.queryByTestId("tab-runs")).not.toBeInTheDocument());
  });
});
