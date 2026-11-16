/**
 * 파일 목록 한 줄의 단추. Skill 의 파일 편집기·파일 탭·리비전 비교와 채팅의 기억 서랍이 같은 모양을 쓴다.
 *
 * 고른 줄은 `aria-current` 와 강조색 하나로 알린다. 화면마다 강조색이 다르면 같은 "고른 파일" 이 다른 뜻으로 읽힌다. 단추는 줄을
 * 채우므로 삭제처럼 곁에 서는 단추는 부르는 쪽이 같은 `li` 안에 둔다.
 */
import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

export function FileListButton({
  active,
  onClick,
  testId,
  className,
  style,
  children,
}: {
  active: boolean;
  onClick: () => void;
  testId?: string;
  className?: string;
  /** 트리의 들여쓰기처럼 줄마다 다른 값이다. */
  style?: CSSProperties;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-current={active ? "true" : undefined}
      data-testid={testId}
      className={cn("flex w-full min-w-0 items-center gap-1.5 rounded-item px-2 py-1.5 text-left text-xs", active ? "bg-accent-soft font-semibold text-heading" : "text-fg hover:bg-hover", className)}
      style={style}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
