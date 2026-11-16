import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 상세 화면의 숫자 칸 하나. 이름표 아래 큰 값이고 note 가 그 아래에 붙는다.
 *
 * 카드 안에 다시 서는 칸이라 한 겹 떠 있는 면(surface-strong)에 선만 두르고, 이름표는 보조 글자 그대로 쓴다.
 * 한글 이름표에 자간과 대문자를 주면 낱자가 흩어져 읽힌다 (P81 6절)
 */
export function Tile({ label, value, tone, note, testId }: { label: string; value: string | number; tone?: "warn"; note?: ReactNode; testId: string }) {
  return (
    <div className={cn("flex flex-col gap-1.5 rounded-tile border border-line bg-surface-strong px-3.5 py-3", tone === "warn" && "border-warn-line")}>
      <span className="text-[12.5px] text-muted">{label}</span>
      <span className={cn("text-[22px] leading-none font-bold tracking-[-0.02em] text-heading tabular-nums", tone === "warn" && "text-warn-fg")} data-testid={testId}>
        {value}
      </span>
      {note}
    </div>
  );
}
