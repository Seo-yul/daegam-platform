/**
 * 입력칸 아래 줄의 pill 둘 (D45 둘째). 왼쪽이 상대, 오른쪽이 모델이다.
 *
 * 둘 다 기존 `Select` 를 pill 모양으로 입힌 것이다. 다른 고르개로 바꾸지 않는 이유는 계약이다. 테스트
 * 헬퍼(`src/test/select.ts`, `e2e/select.ts`)가 `role="option"` 으로 항목을 찾고 홈 챗 테스트와
 * `e2e/agent.spec.ts` 가 `home-agent-select` 를 그 방식으로 조작한다.
 *
 * 모델은 상대에 따라 성질이 다르다. 청지기면 등록된 provider 중에서 고르고, Agent 면 그 정의가 정한
 * provider·model 을 읽기 전용으로 보인다. Agent 의 모델은 캔버스가 정하는 것이라 여기서 바꾸지 않는다.
 */
import { ChevronDown } from "lucide-react";

import type { AssistantProvider } from "@/api/types";
import { CounterpartBadge } from "@/components/icons/brand";
import { Select } from "@/components/ui/input";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * pill 공통 룩. 입력칸 카드 안에 테두리 없이 앉고 올리면 옅은 면이 깔린다 (P81 6절). 카드 테두리가 이미 둘레를
 * 그으므로 또 상자를 두르면 카드 안이 상자 셋이 된다. 고르개임은 앞의 표시와 뒤의 꺾쇠가 말한다.
 */
const PILL = "h-7 w-auto max-w-[15rem] gap-1.5 rounded-chip border-transparent bg-transparent px-2 text-[13px] text-fg hover:bg-hover";

export function AgentPill({
  value,
  onChange,
  children,
  disabled = false,
  assistantValue,
}: {
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
  disabled?: boolean;
  /** 비서를 뜻하는 값. 고른 값이 이것이면 비서의 표시(컨시어지 종)를, 아니면 Agent 의 표시를 앞에 둔다 (P81 5절). */
  assistantValue?: string;
}) {
  const t = useT();
  return (
    <Select
      aria-label={t.chat.pills.agent}
      data-testid="home-agent-select"
      className={PILL}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      renderTrigger={(selected) => (
        <>
          {selected ? <CounterpartBadge kind={selected.value === assistantValue ? "assistant" : "agent"} size={18} /> : null}
          <span className={cn("min-w-0 flex-1 truncate", !selected && "text-muted")}>{selected?.label ?? ""}</span>
          <ChevronDown size={14} className="shrink-0 text-muted" aria-hidden />
        </>
      )}
    >
      {children}
    </Select>
  );
}

/** 청지기의 모델 고르개. 목록은 `GET /assistant/config` 의 providers 다. */
export function ModelPill({
  value,
  providers,
  onChange,
  disabled = false,
}: {
  value: string;
  providers: AssistantProvider[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const text = t.chat.pills;
  return (
    <Select
      aria-label={text.model}
      data-testid="home-model-select"
      className={cn(PILL, "text-muted")}
      placeholder={text.defaultModel}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {providers.map((p) => (
        <option key={p.name} value={p.name}>
          {p.name} · {p.model}
        </option>
      ))}
    </Select>
  );
}

/** Agent 의 모델. 정의가 정한 값이라 읽기 전용이다. */
export function ModelLabel({ text }: { text: string }) {
  const t = useT();
  return (
    <span
      className="flex h-7 max-w-[15rem] items-center truncate px-2 text-[13px] text-muted"
      data-testid="home-model-readonly"
      title={t.chat.pills.modelReadonly}
    >
      {text}
    </span>
  );
}
