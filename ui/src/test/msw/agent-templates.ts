/**
 * Agent 템플릿과 그 빈 자리를 채운 그래프, 그래프 검증의 목 문맥 (D26, D28, D64). agents.ts 의 픽스처·validate 핸들러와
 * 그래프 검증 테스트가 같이 쓴다. 템플릿은 서버 services/agents.py 의 _SUPPORT_AGENT 와 같은 모양이고 D28 의 model 노드가
 * 있다.
 */
import type { AgentTemplate } from "@/api/types";
import type { Graph, ValidationContext } from "@/lib/agentGraph";
import { pinnedSkills } from "@/lib/agentSkills";

import { platformSettings, tenantSettings } from "./auth";
import { dsPostgres, dsRedis } from "./data-services";
import { gatewayState } from "./gateways";
import { knowledgeState } from "./knowledge";
import { mcpServerState } from "./mcp-servers";
import { skillState } from "./skills";

export const agentTemplateSupport: AgentTemplate = {
  id: "support-agent",
  display_name: "단일 상담 에이전트",
  description: "입구, 메인 에이전트 하나와 모델 하나, Gateway Tool 소스 하나(나눗셈은 승인 뒤 실행), Postgres 메모리, 입력 가드레일 하나로 이뤄진 상담 에이전트다. provider 와 Tool 소스, Database 를 선택하면 바로 배포할 수 있다.",
  graph: {
    nodes: [
      {
        id: "entry",
        type: "entry",
        data: { session_ttl_seconds: 3600, max_turns: 50, rate_limit: { per_user_per_minute: 20 }, max_concurrent_sessions: 100, max_input_chars: 4000, greeting: "안녕하세요. 무엇을 도와드릴까요?" },
      },
      {
        id: "agent-main",
        type: "agent",
        data: {
          role: "main",
          name: "Agent",
          description: "고객 상담을 맡는 메인 에이전트다.",
          system_prompt: "너는 고객 상담 에이전트다. 계산이 필요하면 Tool 을 쓰고, 나눗셈은 승인을 받은 뒤에만 실행한다. 모르는 것은 모른다고 답한다.",
          context: { compaction: { trigger: { fraction: 0.85 }, keep: { fraction: 0.1 } } },
          max_tool_calls_per_turn: 8,
          turn_timeout_seconds: 120,
          retries: 2,
          response_format: "text",
          language: "ko",
          builtin_tools: { filesystem: true },
        },
      },
      {
        id: "model-main",
        type: "model",
        data: { name: "기본 모델", provider: "", model: null, max_output_tokens: 2048, reasoning: { effort: "medium" }, temperature: null, fallback: null },
      },
      // source.kind 는 저장되는 그래프의 값이라 서버가 옛 이름을 그대로 둔다 (D42)
      { id: "tools-calculator", type: "tools", data: { source: { kind: "gateway_instance", id: "" }, allow_tools: ["Add", "Divide"], approval: { Divide: ["approve", "reject"] }, timeouts: { Divide: 30 } } },
      { id: "memory", type: "memory", data: { checkpointer: { data_service_id: "" } } },
      {
        id: "guardrail-input",
        type: "guardrail",
        data: {
          position: "input",
          rules: [
            { kind: "max_length", value: 4000 },
            { kind: "pii_mask", value: { types: ["credit_card"] } },
          ],
          on_violation: "block",
          message: "카드 번호 같은 민감 정보는 보낼 수 없다. 지우고 다시 보내 달라.",
        },
      },
    ],
    edges: [
      { id: "e-entry", source: "entry", target: "agent-main", kind: "entry" },
      { id: "e-model", source: "agent-main", target: "model-main", kind: "model" },
      { id: "e-tools", source: "agent-main", target: "tools-calculator", kind: "tools" },
      { id: "e-memory", source: "agent-main", target: "memory", kind: "memory" },
      { id: "e-guardrail", source: "agent-main", target: "guardrail-input", kind: "guardrail" },
    ],
  },
  // 포트 방향(D28)을 따른 자리: 메모리·모델은 에이전트 위(왼쪽·오른쪽), 도구는 오른쪽, 가드레일은 입구 아래
  layout: { entry: { x: 80, y: 220 }, "agent-main": { x: 400, y: 220 }, "model-main": { x: 544, y: 44 }, "tools-calculator": { x: 760, y: 220 }, memory: { x: 256, y: 44 }, "guardrail-input": { x: 400, y: 420 } },
  service: { budget: { session_tokens: 200000, daily_tokens: null }, observability: { record_bodies: false }, evaluation: null },
};

/**
 * 템플릿의 빈 자리를 채운 그래프. provider·도구 소스·Database 가 목 픽스처를 가리킨다. 서버 fixture 와 같은 모양이라
 * 그래프 검증 테스트도 이것에서 출발한다.
 */
export function filledTemplateGraph(): Graph {
  const g = JSON.parse(JSON.stringify(agentTemplateSupport.graph)) as Graph;
  for (const n of g.nodes) {
    if (n.id === "model-main") (n.data as { provider: string }).provider = "openai-main";
    if (n.id === "tools-calculator") (n.data as { source: { id: string } }).source.id = "gw-support";
    if (n.id === "memory") (n.data as { checkpointer: { data_service_id: string } }).checkpointer.data_service_id = "ds-pg";
  }
  return g;
}

/** 검증 문맥. 서버 GraphContext 와 같은 재료를 목 픽스처에서 모은다. */
export function mockValidationContext(): ValidationContext {
  return {
    providers: platformSettings.llm_providers,
    oidcMode: false,
    agentOauth: tenantSettings.agent_oauth,
    agentClientIds: tenantSettings.agent_client_ids,
    // 도구 소스는 배포된 것만 쓸 수 있다. id 와 함께 상태를 넘긴다 (D42)
    gateways: gatewayState.items.map((g) => ({ id: g.id, state: g.state })),
    mcpServers: mcpServerState.items.map((m) => ({ id: m.id, state: m.state })),
    dataServices: [dsPostgres, dsRedis].map((d) => ({ id: d.id, name: d.name, kind: d.kind, capabilities: d.capabilities })),
    knowledgeBases: knowledgeState.items,
    // 목은 리비전을 모두 들고 있으므로 뒤처진 핀의 리비전도 한 번에 넘긴다
    skillRevisions: pinnedSkills(skillState.items, Object.values(skillState.revisions).flat()),
  };
}
