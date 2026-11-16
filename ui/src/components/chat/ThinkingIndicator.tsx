/**
 * "생각하는 중…" 표시 (P36 11절). 턴이 열려 있는데 흐르는 말풍선도 도는 도구 카드도 없을 때 전사 끝에 선다(items.ts 의
 * showsThinking). 모델의 추론 본문은 출력 가드레일을 거치지 않아 보이지 않으므로 이 한 줄이 그 자리를 대신한다.
 *
 * 기다림이 길어지면 흐른 초를 붙인다. 초는 이 줄이 선 때부터 세므로 도구 결과 뒤 모델이 다시 생각할 때는 0부터 다시 센다.
 * 이 줄은 섰다 사라지므로 알림 줄이 아니다. 낭독기에는 부르는 쪽이 늘 두는 알림 줄(components/ui/live-region.tsx)이
 * 이 문장(chat.thinking.label)만 한 번 알리고, 매초 바뀌는 숫자는 읽지 않는다.
 */
import { useEffect, useState } from "react";

import { useT } from "@/i18n";

/** 흐른 초를 붙이기 시작하는 때(초). 짧은 기다림에 숫자가 깜박이지 않게 한다. */
export const THINKING_SECONDS_FROM = 5;
const TICK_MS = 1_000;

export function ThinkingIndicator({ testId = "chat-thinking" }: { testId?: string }) {
  const t = useT();
  const text = t.chat.thinking;
  const [startedAt] = useState(() => Date.now());
  const [now, setNow] = useState(startedAt);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  const seconds = Math.floor((now - startedAt) / TICK_MS);
  return (
    <p className="text-xs text-muted" data-testid={testId}>
      <span className="motion-safe:animate-pulse">{text.label}</span>
      {seconds >= THINKING_SECONDS_FROM ? (
        <span aria-hidden="true" className="ml-1 tabular-nums" data-testid={`${testId}-seconds`}>
          {text.seconds(seconds)}
        </span>
      ) : null}
    </p>
  );
}
