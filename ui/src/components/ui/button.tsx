import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/*
 * 채운 버튼(주·지우기)의 비활성. 먹 바탕을 반투명으로 두면 켜진 버튼처럼 보이므로 무채색 면과 입력칸 선으로 바꾼다
 */
const DISABLED_FILLED = "disabled:border disabled:border-field-line disabled:bg-surface-strong disabled:text-dim disabled:shadow-none disabled:opacity-70";

/*
 * 버튼 (P81 4절, 시안의 "부품"). 주 버튼은 먹으로 채우고 화면에 하나뿐이다(D44). 나머지는 테두리나 글자뿐이라
 * 화면에서 진한 면은 주 버튼 하나다. 크기는 sm 28 · default 34 · lg 40px 이고, 그림 하나만 든 네모 버튼은
 * icon 34 · icon-sm 28px 이다(대화 입력칸의 보내기).
 *
 * `buttonVariants` 를 cn 없이 바로 쓰는 링크·라벨이 있어 겹치는 클래스가 merge 되지 않는다. 그래서 바탕 줄에는
 * 변형·크기가 바꾸는 속성(테두리 색·글자 크기·간격·모서리·아이콘 크기)을 두지 않고 각자 하나씩만 갖는다.
 */
export const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap border font-semibold transition-colors disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:ring-offset-1 focus-visible:ring-offset-surface [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        /* 먹 바탕 + 흰 글자. 다크에서는 종이색 바탕 + 먹 글자로 뒤집힌다 */
        default: `border-transparent bg-primary text-on-primary shadow-hairline hover:bg-primary-hover ${DISABLED_FILLED}`,
        /* 머리 줄의 보조 동작. 흰 면에 테두리와 머리카락 그림자를 얹어 눌리는 것으로 읽힌다 */
        secondary: "border-control-line bg-surface text-heading shadow-hairline hover:border-field-line hover:bg-surface-strong disabled:opacity-60",
        /* 표 칸·카드 안처럼 그림자가 겹치는 자리의 테두리 버튼 */
        outline: "border-control-line bg-surface text-fg hover:bg-surface-strong hover:text-heading disabled:opacity-60",
        ghost: "border-transparent text-fg-soft hover:bg-hover hover:text-heading",
        /* 지우기 확인 창의 확정 버튼. 붉은 면 위의 글자는 on-primary 라 두 테마 모두 4.5:1 을 넘는다 */
        destructive: `border-transparent bg-err text-on-primary shadow-hairline hover:brightness-95 ${DISABLED_FILLED}`,
        /* 무언가를 멈추는 동작(배포 해제). 글자만 붉고 손을 올리면 옅은 붉은 면이 깔린다 */
        danger: "border-transparent text-err-fg hover:bg-err-soft",
      },
      size: {
        default: "h-[34px] gap-1.5 rounded-chip px-3.5 text-[13.5px] [&_svg]:size-4",
        sm: "h-7 gap-1 rounded-item px-2.5 text-[12.5px] [&_svg]:size-3.5",
        lg: "h-10 gap-1.5 rounded-chip px-[18px] text-[14.5px] [&_svg]:size-4",
        icon: "size-[34px] rounded-chip [&_svg]:size-4",
        "icon-sm": "size-7 rounded-item [&_svg]:size-3.5",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

/** ComponentProps 라 ref 도 받는다 (React 19 는 ref 가 일반 prop 이다). */
export interface ButtonProps extends ComponentProps<"button">, VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, type = "button", ...props }: ButtonProps) {
  return <button type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
