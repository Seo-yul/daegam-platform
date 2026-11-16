/**
 * 채팅 입력칸 (D45 둘째). Agent 채팅(ChatPanel)과 플랫폼 비서(AssistantPanel)와 홈의 시작 화면이 같은 부품을 쓴다.
 *
 * 모양은 두 가지다. `card` 는 홈 시작 화면의 큰 카드(`max-w-[740px] w-full`. 1280px 창에서 본문에 남는 폭이
 * 그보다 좁을 수 있어 고정 폭을 주면 가로 스크롤이 난다)이고, 기본은 전사 아래에 붙는 줄이다. 둘 다 안쪽 구성은 같다.
 * 위가 textarea, 아래 줄 왼쪽이 상대(`left`), 오른쪽이 모델(`model`)과 보내기 버튼이다. 줄 모양도 테두리 둔 상자로
 * 감싸 어디에 쓰는지가 바로 보이게 한다. 카드는 둥근 16px 에 옅은 그림자를 얹어 시작 화면에서 한 단 떠 보인다 (P81 6절).
 *
 * textarea 는 쓰는 만큼 키가 자라고 여덟 줄에서 멈춘 뒤 스스로 스크롤한다. 긴 지시문을 한 줄 창으로 쓰게 하면
 * 방금 쓴 문장이 안 보인다.
 *
 * testid 는 부르는 쪽이 정한다. `chat-input`·`chat-send`(ChatPanel)와 `assistant-input`·`assistant-send`
 * (비서 패널) 두 집합이 이미 e2e 의 계약이라 부품을 합치면서도 이름은 그대로 둔다.
 *
 * 보내기 버튼은 두 가지로 쓴다. `onAbort` 를 주면 도는 동안 같은 버튼이 "중단" 이 되고(갈아 끼우면 포커스가
 * 사라진다), 안 주면 `busyLabel` 을 보이며 잠긴다. 모양은 먹색 둥근 네모 안의 아이콘(위 화살표, 정지, 도는 고리)이고
 * 글은 화면 읽기용으로 남긴다. 접근 이름과 글 내용이 그대로라 테스트가 읽는 계약도 그대로다.
 *
 * `attach` 를 주면 파일을 첨부한다(P36 9절, AttachControls.tsx). 아래 줄 왼쪽 맨 앞에 클립 단추가 서고 붙여 넣은 파일(화면 캡처
 * 같은 이미지)도 받는다. 업로드 중이거나 실패한 칩이 있으면 보내기가 잠긴다. 끌어 놓을 자리는 부르는 쪽이 정한다(`useFileDrop`).
 * `chips` 는 카드 안 입력칸 위에 서는 칩 줄이다. 첨부 칩과 메시지 수정의 읽기 전용 칩이 온다.
 */
import { useLayoutEffect, useRef, type FormEvent, type ReactNode, type RefObject } from "react";

import { ArrowUp, LoaderCircle, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

import { AttachButton, pasteFiles, type ComposerAttach } from "./AttachControls";

/** textarea 가 자랄 수 있는 최대 높이(px). 여덟 줄쯤이고 그 위로는 스스로 스크롤한다. */
const MAX_TEXTAREA_HEIGHT = 200;

/** 쓰는 만큼 키가 자란다. 먼저 auto 로 되돌려야 지운 만큼 다시 줄어든다. */
function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: string) {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [value, ref]);
}

export interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  /** 보낼 때. 빈 값·busy·disabled 는 이 부품이 먼저 걸러 준다. */
  onSubmit: () => void;
  /** Escape 를 눌렀을 때. 메시지 수정처럼 쓰다가 그만둘 수 있는 자리가 준다. */
  onCancel?: () => void;
  busy?: boolean;
  /** 주면 도는 동안 보내기 버튼이 중단 버튼이 된다. */
  onAbort?: () => void;
  disabled?: boolean;
  placeholder?: string;
  /** 시작 화면의 큰 카드인가. 아니면 전사 아래에 붙는 줄이다. */
  card?: boolean;
  /** 아래 줄 왼쪽. 홈의 상대 pill 이 여기 온다. */
  left?: ReactNode;
  /** 아래 줄 오른쪽, 보내기 버튼 앞. 모델 chip 이 여기 온다. */
  model?: ReactNode;
  /** 입력칸 아래에 붙는 것. 홈의 빠른 시작 칩이 여기 온다. */
  below?: ReactNode;
  /** 파일 첨부(P36 9절). 주면 클립 단추가 서고 붙여 넣은 파일을 받는다. */
  attach?: ComposerAttach;
  /** 카드 안 입력칸 위에 서는 칩 줄. */
  chips?: ReactNode;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
  inputLabel: string;
  inputTestId: string;
  sendTestId: string;
  abortTestId?: string;
  formTestId?: string;
  /** 도는 동안 잠긴 버튼에 보일 글. `onAbort` 가 있으면 쓰지 않는다. */
  busyLabel?: string;
  rows?: number;
  className?: string;
}

