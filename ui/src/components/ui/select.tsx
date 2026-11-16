import {
  Children,
  Fragment,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type OptionHTMLAttributes,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { fieldClass } from "./field";
import { CheckIcon } from "./icons";
import { POPOVER_PANEL_CLASS, usePopover } from "./usePopover";

/**
 * 커스텀 셀렉트. 네이티브 <select> 의 호출 방식(value, onChange(e.target.value),
 * children 으로 <option>)을 그대로 받되, 메뉴는 OS 위젯이 아니라 떠 있는 패널로 그린다.
 *
 * - 트리거는 <button role="combobox">. id 로 <label htmlFor> 가 붙고, value 속성에 현재 값을 실어
 *   테스트의 toHaveValue 가 그대로 통한다.
 * - 패널은 body 에 portal 로 그려 카드의 overflow 에 잘리지 않는다. 트리거 바로
 *   아래에 트리거 폭으로 열리고, 아래 공간이 모자라면 위로 연다.
 * - 키보드: ArrowUp/Down·Home/End 이동, Enter/Space 선택, Esc 닫기, Tab 은 닫고 포커스 이동,
 *   열린 상태에서 문자 입력은 첫 글자 일치 항목으로 이동. 바깥 클릭·Escape·blur 에 닫힌다.
 * - 자리 잡기는 usePopover 가 맡는다. 스크롤과 리사이즈에는 닫지 않고 앵커를 따라 다시 자리 잡는다.
 * - 나머지 prop 은 트리거 버튼에 그대로 펼친다. Field 가 얹는 aria-describedby·aria-invalid 가 그 길로 닿는다.
 * - aria-disabled 는 disabled 처럼 열리지 않게 하되 트리거가 초점을 지킨다. 고른 뒤 후보가 떨어져 잠기는 자리에서 disabled 를
 *   걸면 방금 누른 트리거가 초점을 잃어 body 로 떨어지기 때문이다(캔버스의 Skill 붙이기).
 * - required 는 숨은 native input 이 <form> 검증을 맡는다. invalid 이면 기본 말풍선 대신 트리거에
 *   포커스를 주고 aria-invalid 로 표시한다. 숨은 input 은 pointer-events 가 없어 클릭 대상이 아니다.
 */

export type SelectChangeEvent = {
  target: { value: string; name?: string };
  currentTarget: { value: string; name?: string };
};

/** 트리거 버튼이 받는 나머지 속성. value·onChange·children 은 이 컴포넌트의 뜻이 달라 뺀다. */
type TriggerAttrs = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "value" | "onChange" | "children" | "type" | "role">;

export type SelectProps = TriggerAttrs & {
  value?: string | number;
  onChange?: (e: SelectChangeEvent) => void;
  /** <option value disabled>label</option> 목록. Fragment·배열·null 은 평탄화한다. */
  children?: ReactNode;
  required?: boolean;
  /** 일치하는 option 이 없을 때 트리거에 보일 글. */
  placeholder?: string;
  /**
   * 트리거 안을 직접 그린다. 주면 고른 항목의 글과 갈매기 표시 대신 이것이 선다. 사이드바의 테넌트 전환처럼 고른 값을
   * 글 한 줄이 아닌 모양(머리글자 칸, 두 줄)으로 보여야 하는 자리에 쓴다 (P81 5절).
   */
  renderTrigger?: (selected: { value: string; label: string } | null) => ReactNode;
  "data-testid"?: string;
};

type Item = { value: string; label: string; disabled: boolean };

const MAX_PANEL_HEIGHT = 280;
const TYPEAHEAD_RESET_MS = 500;

function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function collectItems(children: ReactNode, out: Item[]): Item[] {
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    if (child.type === Fragment) {
      collectItems((child.props as { children?: ReactNode }).children, out);
      return;
    }
    if (child.type === "option") {
      const p = child.props as OptionHTMLAttributes<HTMLOptionElement>;
      // 네이티브 option 처럼 공백을 접는다. value 가 없으면 라벨이 값이다.
      const label = textOf(p.children).replace(/\s+/g, " ").trim();
      out.push({ value: p.value == null ? label : String(p.value), label, disabled: Boolean(p.disabled) });
    }
  });
  return out;
}

function ChevronIcon({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M4 6l4 4 4-4" />
    </svg>
  );
}

