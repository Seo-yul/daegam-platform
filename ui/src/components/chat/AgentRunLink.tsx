/**
 * 다른 Agent 를 부른 Tool 카드의 "실행 보기" 링크 (P46 6.3절). 호출 하나는 호출받은 Agent 의 실행이라 그 Agent 의 자동 실행 탭에 기록이
 * 있다. Tool 이름(ask_*)에서 그 Agent 를 게시한 허브 항목을 찾아 연다. 항목을 볼 수 없거나 게시를 거뒀으면 링크를 그리지 않는다.
 * Tool 결과는 실행의 id 를 싣지 않으므로 실행 하나가 아니라 그 탭을 연다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listHubItems } from "@/api/resources";
import { useT } from "@/i18n";
import { agentCallToolName, agentHubItems } from "@/lib/hub";
import { hubItemsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

export function AgentRunLink({ tool, testId }: { tool: string; testId: string }) {
  const t = useT();
  const tenant = useTenant();
  const q = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const agentId = agentHubItems(q.data).find((item) => agentCallToolName(item.name) === tool)?.source.agent_id;
  if (!agentId) return null;
  return (
    <Link className="mt-1 block w-fit text-xs underline underline-offset-2" to={`/agents/${agentId}?tab=runs`} data-testid={testId}>
      {t.chat.tool.viewRun}
    </Link>
  );
}
