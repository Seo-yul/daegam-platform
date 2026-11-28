/**
 * 다른 Agent 를 부른 Tool 카드의 "실행 보기" 링크 (P46 6.3절). 호출 하나는 호출받은 Agent 의 실행이라 그 Agent 의 자동 실행 탭에 기록이
 * 있다. Tool 이름(ask_*)에서 그 Agent 를 게시한 허브 항목을 찾아 연다. 항목을 볼 수 없거나 게시를 거뒀으면 링크를 그리지 않는다.
 * 결과가 실행의 id 를 실었으면(`run_id`) 그 탭에서 그 실행 하나를 짚고, 옛 런타임의 결과는 탭만 연다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listHubItems } from "@/api/resources";
import { useT } from "@/i18n";
import { agentCallToolName, agentHubItems } from "@/lib/hub";
import { hubItemsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** 호출받은 Agent 의 자동 실행 탭에서 실행 하나를 짚는 주소다(RunHistoryCard 의 ?run=). runId 가 없으면 탭만 연다. */
export function agentRunPath(agentId: string, runId?: string | null): string {
  const search = new URLSearchParams({ tab: "runs", ...(runId ? { run: runId } : {}) });
  return `/agents/${agentId}?${search.toString()}`;
}

export function AgentRunLink({ tool, runId, testId }: { tool: string; runId?: string; testId: string }) {
  const t = useT();
  const tenant = useTenant();
  const q = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const agentId = agentHubItems(q.data).find((item) => agentCallToolName(item.name) === tool)?.source.agent_id;
  if (!agentId) return null;
  return (
    <Link className="mt-1 block w-fit text-xs underline underline-offset-2" to={agentRunPath(agentId, runId)} data-testid={testId}>
      {t.chat.tool.viewRun}
    </Link>
  );
}
