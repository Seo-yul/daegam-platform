import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { Button } from "./button";
import { Alert } from "./card";
import { useDialogShell } from "./dialog-shell";
import { Input } from "./input";

/**
 * 확인 대화상자. 파괴적인 동작(삭제) 앞에 한 번 더 묻는다.
 *
 * - body 에 portal 로 그린다. 배경은 dim 이고, 패널은 팝오버와 같은 불투명 면이다.
 * - role="dialog" + aria-modal, 제목·설명을 aria-labelledby/describedby 로 잇는다.
 * - 포커스는 대화상자 안에 갇힌다(Tab 순환). 열릴 때 취소 버튼(이름 입력이 있으면 입력)에 먼저 가고,
 *   닫히면 열었던 요소로 돌아간다. 그 요소가 사라졌으면 페이지 제목으로 간다.
 * - Esc·바깥 클릭으로 닫힌다. pending 이면 닫히지 않는다.
 * - requireName 이 주어지면 입력이 정확히 일치해야 확인 버튼이 살아난다.
 * - error 는 대화상자 안에 붉게 보여 주고 대화상자는 닫지 않는다.
 * - 등장 애니메이션은 prefers-reduced-motion 이면 생략한다 (index.css 의 dialog-enter).
 */
export type ConfirmDialogProps = {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 주어지면 이 값과 똑같이 입력해야 확인할 수 있다. */
  requireName?: string;
  /** 부르는 쪽의 입력이 아직 덜 됐다(예: 비밀번호가 짧다). 확인 버튼을 끈다. */
  confirmDisabled?: boolean;
  /** 열릴 때 포커스를 줄 요소. 없으면 이름 확인 칸, 그다음 취소 버튼이다. */
  initialFocus?: RefObject<HTMLElement | null>;
  /** 진행 중. 버튼이 비활성화되고 확인 버튼이 pendingLabel 로 바뀐다. */
  pending?: boolean;
  pendingLabel?: string;
  /** 서버 오류 등. 있으면 대화상자 안에 붉은 안내로 보인다. */
  error?: ReactNode;
  /** 확인·취소 사이에 끼울 추가 버튼(예: 강제 삭제). */
  extraActions?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  requireName,
  confirmDisabled = false,
  initialFocus,
  pending = false,
  pendingLabel,
  error,
  extraActions,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const t = useT();
  const reactId = useId();
  const titleId = `${reactId}-title`;
  const descId = `${reactId}-desc`;
  const cancelRef = useRef<HTMLButtonElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");

  const nameOk = !requireName || name === requireName;
  const canConfirm = nameOk && !pending && !confirmDisabled;

  // 열릴 때마다 이름 확인 칸을 비운다. 포커스·스크롤 잠금·닫힐 때 되돌리기는 공통 껍데기가 한다
  useEffect(() => {
    if (open) setName("");
  }, [open]);
  const shell = useDialogShell({
    open,
    onClose: onCancel,
    canClose: !pending,
    initialFocus: () => initialFocus?.current ?? nameRef.current ?? cancelRef.current,
  });

  if (!open) return null;

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Enter" && e.target === nameRef.current) {
      e.preventDefault();
      if (canConfirm) onConfirm();
      return;
    }
    shell.onKeyDown(e);
  }

  return createPortal(
    <div
      className="dialog-dim-enter fixed inset-0 z-(--ui-z-dialog) flex items-center justify-center bg-dialog-dim p-4"
      data-testid="confirm-dialog-backdrop"
      onMouseDown={shell.onBackdropMouseDown}
    >
      <div
        ref={shell.panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        data-testid="confirm-dialog"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="dialog-enter flex w-full max-w-md flex-col gap-4 rounded-dialog border border-line-strong bg-surface-popover p-6 shadow-popover outline-none"
      >
        <div className="flex flex-col gap-1.5">
          <h2 id={titleId} className="text-base font-semibold text-heading">
            {title}
          </h2>
          {description ? (
            <div id={descId} className="flex flex-col gap-1 text-sm text-muted">
              {description}
            </div>
          ) : null}
        </div>

        {requireName ? (
          <div className="flex flex-col gap-1">
            <Input
              ref={nameRef}
              data-testid="confirm-name"
              aria-label={t.common.nameConfirm.label}
              autoComplete="off"
              spellCheck={false}
              placeholder={requireName}
              value={name}
              disabled={pending}
              onChange={(e) => setName(e.target.value)}
              className={cn("font-mono text-xs", name && !nameOk && "border-err-line")}
            />
            <p className="text-xs text-label">{t.common.nameConfirm.hint}</p>
          </div>
        ) : null}

        {error ? (
          <Alert variant="error" data-testid="confirm-error">
            {error}
          </Alert>
        ) : null}

        <div className="flex items-center justify-end gap-2">
          <Button ref={cancelRef} variant="outline" data-testid="confirm-cancel" disabled={pending} onClick={onCancel}>
            {cancelLabel ?? t.common.cancel}
          </Button>
          {extraActions}
          <Button variant="destructive" data-testid="confirm-submit" disabled={!canConfirm} onClick={onConfirm}>
            {pending ? (pendingLabel ?? t.common.processing) : (confirmLabel ?? t.common.confirm)}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
