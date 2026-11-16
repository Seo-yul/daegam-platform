import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from "react";

/**
 * 떠 있는 패널(Select 의 목록, 메뉴)의 자리 잡기 (D43 다섯째).
 *
 * 패널은 body 에 portal 로 그려 카드의 overflow 에 잘리지 않는다. 그 대신 앵커와 자리가
 * 끊어지므로 앵커가 움직이면 다시 재야 한다. 앱의 스크롤러는 `main` 이라 스크롤은 문서 캡처로 받는다.
 * 다시 재는 것은 `requestAnimationFrame` 안에서 패널 DOM 의 style 에 직접 쓴다. 프레임마다 setState 를
 * 부르면 렌더가 프레임마다 돌기 때문이다.
 *
 * 닫는 것은 바깥 pointerdown 뿐이다. 스크롤과 리사이즈는 닫지 않는다. 예전에는 둘 다 닫았는데, 관성 1px
 * 틱이나 포커스가 부른 스크롤에도 메뉴가 사라졌다. 패널에서 연 모달이 위에 떠 있는 동안의 누름은 그 모달의 것이라
 * 바깥으로 치지 않는다(`hasModalAbove`). Escape 는 쓰는 쪽이 자기 키 핸들러에서 받는다.
 * Select 는 대화상자 안에서 Escape 를 멈춰야 해서(목록만 닫고 대화상자는 남겨야 한다) 문서 수준
 * 리스너로는 그 구분을 할 수 없다.
 */

/** `usePopover` 로 자리를 잡는 떠 있는 패널의 모양이다. 폭과 안쪽 여백은 쓰는 쪽이 `cn` 으로 더한다. */
export const POPOVER_PANEL_CLASS =
  "popover-enter fixed z-(--ui-z-popover) box-border rounded-tile border border-line-strong bg-surface-popover shadow-popover outline-none";

const GAP = 4; // 앵커와 패널 사이
const VIEWPORT_MARGIN = 8; // 뷰포트 가장자리에 남기는 여백
const MIN_PANEL_HEIGHT = 72; // 이보다 좁게는 자르지 않는다. 항목 둘은 보여야 고를 수 있다

export type PopoverSide = "bottom" | "top";

export interface PopoverOptions {
  open: boolean;
  /** 자리의 기준이 되는 앵커. */
  triggerRef: RefObject<HTMLElement | null>;
  panelRef: RefObject<HTMLElement | null>;
  /** 바깥 pointerdown 일 때 부른다. */
  onClose: () => void;
  /**
   * 패널을 앵커 폭 이상으로 연다. 항목이 더 길면 뷰포트 안에서 글자 폭만큼 넓어진다. 앵커 폭에 딱 맞추면
   * 좁은 칸의 셀렉트(표 안의 provider 종류 등)에서 항목이 "…" 로 잘렸다.
   */
  matchWidth?: boolean;
  /** 가로 맞춤. start 는 앵커 왼쪽, end 는 앵커 오른쪽이다. 기본은 start 다. */
  align?: "start" | "end";
  /** 패널 높이 상한. 주면 남은 공간에 맞춰 max-height 도 함께 건다. */
  maxHeight?: number;
  /** 내용이 바뀌어 크기가 달라졌을 때 다시 재게 하는 값. */
  revision?: number;
}

/** 패널에 쓰는 뷰포트 좌표. 숫자는 React 가 px 로 붙인다. */
interface PanelBox {
  top?: number;
  bottom?: number;
  left?: number;
  right?: number;
  minWidth?: number;
  maxWidth?: number;
  maxHeight?: number;
}

export interface PopoverPlacement {
  side: PopoverSide;
  /** 패널의 style prop 에 그대로 준다. */
  style: CSSProperties;
}

/**
 * 아직 재지 못한 첫 커밋. 자리가 틀린 패널이 번쩍이지 않게 투명하게 두고 누를 수도 없게 한다. `visibility: hidden` 으로
 * 감추면 안 된다. 쓰는 쪽은 열린 커밋의 레이아웃 효과에서 패널이나 첫 항목에 포커스를 주는데, 실제 자리는 다음 커밋에 들어가므로
 * 그때 패널은 아직 이 모양이고 브라우저는 visibility 로 숨은 요소에 포커스를 주지 않는다. 투명한 요소는 포커스를 받는다.
 */
const UNMEASURED: PopoverPlacement = { side: "bottom", style: { top: 0, left: 0, opacity: 0, pointerEvents: "none" } };

