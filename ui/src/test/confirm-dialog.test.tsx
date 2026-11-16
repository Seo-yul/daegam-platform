import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { ConfirmDialog, type ConfirmDialogProps } from "@/components/ui/confirm-dialog";

/** 여는 버튼 + 대화상자. 포커스 복귀를 검사하려면 여는 버튼이 실제로 있어야 한다. */
function Harness(props: Partial<ConfirmDialogProps> & { onConfirm?: () => void; onCancel?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" data-testid="opener" onClick={() => setOpen(true)}>
        open
      </button>
      <ConfirmDialog
        open={open}
        title="Delete thing"
        description={<p>설명입니다.</p>}
        confirmLabel="Delete"
        pendingLabel="Deleting…"
        {...props}
        onConfirm={() => props.onConfirm?.()}
        onCancel={() => {
          props.onCancel?.();
          setOpen(false);
        }}
      />
    </div>
  );
}

describe("ConfirmDialog", () => {
  it("renders nothing when closed and a modal dialog in a body portal when open", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("opener"));
    const dialog = screen.getByTestId("confirm-dialog");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    // 제목·설명이 aria 로 이어진다
    const title = document.getElementById(dialog.getAttribute("aria-labelledby")!);
    expect(title).toHaveTextContent("Delete thing");
    const desc = document.getElementById(dialog.getAttribute("aria-describedby")!);
    expect(desc).toHaveTextContent("설명입니다.");
    // 첫 포커스는 취소 버튼
    expect(screen.getByTestId("confirm-cancel")).toHaveFocus();
    expect(screen.getByTestId("confirm-submit")).toBeEnabled();
  });

  it("closes on Escape and returns focus to the opener", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<Harness onCancel={onCancel} />);
    await user.click(screen.getByTestId("opener"));
    await screen.findByTestId("confirm-dialog");
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("opener")).toHaveFocus();
  });

  it("여는 버튼이 사라졌으면 포커스가 페이지 제목으로 간다", async () => {
    const user = userEvent.setup();
    // 배포를 누르면 "배포" 버튼이 "재배포" 로 바뀌고, 삭제를 누르면 그 행이 없어진다. 그때 포커스가
    // body 로 떨어지면 낭독기는 문서 처음으로 돌아가고 키보드는 Tab 을 처음부터 다시 밟는다
    function Vanishing() {
      const [open, setOpen] = useState(false);
      const [gone, setGone] = useState(false);
      return (
        <div>
          <h1 data-page-heading data-testid="heading">
            MCP Server
          </h1>
          {gone ? null : (
            <button type="button" data-testid="opener" onClick={() => setOpen(true)}>
              open
            </button>
          )}
          <ConfirmDialog
            open={open}
            title="Delete thing"
            onConfirm={() => {
              setGone(true);
              setOpen(false);
            }}
            onCancel={() => setOpen(false)}
          />
        </div>
      );
    }
    render(<Vanishing />);
    await user.click(screen.getByTestId("opener"));
    await user.click(screen.getByTestId("confirm-submit"));
    expect(screen.queryByTestId("opener")).not.toBeInTheDocument();
    expect(screen.getByTestId("heading")).toHaveFocus();
    // 제목은 Tab 순서에는 들어가지 않는다
    expect(screen.getByTestId("heading")).toHaveAttribute("tabindex", "-1");
  });

  it("closes on backdrop click but not on clicks inside the panel", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<Harness onCancel={onCancel} />);
    await user.click(screen.getByTestId("opener"));
    await user.click(screen.getByTestId("confirm-dialog"));
    expect(onCancel).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("confirm-dialog-backdrop"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("confirm-dialog")).not.toBeInTheDocument();
  });

  it("confirm and cancel buttons call their handlers", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<Harness onConfirm={onConfirm} onCancel={onCancel} />);
    await user.click(screen.getByTestId("opener"));
    await user.click(screen.getByTestId("confirm-submit"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("confirm-cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab focus inside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByTestId("opener"));
    expect(screen.getByTestId("confirm-cancel")).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId("confirm-submit")).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId("confirm-cancel")).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByTestId("confirm-submit")).toHaveFocus();
  });

  it("gates the confirm button on an exact name match and submits with Enter", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<Harness requireName="calc-1" onConfirm={onConfirm} />);
    await user.click(screen.getByTestId("opener"));
    const input = screen.getByTestId("confirm-name");
    expect(input).toHaveFocus();
    expect(screen.getByTestId("confirm-submit")).toBeDisabled();
    expect(screen.getByTestId("confirm-dialog")).toHaveTextContent("삭제하려면 이름을 입력해 주세요.");
    await user.type(input, "calc");
    expect(screen.getByTestId("confirm-submit")).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(onConfirm).not.toHaveBeenCalled();
    await user.type(input, "-1");
    expect(screen.getByTestId("confirm-submit")).toBeEnabled();
    await user.keyboard("{Enter}");
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("disables everything while pending and ignores Escape", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<Harness pending onCancel={onCancel} />);
    await user.click(screen.getByTestId("opener"));
    expect(screen.getByTestId("confirm-submit")).toBeDisabled();
    expect(screen.getByTestId("confirm-submit")).toHaveTextContent("Deleting…");
    expect(screen.getByTestId("confirm-cancel")).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onCancel).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("confirm-dialog-backdrop"));
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
  });

  it("shows the error inside the dialog and stays open", async () => {
    const user = userEvent.setup();
    render(<Harness error={<span>in_use: still referenced</span>} />);
    await user.click(screen.getByTestId("opener"));
    const err = screen.getByTestId("confirm-error");
    expect(err).toHaveAttribute("role", "alert");
    expect(err).toHaveTextContent("in_use: still referenced");
    expect(within(screen.getByTestId("confirm-dialog")).getByTestId("confirm-error")).toBe(err);
    expect(screen.getByTestId("confirm-submit")).toBeEnabled();
  });
});
