/**
 * Gateway 삭제 대화상자에 미리 끼우는 게시 경고 (MCP Server 의 McpServerDeleteWarning 과 같은 자리).
 *
 * 게시된 Gateway 는 서버가 409 `published` 로 거부한다(`services/gateway.py` 의 delete). 거부 문구만으로는
 * 어떤 허브 항목이 막고 있는지 알 수 없어서, 누르기 전에 항목을 이름 링크로 미리 보여 준다.
 * 목록 행과 상세가 같이 쓰므로 두 화면 어느 쪽에도 두지 않고 따로 뺐다.
 *
 * 허브 항목의 출처 필드는 서버가 `gateway_instance_id` 이름을 그대로 쓴다. 리소스가 합쳐져도(D42)
 * 가리키는 것은 배포된 vMCP 하나라 계약이 바뀌지 않았다.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listHubItems } from "@/api/resources";
import type { HubItem } from "@/api/types";
import { useT } from "@/i18n";
import { hubItemsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";

/** 이 Gateway 를 게시한 허브 항목들. 삭제 경고와 상세의 게시 카드가 같은 목록을 본다. */
export function publishedFrom(items: HubItem[], gatewayId: string | null | undefined): HubItem[] {
  return items.filter((i) => i.kind === "published" && i.source.gateway_instance_id === gatewayId);
}

export function HubPublishDeleteWarning({ gatewayId }: { gatewayId: string | null | undefined }) {
  const t = useT();
  const tenant = useTenant();
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems, enabled: Boolean(gatewayId) });
  const published = publishedFrom(hub.data ?? [], gatewayId);
  if (!gatewayId || !published.length) return null;
  return (
    <div className="rounded-chip border border-warn-line bg-warn-soft px-3 py-2 text-xs text-warn-fg" data-testid="delete-published-warning">
      {t.gateways.publish.deleteBlocked}
      <ul className="mt-1 flex flex-wrap gap-1">
        {published.map((i) => (
          <li key={i.id}>
            <Link className="font-mono text-xs underline-offset-2 hover:underline" to={`/hub/${i.id}`} data-testid={`delete-published-item-${i.name}`}>
              {i.name}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
