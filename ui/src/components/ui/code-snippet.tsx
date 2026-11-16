/**
 * 복사할 수 있는 코드 칸. 머리에 이름과 복사 버튼이 있고 아래에 코드가 그대로 보인다.
 *
 * Agent 외부 호출 카드의 curl 예시와 연결 카드의 설정 조각이 같이 쓴다. 코드는 줄을 접지 않고 가로로
 * 스크롤한다. 명령과 설정 파일은 줄바꿈이 뜻을 가지므로 화면 폭에 맞춰 접으면 그대로 붙여 넣을 수 없다.
 *
 * 모양은 두 가지다. `surface` 는 카드 위의 밝은 칸이고, `sunken` 은 대화 전사의 코드(답의 코드 블록, 도구 인자·결과)다.
 * 흰 본문에서 한 단 꺼진 면이라 답의 흐름을 끊지 않는다 (P81 6절). 꺼진 칸의 머리에는 이름 대신 언어 이름이 온다.
 */
import type { HTMLAttributes, ReactNode } from "react";

import { CopyButton } from "@/components/ui/copy-button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export type CodeTone = "surface" | "sunken";

const TONE: Record<CodeTone, { gap: string; title: string; pre: string }> = {
  surface: {
    gap: "gap-1",
    title: "text-xs font-semibold text-heading",
    pre: "overflow-x-auto rounded-chip border border-line bg-surface-strong px-3 py-2 font-mono text-xs leading-5",
  },
  sunken: {
    gap: "gap-1",
    title: "font-mono text-[11px] text-muted",
    pre: "overflow-auto rounded-tile border border-line bg-sunken px-3 py-2 font-mono text-[12px] leading-5 text-heading",
  },
};

/** 코드 글을 담는 pre 하나. 머리와 복사 단추가 필요 없는 자리(도구 인자·결과)가 바로 쓴다. */
export function CodePre({ tone = "surface", className, ...props }: HTMLAttributes<HTMLPreElement> & { tone?: CodeTone }) {
  return <pre className={cn(TONE[tone].pre, className)} {...props} />;
}

export function CodeSnippet({
  title,
  code,
  copyLabel,
  note,
  tone = "surface",
  lang,
  testId,
  copyTestId,
}: {
  /** 머리 왼쪽의 이름. 없어도 머리는 남아 복사 버튼이 오른쪽에 선다. */
  title?: string;
  code: string;
  /** 복사 버튼이 무엇을 복사한다고 말할지. 없으면 제목이고, 제목도 없으면 "코드"(`ui.codeSnippet.copyLabel`)다. */
  copyLabel?: string;
  /** 코드 아래의 한 줄. 붙여 넣기 전에 알아야 할 것을 적는다. */
  note?: ReactNode;
  tone?: CodeTone;
  /** 코드의 언어 이름. pre 의 `data-lang` 에 싣는다. */
  lang?: string;
  testId?: string;
  copyTestId?: string;
}) {
  const t = useT();
  const style = TONE[tone];
  return (
    <div className={cn("flex min-w-0 flex-col", style.gap)}>
      <div className="flex items-center justify-between gap-2">
        <span className={style.title}>{title}</span>
        <CopyButton value={code} label={copyLabel ?? title ?? t.ui.codeSnippet.copyLabel} testId={copyTestId} />
      </div>
      <CodePre tone={tone} data-testid={testId} data-lang={lang}>
        {code}
      </CodePre>
      {note ? <p className="text-xs text-muted">{note}</p> : null}
    </div>
  );
}