function place(trigger: HTMLElement, panel: HTMLElement, opts: PopoverOptions): { side: PopoverSide; box: PanelBox } {
  const rect = trigger.getBoundingClientRect();
  // max-height 가 이미 걸려 있으면 rect 는 잘린 높이라 어느 쪽이 넓은지 잘못 고른다. scrollHeight 로 원래 크기를 본다
  const natural = Math.max(panel.scrollHeight, panel.getBoundingClientRect().height);
  const height = opts.maxHeight == null ? natural : Math.min(natural, opts.maxHeight);
  const below = window.innerHeight - rect.bottom - GAP - VIEWPORT_MARGIN;
  const above = rect.top - GAP - VIEWPORT_MARGIN;
  const side: PopoverSide = height <= below || below >= above ? "bottom" : "top";
  const box: PanelBox = side === "bottom" ? { top: rect.bottom + GAP } : { bottom: window.innerHeight - rect.top + GAP };
  if (opts.align === "end") box.right = Math.max(VIEWPORT_MARGIN, window.innerWidth - rect.right);
  else box.left = rect.left;
  if (opts.matchWidth) {
    box.minWidth = rect.width;
    box.maxWidth = Math.max(rect.width, window.innerWidth - rect.left - VIEWPORT_MARGIN);
  }
  if (opts.maxHeight != null) box.maxHeight = Math.max(MIN_PANEL_HEIGHT, Math.min(opts.maxHeight, side === "bottom" ? below : above));
  return { side, box };
}

/** 렌더를 거치지 않고 패널에 바로 쓴다. 안 쓰는 속성은 비워 위아래가 뒤집혀도 옛 값이 남지 않는다. */
function write(panel: HTMLElement, side: PopoverSide, box: PanelBox): void {
  const px = (v: number | undefined) => (v == null ? "" : `${v}px`);
  panel.style.top = px(box.top);
  panel.style.bottom = px(box.bottom);
  panel.style.left = px(box.left);
  panel.style.right = px(box.right);
  panel.style.minWidth = px(box.minWidth);
  panel.style.maxWidth = px(box.maxWidth);
  panel.style.maxHeight = px(box.maxHeight);
  panel.style.opacity = "";
  panel.style.pointerEvents = "";
  panel.dataset.side = side;
}

/**
 * 앵커 위에 뜬 모달이 있는가. 앵커를 품지 않은 `aria-modal` 대화상자다. 계정 메뉴의 API 문서처럼 패널 안의 단추가 연
 * 대화상자는 body 에 portal 로 그려져 패널 밖에 있다. 그 대화상자나 배경을 누른 것을 바깥 누름으로 치면 패널이 닫히고,
 * 대화상자가 닫히며 포커스를 돌려줄 단추가 감춰져 포커스가 body 로 떨어진다. 대화상자 안에 앵커가 있는 셀렉트는 그
 * 대화상자를 품은 쪽이라 해당하지 않는다.
 */
export function hasModalAbove(trigger: HTMLElement | null): boolean {
  return Array.from(document.querySelectorAll('[aria-modal="true"]')).some((dialog) => !trigger || !dialog.contains(trigger));
}

export function usePopover(options: PopoverOptions): PopoverPlacement {
  const { open, revision } = options;
  const [placement, setPlacement] = useState<PopoverPlacement | null>(null);
  // 리스너를 다시 달지 않으면서 최신 옵션을 보게 한다
  const latest = useRef(options);
  latest.current = options;

  // 열릴 때 한 번. 레이아웃 효과라 첫 페인트 전에 자리가 잡힌다
  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    const { triggerRef, panelRef } = latest.current;
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    const { side, box } = place(trigger, panel, latest.current);
    setPlacement({ side, style: box });
  }, [open, revision]);

  // 스크롤·리사이즈에는 닫지 않고 다시 잰다
  useEffect(() => {
    if (!open) return;
    let frame = 0;
    const reposition = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const { triggerRef, panelRef } = latest.current;
        const trigger = triggerRef.current;
        const panel = panelRef.current;
        if (!trigger || !panel) return;
        const { side, box } = place(trigger, panel, latest.current);
        write(panel, side, box);
      });
    };
    document.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      document.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  // 바깥 pointerdown 만 닫는다
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      const { triggerRef, panelRef, onClose } = latest.current;
      const target = e.target;
      const inside = target instanceof Node && (Boolean(triggerRef.current?.contains(target)) || Boolean(panelRef.current?.contains(target)));
      if (!inside && !hasModalAbove(triggerRef.current)) onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  return placement ?? UNMEASURED;
}
