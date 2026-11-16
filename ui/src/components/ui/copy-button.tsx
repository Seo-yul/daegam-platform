/**
 * 주소·값 복사 버튼. 목록의 URL 칸과 상세의 주소 줄, 채팅 말풍선과 코드 블록이 같이 쓴다.
 *
 * 글자("복사")가 아니라 아이콘이다. 주소 옆에 낱말이 붙으면 좁은 칸에서 정작 주소가 더 잘리고,
 * 복사는 어느 화면에서나 같은 뜻이라 그림 하나로 충분하다. 대신 아이콘만 있는 버튼이므로 뜻은
 * `aria-label` 과 `title` 이 글자로 말한다.
 *
 * 눌렀는지 알 수 있게 잠깐 체크 표시로 바뀐다. 복사 자체는 `lib/clipboard` 가 맡는다. 평문 http 에서는
 * `navigator.clipboard` 가 없어 거기서 폴백을 쓴다. 그래도 안 되면 조용히 넘어가지 않고 그 자리에서
 * 못 했다고 말한다. 눌렀는데 아무 일도 안 일어나는 것이 가장 나쁘다.
 */
import { useEffect, useRef, useState } from "react";

import { CheckIcon } from "@/components/ui/icons";
import { useT } from "@/i18n";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

export function CopyButton({
  value,
  label,
  testId,
  className,
}: {
  value: string;
  /** 무엇을 복사하는지. 문장 가운데 들어가는 명사이고 `aria-label` 이 "{label} 복사" 가 된다. 기본은 "주소" 다. */
  label?: string;
  testId?: string;
  className?: string;
}) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  const copied = state === "ok";
  const failed = state === "fail";
  const t = useT();
  const name = label ?? t.common.copy.defaultLabel;
  // 되돌리는 타이머가 도는 중에 버튼이 사라지면(탭 전환, 목록 갱신) 없는 것에 setState 한다.
  // 연달아 누를 때도 앞 타이머가 살아 있어 방금 누른 표시를 일찍 지운다
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={copied ? t.common.copy.copied(name) : failed ? t.common.copy.failed(name) : t.common.copy.action(name)}
      title={copied ? t.common.copy.copiedTitle : failed ? t.common.copy.failedTitle(name) : t.common.copy.title}
      className={cn(
        "inline-flex size-7 shrink-0 items-center justify-center rounded-item text-label transition-colors",
        "hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none",
        copied && "text-ok-fg",
        failed && "text-err-fg",
        className,
      )}
      onClick={() => {
        void copyText(value).then((ok) => {
          setState(ok ? "ok" : "fail");
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setState("idle"), ok ? 1500 : 3000);
        });
      }}
    >
      {failed ? (
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
          <path d="M8 4v5M8 11.5v.5" />
        </svg>
      ) : copied ? (
        <CheckIcon size={14} />
      ) : (
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
          <path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 0 2.5 3v5a1.5 1.5 0 0 0 1.5 1.5h.5" />
        </svg>
      )}
    </button>
  );
}
