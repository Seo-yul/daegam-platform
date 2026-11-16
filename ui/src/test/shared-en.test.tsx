/**
 * 영어로 그린 공용 컴포넌트의 언어 전환 (P49 F3a). Advanced 편집기의 파싱 문제, 승인 카드의 결정 오류, 화면 그리기 오류는
 * 상태에 문장 대신 코드를 두므로 언어를 바꾸면 다시 그려지고, 영어일 때 자기 글에 한글이 남지 않는다.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import type { AgentPending } from "@/api/types";
import { AdvancedEditor } from "@/components/AdvancedEditor";
import { ApprovalCard } from "@/components/ApprovalCard";
import { RouteErrorBoundary } from "@/components/ErrorBoundary";
import { setLanguage } from "@/lib/language";

import { leftKorean } from "./leftKorean";
import { manifestDoc } from "./msw";
import { renderWithProviders } from "./render";

function pending(allowed: AgentPending["review_configs"][number]["allowed_decisions"]): AgentPending {
  return {
    session_id: "s-1",
    action_requests: [{ name: "Divide", args: { a: 8, b: 2 }, description: "Divides two numbers." }],
    review_configs: [{ action_name: "Divide", allowed_decisions: allowed }],
  } as AgentPending;
}

function Boom(): never {
  throw new Error("metrics payload is malformed");
}

describe("shared components in English", () => {
  beforeEach(() => {
    setLanguage("en");
  });

  it("draws the Advanced editor in English and redraws a parse problem in the new language", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AdvancedEditor documents={[manifestDoc]} onSave={vi.fn()} onReset={vi.fn()} onAbsorb={vi.fn()} />);
    await screen.findByTestId("monaco");
    const root = screen.getByTestId("advanced-editor");
    expect(screen.getByTestId("view-edit")).toHaveTextContent("Edit (final)");
    expect(screen.getByTestId("view-drift")).toHaveTextContent("Drift (1)");
    expect(screen.getByTestId("path-panel")).toHaveTextContent("Platform-managed values");
    expect(leftKorean(root)).toEqual([]);

    await user.click(screen.getByTestId("view-drift"));
    expect(screen.getByTestId("drift-managers")).toHaveTextContent("Other managers also change this resource: kubectl-edit");
    expect(screen.getByTestId("drift-reset")).toHaveTextContent("Revert to platform values");
    expect(within(screen.getByTestId("drift-diff")).getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["Path", "Before", "After"]);
    expect(leftKorean(root)).toEqual([]);

    await user.click(screen.getByTestId("view-edit"));
    await user.clear(await screen.findByTestId("monaco"));
    await user.type(screen.getByTestId("monaco"), "- a");
    expect(screen.getByTestId("dirty-badge")).toHaveTextContent("Unsaved");
    await user.click(screen.getByTestId("save-button"));
    expect(await screen.findByTestId("parse-error")).toHaveTextContent("YAML: The top level must be a single mapping");
    act(() => setLanguage("ko"));
    await waitFor(() => expect(screen.getByTestId("parse-error")).toHaveTextContent("YAML: 최상위가 매핑 하나여야 합니다"));
  });

  it("draws the approval card in English and keeps a decision error as a code across a language switch", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ApprovalCard pending={pending(["approve", "edit", "reject", "respond"])} onSubmit={() => {}} />);
    const card = screen.getByTestId("approval-card");
    expect(card).toHaveTextContent("Approval needed");
    expect(card).toHaveTextContent("1 Tool call awaits a decision.");
    expect(screen.getByTestId("decision-edit")).toHaveTextContent("Edit and approve");
    await user.click(screen.getByTestId("decision-respond"));
    expect(screen.getByTestId("decision-message")).toHaveAttribute("placeholder", "Message for the model instead of running the Tool");
    await user.click(screen.getByTestId("decision-submit"));
    expect(screen.getByTestId("decision-error")).toHaveTextContent("Enter a message.");
    expect(leftKorean(card)).toEqual([]);
    act(() => setLanguage("ko"));
    expect(screen.getByTestId("decision-error")).toHaveTextContent("메시지를 입력해 주세요.");
  });

  it("draws the render error in English and redraws it when the language changes", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderWithProviders(
      <RouteErrorBoundary>
        <Routes>
          <Route path="/boom" element={<Boom />} />
        </Routes>
      </RouteErrorBoundary>,
      { initialPath: "/boom" },
    );
    const box = screen.getByTestId("render-error");
    expect(box).toHaveTextContent("Could not display this screen.");
    expect(screen.getByTestId("render-error-retry")).toHaveTextContent("Try again");
    expect(leftKorean(box)).toEqual([]);
    act(() => setLanguage("ko"));
    expect(screen.getByTestId("render-error")).toHaveTextContent("이 화면을 그리지 못했습니다.");
  });
});
