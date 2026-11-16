/**
 * 캔버스와 서버가 같은 그래프를 같이 거부하는지 보는 묶음의 캔버스 쪽이다 (P77, D89).
 *
 * 묶음은 `platform/tests/fixtures/agent-graph-corpus.json` 하나이고 서버 쪽은 `platform/tests/test_agent_graph_corpus.py` 가
 * 같은 파일을 `validate` 로 돌린다. 여기는 중립 문맥을 목록이 다 읽힌 `ValidationContext` 로 바꿔 `validateGraph` 를 돌리고,
 * 이슈를 문장 없이 (code, level, node_id, edge_id) 로 견준다. `server_only` 케이스는 이유를 두고 건너뛴다.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { LLMProvider } from "@/api/types";
import { gatewayNeedsAgentOauth, validateGraph, type Graph, type GraphNode, type ServiceSettings, type ValidationContext } from "@/lib/agentGraph";

import { kbFaq } from "./msw/knowledge";

interface IssueLike {
  code: string;
  level?: string;
  node_id?: string | null;
  edge_id?: string | null;
}

interface GraphPatch {
  add_nodes?: { id: string; type: string; data?: Record<string, unknown> }[];
  add_edges?: Graph["edges"];
  drop_nodes?: string[];
  drop_edges?: string[];
  data?: Record<string, Record<string, unknown>>;
}

interface NeutralContext {
  providers: { name: string; kind: string; has_key: boolean; secret_set?: boolean }[];
  auth_mode: string;
  agent_oauth: { client_id: string } | null;
  agent_client_ids: string[];
  gateways: { id: string; state: string }[];
  mcp_servers: { id: string; state: string }[];
  data_services: { id: string; name: string; kind: string; db?: number | null; capabilities?: Record<string, string> }[];
  knowledge_bases: { id: string; name: string; provider: string; indexed?: number; failed?: number; deleting?: boolean }[];
  skill_revisions: { id: string; name: string; revision: number; latest: number; bytes: number; scripts: boolean }[];
}

interface Case {
  name: string;
  server_only?: string;
  graph?: GraphPatch;
  service?: Partial<ServiceSettings>;
  context?: Partial<NeutralContext>;
  expect: IssueLike[];
}

interface Corpus {
  defaults: Record<string, Record<string, unknown>>;
  base: { nodes: { id: string; type: string; data?: Record<string, unknown> }[]; edges: Graph["edges"] };
  base_service: ServiceSettings;
  base_context: NeutralContext;
  cases: Case[];
}

const CORPUS = JSON.parse(readFileSync(resolve(import.meta.dirname, "../../../platform/tests/fixtures/agent-graph-corpus.json"), "utf8")) as Corpus;

/** base 를 케이스의 graph 로 고친다. 순서와 규칙은 묶음 파일의 about 이 정한다(서버 쪽 build_graph 와 같다). */
function buildGraph(patch: GraphPatch = {}): Graph {
  const dropped = new Set(patch.drop_nodes ?? []);
  const droppedEdges = new Set(patch.drop_edges ?? []);
  const nodes = [...CORPUS.base.nodes, ...(patch.add_nodes ?? [])].filter((n) => !dropped.has(n.id));
  const edges = [...CORPUS.base.edges, ...(patch.add_edges ?? [])].filter((e) => !dropped.has(e.source) && !dropped.has(e.target) && !droppedEdges.has(e.id));
  return {
    nodes: nodes.map((n) => ({ id: n.id, type: n.type, data: structuredClone({ ...CORPUS.defaults[n.type], ...n.data, ...patch.data?.[n.id] }) }) as unknown as GraphNode),
    edges: structuredClone(edges),
  };
}

/** 중립 문맥을 목록이 다 읽힌 캔버스 문맥으로 옮긴다. 키 존재는 api_key_secret 이 정해졌는지로 옮긴다. */
function buildContext(ctx: NeutralContext): ValidationContext {
  return {
    providers: ctx.providers.map((p) => ({ name: p.name, kind: p.kind, api_key_secret: (p.secret_set ?? p.has_key) ? "llm-key" : null }) as unknown as LLMProvider),
    oidcMode: gatewayNeedsAgentOauth(ctx.auth_mode),
    agentOauth: ctx.agent_oauth,
    agentClientIds: ctx.agent_client_ids,
    gateways: ctx.gateways,
    mcpServers: ctx.mcp_servers,
    dataServices: ctx.data_services,
    // 목 픽스처 kbFaq 위에 케이스가 정하는 칸만 덮는다. 검사가 읽는 것은 id·이름·임베딩 provider·status·deleting_at 이다
    knowledgeBases: ctx.knowledge_bases.map((k) => ({
      ...kbFaq,
      id: k.id,
      name: k.name,
      embedding: { ...kbFaq.embedding, provider: k.provider },
      status: { ...kbFaq.status, indexed: k.indexed ?? 0, failed: k.failed ?? 0 },
      deleting_at: k.deleting ? "2026-10-16T00:00:00Z" : null,
    })),
    skillRevisions: ctx.skill_revisions,
  };
}

function key(i: IssueLike): string {
  return JSON.stringify([i.code, i.level ?? "error", i.node_id ?? null, i.edge_id ?? null]);
}

describe("agent graph corpus (the canvas side of P77)", () => {
  const cases = CORPUS.cases.filter((c) => !c.server_only);

  it("reads the shared corpus", () => {
    expect(cases.length).toBeGreaterThan(20);
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const service = { ...CORPUS.base_service, ...c.service };
    const issues = validateGraph(buildGraph(c.graph), service, buildContext({ ...CORPUS.base_context, ...c.context }));
    expect(issues.map(key).sort()).toEqual(c.expect.map(key).sort());
  });
});
