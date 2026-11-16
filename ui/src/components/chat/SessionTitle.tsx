/**
 * 대화 이름을 목록의 행 안에서 바꾸는 입력칸 (P36 4절). Enter 로 저장하고 Escape 로 취소한다. 한글을 조합하는 중의 두 키는
 * 조합을 끝내는 키라 받지 않는다(components/ui/useInlineEdit.ts).
 *
 * 포커스를 잃으면 취소다. 다만 까닭을 보이는 중에는 닫지 않는다. 닫으면 서버가 거절한 까닭도 함께 사라진다. 글을 고치면 그
 * 까닭을 걷으므로 그다음부터는 다시 포커스를 잃을 때 닫힌다.
 *
 * 저장하는 동안에는 입력을 잠그되 포커스는 입력칸에 둔다. 서버가 거절하면(422 invalid_title 등) 입력칸을 그대로 두고 아래에
 * 까닭을 보이며, 입력칸이 그 문단을 설명으로 읽게 잇는다. 이름이 그대로면 서버를 부르지 않고 닫는다.
 */
import { useEffect, useId, useRef } from "react";

import { describeError } from "@/components/ErrorMessage";
import { Input } from "@/components/ui/input";
import { useInlineEdit } from "@/components/ui/useInlineEdit";
import { useT } from "@/i18n";
import { SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitle";
import { cn } from "@/lib/utils";

/** 빈 이름으로 저장하려 한 까닭. 문장 대신 이 표시를 오류 자리에 두고 그릴 때 사전(chat.sessionTitle.empty)의 문장을 쓴다. */
const EMPTY_TITLE = Symbol("emptyTitle");

export interface SessionTitleProps {
  initial: string;
  onSave: (title: string) => Promise<void>;
  onCancel: () => void;
  testId: string;
}

export function SessionTitle({ initial, onSave, onCancel, testId }: SessionTitleProps) {
  const t = useT();
  const text = t.chat.sessionTitle;
  const edit = useInlineEdit(initial, { onEnter: () => void save(), onEscape: onCancel });
  const ref = useRef<HTMLInputElement>(null);
  const errorId = useId();
  const error = edit.error === EMPTY_TITLE ? text.empty : edit.error ? describeError(t, edit.error).text : null;

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  async function save(): Promise<void> {
    const title = edit.value.trim();
    if (title === initial) return onCancel();
    if (!title) return edit.fail(EMPTY_TITLE);
    await edit.run(() => onSave(title));
  }

  return (
    <div className="min-w-0 flex-1 px-1 py-0.5">
      <Input
        ref={ref}
        value={edit.value}
        maxLength={SESSION_TITLE_MAX_CHARS}
        // disabled 는 포커스를 body 로 떨어뜨린다. 저장 중에는 읽기 전용으로 잠그고 흐리게만 한다
        readOnly={edit.saving}
        aria-busy={edit.saving || undefined}
        aria-label={text.label}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        data-testid={testId}
        className={cn("h-7 text-xs", edit.saving && "opacity-50")}
        onChange={(e) => edit.setValue(e.target.value)}
        onKeyDown={edit.onKeyDown}
        onBlur={() => {
          if (!edit.saving && !error) onCancel();
        }}
      />
      {error ? (
        <p id={errorId} role="alert" className="mt-1 text-[10px] leading-snug text-err-fg" data-testid={`${testId}-error`}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
