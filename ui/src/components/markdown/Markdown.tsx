/**
 * 마크다운 글 (D61). 채팅 말풍선이 쓰고 채팅 밖의 미리보기도 같은 부품을 쓴다.
 *
 * 첫 화면이 채팅이라 파서(react-markdown·remark-gfm)는 지연 청크(`MarkdownBody`)에 둔다. 청크가 오기 전에는 줄바꿈과 ```
 * 코드 펜스만 다루는 대체 화면을 보인다. 스트리밍 중에는 토큰마다 다시 파싱하지 않고 100ms 에 한 번만 새 글을 넘긴다.
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import type { CitationMarkdown } from "@/lib/citations";
import { splitCodeFences } from "@/lib/codeFences";
import { cn } from "@/lib/utils";

import { CodeBlock, Paragraph } from "./blocks";

const MarkdownBody = lazy(() => import("./MarkdownBody"));

/** 스트리밍 중 다시 파싱하는 가장 짧은 간격. */
export const STREAM_PARSE_INTERVAL_MS = 100;

export interface MarkdownProps {
  text: string;
  /** 글이 아직 스트리밍 중이다. 다시 파싱을 묶는다. */
  streaming?: boolean;
  /** 코드 블록은 `{testId}-code`, 그 복사 단추는 `{testId}-code-copy` 다. */
  testId?: string;
  /** 블록을 감싸는 칸에 덧붙인다. 말풍선이 스트리밍 커서를 마지막 블록 뒤에 붙이는 데 쓴다. */
  className?: string;
  /** 답의 `[n]` 을 번호 칩으로 바꾼다(P36 7절). 다시 파싱을 피하려면 부르는 쪽이 같은 값을 유지한다. */
  citations?: CitationMarkdown;
}

export function Markdown({ text, streaming = false, testId, className, citations }: MarkdownProps) {
  const shown = useStreamThrottle(text, streaming);
  const codeTestId = testId ? `${testId}-code` : undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-2", className)}>
      <Suspense fallback={<MarkdownFallback text={shown} codeTestId={codeTestId} />}>
        <MarkdownBody text={shown} codeTestId={codeTestId} citations={citations} />
      </Suspense>
    </div>
  );
}

/** 지연 청크가 오기 전의 대체 화면. 줄바꿈과 ``` 코드 펜스만 다루고 닫히지 않은 코드 펜스는 끝까지 코드다. */
export function MarkdownFallback({ text, codeTestId }: { text: string; codeTestId?: string }) {
  return (
    <>
      {splitCodeFences(text).map((seg, i) =>
        seg.kind === "code" ? <CodeBlock key={i} text={seg.text} lang={seg.lang} testId={codeTestId} /> : <Paragraph key={i}>{seg.text}</Paragraph>,
      )}
    </>
  );
}

/**
 * 스트리밍 중이면 글을 `STREAM_PARSE_INTERVAL_MS` 에 한 번만 바꿔 돌려준다. 그 사이에 온 토큰은 다음 차례에 한꺼번에 실린다.
 * 스트리밍이 끝난 글은 기다리지 않고 바로 돌려준다.
 */
function useStreamThrottle(text: string, streaming: boolean): string {
  const [shown, setShown] = useState(text);
  const lastAt = useRef(0);
  useEffect(() => {
    if (!streaming) return;
    const publish = () => {
      lastAt.current = Date.now();
      setShown(text);
    };
    const wait = lastAt.current + STREAM_PARSE_INTERVAL_MS - Date.now();
    if (wait <= 0) {
      publish();
      return;
    }
    const timer = window.setTimeout(publish, wait);
    return () => window.clearTimeout(timer);
  }, [text, streaming]);
  return streaming ? shown : text;
}
