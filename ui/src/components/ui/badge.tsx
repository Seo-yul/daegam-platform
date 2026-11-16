import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  // 배지 (P81 4절): 22px, radius-badge. 상태 색은 옅은 면에 글자만 물들이고 테두리는 두지 않는다
  "inline-flex h-[22px] items-center gap-1.5 rounded-badge border px-2 text-xs font-semibold whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "border-line-strong bg-surface text-fg",
        ok: "border-transparent bg-ok-soft text-ok-fg",
        warn: "border-transparent bg-warn-soft text-warn-fg",
        error: "border-transparent bg-err-soft text-err-fg",
        info: "border-transparent bg-info-soft text-info-fg",
        muted: "border-line bg-sunken text-muted",
        accent: "border-transparent bg-accent-soft text-link",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/** 상태 점. 카운트 줄·상태 칸에 쓴다. 진행 중(warn)만 옅은 고리를 두른다 (index.css `status-dot-warn`). */
export function StatusDot({ variant, className }: { variant: "ok" | "warn" | "err" | "info"; className?: string }) {
  return <span aria-hidden className={cn("inline-block h-2 w-2 rounded-full", `status-dot-${variant}`, className)} />;
}

/** verdict level / check status / bool 을 배지 색으로. */
export function levelVariant(level: string | boolean | null | undefined): BadgeProps["variant"] {
  if (level === true) return "ok";
  if (level === false) return "error";
  switch (level) {
    case "ok":
    case "pass":
      return "ok";
    case "warn":
      return "warn";
    case "error":
    case "fail":
      return "error";
    case "info":
      return "info";
    case "skip":
      return "muted";
    default:
      return "default";
  }
}
