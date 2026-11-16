/**
 * 세 리소스의 Tool 규칙 카드 (P52 7절). 리소스마다 규칙을 둘 Tool 을 모으고, 저장을 그 리소스의 저장 API 로 보낸다. 카드는
 * components/ToolRules.tsx 하나다.
 *
 * - MCP Server: 백엔드마다 노출하는 operationId 가 키이고 화면 이름은 오버라이드한 이름이다. 인자와 메서드는 그 Backend API
 *   버전의 Tool 상세에서 읽는다. 저장은 정의 전체의 PUT 이라 새 리비전이 생기고, 배포한 서버에는 다시 배포해야 반영된다.
 * - Gateway: 백엔드(허브 항목)마다 고른 Tool, 고르지 않았으면 항목의 Tool 이름이 키다(원래 이름). 저장은 정의 전체의 PUT 이다.
 * - 허브 image 항목: 항목이 내놓는 Tool(tools_filter, 없으면 tool_names)이 키다. 저장은 규칙만의 PUT 이고 항목을 다시 적용한다.
 */
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { getVersionDetail, listAgents, listBackendApis, listHubItems, putGateway, putMcpServer, setHubToolRules } from "@/api/resources";
import type { ConflictResolution, FailureMode, Gateway, HubItem, MCPServer } from "@/api/types";
import { ToolRulesCard, type RuleSection } from "@/components/ToolRules";
import { useT } from "@/i18n";
import { hasScope, idpDataPlane, useAuthState } from "@/lib/auth";
import { displayName } from "@/lib/names";
import { hubItemKey, hubItemsKey } from "@/lib/queryKeys";
import { useTenant } from "@/lib/tenant";
import { PINNED_LIST_REFUSES_CALLS, methodReadOnly } from "@/lib/toolRules";

/** 이 테넌트의 Agent. 규칙의 Agent 선택지이고, 저장된 id 를 이름으로 보이는 표다. */
function useTenantAgents(): { id: string; name: string }[] {
  const tenant = useTenant();
  const q = useQuery({ queryKey: ["agents", tenant], queryFn: listAgents });
  return (q.data ?? []).map((a) => ({ id: a.id, name: displayName(a) }));
}

export function ServerToolRules({ server }: { server: MCPServer }) {
  const t = useT();
  const text = t.toolRules;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const { me, mode } = useAuthState();
  const agents = useTenantAgents();
  const backends = useQuery({ queryKey: ["backend-apis", tenant], queryFn: listBackendApis });
  const versions = useQueries({
    queries: server.backends.map((b) => ({
      queryKey: ["backend-apis", tenant, b.backend_profile_id, "versions", b.spec_version],
      queryFn: () => getVersionDetail(b.backend_profile_id, b.spec_version),
    })),
  });
  const sections: RuleSection[] = server.backends.map((b, i) => {
    const version = versions[i]?.data;
    const details = new Map((version?.tool_details ?? []).map((d) => [d.operation_id, d]));
    const operations = b.tools.length ? b.tools : (version?.tool_names ?? []);
    const name = backends.data?.find((x) => x.id === b.backend_profile_id)?.name ?? t.ui.names.deleted;
    return {
      id: b.backend_profile_id,
      title: server.backends.length > 1 ? `${name} v${b.spec_version}` : null,
      tools: operations.map((op) => ({
        key: op,
        name: b.tool_overrides[op]?.name || op,
        arguments: details.get(op)?.arguments ?? null,
        readOnlyDefault: methodReadOnly(details.get(op)?.method),
      })),
      rules: b.tool_rules ?? {},
    };
  });
  return (
    <ToolRulesCard
      kind="server"
      sections={sections}
      loading={versions.some((v) => v.isPending)}
      loadError={versions.find((v) => v.error)?.error ?? null}
      canEdit={hasScope(me, tenant, "mcp:write")}
      oidc={mode === "oidc"}
      agents={agents}
      version={server.revision}
      notes={[text.callersHint, text.agentHint, text.namespaceHint]}
      footer={t.mcpServers.form.revisionNotice(server.revision + 1)}
      save={async (rules) => {
        const saved = await putMcpServer(server.id, {
          name: server.name,
          display_name: server.display_name,
          backends: server.backends.map((b) => ({ ...b, tool_rules: rules[b.backend_profile_id] ?? {} })),
          runtime: server.runtime,
          bindings: server.bindings,
        });
        return saved.rules_notice;
      }}
      onSaved={() => queryClient.invalidateQueries({ queryKey: ["mcp-servers", tenant, server.id] })}
    />
  );
}

