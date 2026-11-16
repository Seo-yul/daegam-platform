/**
 * 화면에는 안 보이고 낭독기만 읽는 한 줄과, 그 줄을 채우는 훅.
 *
 * 배포·재배포·배포 해제는 대화상자가 닫히는 것으로 끝난다. 눈으로 보는 사람은 배너와 배지가 바뀌는
 * 것을 보지만, 낭독기를 쓰는 사람에게는 아무 일도 일어나지 않은 것과 같다. 시작했다는 사실을 여기서
 * 말한다 (D43).
 */
import { useCallback, useState } from "react";

/** 낭독기만 읽는 한 줄. 비어 있으면 아무것도 읽지 않는다. */
export function LiveRegion({ message, testId = "live-region" }: { message: string; testId?: string }) {
  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-testid={testId}>
      {message}
    </p>
  );
}

/**
 * 알림 상자. `announce("…")` 로 채우고 `message` 를 LiveRegion 에 넘긴다.
 *
 * 몇 번째 알림인지 함께 센다. live region 은 내용이 바뀔 때만 읽히므로, 같은 말을 다시 할 때는
 * 호출부가 이 수를 열쇠로 써서 줄을 다시 세운다.
 */
export function useAnnounce(): { message: string; count: number; announce: (text: string) => void } {
  const [state, setState] = useState({ text: "", n: 0 });
  const announce = useCallback((text: string) => setState((prev) => ({ text, n: prev.n + 1 })), []);
  return { message: state.text, count: state.n, announce };
}
