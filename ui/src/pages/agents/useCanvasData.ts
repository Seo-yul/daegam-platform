/**
 * 캔버스가 읽는 쿼리와, 거기서 나오는 어댑터 셋(인스펙터 옵션·검증 문맥·이름 조회).
 * 화면은 이것을 한 줄로 받아 쓰고, "무엇을 읽고 무엇을 넘기는가" 는 여기서만 본다.
 *
 * 도구 소스 목록이 넷에서 둘로 줄었다 (D42). 옛 모델에서는 Gateway Instance·MCP Instance 를 고르고 그 도구를
 * 알려면 MCP Profile 을 한 번 더 읽어야 했는데, 이제 문서 하나가 정의와 배포를 함께 들어 목록 둘이면 된다.
 *
 * Skill 은 목록 하나가 최신 리비전의 메타를 준다(D64). 최신보다 낮게 고정한 핀은 크기와 스크립트 여부가 다를 수 있어
 * 그 Skill 의 리비전 목록을 더 읽는다. 그래서 이 훅은 편집 중인 그래프를 받는다.
 */
import { useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useMemo } from "react";

import {
  getAgent,
  getAuthConfig,
  getClusterSandbox,
  getPlatformSettings,
  getTenantSettings,
  listAgentTemplates,
  listDataServices,
  listGateways,
  listKnowledgeBases,
  listMcpServers,
} from "@/api/resources";
import { listSkillRevisions, listSkills } from "@/api/skills";
import type { Agent, AgentTemplate, SkillRevisionMeta } from "@/api/types";
import type { InspectorOptions } from "@/components/canvas/Inspector";
import type { CanvasLookup } from "@/components/canvas/types";
import { useT } from "@/i18n";
import { SOURCE_KINDS, gatewayNeedsAgentOauth, type Graph, type ValidationContext } from "@/lib/agentGraph";
import { behindSkillIds, pinnedSkills, skillInfo } from "@/lib/agentSkills";
import { gateReason } from "@/lib/codeExecution";
import { displayName } from "@/lib/names";
import { AUTH_CONFIG_KEY, PLATFORM_SETTINGS_KEY, dataServicesKey, knowledgeBasesKey, skillRevisionsKey, skillsKey, tenantSettingsKey } from "@/lib/queryKeys";

export interface CanvasData {
  /** 인스펙터의 셀렉트가 고르는 것. */
  options: InspectorOptions;
  /** 클라이언트 검증이 서버와 같은 규칙을 돌리는 데 쓰는 문맥. */
  ctx: ValidationContext;
  /**
   * 그 문맥을 이루는 쿼리들이 다 왔는가. 아직이거나 실패했으면 목록이 비어 있는 것이지 참조가 지워진 것이
   * 아니므로, 화면은 그 종류의 검증을 보류하고 못 읽었다는 것을 말한다 (저장은 서버가 다시 검증한다).
   * `failed` 는 실패한 조회의 화면 이름이고 `error` 는 그중 첫 오류다. 화면은 이 둘로 무엇을 왜 못 읽었는지 말한다.
   */
  context: { ready: boolean; error: unknown; failed: string[] };
  /** 노드 카드·인스펙터가 id 를 이름으로 푸는 조회. */
  lookup: CanvasLookup;
  /** 템플릿. 못 읽은 것과 하나도 없는 것을 팔레트가 갈라 말할 수 있게 상태를 함께 낸다. */
  templates: { list: AgentTemplate[]; isLoading: boolean; error: unknown };
  agent: {
    data: Agent | undefined;
    isLoading: boolean;
    error: unknown;
  };
}

/** 리비전 목록 쿼리들을 하나로 모은다. 모듈 함수라 useQueries 가 결과가 바뀔 때만 다시 부르고 같은 배열을 돌려준다. */
function combineRevisions(results: UseQueryResult<SkillRevisionMeta[]>[]) {
  return { results, metas: results.flatMap((r) => r.data ?? []) };
}