export function GatewayToolRules({ gateway }: { gateway: Gateway }) {
  const t = useT();
  const text = t.toolRules;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const auth = useAuthState();
  const { me, mode } = auth;
  const oidc = mode === "oidc";
  // 아직 IdP 토큰만 받는 oidc 모드의 Gateway 규칙은 Agent 를 받지 않는다(P53b 6.1절). 그때도 전에 저장된 Agent 를 이름으로
  // 보이려고 읽는다
  const gatewayAgents = !idpDataPlane(auth);
  const agents = useTenantAgents();
  const hub = useQuery({ queryKey: hubItemsKey(tenant), queryFn: listHubItems });
  const sections: RuleSection[] = gateway.backends.map((b) => {
    const item = hub.data?.find((h) => h.id === b.hub_item_id);
    const names = b.tools?.length ? b.tools : (item?.tool_names ?? []);
    return {
      id: b.hub_item_id,
      title: item ? displayName(item) : t.ui.names.deleted,
      tools: names.map((name) => ({ key: name, name })),
      rules: b.tool_rules ?? {},
    };
  });
  return (
    <ToolRulesCard
      kind="gateway"
      sections={sections}
      loading={hub.isPending}
      canEdit={hasScope(me, tenant, "gateway:write")}
      oidc={oidc}
      gatewayAgents={gatewayAgents}
      agents={agents}
      version={gateway.revision}
      notes={[text.callersHint, ...(gatewayAgents ? [text.agentHint] : []), text.gatewayHint, ...(PINNED_LIST_REFUSES_CALLS ? [text.gatewayPinHint] : [])]}
      footer={t.gateways.form.revisionNotice(gateway.revision + 1)}
      save={async (rules) => {
        const saved = await putGateway(gateway.id, {
          name: gateway.name,
          display_name: gateway.display_name,
          backends: gateway.backends.map((b) => ({ ...b, tool_rules: rules[b.hub_item_id] ?? {} })),
          conflict_resolution: gateway.conflict_resolution as ConflictResolution,
          failure_mode: gateway.failure_mode as FailureMode,
          authz_policy: gateway.authz_policy,
          runtime: { replicas: gateway.runtime.replicas, resources: gateway.runtime.resources },
        });
        return saved.rules_notice;
      }}
      onSaved={() => queryClient.invalidateQueries({ queryKey: ["gateways", tenant, gateway.id] })}
    />
  );
}

/** image 항목만 규칙을 든다. 다른 종류면 아무것도 그리지 않는다. */
export function ItemToolRules({ item }: { item: HubItem }) {
  const t = useT();
  const text = t.toolRules;
  const tenant = useTenant();
  const queryClient = useQueryClient();
  const { mode } = useAuthState();
  if (item.kind !== "image") return null;
  const names = item.source.tools_filter ?? item.tool_names;
  const sections: RuleSection[] = [
    { id: item.id, title: null, tools: names.map((name) => ({ key: name, name, readOnlyDefault: false })), rules: item.tool_rules ?? {} },
  ];
  return (
    <ToolRulesCard
      kind="item"
      sections={sections}
      canEdit={item.permissions.manage}
      oidc={mode === "oidc"}
      agents={[]}
      version={item.updated_at ?? ""}
      notes={[text.itemHint, ...(PINNED_LIST_REFUSES_CALLS ? [text.itemPinHint] : [])]}
      save={async (rules) => {
        const saved = await setHubToolRules(item.id, rules[item.id] ?? {});
        queryClient.setQueryData(hubItemKey(tenant, item.id), saved);
        return saved.rules_notice;
      }}
      onSaved={() => queryClient.invalidateQueries({ queryKey: hubItemsKey(tenant) })}
    />
  );
}
