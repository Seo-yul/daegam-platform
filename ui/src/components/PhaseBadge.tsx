/**
 * CR phase 를 색으로 보이는 점과 배지. 목록의 행, 상세의 준비 카드, 홈 챗이 같이 쓴다.
 *
 * 색은 lib/deployment 의 `PHASE_TONE` 이 정한다. 카드의 상태와 요약 줄도 같은 표를 보므로 같은 phase 가
 * 자리마다 다른 색으로 보이지 않는다.
 */
import { Badge, StatusDot, type BadgeProps } from "@/components/ui/badge";
import { useT } from "@/i18n";
import { PHASE_TONE } from "@/lib/deployment";

/** 서버 스키마의 phase 는 string 이다. 알려진 값만 색을 입히고 나머지는 muted 다. */
export function phaseVariant(phase: string | null | undefined): BadgeProps["variant"] {
  return PHASE_TONE[phase ?? ""] ?? "muted";
}

/**
 * CR phase 를 색 점 하나로. 목록의 행이 쓴다.
 *
 * 목록 위의 요약 줄이 이미 같은 색으로 Ready·Progressing·Failed 를 세고 있어서, 행마다 낱말을 또
 * 적으면 같은 말이 두 번 선다. 색만 두고 뜻은 `title` 과 낭독기용 글자가 말한다.
 */
export function PhaseDot({ phase, testId }: { phase: string | null | undefined; testId?: string }) {
  const t = useT();
  const text = t.detail.phaseBadge;
  // 목록 위 요약 줄(CountLine)과 같은 점을 쓴다. 색을 여기서 따로 적으면 같은 뜻이 두 색으로 보인다
  const variant = { ok: "ok", warn: "warn", error: "err", info: "info" }[phaseVariant(phase) as string];
  const label = phase ?? text.unknown;
  return (
    <span className="inline-flex items-center" data-testid={testId} data-phase={phase ?? "unknown"} title={label}>
      {variant ? (
        <StatusDot variant={variant as "ok" | "warn" | "err" | "info"} className="h-[7px] w-[7px]" />
      ) : (
        // muted 는 색이 없는 점이다. CountLine 의 "미배포" 와 같은 모양이다
        <span aria-hidden className="inline-block h-[7px] w-[7px] rounded-full bg-label" />
      )}
      <span className="sr-only">{text.dot(label)}</span>
    </span>
  );
}

/**
 * 인스턴스 상태. `live` 를 주면 바뀔 때 낭독기가 읽는다.
 *
 * 배포는 몇 분이 걸리고 화면은 폴링으로 조용히 바뀐다. 눈으로 보는 사람은 배지 색이 변하는 것을
 * 알아채지만 그렇지 않은 사람에게는 아무 일도 일어나지 않은 것과 같다. 목록의 배지 수십 개가
 * 저마다 떠들면 시끄러우므로 상세 화면처럼 하나만 있는 자리에서만 켠다.
 */
export function PhaseBadge({ phase, testId, live = false }: { phase: string | null | undefined; testId?: string; live?: boolean }) {
  const t = useT();
  const text = t.detail.phaseBadge;
  const badge = (
    <Badge variant={phaseVariant(phase)} data-testid={testId}>
      {phase ?? text.unknown}
    </Badge>
  );
  if (!live) return badge;
  return (
    <span role="status" aria-live="polite" aria-atomic="true">
      {badge}
      <span className="sr-only">{text.live(phase ?? text.unknown)}</span>
    </span>
  );
}
