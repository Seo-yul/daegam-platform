/**
 * 마운트하자마자 보낼 첫 메시지 (D45 다섯째). 홈의 시작 화면에서 쓴 말이 그대로 첫 턴이 된다.
 * ChatPanel(Agent) 과 AssistantPanel(플랫폼 비서) 이 같이 쓴다.
 *
 * 마운트 때 한 번 보내고 잊는다. StrictMode 가 효과를 두 번 돌려도 ref 를 비워 두어 두 번 보내지 않는다.
 */
import { useEffect, useRef } from "react";

export function useSendOnce(initialMessage: string | null, send: (text: string) => unknown): void {
  const firstRef = useRef(initialMessage);
  useEffect(() => {
    const text = firstRef.current;
    if (!text) return;
    firstRef.current = null;
    void send(text);
    // 마운트 때 한 번이다. send 가 바뀐다고 다시 보내면 안 된다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
