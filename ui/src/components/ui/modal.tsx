import { useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { useDialogShell } from "./dialog-shell";

/**
 * 모달. ConfirmDialog 와 같은 규칙으로 body 에 portal 로 그리고, 내용은 호출부가 채운다.
 *
 * - role="dialog" + aria-modal, 제목을 aria-labelledby 로 잇는다.
 * - 포커스는 패널 안에 갇힌다(Tab 순환). 열릴 때 닫기 버튼에 먼저 가고, 닫히면 열었던 요소로
 *   돌아간다. 그 요소가 사라졌으면 페이지 제목으로 간다.
 * - Esc·바깥 클릭·닫기 버튼으로 닫힌다. 열려 있는 동안 body 스크롤을 막는다.
 * - size "lg" 는 90vw × 85vh 의 문서 뷰어 크기이고, 자식이 스크롤을 맡는다 (min-h-0 flex column).
 */
export interface ModalProps {
  open: boolean;
  title: ReactNode;
  /** 제목 옆에 붙는 배지 등. */
  titleExtra?: ReactNode;
  size?: "md" | "lg";
  testId?: string;
  closeTestId?: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({
  open,
  title,
  titleExtra,
  size = "md",
  testId = "modal",
  closeTestId = "modal-close",
  onClose,
  children,
}: ModalProps) {
  const t = useT();
  const titleId = `${useId()}-title`;
  const closeRef = useRef<HTMLButtonElement>(null);
  const { panelRef, onKeyDown, onBackdropMouseDown } = useDialogShell({ open, onClose, initialFocus: () => closeRef.current });

  if (!open) return null;

  return createPortal(
    <div
      className="dialog-dim-enter fixed inset-0 z-(--ui-z-dialog) flex items-center justify-center bg-dialog-dim p-4"
      data-testid={`${testId}-backdrop`}
      onMouseDown={onBackdropMouseDown}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid={testId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={cn(
          "dialog-enter flex flex-col rounded-dialog border border-line-strong bg-surface-popover shadow-popover outline-none",
          // md 는 내용이 화면보다 길어지면(게시 대화상자의 이름 두 칸 + 정책 편집기) 본문이 스크롤한다
          size === "lg" ? "h-[85vh] w-[90vw] max-w-none" : "max-h-[calc(100vh-2rem)] w-full max-w-lg",
        )}
      >
        <div className="flex shrink-0 items-center gap-3 border-b border-line px-5 py-3.5">
          <h2 id={titleId} className="text-base font-semibold text-heading">
            {title}
          </h2>
          {titleExtra}
          <button
            ref={closeRef}
            type="button"
            data-testid={closeTestId}
            aria-label={t.ui.modal.close}
            onClick={onClose}
            className="ml-auto rounded-item p-1.5 text-muted hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>
        <div className={cn("flex min-h-0 flex-1 flex-col", size === "md" && "overflow-y-auto")}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}