export function useCanvasData(tenant: string, id: string | undefined, graph: Graph): CanvasData {
  const t = useT();
  const isNew = !id;
  const agent = useQuery({ queryKey: ["agents", tenant, id], queryFn: () => getAgent(id as string), enabled: !isNew });
  const templates = useQuery({ queryKey: ["agent-templates"], queryFn: listAgentTemplates });
  const settings = useQuery({ queryKey: PLATFORM_SETTINGS_KEY, queryFn: getPlatformSettings });
  const authConfig = useQuery({ queryKey: AUTH_CONFIG_KEY, queryFn: getAuthConfig });
  const tenantSettings = useQuery({ queryKey: tenantSettingsKey(tenant), queryFn: () => getTenantSettings(tenant) });
  const gateways = useQuery({ queryKey: ["gateways", tenant], queryFn: listGateways });
  const mcpServers = useQuery({ queryKey: ["mcp-servers", tenant], queryFn: listMcpServers });
  const dataServices = useQuery({ queryKey: dataServicesKey(tenant), queryFn: listDataServices });
  const knowledgeBases = useQuery({ queryKey: knowledgeBasesKey(tenant), queryFn: listKnowledgeBases });
  const skills = useQuery({ queryKey: skillsKey(tenant), queryFn: listSkills });
  // 배포된 Agent 의 클러스터 코드 실행 게이트(P40 8절). 닫혔으면 메인 에이전트의 코드 실행 칸이 그 까닭을 말한다. 배포 전에는
  // 클러스터를 모르므로 읽지 않고, 배포가 409 로 까닭을 말한다
  const clusterId = agent.data?.cluster_id ?? null;
  const sandboxGate = useQuery({
    queryKey: ["clusters", clusterId, "sandbox"],
    queryFn: () => getClusterSandbox(clusterId as string),
    enabled: Boolean(clusterId),
    retry: false,
  });
  // 뒤처진 핀이 있는 Skill 의 리비전 목록. Skill 상세의 리비전 탭과 같은 키라 캐시를 함께 쓴다
  const behind = behindSkillIds(graph, skills.data ?? []);
  const revisions = useQueries({ queries: behind.map((sid) => ({ queryKey: skillRevisionsKey(tenant, sid), queryFn: () => listSkillRevisions(sid) })), combine: combineRevisions });

  // 이름을 푸는 목록들. 하나라도 아직이면 아래 조회가 돌려주는 null 은 "없다" 가 아니라 "아직 모른다" 다.
  // 화면 이름을 함께 둔다. 실패했을 때 화면이 어느 것을 못 읽었는지 말하는 데 쓴다
  const nameQueries = [
    { label: "Platform Settings", query: settings },
    { label: "Gateway", query: gateways },
    { label: "MCP Server", query: mcpServers },
    { label: "Database", query: dataServices },
    { label: "Knowledge Base", query: knowledgeBases },
    { label: "Skill", query: skills },
  ];
  const lookupReady = nameQueries.every(({ query }) => query.isSuccess);

  const options: InspectorOptions = useMemo(
    () => ({
      providers: settings.data?.llm_providers ?? [],
      gateways: gateways.data ?? [],
      mcpServers: mcpServers.data ?? [],
      dataServices: dataServices.data ?? [],
      knowledgeBases: knowledgeBases.data ?? [],
      skills: skills.data ?? [],
      ready: lookupReady,
      codeExecutionGate: sandboxGate.data && !sandboxGate.data.gate.open ? gateReason(sandboxGate.data.gate.reason) : null,
    }),
    [settings.data, gateways.data, mcpServers.data, dataServices.data, knowledgeBases.data, skills.data, lookupReady, sandboxGate.data],
  );

  const ctx: ValidationContext = useMemo(
    () => ({
      providers: options.providers,
      oidcMode: gatewayNeedsAgentOauth(authConfig.data?.auth_mode),
      agentOauth: tenantSettings.data?.agent_oauth ?? null,
      agentClientIds: tenantSettings.data?.agent_client_ids ?? [],
      // 상태를 함께 넘긴다. 검증이 "있는가" 와 "배포됐는가" 를 따로 보기 때문이다 (D42)
      gateways: options.gateways.map((g) => ({ id: g.id, state: g.state })),
      mcpServers: options.mcpServers.map((m) => ({ id: m.id, state: m.state })),
      dataServices: options.dataServices.map((d) => ({ id: d.id, name: d.name, kind: d.kind, db: d.db, capabilities: d.capabilities })),
      knowledgeBases: options.knowledgeBases,
      skillRevisions: pinnedSkills(options.skills, revisions.metas),
    }),
    [options, authConfig.data, tenantSettings.data, revisions.metas],
  );

  const lookup: CanvasLookup = useMemo(
    () => ({
      ready: lookupReady,
      providerKind: (name) => options.providers.find((p) => p.name === name)?.kind ?? null,
      providerModel: (name) => options.providers.find((p) => p.name === name)?.model ?? null,
      sourceName: (kind, sid) => {
        const x = options[SOURCE_KINDS[kind].listKey].find((s) => s.id === sid);
        return x ? displayName(x) : null;
      },
      dataServiceName: (dsId) => {
        const d = options.dataServices.find((x) => x.id === dsId);
        return d ? displayName(d) : null;
      },
      dataServiceKind: (dsId) => options.dataServices.find((x) => x.id === dsId)?.kind ?? null,
      knowledgeBaseName: (kbId) => {
        const k = options.knowledgeBases.find((x) => x.id === kbId);
        return k ? displayName(k) : null;
      },
      knowledgeBaseStatus: (kbId) => {
        const k = options.knowledgeBases.find((x) => x.id === kbId);
        return k ? { indexed: k.status.indexed, documents: k.status.documents } : null;
      },
      skill: (sid) => skillInfo(options.skills, sid),
    }),
    [options, lookupReady],
  );

  // 검증 문맥을 이루는 쿼리들. 하나라도 아직이거나 실패했으면 "참조가 없다" 가 아니라 "아직 모른다" 다.
  // 실패한 것은 그것만 이름으로 모은다. 목록 다섯을 늘 한꺼번에 탓하면 Tenant Settings 가 실패했을 때도
  // 멀쩡히 읽은 목록을 못 읽었다고 말하게 된다
  const contextQueries = [...nameQueries, { label: "Tenant Settings", query: tenantSettings }, ...revisions.results.map((query) => ({ label: t.agents.canvasPage.skillRevisions, query }))];
  const failed = contextQueries.filter(({ query }) => query.isError);
  const context = {
    ready: contextQueries.every(({ query }) => query.isSuccess),
    error: failed[0]?.query.error ?? null,
    failed: [...new Set(failed.map(({ label }) => label))],
  };

  return {
    options,
    ctx,
    context,
    lookup,
    templates: { list: templates.data ?? [], isLoading: templates.isLoading, error: templates.error },
    agent: { data: agent.data, isLoading: agent.isLoading, error: agent.error },
  };
}
