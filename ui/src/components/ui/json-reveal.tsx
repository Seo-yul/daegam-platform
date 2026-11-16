/**
 * 접어 둔 JSON 하나 (P37 12절). 이벤트 탭의 본문과 트레이스 그림의 속성처럼 남이 만든 값을 펴 보는 자리가 같이 쓴다. 펼침은
 * `<details>` 가 맡아 키보드와 낭독기 몫을 브라우저가 한다. 값은 글로만 그린다(React 가 이스케이프한다).
 */
import { CodePre } from "@/components/ui/code-snippet";
import { cn } from "@/lib/utils";

/** 키보드 초점의 표준 고리. */
export const FOCUS_RING = "focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none";

export function JsonReveal({ summary, value, testId, className }: { summary: string; value: unknown; testId?: string; className?: string }) {
  return (
    <details className={cn("text-xs", className)} data-testid={testId}>
      <summary className={cn("w-fit cursor-pointer rounded-item text-accent", FOCUS_RING)}>{summary}</summary>
      <CodePre className="mt-1 max-w-md text-xs">{JSON.stringify(value, null, 2)}</CodePre>
    </details>
  );
}
