/**
 * 홈의 시작 화면 (D45 첫째·둘째·넷째). 대화가 아직 없을 때 첫 화면이 여기다.
 *
 * 가운데에 마크와 인사가 있고 그 아래에 입력칸 카드 하나가 온다. 카드를 둘러싸는 큰 상자는 두지 않는다.
 * 입력칸 카드 하나만 떠 있어야 첫 화면에서 할 일이 한눈에 보인다.
 *
 * 인사에 `sub` 를 쓰지 않는다. local 은 uuid, header 모드는 "header" 라 사람 이름이 아니다. `/me` 의
 * `name` 이 있고 주체가 사람일 때만 이름을 부르고, 아니면 "무엇을 도와드릴까요?" 다.
 *
 * 상대가 없을 때(에이전트도 비서도 없음)는 입력칸 자리에 무엇을 만들면 되는지가 대신 온다. 갓 설치한
 * 플랫폼의 기본 경로라 이 자리가 제일 중요하다(D35).
 */
import { Activity } from "lucide-react";
import { useRef, type ReactNode } from "react";

import type { MeOut } from "@/api/types";
import { Composer } from "@/components/chat";
import { type NavIcon, DaegamMark } from "@/components/icons/brand";
import { navIcon } from "@/components/Layout";
import { type Messages, useT } from "@/i18n";

/**
 * 빠른 시작 칩 넷의 id (D45 넷째). 이 플랫폼에서 청지기가 잘하는 일이다. 칩의 글은 입력칸에 채워 청지기에게 보내는
 * 말이기도 해서 둘 다 화면 언어를 따른다(shell.homeStart.quickStarts).
 */
const QUICK_STARTS = ["mcp", "status", "hub", "agent"] as const;
export type QuickStartId = (typeof QUICK_STARTS)[number];

/** 칩 앞의 아이콘. 그 일이 닿는 메뉴의 아이콘과 같아 칩이 어디로 이어지는지 보인다 (P81 6절). */
const QUICK_START_ICONS: Record<QuickStartId, NavIcon> = { mcp: navIcon("/mcp-servers"), status: Activity, hub: navIcon("/hub"), agent: navIcon("/agents") };

/** 화면이 부를 이름. 사람이 아니거나 이름을 모르면 null 이고 그때는 이름 없는 인사다. */
export function greetingName(me: MeOut | null): string | null {
  if (!me || me.subject_type !== "user") return null;
  return me.name?.trim() || null;
}

export function greetingText(t: Messages, me: MeOut | null): string {
  const name = greetingName(me);
  return name ? t.shell.homeStart.greeting(name) : t.shell.homeStart.ask;
}

export interface HomeStartProps {
  me: MeOut | null;
  /** 인사 위의 표시. 대화 상대의 것이다(비서는 컨시어지 종, Agent 는 워크플로). 상대가 없으면 제품 마크다 (P81 5절). */
  badge?: ReactNode;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  disabled?: boolean;
  /** 입력칸 아래 줄 왼쪽의 상대 pill. */
  pill: ReactNode;
  /** 입력칸 아래 줄 오른쪽의 모델 chip. */
  model: ReactNode;
  /** 빠른 시작 칩을 보일지. 상대가 Agent 면 감춘다 (D45 넷째). */
  quickStarts: boolean;
  onQuickStart: (text: string) => void;
  /** 대화할 상대가 없을 때. 입력칸 대신 이것이 온다. */
  empty?: ReactNode;
  /** 인사와 입력칸 사이. 지금 상대와 대화할 수 없는 이유 같은 것이 온다. */
  notice?: ReactNode;
  /** 칩 아래 한 줄. 고른 상대가 누구로서 도는지를 말한다 (D35). */
  note?: ReactNode;
}

export function HomeStart({ me, badge, value, onChange, onSubmit, disabled = false, pill, model, quickStarts, onQuickStart, empty, notice, note }: HomeStartProps) {
  const t = useT();
  const text = t.shell.homeStart;
  const inputRef = useRef<HTMLTextAreaElement>(null);
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 overflow-y-auto px-6 pb-[8vh] pt-8" data-testid="home-start">
      <div className="flex flex-col items-center gap-[18px]">
        {badge ?? <DaegamMark size={52} aria-hidden />}
        {/* 화면에서 가장 큰 글자다. 제목(24px)보다 두 단 위다 (P81 6절) */}
        <h1 className="text-center text-[30px] font-bold leading-tight tracking-[-0.03em] text-heading" data-testid="home-greeting">
          {greetingText(t, me)}
        </h1>
      </div>
      {notice}
      {empty ?? (
        <Composer
          card
          value={value}
          onChange={onChange}
          onSubmit={onSubmit}
          disabled={disabled}
          placeholder={text.placeholder}
          left={pill}
          model={model}
          inputRef={inputRef}
          inputLabel={text.ask}
          inputTestId="home-start-input"
          sendTestId="home-start-send"
          formTestId="home-start-form"
          below={
            <>
              {quickStarts ? (
                <div className="flex flex-wrap justify-center gap-2 px-2" data-testid="home-quick-starts">
                  {QUICK_STARTS.map((id) => (
                    <button
                      key={id}
                      type="button"
                      data-testid={`home-quick-${id}`}
                      className="inline-flex h-8 items-center gap-[7px] rounded-full border border-line bg-surface px-[13px] text-[13px] text-fg transition-colors hover:border-line-strong hover:bg-surface-strong hover:text-heading focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring"
                      onClick={() => {
                        // 채우고 바로 이어 쓰게 포커스를 준다. 칩은 문장의 시작일 뿐이다
                        onQuickStart(text.quickStarts[id]);
                        inputRef.current?.focus();
                      }}
                    >
                      <QuickStartIcon id={id} />
                      {text.quickStarts[id]}
                    </button>
                  ))}
                </div>
              ) : null}
              {note ? <div className="mt-1.5 flex justify-center px-1 text-center">{note}</div> : null}
            </>
          }
        />
      )}
    </div>
  );
}

function QuickStartIcon({ id }: { id: QuickStartId }) {
  const Icon = QUICK_START_ICONS[id];
  return <Icon size={14} className="shrink-0 text-muted" aria-hidden />;
}
