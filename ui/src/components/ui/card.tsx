import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/** 카드 (P81: 평평한 본문 면 + 1px 선, radius 12) */
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("surface rounded-card overflow-hidden", className)} {...props} />;
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex items-center justify-between gap-2 border-b border-line px-[18px] py-3", className)} {...props} />;
}

/** 카드 제목은 14px 먹 글자다. 머리 줄의 선이 제목과 내용을 가르므로 대문자·자간으로 겨루지 않는다 (P81 6절) */
export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-sm font-semibold text-heading", className)} {...props} />;
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-[18px] py-4", className)} {...props} />;
}

/** 왼쪽 띠의 갈래 색. 승인은 경고색, 질문은 안내색이다. */
const STATUS_BAR = { warn: "border-l-warn", info: "border-l-info" } as const;

/**
 * 대화 안에서 사람의 답을 기다리는 카드(승인, 질문)의 모양 (P81 6절). 흰 카드에 왼쪽 3px 띠 하나로 기다림을 알린다.
 * 면 전체를 물들이면 전사에서 카드가 소리친다. 답이 끝난 카드는 띠가 선 색으로 물러나고 흐려진다.
 */
export function statusCardClass(tone: keyof typeof STATUS_BAR, resolved: boolean): string {
  return cn(
    "flex flex-col gap-3 rounded-card border border-l-[3px] border-line-strong bg-surface p-4 shadow-hairline",
    resolved ? "border-l-line-strong opacity-70" : STATUS_BAR[tone],
  );
}

export function Alert({
  className,
  variant = "info",
  ...props
}: HTMLAttributes<HTMLDivElement> & { variant?: "info" | "error" | "ok" | "warn" }) {
  const color = {
    info: "bg-info-soft text-info-fg",
    error: "bg-err-soft text-err-fg",
    ok: "bg-ok-soft text-ok-fg",
    warn: "bg-warn-soft text-warn-fg",
  }[variant];
  return (
    <div
      role={variant === "error" ? "alert" : "status"}
      className={cn("rounded-tile px-3.5 py-2.5 text-sm", color, className)}
      {...props}
    />
  );
}