export function Composer({
  value,
  onChange,
  onSubmit,
  onCancel,
  busy = false,
  onAbort,
  disabled = false,
  placeholder,
  card = false,
  left,
  model,
  below,
  attach,
  chips,
  inputRef,
  inputLabel,
  inputTestId,
  sendTestId,
  abortTestId,
  formTestId,
  busyLabel,
  rows = 2,
  className,
}: ComposerProps) {
  const ownRef = useRef<HTMLTextAreaElement>(null);
  const ref = inputRef ?? ownRef;
  const blocked = busy || disabled || !value.trim() || Boolean(attach?.blocking);
  useAutoGrow(ref, value);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    if (blocked) return;
    onSubmit();
  }

  return (
    <div className={cn("flex flex-col", card ? "w-full max-w-[740px] gap-4" : "gap-2 px-3 pb-3 pt-2", className)}>
      <form
        className={cn(
          "flex flex-col gap-1 border bg-surface transition-colors focus-within:border-field-line",
          card ? "rounded-dialog border-line-strong pb-2.5 pl-4 pr-3 pt-3.5 shadow-raised" : "rounded-tile border-line-strong py-2 pl-3 pr-2 shadow-hairline",
        )}
        data-testid={formTestId}
        onSubmit={submit}
      >
        {chips}
        <Textarea
          ref={ref}
          data-testid={inputTestId}
          aria-label={inputLabel}
          rows={rows}
          className={cn(
            "resize-none rounded-none border-0 bg-transparent px-0.5 py-1 font-sans shadow-none focus-visible:ring-0",
            card ? "min-h-14 text-[15px] leading-relaxed" : "min-h-9 text-sm",
          )}
          placeholder={placeholder}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          onPaste={attach ? (e) => pasteFiles(e, attach.onFiles) : undefined}
          onKeyDown={(e) => {
            // 한글을 조합하는 중의 Enter·Escape 는 조합을 끝내는 키다
            if (e.nativeEvent.isComposing) return;
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape" && onCancel) {
              e.preventDefault();
              onCancel();
            }
          }}
        />
        <div className="flex flex-wrap items-center gap-1.5">
          {attach ? <AttachButton attach={attach} disabled={disabled} /> : null}
          {left}
          <div className="ml-auto flex items-center gap-1.5">
            {model}
            <SendButton busy={busy} blocked={blocked} onAbort={onAbort} card={card} sendTestId={sendTestId} abortTestId={abortTestId} busyLabel={busyLabel} />
          </div>
        </div>
      </form>
      {below}
    </div>
  );
}

/** 보내기와 중단은 한 버튼이다. 갈아 끼우면 포커스가 사라진다. */
function SendButton({
  busy,
  blocked,
  onAbort,
  card,
  sendTestId,
  abortTestId,
  busyLabel,
}: Pick<ComposerProps, "onAbort" | "sendTestId" | "abortTestId" | "busyLabel"> & { busy: boolean; blocked: boolean; card: boolean }) {
  const t = useT();
  const aborting = busy && Boolean(onAbort);
  const label = aborting ? t.chat.composer.abort : busy ? (busyLabel ?? t.chat.composer.busy) : t.chat.composer.send;
  const Icon = aborting ? Square : busy ? LoaderCircle : ArrowUp;
  // 주 버튼 그대로다(먹 바탕). 카드 입력칸은 34px, 좁은 입력칸은 28px 네모다
  return (
    <Button
      type={aborting ? "button" : "submit"}
      size={card ? "icon" : "icon-sm"}
      className="shrink-0"
      data-testid={aborting ? (abortTestId ?? sendTestId) : sendTestId}
      disabled={aborting ? false : blocked}
      onClick={aborting ? onAbort : undefined}
      title={label}
    >
      <Icon strokeWidth={aborting ? 0 : 2.25} fill={aborting ? "currentColor" : "none"} className={busy && !aborting ? "motion-safe:animate-spin" : undefined} aria-hidden />
      <span className="sr-only">{label}</span>
    </Button>
  );
}
