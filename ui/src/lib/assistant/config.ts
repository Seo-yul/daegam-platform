/** 비서 설정과 내 세션 목록 (D31). 설정을 못 읽으면 비활성으로 본다. */
import { useQuery } from "@tanstack/react-query";

import { getAssistantConfig, listAssistantSessions } from "@/api/resources";
import { ASSISTANT_SETTINGS_DEFAULTS, type AssistantConfig, type AssistantSessionSummary } from "@/api/types";
import { ASSISTANT_CONFIG_KEY, assistantSessionsKey } from "@/lib/queryKeys";

const DISABLED_CONFIG: AssistantConfig = {
  enabled: false,
  provider: null,
  providers: [],
  confirm_deploy: ASSISTANT_SETTINGS_DEFAULTS.confirm_deploy,
  limits: { max_tool_calls_per_turn: ASSISTANT_SETTINGS_DEFAULTS.max_tool_calls_per_turn, turn_timeout_seconds: ASSISTANT_SETTINGS_DEFAULTS.turn_timeout_seconds },
};

/** GET /assistant/config. 오류·비활성 모두 `enabled=false` 라 패널과 토글이 아예 없다. */
export function useAssistantConfig(): AssistantConfig {
  return useAssistantConfigState().config;
}

/**
 * 설정과 함께 응답을 받았는지(`settled`)도 준다. 받기 전에도 config 는 비활성이라, "꺼짐" 처럼 비활성을 알리는 표시는
 * settled 일 때만 그린다. 그러지 않으면 켜진 비서도 첫 화면에서 잠깐 꺼짐으로 보인다.
 */
export function useAssistantConfigState(): { config: AssistantConfig; settled: boolean } {
  const q = useQuery({ queryKey: ASSISTANT_CONFIG_KEY, queryFn: getAssistantConfig, staleTime: 60_000, retry: false });
  const settled = !q.isPending;
  if (q.isError || !q.data || typeof q.data.enabled !== "boolean") return { config: DISABLED_CONFIG, settled };
  return { config: q.data, settled };
}

/** 내 세션 목록(테넌트 범위). 패널의 드롭다운과 감사 화면의 세션 링크가 쓴다. */
export function useAssistantSessions(tenant: string, enabled = true) {
  return useQuery<AssistantSessionSummary[]>({ queryKey: assistantSessionsKey(tenant), queryFn: listAssistantSessions, enabled });
}
