/**
 * 버튼 하나가 여는 동작 메뉴 (D44 둘째).
 *
 * 화면마다 주 동작은 하나다. 이 메뉴는 버튼이 카드·행마다 늘어날 자리에서만 동작을 모으고, 상세 머리의
 * 동작은 버튼으로 선다(`DetailActions`). 원래는 Backend API 버전 행의 "다듬기" 메뉴(`GenerateMenu`,
 * D25)에만 있던 것을 그대로 뽑아 온 것이라 동작 규칙도 그때 것이다.
 *
 * 패널은 body 에 portal 로 그려 카드의 overflow 에 잘리지 않고, 자리 잡기는 Select 와 같은
 * 훅(`usePopover`)을 쓴다. 그래서 스크롤·리사이즈에 닫히지 않고 트리거를 따라간다. Escape 와 화살표 이동은
 * 패널 자신의 키 핸들러가 받는다. 표의 행 안에서도 쓰이므로 트리거·패널의 클릭은 전파하지 않는다.
 *
 * 파괴적 항목(배포 해제·삭제)은 준 순서와 상관없이 구분선 아래에 모으고 붉은 글자로 둔다. 붉은색은
 * `err-fg` 다. `err` 는 점·테두리에 쓰는 값이라 라이트 테마에서 글자로 쓰면 AA 에 못 미친다.
 */
