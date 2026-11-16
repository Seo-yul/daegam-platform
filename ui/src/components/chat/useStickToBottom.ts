/**
 * 채팅 전사가 바닥을 따라가게 한다 (D26·D31). ChatPanel(Agent) 과 AssistantPanel(플랫폼 비서) 이 같이 쓴다.
 *
 * 항목 수만 보고 스크롤하면 안 된다. 말풍선 하나에 토큰이 이어 붙는 동안 전사 항목 수는 그대로여서,
 * 모델이 길게 답하면 첫 토큰에서 한 번 내려간 뒤로 화면이 멈춘다(목 서버는 말풍선당 토큰을 하나만
 * 보내서 이 결함이 테스트에 안 잡혔다). 그래서 렌더마다 본다.
 *
 * 다만 사용자가 위로 올려 읽는 중이면 따라가지 않는다. 스트리밍 중에 화면을 도로 끌어내리면 지나간
 * 말을 읽을 수가 없다. 바닥에서 STICK_THRESHOLD_PX 안에 있을 때만 붙어 있는 것으로 본다.
 *
 * 전사 컨테이너만 스크롤한다. 끝의 요소에 scrollIntoView 를 부르면 스크롤할 수 있는 조상(껍데기의 `main`)까지
 * 함께 움직여, 패널이 놓인 화면 전체가 아래로 밀린다.
 */
import { useCallback, useEffect, useRef } from "react";

/** 바닥으로 볼 여유. 한 줄 남짓이라 마지막 줄이 반쯤 걸쳐 있어도 따라간다. */
const STICK_THRESHOLD_PX = 48;

export interface StickToBottom {
  /** `overflow-y-auto` 인 전사 컨테이너에 단다. */
  scrollerRef: React.RefObject<HTMLDivElement | null>;
  /** 컨테이너의 onScroll 에 단다. */
  onScroll: () => void;
}

export function useStickToBottom(): StickToBottom {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const stuckRef = useRef(true);

  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    stuckRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX;
  }, []);

  // 의존 배열이 없다. 토큰이 붙어도 항목 수는 그대로라 길이만 보면 스트리밍 중에 따라가지 못한다.
  useEffect(() => {
    const el = scrollerRef.current;
    if (stuckRef.current && el) el.scrollTo?.({ top: el.scrollHeight });
  });

  return { scrollerRef, onScroll };
}
