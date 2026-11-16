import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/**
 * 화면의 좌우 여백과 폭 상한 (D44 첫째). 목록·상세·폼이 자기 바깥 `div` 를 이것으로 바꿔 쓴다.
 *
 * 껍데기의 `main` 에 래퍼를 끼우지 않는 것은 높이 사슬 때문이다. `main` 이
 * `flex min-h-0 flex-1 flex-col overflow-y-auto` 로 남은 높이를 홈 챗에 넘기는데(D35) 그 사이에 층이 하나 끼면
 * 높이가 끊겨 입력칸이 화면 밖으로 밀린다. 그래서 폭이 필요한 화면만 스스로 감싼다.
 *
 * 좌우 여백은 껍데기의 `main` 이 주므로 여기는 폭만 쥔다. `full` 은 그 폭 상한을 끄는 것이라 캔버스와
 * Advanced 탭의 Monaco 처럼 넓을수록 잘 보이는 자리에 쓴다. 탭으로 갈리는 상세는 탭 값을 보고 이 값을 준다.
 */
export function PageWidth({ full = false, className, ...props }: HTMLAttributes<HTMLDivElement> & { full?: boolean }) {
  return <div className={cn(!full && "content-shell", className)} {...props} />;
}
