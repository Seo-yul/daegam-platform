/**
 * 아이콘만 있는 작은 버튼. 제목 줄처럼 낱말을 늘어놓을 자리가 없는 곳이 쓴다.
 *
 * 아이콘만으로는 뜻이 서지 않으므로 `aria-label` 과 `title` 이 글자로 말한다. 화면 낭독기는
 * 앞의 것을, 마우스를 올린 사람은 뒤의 것을 읽는다.
 *
 * `danger` 는 되돌릴 수 없는 것(삭제)에만 쓴다. 평소에는 다른 아이콘과 같은 회색이고 손을 올렸을
 * 때만 붉어진다. 늘 붉으면 줄 전체가 경고처럼 보여 정작 위험한 것이 묻힌다.
 *
 * 상태 색은 `CopyButton` 과 같다. 크기는 다르다. 이 버튼은 낱말 버튼 옆에 서므로 32px 이고, 복사 단추는 주소칸과
 * 표 칸 안에 들어가므로 28px 이다.
 *
 * `bordered` 는 상세 머리 줄처럼 낱말 버튼 옆에 서는 자리다. 테두리가 있어야 옆 버튼과 같은 무게로 읽힌다 (P81 6절).
 */
import type { ButtonHTMLAttributes, MouseEvent, ReactNode, Ref } from "react";

import { cn } from "@/lib/utils";

export function IconButton({
  ref,
  label,
  title,
  testId,
  disabled,
  danger = false,
  bordered = false,
  pressed,
  onClick,
  children,
  className,
  ...rest
}: {
  /** 닫힌 입력칸이 포커스를 돌려줄 때처럼 버튼 요소가 필요한 자리가 준다. */
  ref?: Ref<HTMLButtonElement>;
  /** 무엇을 하는 버튼인가. `aria-label` 과 `title` 이 된다. */
  label: string;
  /** 툴팁을 `label` 과 다르게 둘 때. 메뉴 트리거는 낭독기에 무엇의 메뉴인지, 툴팁에는 "더보기"를 보인다. */
  title?: string;
  testId?: string;
  disabled?: boolean;
  danger?: boolean;
  /** 테두리와 머리카락 그림자를 두른다. 머리 줄에서 낱말 버튼 옆에 설 때다. */
  bordered?: boolean;
  /** 켜고 끄는 단추(답변 피드백의 엄지)의 상태다. 주면 `aria-pressed` 가 붙고 켜졌으면 강조색이다. */
  pressed?: boolean;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  children: ReactNode;
  className?: string;
  /** 나머지 단추 속성(메뉴 트리거의 aria-haspopup·aria-expanded·onKeyDown 등)은 그대로 편다. */
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "className" | "title" | "onClick" | "type" | "disabled">) {
  return (
    <button
      ref={ref}
      type="button"
      {...rest}
      data-testid={testId}
      aria-label={label}
      title={title ?? label}
      disabled={disabled}
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "inline-flex size-8 shrink-0 items-center justify-center rounded-chip text-label transition-colors",
        "hover:bg-hover focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none",
        "disabled:pointer-events-none disabled:opacity-40",
        danger ? "hover:bg-err-soft hover:text-err-fg" : "hover:text-heading",
        bordered && "border border-control-line bg-surface shadow-hairline",
        pressed && "text-accent hover:text-accent",
        className,
      )}
    >
      {children}
    </button>
  );
}
