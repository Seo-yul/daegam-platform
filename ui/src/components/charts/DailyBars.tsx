/**
 * 날마다(또는 시간마다) 쌓인 막대 (P37 13절, P51 5절). 의존 없는 SVG 다. 막대 하나가 한 단계이고 칸은 부르는 쪽이 정한다.
 * 칸 색은 테마 토큰이라 다크·라이트를 따른다. 첫 칸은 쪽빛이고 그다음부터 캔버스의 갈래 색을 빌린다. 막대 하나뿐인
 * 화면이 대부분이라 첫 색이 화면의 강조색과 같아야 차분하다 (P81 6절). 각 칸의 `<title>` 이 이름과 값을 말한다.
 *
 * 칸이 몇 개 안 되면 막대 하나가 그림 전체로 퍼져 판처럼 보이므로, 그림의 너비는 적어도 `MIN_SLOTS` 칸으로 잡는다.
 * 그때 남는 칸이 값이 0 인 날로 읽히지 않도록, 실제 단계가 있는 너비에만 옅은 바닥선을 긋는다.
 *
 * 색은 칸이 처음 나온 차례로 돌린다. `colors` 를 주면 열쇠마다 그 색이라, 기간을 바꿔도 같은 칸이 같은 색이다(MCP 메트릭의
 * 결과 갈래). 막대의 React 열쇠는 이름표가 아니라 `key` 다. 서머타임이 끝나는 날에는 시간 이름표가 두 번 나온다.
 */
import { useT } from "@/i18n";

/** 막대 하나의 칸. key 가 null 이면 나머지다. */
export interface BarPart {
  key: string | null;
  label: string;
  value: number;
}

export interface BarDay {
  /** 단계마다 다른 값. 단계의 시작 시각처럼 이름표가 겹쳐도 겹치지 않는 것이다. */
  key: string;
  label: string;
  parts: BarPart[];
}

const COLORS = ["var(--ui-accent)", ...["memory", "guardrail", "model", "knowledge", "entry"].map((k) => `var(--ui-kind-${k})`)];
const OTHER = "var(--ui-dim)";
const HEIGHT = 140;
/** 하루 막대의 칸 너비와 막대 사이 틈(viewBox 단위). */
const BAR_WIDTH = 24;
const BAR_GAP = 4;
/** 그림이 적어도 차지하는 칸 수. 이보다 적은 막대는 왼쪽부터 제 너비로 선다. */
const MIN_SLOTS = 14;

export function DailyBars({
  days,
  format,
  label,
  empty,
  colors,
  testId = "daily-bars",
}: {
  days: BarDay[];
  format: (v: number) => string;
  /** 그림 전체의 접근 이름. */
  label: string;
  /** 그릴 값이 없을 때의 문장. */
  empty: string;
  /** 칸 열쇠마다 고정한 색. 표에 없는 열쇠는 나머지 색이다. */
  colors?: Readonly<Record<string, string>>;
  testId?: string;
}) {
  const t = useT();
  const max = Math.max(0, ...days.map((d) => d.parts.reduce((s, p) => s + p.value, 0)));
  const width = Math.max(MIN_SLOTS, days.length) * BAR_WIDTH;
  const keys = [...new Set(days.flatMap((d) => d.parts.map((p) => p.key)).filter((k): k is string => k !== null))];
  const color = (key: string | null) => (key === null ? OTHER : colors ? (colors[key] ?? OTHER) : COLORS[keys.indexOf(key) % COLORS.length]);
  if (max <= 0) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${width} ${HEIGHT}`} className="h-36 w-full" preserveAspectRatio="none" data-testid={testId}>
      {/* 바닥선. 그림이 가로로 늘어도 굵기가 1px 로 남게 non-scaling-stroke 다 */}
      <line x1={0} y1={HEIGHT - 0.5} x2={days.length * BAR_WIDTH} y2={HEIGHT - 0.5} stroke="var(--ui-line-strong)" vectorEffect="non-scaling-stroke" data-testid={`${testId}-baseline`} />
      {days.map((day, i) => {
        let y = HEIGHT;
        return (
          <g key={day.key} data-testid={`${testId}-day`}>
            {day.parts.map((part) => {
              const h = (part.value / max) * HEIGHT;
              y -= h;
              return (
                <rect key={part.key ?? "other"} x={i * BAR_WIDTH + BAR_GAP / 2} y={y} width={BAR_WIDTH - BAR_GAP} height={h} fill={color(part.key)} data-part={part.key ?? "other"}>
                  <title>{t.shared.dailyBars.part(day.label, part.label, format(part.value))}</title>
                </rect>
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}
