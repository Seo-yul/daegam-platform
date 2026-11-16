/**
 * 상세 화면의 탭 막대 (D48). 고른 탭을 URL 에 두는 것은 `lib/tabs` 의 `useTabParam` 이 맡는다.
 *
 * MCP Server·Gateway·Agent 상세가 같은 마크업을 각자 들고 있었고 허브까지 넷이 되어 한 곳으로 모았다.
 * testid(`tab-{이름}`)와 `role`·`aria-selected` 는 그때 것 그대로다.
 *
 * 꺼진 탭의 이유는 `title` 이 아니라 막대 옆 문장이다. disabled 버튼은 포커스를 받지 못해 title 툴팁이
 * 키보드·터치 사용자에게 끝내 닿지 않는다.
 */
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 고른 탭과 안 고른 탭의 밑줄·글자색 (P81 4절). 고른 탭은 먹 밑줄과 먹 글자다. 쪽빛은 링크의 색이라 탭에는
 * 쓰지 않는다. 모양이 다른 탭(편집기의 두 줄짜리 문서 탭)은 이 색만 빌린다.
 */
export const TAB_TONE = {
  active: "border-heading text-heading",
  idle: "border-transparent text-muted hover:text-heading",
} as const;

/** 탭 크기. md 는 화면의 탭 막대이고 sm 은 캔버스 검사기처럼 좁은 패널 안의 탭이다. */
const TAB_SIZE = {
  md: "h-[38px] gap-1.5 px-3 text-[13.5px]",
  sm: "gap-1 px-2 py-1.5 text-xs",
} as const;

/** 밑줄 탭 한 칸의 모양. 탭 막대 밖에서 탭을 그리는 곳(경로로 고르는 감사 탭, 검사기 탭)도 이 모양을 쓴다. */
export function tabClass(active: boolean, size: keyof typeof TAB_SIZE = "md"): string {
  return cn(
    "-mb-px inline-flex items-center rounded-t-item border-b-2 font-semibold focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none disabled:cursor-not-allowed disabled:text-dim",
    TAB_SIZE[size],
    active ? TAB_TONE.active : TAB_TONE.idle,
  );
}

export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  label,
  disabled,
  reason,
}: {
  tabs: readonly T[];
  value: T;
  onChange: (t: T) => void;
  /** 탭에 적을 것. 라벨 옆에 배지를 다는 화면이 있어 문자열이 아니라 노드다. */
  label: (t: T) => ReactNode;
  disabled?: (t: T) => boolean;
  /** 꺼진 탭이 하나라도 있을 때 막대 옆에 서는 한 줄. 없으면 그리지 않는다. */
  reason?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line">
      <div className="flex flex-wrap gap-0.5" role="tablist">
        {tabs.map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={value === t}
            data-testid={`tab-${t}`}
            disabled={disabled?.(t) ?? false}
            className={tabClass(value === t)}
            onClick={() => onChange(t)}
          >
            {label(t)}
          </button>
        ))}
      </div>
      {reason ? (
        <p className="py-1.5 text-xs text-muted" data-testid="tabs-disabled-reason">
          {reason}
        </p>
      ) : null}
    </div>
  );
}

/** 탭 이름 옆의 개수 방울. 손이 필요한 것(승인 대기)을 세므로 warn 색이다. */
export function TabCount({ children }: { children: ReactNode }) {
  return (
    <span className="inline-grid h-[18px] min-w-[18px] place-items-center rounded-full bg-warn-soft px-1.5 text-[11px] font-semibold text-warn-fg tabular-nums">
      {children}
    </span>
  );
}