import { useCallback, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";

import { IconButton } from "@/components/ui/icon-button";
import { MoreDotsIcon } from "@/components/ui/icons";
import { POPOVER_PANEL_CLASS, usePopover } from "@/components/ui/usePopover";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export interface MenuItem {
  label: string;
  /** 눌렀을 때 할 일. `to` 와 둘 중 하나만 준다 */
  onClick?: () => void;
  /** 링크 항목이 갈 곳 */
  to?: string;
  /** 라벨 아래에 붙는 설명 한 줄 */
  hint?: string;
  /** 파괴적 동작인가. 구분선 아래에 붉은 글자로 모인다 */
  danger?: boolean;
  disabled?: boolean;
  testId?: string;
}

/** 트리거에 그대로 펼쳐 넣는 것. 열림 표시와 키보드 규칙이 여기 들어 있다. */
export interface ActionMenuTriggerProps {
  ref: RefObject<HTMLButtonElement | null>;
  "aria-haspopup": "menu";
  "aria-expanded": boolean;
  "aria-controls": string | undefined;
  onClick: (e: MouseEvent) => void;
  onKeyDown: (e: KeyboardEvent<HTMLButtonElement>) => void;
}

export interface ActionMenuProps {
  items: MenuItem[];
  /** 패널의 aria-label. 화면 읽는 사람에게 이 메뉴가 무엇인지 말한다 */
  label?: string;
  /** 트리거를 직접 그린다. 안 주면 동그란 `···` 버튼이다 */
  trigger?: (props: ActionMenuTriggerProps, open: boolean) => ReactNode;
  /** 기본 트리거의 크기. md 는 머리줄의 버튼이고 sm 은 카드·목록 행 안의 작은 버튼이다 */
  triggerSize?: "md" | "sm";
  /** 기본 트리거의 aria-label. 한 화면에 메뉴가 여럿이면 무엇의 메뉴인지 이름을 넣는다 */
  triggerLabel?: string;
  /** 기본 트리거에 덧붙일 클래스. 카드의 덮개 링크 위로 올리거나 행 안에서 숨겼다 보일 때 쓴다. 열리면 늘 보인다 */
  triggerClassName?: string;
  triggerTestId?: string;
  panelTestId?: string;
  panelClassName?: string;
}

/** 포커스를 받을 수 있는 항목만 고른다. disabled 인 button 은 focus() 가 듣지 않는다. */
const FOCUSABLE = '[role="menuitem"]:not(:disabled)';

/*
 * 기본 트리거. md 는 머리 줄에서 낱말 버튼 옆에 서므로 테두리 두른 아이콘 버튼(`IconButton` 의 bordered)이고,
 * sm 은 카드·행 안이라 평소 테두리가 없다가 포인터가 오거나 열리면 선다
 */
const SM_TRIGGER =
  "flex size-7 shrink-0 items-center justify-center rounded-chip border border-transparent text-label transition-colors hover:border-line hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring";

export function ActionMenu({
  items,
  label,
  trigger,
  triggerSize = "md",
  triggerLabel,
  triggerClassName,
  triggerTestId = "header-menu",
  panelTestId = "action-menu-panel",
  panelClassName,
}: ActionMenuProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const menuId = `${useId()}-menu`;

  const close = useCallback(() => setOpen(false), []);
  // 메뉴는 트리거 오른쪽 끝에 맞춘다. 머리의 트리거가 화면 오른쪽 끝이라 왼쪽 맞춤이면 밖으로 나간다
  const { style } = usePopover({ open, triggerRef, panelRef, onClose: close, align: "end" });

  useLayoutEffect(() => {
    if (!open) return;
    // 첫 항목에 포커스를 주어 키보드로 바로 고를 수 있게 한다
    panelRef.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, [open]);

  function onPanelKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    const i = focusable.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      triggerRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      focusable[(i + 1) % focusable.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusable[(i - 1 + focusable.length) % focusable.length]?.focus();
    } else if (e.key === "Tab") {
      close();
    }
  }

  const triggerProps: ActionMenuTriggerProps = {
    ref: triggerRef,
    "aria-haspopup": "menu",
    "aria-expanded": open,
    "aria-controls": open ? menuId : undefined,
    onClick: (e) => {
      e.stopPropagation();
      setOpen((v) => !v);
    },
    onKeyDown: (e) => {
      if (e.key === "ArrowDown" && !open) {
        e.preventDefault();
        setOpen(true);
      }
    },
  };

  const plain = items.filter((it) => !it.danger);
  const danger = items.filter((it) => it.danger);

  const panel = open
    ? createPortal(
        <div
          ref={panelRef}
          id={menuId}
          role="menu"
          aria-label={label ?? t.common.menu.label}
          data-testid={panelTestId}
          style={style}
          onKeyDown={onPanelKeyDown}
          onClick={(e) => e.stopPropagation()}
          className={cn(POPOVER_PANEL_CLASS, "w-56 p-1", panelClassName)}
        >
          {plain.map((item) => (
            <ActionMenuItem key={item.testId ?? item.label} item={item} onDone={close} />
          ))}
          {danger.length && plain.length ? <div className="my-1 h-px bg-line-soft" aria-hidden /> : null}
          {danger.map((item) => (
            <ActionMenuItem key={item.testId ?? item.label} item={item} onDone={close} />
          ))}
        </div>,
        document.body,
      )
    : null;

  return (
    <>
      {trigger ? (
        trigger(triggerProps, open)
      ) : triggerSize === "md" ? (
        <IconButton
          {...triggerProps}
          bordered
          label={triggerLabel ?? t.common.menu.more}
          title={t.common.menu.more}
          testId={triggerTestId}
          className={cn(triggerClassName, open && "bg-hover text-heading opacity-100")}
        >
          <MoreDotsIcon size={16} />
        </IconButton>
      ) : (
        <button
          type="button"
          {...triggerProps}
          data-testid={triggerTestId}
          aria-label={triggerLabel ?? t.common.menu.more}
          title={t.common.menu.more}
          className={cn(SM_TRIGGER, triggerClassName, open && "border-line bg-hover text-fg opacity-100")}
        >
          <MoreDotsIcon size={15} />
        </button>
      )}
      {panel}
    </>
  );
}

function ActionMenuItem({ item, onDone }: { item: MenuItem; onDone: () => void }) {
  const className = cn(
    "flex w-full flex-col gap-0.5 rounded-item px-2.5 py-1.5 text-left text-sm outline-none",
    "hover:bg-hover focus-visible:bg-hover focus-visible:ring-2 focus-visible:ring-focus-ring",
    item.danger ? "text-err-fg" : "text-fg",
    item.disabled && "opacity-50",
  );
  const body = (
    <>
      <span className={cn("font-medium", item.danger ? "text-err-fg" : "text-heading")}>{item.label}</span>
      {item.hint ? <span className="text-xs text-muted">{item.hint}</span> : null}
    </>
  );
  if (item.to && !item.disabled) {
    return (
      <Link role="menuitem" to={item.to} data-testid={item.testId} onClick={onDone} className={className}>
        {body}
      </Link>
    );
  }
  return (
    <button
      type="button"
      role="menuitem"
      data-testid={item.testId}
      disabled={item.disabled}
      className={className}
      onClick={() => {
        onDone();
        item.onClick?.();
      }}
    >
      {body}
    </button>
  );
}
