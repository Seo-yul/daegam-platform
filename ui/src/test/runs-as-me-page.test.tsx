/**
 * 계정 화면 "나를 대신하는 자동 실행" (P41 6절). 모든 테넌트에서 나에게 묶인 일정과 트리거를 보이고 멈춘다. 자동 실행이 꺼진
 * 플랫폼에서는 머리의 링크가 서지 않는다.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TestApp } from "@/App";

import { lastRequest, runsState } from "./msw";

describe("나를 대신하는 자동 실행", () => {
  it("나에게 묶인 일정과 트리거를 테넌트와 Agent 이름으로 보이고 하나를 멈춘다", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/runs-as-me" />);
    expect(await screen.findByTestId("page-title")).toHaveTextContent("나를 대신하는 자동 실행");
    const schedule = await screen.findByTestId("runs-as-me-sc-daily");
    expect(schedule).toHaveTextContent("default");
    expect(schedule).toHaveTextContent("support");
    expect(schedule).toHaveTextContent("0 9 * * 1-5");
    expect(screen.getByTestId("runs-as-me-tr-ci")).toHaveTextContent("웹훅 ci");
    // 다른 사람에게 묶인 일정은 없다
    expect(screen.queryByTestId("runs-as-me-sc-weekly")).not.toBeInTheDocument();
    await user.click(within(schedule).getByTestId("runs-as-me-sc-daily-pause"));
    await waitFor(() => expect(within(screen.getByTestId("runs-as-me-sc-daily")).getByTestId("runs-as-me-sc-daily-state")).toHaveTextContent("일시 정지됨"));
    expect(lastRequest("POST", "/me/runs-as-me/schedules/sc-daily/pause")).toBeDefined();
  });

  it("나에게 묶인 것이 없으면 빈 줄의 문장이다", async () => {
    runsState.schedules = [];
    runsState.triggers = [];
    render(<TestApp initialPath="/account/runs-as-me" />);
    expect(await screen.findByText("나를 대신해 실행하는 일정이나 트리거가 없습니다.")).toBeInTheDocument();
  });

  it("머리의 링크는 자동 실행이 켜져 있을 때만 선다", async () => {
    render(<TestApp initialPath="/" />);
    expect(await screen.findByTestId("runs-as-me-link")).toHaveTextContent("나를 대신하는 자동 실행");
  });

  it("자동 실행이 꺼지면 머리의 링크가 없다", async () => {
    runsState.enabled = false;
    render(<TestApp initialPath="/" />);
    expect(await screen.findByTestId("approval-choices-link")).toBeInTheDocument();
    await waitFor(() => expect(lastRequest("GET", "/me/runs-as-me")).toBeDefined());
    await waitFor(() => expect(screen.queryByTestId("runs-as-me-link")).not.toBeInTheDocument());
  });
});