export function Select({
  value,
  onChange,
  children,
  id,
  name,
  disabled = false,
  required = false,
  autoFocus,
  className,
  placeholder = "",
  renderTrigger,
  "data-testid": testId,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-invalid": ariaInvalid,
  "aria-disabled": ariaDisabled,
  ...rest
}: SelectProps) {
  const t = useT();
  const locked = disabled || ariaDisabled === true || ariaDisabled === "true";
  const items = useMemo(() => collectItems(children, []), [children]);
  const current = value == null ? "" : String(value);
  const selectedIndex = items.findIndex((it) => it.value === current);
  const selected = selectedIndex >= 0 ? items[selectedIndex] : null;

  const reactId = useId();
  const listboxId = `${reactId}-listbox`;
  const optionId = (i: number) => `${reactId}-opt-${i}`;

  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: "", at: 0 });
  const swallowSpaceUp = useRef(false);

  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [invalid, setInvalid] = useState(false);

  const firstEnabled = items.findIndex((it) => !it.disabled);
  const lastEnabled = items.findLastIndex((it) => !it.disabled);

  const openMenu = useCallback(() => {
    if (locked) return;
    setActive(selectedIndex >= 0 && !items[selectedIndex].disabled ? selectedIndex : firstEnabled);
    setOpen(true);
    // macOS Safari 는 클릭한 버튼에 포커스를 주지 않는다. 키보드 조작·blur 닫기가 되도록 직접 준다.
    triggerRef.current?.focus();
  }, [locked, selectedIndex, items, firstEnabled]);

  const closeMenu = useCallback(() => {
    setOpen(false);
    typeahead.current = { text: "", at: 0 };
  }, []);

  // 트리거 아래(또는 위)에 트리거 폭으로 연다. 스크롤·리사이즈에는 닫지 않고 따라간다
  const { side, style: panelStyle } = usePopover({
    open,
    triggerRef,
    panelRef,
    onClose: closeMenu,
    matchWidth: true,
    maxHeight: MAX_PANEL_HEIGHT,
    revision: items.length,
  });

  const commit = useCallback(
    (item: Item | undefined) => {
      if (!item || item.disabled) return;
      closeMenu();
      triggerRef.current?.focus();
      if (item.value !== current) {
        const detail = { value: item.value, name };
        onChange?.({ target: detail, currentTarget: detail });
      }
    },
    [closeMenu, current, name, onChange],
  );

  useEffect(() => {
    setInvalid(false);
  }, [current]);

  useEffect(() => {
    if (open && locked) closeMenu();
  }, [open, locked, closeMenu]);

  /*
   * 대화상자 안에서 열리면 그 대화상자가 이 목록을 논리적 자식으로 갖게 한다.
   *
   * 패널은 body 로 portal 되므로 DOM 으로는 `aria-modal="true"` 바깥이고, 그대로 두면 낭독기의 모달
   * 모드에서 목록에 아예 닿지 못한다. 패널을 대화상자 안으로 옮기는 길은 막혀 있다. 대화상자 본문은
   * 넘치면 스크롤하는 상자라 그 안의 목록이 잘리고, 등장 애니메이션(`dialog-enter`)의 transform 이 도는
   * 동안은 대화상자가 `position: fixed` 의 컨테이닝 블록이 되어 뷰포트 좌표가 어긋난다.
   * 그래서 자리는 그대로 두고 `aria-owns` 로 관계만 잇는다. 대화상자 쪽 컴포넌트는 건드리지 않는다.
   */
  useEffect(() => {
    if (!open) return;
    const dialog = triggerRef.current?.closest('[role="dialog"]');
    if (!dialog) return;
    const before = dialog.getAttribute("aria-owns");
    dialog.setAttribute("aria-owns", [before, listboxId].filter(Boolean).join(" "));
    return () => {
      const left = (dialog.getAttribute("aria-owns") ?? "").split(/\s+/).filter((id) => id && id !== listboxId);
      if (left.length) dialog.setAttribute("aria-owns", left.join(" "));
      else dialog.removeAttribute("aria-owns");
    };
  }, [open, listboxId]);

  // 열린 채로 항목이 줄어들면(비동기 목록 교체) 활성 인덱스를 범위 안으로 되돌린다.
  useEffect(() => {
    if (open && active >= items.length) setActive(firstEnabled);
  }, [open, active, items.length, firstEnabled]);

  // 활성 항목이 패널 밖이면 스크롤해 보이게 한다.
  useEffect(() => {
    if (!open || active < 0) return;
    const el = panelRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    el?.scrollIntoView?.({ block: "nearest" });
  }, [open, active]);

  function move(delta: 1 | -1) {
    let i = active < 0 ? (delta === 1 ? -1 : items.length) : active;
    for (;;) {
      i += delta;
      if (i < 0 || i >= items.length) return;
      if (!items[i].disabled) {
        setActive(i);
        return;
      }
    }
  }

  function typeTo(ch: string) {
    const now = Date.now();
    const buf = typeahead.current;
    buf.text = now - buf.at > TYPEAHEAD_RESET_MS ? ch : buf.text + ch;
    buf.at = now;
    let query = buf.text.toLowerCase();
    // 같은 글자를 반복하면 그 글자로 시작하는 항목을 순환한다.
    const repeated = query.length > 1 && query.split("").every((c) => c === query[0]);
    if (repeated) query = query[0];
    const start = query.length === 1 ? active + 1 : Math.max(active, 0);
    for (let k = 0; k < items.length; k++) {
      const i = (start + k) % items.length;
      if (!items[i].disabled && items[i].label.toLowerCase().startsWith(query)) {
        setActive(i);
        return;
      }
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (locked) return;
    if (!open) {
      // Enter/Space 는 button 의 기본 click 이 열어 준다.
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        openMenu();
      }
      return;
    }
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        move(-1);
        break;
      case "Home":
        e.preventDefault();
        if (firstEnabled >= 0) setActive(firstEnabled);
        break;
      case "End":
        e.preventDefault();
        if (lastEnabled >= 0) setActive(lastEnabled);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        swallowSpaceUp.current = e.key === " ";
        if (active >= 0) commit(items[active]);
        else closeMenu();
        break;
      case "Escape":
        e.preventDefault();
        // 대화상자 안에서 열렸으면 Escape 는 목록만 닫아야 한다. 그대로 올려보내면 대화상자가 함께 닫힌다.
        e.stopPropagation();
        closeMenu();
        triggerRef.current?.focus();
        break;
      case "Tab":
        closeMenu();
        break;
      default:
        if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault();
          typeTo(e.key);
        }
    }
  }

  function onKeyUp(e: KeyboardEvent<HTMLButtonElement>) {
    // Space 의 click 은 keyup 에서 난다. 선택에 쓴 Space 가 트리거를 다시 열지 않게 막는다.
    if (e.key === " " && swallowSpaceUp.current) {
      e.preventDefault();
      swallowSpaceUp.current = false;
    }
  }

  const panel = open
    ? createPortal(
        <div
          ref={panelRef}
          id={listboxId}
          role="listbox"
          aria-labelledby={ariaLabelledBy}
          aria-label={ariaLabel}
          data-testid={testId ? `${testId}-listbox` : undefined}
          data-side={side}
          tabIndex={-1}
          style={panelStyle}
          onMouseDown={(e) => e.preventDefault()} // 트리거의 포커스를 지켜 blur 로 닫히지 않게
          className={cn(POPOVER_PANEL_CLASS, "overflow-y-auto p-1")}
        >
          {items.length === 0 ? (
            <div className="px-2.5 py-1.5 text-sm text-label">{t.ui.select.noOptions}</div>
          ) : (
            items.map((item, i) => {
              const isSelected = i === selectedIndex;
              const isActive = i === active;
              return (
                <div
                  key={`${item.value}\u0000${i}`}
                  id={optionId(i)}
                  role="option"
                  aria-selected={isSelected}
                  aria-disabled={item.disabled || undefined}
                  data-index={i}
                  data-value={item.value}
                  data-active={isActive || undefined}
                  onMouseMove={() => {
                    if (!item.disabled && !isActive) setActive(i);
                  }}
                  onClick={() => commit(item)}
                  className={cn(
                    "flex cursor-pointer select-none items-center gap-2 rounded-item px-2.5 py-1.5 text-sm text-fg",
                    isActive && !item.disabled && "bg-accent-soft",
                    isSelected && "font-semibold text-heading",
                    item.disabled && "cursor-not-allowed opacity-40",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  {isSelected ? <CheckIcon className="size-4 shrink-0 text-link" /> : <span className="size-4 shrink-0" />}
                </div>
              );
            })
          )}
        </div>,
        document.body,
      )
    : null;

  return (
    <>
      <button
        {...rest}
        ref={triggerRef}
        type="button"
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={open && active >= 0 ? optionId(active) : undefined}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-required={required || undefined}
        aria-disabled={ariaDisabled}
        // 숨은 input 이 잡은 것과 Field 가 오류로 얹은 것 둘 다 트리거에 보인다
        aria-invalid={invalid || ariaInvalid === true || ariaInvalid === "true" || undefined}
        data-testid={testId}
        data-state={open ? "open" : "closed"}
        value={current}
        disabled={disabled}
        autoFocus={autoFocus}
        onClick={() => (open ? closeMenu() : openMenu())}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onBlur={(e) => {
          if (open && !panelRef.current?.contains(e.relatedTarget as Node | null)) closeMenu();
        }}
        className={cn(
          fieldClass,
          "h-[34px] cursor-pointer items-center justify-between gap-2 text-left",
          "aria-invalid:border-err-line aria-invalid:ring-2 aria-invalid:ring-err/30",
          "aria-disabled:cursor-not-allowed aria-disabled:opacity-50",
          className,
        )}
      >
        {renderTrigger ? (
          renderTrigger(selected)
        ) : (
          <>
            <span className={cn("min-w-0 flex-1 truncate", (!selected || selected.value === "") && "text-muted")}>
              {selected ? selected.label : placeholder}
            </span>
            <ChevronIcon className={cn("size-4 shrink-0 text-label transition-transform duration-150", open && "rotate-180")} />
          </>
        )}
      </button>
      {required ? (
        <input
          tabIndex={-1}
          aria-hidden="true"
          required
          name={name}
          value={current}
          onChange={() => {}}
          onInvalid={(e) => {
            e.preventDefault();
            setInvalid(true);
            triggerRef.current?.focus();
          }}
          className="pointer-events-none absolute m-0 h-0 w-0 border-0 p-0 opacity-0"
        />
      ) : null}
      {panel}
    </>
  );
}
