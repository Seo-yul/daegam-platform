/**
 * 채팅 패널이 쥐는 답변 피드백 (P36 6절). 세션의 내 평가를 한 번 읽어 다시 연 대화의 엄지 상태를 되살리고, 매기거나 거두면
 * 그 캐시를 고친 뒤 피드백 탭의 목록을 무효화한다. 같은 읽기가 설치의 평가 보존 기간도 가져와 의견 창의 동의 안내가 적는다
 * (P45 8절). 세션이 아직 없으면(첫 턴 전) 엄지를 세울 턴도 없으므로 undefined 다.
 *
 * 평가는 세션 소유자만 매긴다. 남의 세션을 읽기 전용으로 그리는 세션 탭은 이 훅을 쓰지 않아 엄지가 없다. 미리 보기에는 피드백
 * 라우트가 없으므로(P44b 6절) 패널이 Agent 를 주지 않고 엄지도 없다.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { deleteTurnFeedback, listSessionFeedback, putTurnFeedback } from "@/api/agentChat";
import type { AgentFeedback, AgentFeedbackBody, AgentSessionFeedback } from "@/api/types";
import { agentFeedbackListKey, sessionFeedbackKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/**
 * 사람이 매긴 평가다. `ephemeral` 은 패널이 붙인다. `evaluation_use` 는 평가 사례로 쓰는 동의이고(P45 8절) 켜져 있을 때만
 * 싣는다. 싣지 않으면 서버는 동의하지 않은 것으로 읽는다.
 */
export type FeedbackValue = Omit<AgentFeedbackBody, "ephemeral">;

/** 남긴 동의가 켜져 있으면 다음 저장에도 싣는 칸이다. */
export function keptConsent(previous: AgentFeedback | undefined): { evaluation_use?: true } {
  return previous?.evaluation_use ? { evaluation_use: true } : {};
}

/**
 * 세션 평가 읽기의 캐시다. 그 값을 싣지 않는 옛 플랫폼의 응답과 읽기가 끝나기 전의 저장이 만든 캐시에는 `retention_days` 가
 * 없다.
 */
type SessionFeedbackCache = Omit<AgentSessionFeedback, "retention_days"> & Partial<Pick<AgentSessionFeedback, "retention_days">>;

export interface TurnFeedback {
  /** 이 턴의 내 평가. 없으면 undefined 다. */
  of: (turnId: string) => AgentFeedback | undefined;
  /**
   * 설치의 평가 보존 기간(일). 의견 창의 동의 안내가 적는다(P45 8절). 서버가 세션 평가와 함께 보내므로 세션에 아직 평가가
   * 없어도 있다. 세션 평가를 아직 읽지 못했거나 그 값을 싣지 않는 옛 플랫폼이면 undefined 다.
   */
  retentionDays?: number;
  save: (turnId: string, value: FeedbackValue) => Promise<void>;
  withdraw: (turnId: string) => Promise<void>;
}

export function useTurnFeedback(agentId: string | null, sessionId: string | null, ephemeral: boolean): TurnFeedback | undefined {
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const mine = useQuery<SessionFeedbackCache>({
    queryKey: sessionFeedbackKey(tenant, agentId ?? "", sessionId),
    queryFn: () => listSessionFeedback(agentId as string, sessionId as string),
    enabled: Boolean(agentId && sessionId),
  });
  const data = mine.data;

  return useMemo(() => {
    if (!agentId || !sessionId) return undefined;
    /**
     * 그 턴의 평가를 바꾼 캐시를 두고 탭의 목록을 다시 읽게 한다. 도는 세션 평가 읽기를 먼저 멈춘다. 저장 전에 나간 읽기가
     * 늦게 오면 고친 캐시를 옛 평가로 덮는다.
     */
    const settle = async (turnId: string, next: AgentFeedback | null) => {
      const key = sessionFeedbackKey(tenant, agentId, sessionId);
      await queryClient.cancelQueries({ queryKey: key });
      queryClient.setQueryData<SessionFeedbackCache>(key, (prev) => ({ ...prev, items: [...(prev?.items ?? []).filter((f) => f.turn_id !== turnId), ...(next ? [next] : [])] }));
      void queryClient.invalidateQueries({ queryKey: agentFeedbackListKey(tenant, agentId) });
    };
    return {
      of: (turnId) => data?.items.find((f) => f.turn_id === turnId),
      retentionDays: data?.retention_days,
      save: async (turnId, value) => settle(turnId, await putTurnFeedback(agentId, sessionId, turnId, { ...value, ephemeral })),
      withdraw: async (turnId) => {
        await deleteTurnFeedback(agentId, sessionId, turnId);
        await settle(turnId, null);
      },
    };
  }, [agentId, data, ephemeral, queryClient, sessionId, tenant]);
}
