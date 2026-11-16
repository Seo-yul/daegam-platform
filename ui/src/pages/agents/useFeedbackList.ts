/**
 * Agent 의 좋아요와 아쉬워요 목록 (P36 6절, `agent:approve`). 피드백 탭의 표와 세션 탭의 "평가 사례로 복사" 가 같은 키의 같은 쪽을
 * 본다(P45 8절). 한 키를 보는 둘이 다른 옵션을 주면 react-query 가 어느 쪽 옵션으로 읽을지 정하지 못하므로 옵션은 여기 하나다.
 */
import { useInfiniteQuery } from "@tanstack/react-query";

import { listAgentFeedback, type AgentFeedbackFilter } from "@/api/agentChat";
import { agentFeedbackListKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

export function useAgentFeedbackList(agentId: string, filter: AgentFeedbackFilter, { enabled = true }: { enabled?: boolean } = {}) {
  const tenant = useTenant();
  return useInfiniteQuery({
    queryKey: agentFeedbackListKey(tenant, agentId, filter),
    queryFn: ({ pageParam }) => listAgentFeedback(agentId, filter, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before,
    enabled,
  });
}
