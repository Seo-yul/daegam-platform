/**
 * 렌더 경계. 화면 하나가 그리다 터져도 껍데기가 남아야 한다.
 *
 * 이 그물이 없던 동안 Agent 메트릭 탭의 `TypeError` 하나가 사이드바까지 날려 흰 화면을 남겼다.
 * 사람이 본 것은 "무엇이 잘못됐다" 가 아니라 아무것도 없는 화면이었고 돌아갈 길도 없었다.
 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RouteErrorBoundary } from "@/components/ErrorBoundary";

import { renderWithProviders } from "./render";

function Boom(): never {
  throw new Error("메트릭을 읽지 못했습니다");
}

/** 사이드바와 그리다 터지는 화면 하나로 된 앱을 그 화면에서 연다. */
function renderBoom() {
  return renderWithProviders(
    <div>
      <span>사이드바</span>
      <RouteErrorBoundary>
        <Routes>
          <Route path="/boom" element={<Boom />} />
        </Routes>
      </RouteErrorBoundary>
    </div>,
    { initialPath: "/boom" },
  );
}

describe("RouteErrorBoundary", () => {
  beforeEach(() => {
    // React 가 잡힌 예외도 콘솔에 쏟는다. 테스트 출력이 그것으로 덮이지 않게 막는다
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  it("터진 화면 대신 문구를 세우고 바깥은 남긴다", () => {
    renderBoom();
    expect(screen.getByTestId("render-error")).toBeInTheDocument();
    // 우리 코드가 낸 문구다. 감추면 무엇이 잘못됐는지 물어볼 말조차 없어진다
    expect(screen.getByTestId("render-error-message")).toHaveTextContent("메트릭을 읽지 못했습니다");
    // 흰 화면이 되지 않는 것이 이 경계의 전부다
    expect(screen.getByText("사이드바")).toBeInTheDocument();
  });

  it("다시 그리기를 누르면 같은 화면을 한 번 더 시도한다", async () => {
    const user = userEvent.setup();
    renderBoom();
    await user.click(screen.getByTestId("render-error-retry"));
    // 원인이 그대로면 또 터진다. 그래도 앱은 살아 있어야 한다
    expect(screen.getByTestId("render-error")).toBeInTheDocument();
    expect(screen.getByText("사이드바")).toBeInTheDocument();
  });
});
