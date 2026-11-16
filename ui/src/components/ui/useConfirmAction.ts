/**
 * 목록의 한 줄에 하는 동작을 확인 대화상자로 한 번 더 물은 뒤 실행한다. 개인 토큰 회수(`TokenTable`), OAuth 연결
 * 끊기(`GrantList`), 대화 삭제(`SessionList`)가 같이 쓴다.
 *
 * `ask` 로 줄을 고르면 대화상자가 열린다. 확인하면 `action` 을 부르고, 끝나면 대화상자를 닫고 `onDone` 으로 목록을 다시
 * 읽게 한다. 서버가 거절하면 대화상자를 닫지 않고 `error` 에 까닭을 둔다. 취소하면 지난 오류를 걷는다. `dialog` 는
 * `ConfirmDialog` 에 그대로 펼친다.
 */
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

export function useConfirmAction<T>(action: (item: T) => Promise<void>, onDone?: () => unknown) {
  const [target, setTarget] = useState<T | null>(null);
  const mutation = useMutation({
    mutationFn: action,
    onSuccess: () => {
      setTarget(null);
      void onDone?.();
    },
  });
  return {
    target,
    ask: setTarget,
    error: mutation.error,
    dialog: {
      open: target !== null,
      pending: mutation.isPending,
      onConfirm: () => {
        if (target !== null) mutation.mutate(target);
      },
      onCancel: () => {
        setTarget(null);
        mutation.reset();
      },
    },
  };
}
