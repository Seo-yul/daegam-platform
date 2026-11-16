/**
 * 상세 화면의 탭을 URL 에 두는 훅 (D42).
 *
 * 리소스 넷의 상세가 개요·정의·배포·Advanced 를 같은 방식으로 나눈다. 탭을 컴포넌트 상태로 두면
 * 새로고침·링크 공유·뒤로가기가 보고 있던 탭을 잃는다. 기본 탭일 때는 `?tab=` 을 아예 빼서
 * 주소가 지저분해지지 않게 한다.
 */
import { useLocation, useSearchParams } from "react-router";

export function useTabParam<T extends string>(tabs: readonly T[], fallback: T): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const raw = params.get("tab");
  const tab = (tabs as readonly string[]).includes(raw ?? "") ? (raw as T) : fallback;
  const setTab = (t: T) => {
    const next = new URLSearchParams(params);
    if (t === fallback) next.delete("tab");
    else next.set("tab", t);
    // 배포 직후 넘어온 warnings 처럼 화면이 들고 있는 state 는 탭을 옮겨도 남아야 한다
    setParams(next, { state: location.state });
  };
  return [tab, setTab];
}

/**
 * MCP Server·Gateway 상세의 탭. 둘이 같은 다섯을 같은 이름(사전의 `ui.tabs.standard`)으로 나눈다. 메트릭 탭은 감사 기록의
 * 시간 묶음을 읽고(P51 5절), 탭을 열 때만 요청한다.
 *
 * Agent 는 캔버스·채팅·세션·승인·지표·이벤트가 더 붙고 MCP Hub 는 아예 다른 넷이라 여기 들어오지
 * 않는다. 그 둘은 자기 자리에 자기 목록을 둔다.
 */
export const STANDARD_TABS = ["overview", "definition", "deployment", "metrics", "advanced"] as const;
export type StandardTab = (typeof STANDARD_TABS)[number];
