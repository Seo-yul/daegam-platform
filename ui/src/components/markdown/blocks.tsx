/**
 * 마크다운의 문단과 코드 블록 (D61). 지연 청크가 오기 전의 대체 화면과 마크다운 본문이 같은 부품을 써서 청크가 와도 글과
 * 블록의 모양이 바뀌지 않는다.
 */
import type { HTMLAttributes } from "react";

import { CodeSnippet } from "@/components/ui/code-snippet";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

/** 문단. 모델은 줄 하나만 바꿔 줄을 나누는 일이 많아 줄바꿈을 그대로 보이고, 긴 URL 처럼 띄어 쓸 곳이 없는 글도 줄을 바꾼다. */
export function Paragraph({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("whitespace-pre-wrap [overflow-wrap:anywhere]", className)} {...props} />;
}

/** 코드 블록. 꺼진 면의 고정폭 블록 위에 언어 이름과 복사 단추를 단다. 모양은 `CodeSnippet` 의 sunken 톤이다. */
export function CodeBlock({ text, lang, testId }: { text: string; lang?: string; testId?: string }) {
  const t = useT();
  return <CodeSnippet tone="sunken" title={lang} lang={lang} code={text} copyLabel={t.detail.markdown.copyCode} testId={testId} copyTestId={testId ? `${testId}-copy` : undefined} />;
}
