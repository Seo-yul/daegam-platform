/**
 * 그 자리에서 이름 하나를 고치는 입력칸의 값·저장 중·오류와 Enter·Escape 처리. 대화 이름(chat/SessionTitle)과 Backend API
 * 표시 이름(pages/backend-apis/New.tsx)이 같이 쓴다. 저장 전 검사와 그리는 모양은 부르는 쪽이 정한다.
 *
 * Enter 는 `onEnter`, Escape 는 `onEscape` 로 간다. 한글을 조합하는 중의 두 키는 조합을 끝내는 키라 받지 않고, 저장하는
 * 동안의 키도 받지 않는다. `run` 은 저장 중에 다시 부르면 무시하고, 저장이 던지면 그 오류를 `error` 에 둔다. 글을 고치면
 * (다시 열며 값을 채울 때도) 지난 오류를 걷는다.
 */
import { useRef, useState, type KeyboardEvent } from "react";

export interface InlineEdit {
  value: string;
  /** 입력칸의 onChange 자리이고 다시 열 때 처음 값을 채우는 데도 쓴다. 지난 오류를 걷는다. */
  setValue: (value: string) => void;
  saving: boolean;
  /** 저장이 던진 오류나 `fail` 로 둔 까닭이다. 없으면 null 이다. */
  error: unknown;
  /** 서버를 부르지 않고 까닭을 둔다. 빈 이름처럼 화면에서 거르는 자리다. */
  fail: (error: unknown) => void;
  /** 저장을 돌린다. 끝까지 가면 true, 던졌거나 이미 저장 중이면 false 다. */
  run: (save: () => Promise<unknown>) => Promise<boolean>;
  /** 입력칸의 onKeyDown 자리다. */
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
}

export function useInlineEdit(initial: string, keys: { onEnter: () => void; onEscape: () => void }): InlineEdit {
  const [value, setRaw] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // 같은 틱에 Enter 가 두 번 와도 한 번만 저장한다. 상태는 다음 그리기에야 바뀐다
  const savingRef = useRef(false);

  // 오류가 함수여도 상태 갱신 함수로 읽히지 않게 감싼다
  function fail(e: unknown): void {
    setError(() => e);
  }

  async function run(save: () => Promise<unknown>): Promise<boolean> {
    if (savingRef.current) return false;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      await save();
      return true;
    } catch (e) {
      fail(e);
      return false;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  return {
    value,
    setValue: (next) => {
      setRaw(next);
      setError(null);
    },
    saving,
    error,
    fail,
    run,
    onKeyDown: (e) => {
      if (e.nativeEvent.isComposing || savingRef.current) return;
      if (e.key === "Enter") {
        e.preventDefault();
        keys.onEnter();
      } else if (e.key === "Escape") {
        e.preventDefault();
        keys.onEscape();
      }
    },
  };
}
