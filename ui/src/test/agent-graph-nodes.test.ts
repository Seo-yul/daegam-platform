import {
  DEFAULT_SERVICE,
  canonicalJson,
  checkNodeData,
  defaultNodeData,
  fromTemplate,
  longTermEnabled,
  nodePorts,
  validateGraph,
  type AgentData,
  type Graph,
  type KnowledgeData,
  type MemoryData,
  type NodeType,
  type PlacedEdge,
  type PlacedNode,
  type ValidationContext,
} from "@/lib/agentGraph";
import { autoLayout, NODE_HEIGHT, NODE_WIDTH } from "@/components/canvas/autoLayout";
import { toCanvas } from "@/components/canvas/types";

import { codes, connectionHintOf, textOf } from "./graph";
import { agentTemplateSupport, filledTemplateGraph, mockValidationContext } from "./msw";

describe("long-term memory on the memory node (D34)", () => {
  const ctx: ValidationContext = mockValidationContext();
  /** 채운 템플릿에서 memory·메인 에이전트의 data 를 함께 집는다. */
  const parts = (g: Graph) => ({
    memory: g.nodes.find((n) => n.id === "memory")!.data as MemoryData,
    main: g.nodes.find((n) => n.id === "agent-main")!.data as AgentData,
  });

  it("꺼져 있으면 저장 본문이 지금과 바이트로 같다 (키를 아예 두지 않는다)", () => {
    // 기본값에는 키가 없다. 새 memory 노드를 놓기만 한 프로필은 지금 저장하던 것과 같은 본문이다
    expect(defaultNodeData("memory")).toEqual({ checkpointer: { data_service_id: "" } });
    expect(Object.keys(defaultNodeData("memory"))).toEqual(["checkpointer"]);
    expect(longTermEnabled(defaultNodeData("memory"))).toBe(false);

    // 저장 본문은 이 그래프를 JSON 으로 실은 것이다. 켰다 끄면(인스펙터가 키를 지운다) 글자 하나까지 같다
    const g = filledTemplateGraph();
    const before = JSON.stringify(g);
    const { memory } = parts(g);
    memory.long_term = { retention_days: 30 };
    expect(JSON.stringify(g)).not.toBe(before);
    memory.long_term = undefined;
    expect(JSON.stringify(g)).toBe(before);
    expect(canonicalJson(g)).toBe(canonicalJson(filledTemplateGraph()));
  });

  it("checkNodeData 가 long_term 을 서버 pydantic 과 같은 문장으로 본다", () => {
    expect(checkNodeData("memory", { checkpointer: { data_service_id: "ds-pg" }, long_term: { retention_days: null } })).toBeNull();
    expect(checkNodeData("memory", { checkpointer: {}, long_term: { retention_days: 30 } })).toBeNull();
    expect(checkNodeData("memory", { long_term: 5 })).toBe("long_term: Input should be a valid dictionary or instance of LongTerm");
    expect(checkNodeData("memory", { long_term: { root: "/memories" } })).toBe("long_term.root: Extra inputs are not permitted");
    expect(checkNodeData("memory", { long_term: { enabled: true } })).toBe("long_term.enabled: Extra inputs are not permitted");
    expect(checkNodeData("memory", { long_term: { retention_days: 0 } })).toBe("long_term.retention_days: Input should be greater than or equal to 1");
    expect(checkNodeData("memory", { long_term: { retention_days: 1.5 } })).toBe("long_term.retention_days: Input should be a valid integer, got a number with a fractional part");
  });

  it("켜면 붙은 저장소와 메인 에이전트의 파일 도구가 함께 있어야 한다", () => {
    const g = filledTemplateGraph();
    const { memory, main } = parts(g);
    memory.long_term = { retention_days: null };
    // 파일 도구는 기본이 노출이라 템플릿 그대로면 통과한다
    expect(validateGraph(g, DEFAULT_SERVICE, ctx).filter((i) => i.level === "error")).toEqual([]);

    // 끄면 /memories/ 를 읽고 쓸 도구가 없으므로 오류다
    main.builtin_tools = { filesystem: false };
    const fs = validateGraph(g, DEFAULT_SERVICE, ctx).find((i) => i.code === "long_term_filesystem_disabled");
    expect(fs?.level).toBe("error");
    expect(fs?.node_id).toBe("memory");
    expect(textOf(fs)).toContain("내장 파일 Tool 노출");
    expect(textOf(fs)).toContain("(파일 Tool 이 꺼진 에이전트: Agent)");
    main.builtin_tools = { filesystem: true };

    // 메모리 간선을 지우면 컴파일된 문서에 저장소가 없다 (런타임의 long_term_memory_missing 자리)
    const detached: Graph = { nodes: g.nodes, edges: g.edges.filter((e) => e.kind !== "memory") };
    const needs = validateGraph(detached, DEFAULT_SERVICE, ctx).find((i) => i.code === "long_term_needs_memory");
    expect(needs?.level).toBe("error");
    expect(needs?.node_id).toBe("memory");
    expect(textOf(needs)).toContain("메모리 포트에서 이 메모리 노드로 이어 주세요");

    // 두 조건은 서로 독립이다. 떼어 놓고 파일 도구까지 끄면 서버(_check_long_term)처럼 두 표식이 함께 붙는다
    main.builtin_tools = { filesystem: false };
    const both = validateGraph(detached, DEFAULT_SERVICE, ctx).filter((i) => i.node_id === "memory" && i.code.startsWith("long_term_"));
    expect(codes(both)).toEqual(["long_term_needs_memory", "long_term_filesystem_disabled"]);
    main.builtin_tools = { filesystem: true };

    // 꺼 두면 둘 다 나지 않는다 (지금 프로필의 판정이 달라지지 않는다)
    memory.long_term = undefined;
    main.builtin_tools = { filesystem: false };
    expect(codes(validateGraph(g, DEFAULT_SERVICE, ctx))).not.toContain("long_term_filesystem_disabled");
    expect(codes(validateGraph(detached, DEFAULT_SERVICE, ctx))).not.toContain("long_term_needs_memory");
  });
});

describe("knowledge nodes (D32, same codes as the server)", () => {
  const ctx: ValidationContext = mockValidationContext();
  /** 채운 템플릿 + 지식 노드 하나. attachTo 가 있으면 그 에이전트의 knowledge 간선으로 붙인다. */
  const withKnowledge = (over: Partial<KnowledgeData> = {}, id = "knowledge", attachTo: string | null = "agent-main"): Graph => {
    const g = filledTemplateGraph();
    g.nodes.push({ id, type: "knowledge", data: { ...defaultNodeData("knowledge"), ...over } });
    if (attachTo) g.edges.push({ id: `e-${attachTo}-${id}`, source: attachTo, target: id, kind: "knowledge" });
    return g;
  };

  it("defaults, ports and edge kind match the server", () => {
    expect(defaultNodeData("knowledge")).toEqual({ knowledge_base_id: "", tool_name: "search_knowledge", description: "", top_k: 5, min_score: null });
    const right = nodePorts("agent", "main")
      .filter((p) => p.side === "right")
      .map((p) => [p.kind, p.at]);
    expect(right).toEqual([
      ["tools", 0.28],
      ["knowledge", 0.5],
      ["delegate", 0.72],
    ]);
    // 서브 에이전트도 지식을 붙인다
    expect(nodePorts("agent", "sub").find((p) => p.kind === "knowledge")?.hidden).toBe(false);
  });

  it("checkNodeData mirrors KnowledgeData: extra=forbid, tool name pattern, top_k 1..20, min_score null or 0..1", () => {
    expect(checkNodeData("knowledge", defaultNodeData("knowledge"))).toBeNull();
    expect(checkNodeData("knowledge", {})).toBeNull();
    // 옛 예약 노드의 임의 키는 서버(extra=forbid)와 같이 거부한다
    expect(checkNodeData("knowledge", { anything: 1 })).toBe("anything: Extra inputs are not permitted");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), tool_name: "search-faq" })).toContain("tool_name: String should match pattern");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), tool_name: "" })).toContain("tool_name: String should match pattern");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), top_k: 0 })).toBe("top_k: Input should be greater than or equal to 1");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), top_k: 21 })).toBe("top_k: Input should be less than or equal to 20");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), top_k: 2.5 })).toBe("top_k: Input should be a valid integer, got a number with a fractional part");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), min_score: 1.5 })).toBe("min_score: Input should be less than or equal to 1");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), min_score: -0.1 })).toBe("min_score: Input should be greater than or equal to 0");
    expect(checkNodeData("knowledge", { ...defaultNodeData("knowledge"), min_score: 0.5 })).toBeNull();
  });

  it("unknown_knowledge_base (empty and unknown id), knowledge_base_deleting, and the embedding provider rules (key, kind) on the knowledge node", () => {
    const empty = validateGraph(withKnowledge(), DEFAULT_SERVICE, ctx);
    expect(empty.find((i) => i.code === "unknown_knowledge_base")).toMatchObject({ level: "error", node_id: "knowledge" });
    expect(textOf(empty.find((i) => i.code === "unknown_knowledge_base"))).toContain("Knowledge Base 를 정하지 못했습니다");
    expect(codes(empty)).not.toContain("reserved_node");
    expect(codes(empty)).not.toContain("unattached_node");
    const unknown = validateGraph(withKnowledge({ knowledge_base_id: "kb-nope" }), DEFAULT_SERVICE, ctx);
    expect(textOf(unknown.find((i) => i.code === "unknown_knowledge_base"))).toContain("이 테넌트에서 찾지 못한 Knowledge Base: kb-nope");
    // 삭제 중(orphaned_table 포함)은 오류이고 문서가 없으면 경고다
    const deleting = validateGraph(withKnowledge({ knowledge_base_id: "kb-orphan" }), DEFAULT_SERVICE, ctx);
    expect(deleting.find((i) => i.code === "knowledge_base_deleting")).toMatchObject({ level: "error", node_id: "knowledge" });
    expect(textOf(deleting.find((i) => i.code === "knowledge_base_deleting"))).toContain("지워지는 중인 Knowledge Base: old-notes");
    expect(deleting.find((i) => i.code === "knowledge_base_empty")).toMatchObject({ level: "warning", node_id: "knowledge" });
    // 임베딩 provider 의 키는 KB 의 embedding.provider 로 본다. 표식은 knowledge 노드다
    const noKey: ValidationContext = { ...ctx, providers: [{ name: "openai-main", kind: "openai", model: null, api_key_secret: null, base_url: null, stream_usage: true }] };
    const nk = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq" }), DEFAULT_SERVICE, noKey);
    expect(textOf(nk.find((i) => i.code === "provider_no_key" && i.node_id === "knowledge"))).toContain("Knowledge Base faq의 임베딩 provider 에 키가 없습니다: openai-main");
    const noProvider: ValidationContext = { ...ctx, providers: [] };
    const np = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq" }), DEFAULT_SERVICE, noProvider);
    expect(textOf(np.find((i) => i.code === "unknown_provider" && i.node_id === "knowledge"))).toContain("Knowledge Base faq의 임베딩 provider 를 찾지 못했습니다: openai-main");
    // 만든 뒤 임베딩 없는 kind 로 바뀐 provider 는 키가 있어도 오류다 (서버와 같은 코드)
    const switched: ValidationContext = { ...ctx, providers: [{ name: "openai-main", kind: "openrouter", model: null, api_key_secret: "k", base_url: null, stream_usage: true }] };
    const sw = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq" }), DEFAULT_SERVICE, switched);
    expect(sw.find((i) => i.code === "embedding_not_supported")).toMatchObject({ level: "error", node_id: "knowledge" });
    expect(codes(sw)).not.toContain("provider_no_key");
    // stub 은 키가 없어도 된다
    const stubKb: ValidationContext = {
      ...ctx,
      providers: [...ctx.providers, { name: "stub-e2e", kind: "stub", model: null, api_key_secret: null, base_url: null, stream_usage: true }],
      knowledgeBases: [{ ...ctx.knowledgeBases[0], id: "kb-stub", embedding: { provider: "stub-e2e", model: "stub", dimensions: 64 } }],
    };
    const st = validateGraph(withKnowledge({ knowledge_base_id: "kb-stub" }), DEFAULT_SERVICE, stubKb);
    expect(st.filter((i) => i.node_id === "knowledge" && i.level === "error")).toEqual([]);
  });

  it("reserved_tool_name, duplicate_tool_name on one agent, knowledge_base_failed warning, unattached_node warning, legacy data", () => {
    const reserved = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq", tool_name: "task" }), DEFAULT_SERVICE, ctx);
    expect(reserved.find((i) => i.code === "reserved_tool_name")).toMatchObject({ level: "error", node_id: "knowledge" });
    expect(textOf(reserved.find((i) => i.code === "reserved_tool_name"))).toContain("내장 Tool 과 겹치는 이름: task");
    // 구조화 출력 도구의 이름도 예약이다(런타임 stub.py 의 BUILTIN_TOOLS 와 같다)
    const structured = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq", tool_name: "structured_response" }), DEFAULT_SERVICE, ctx);
    expect(structured.find((i) => i.code === "reserved_tool_name")).toMatchObject({ level: "error", node_id: "knowledge" });
    expect(reserved.find((i) => i.code === "knowledge_base_failed")).toMatchObject({ level: "warning", node_id: "knowledge" });
    expect(textOf(reserved.find((i) => i.code === "knowledge_base_failed"))).toContain("실패 1건");

    // 같은 에이전트에 붙은 둘의 도구 이름이 같으면 뒤의 노드에 표식이다
    const dup = withKnowledge({ knowledge_base_id: "kb-faq", tool_name: "search_faq" });
    dup.nodes.push({ id: "knowledge-2", type: "knowledge", data: { ...defaultNodeData("knowledge"), knowledge_base_id: "kb-faq", tool_name: "search_faq" } });
    dup.edges.push({ id: "e-dup", source: "agent-main", target: "knowledge-2", kind: "knowledge" });
    const d = validateGraph(dup, DEFAULT_SERVICE, ctx);
    expect(d.find((i) => i.code === "duplicate_tool_name")).toMatchObject({ level: "error", node_id: "knowledge-2" });
    expect(textOf(d.find((i) => i.code === "duplicate_tool_name"))).toContain("겹치는 Tool 이름: search_faq, 먼저 쓴 노드: knowledge");

    // 다른 에이전트(서브)에 붙은 같은 이름은 된다
    const other = withKnowledge({ knowledge_base_id: "kb-faq", tool_name: "search_faq" });
    other.nodes.push({ id: "agent-sub", type: "agent", data: { ...defaultNodeData("agent", "sub"), name: "helper", description: "helps" } });
    other.edges.push({ id: "e-del", source: "agent-main", target: "agent-sub", kind: "delegate" });
    other.edges.push({ id: "e-sub-model", source: "agent-sub", target: "model-main", kind: "model" });
    other.nodes.push({ id: "knowledge-2", type: "knowledge", data: { ...defaultNodeData("knowledge"), knowledge_base_id: "kb-faq", tool_name: "search_faq" } });
    other.edges.push({ id: "e-sub-k", source: "agent-sub", target: "knowledge-2", kind: "knowledge" });
    const o = validateGraph(other, DEFAULT_SERVICE, ctx);
    expect(codes(o)).not.toContain("duplicate_tool_name");
    expect(codes(o)).not.toContain("invalid_edge_kind");
    expect(codes(o)).not.toContain("unattached_node");

    // 붙지 않은 지식 노드는 경고이고 나머지 규칙은 그대로 본다
    const loose = validateGraph(withKnowledge({ knowledge_base_id: "kb-faq" }, "knowledge", null), DEFAULT_SERVICE, ctx);
    expect(loose.find((i) => i.code === "unattached_node")).toMatchObject({ level: "warning", node_id: "knowledge" });
    expect(loose.find((i) => i.code === "knowledge_base_failed")).toBeTruthy();

    // 옛 예약 노드의 임의 data 는 invalid_node_data 이고 그 뒤 규칙은 건너뛴다
    const legacy = validateGraph(withKnowledge({ anything: 1 } as unknown as Partial<KnowledgeData>), DEFAULT_SERVICE, ctx);
    // 상세는 서버 pydantic 문장 그대로다 (저장이 거부됐을 때 받는 것과 같은 필드를 가리킨다)
    expect(textOf(legacy.find((i) => i.code === "invalid_node_data"))).toContain("(anything: Extra inputs are not permitted)");
    expect(codes(legacy)).not.toContain("unknown_knowledge_base");
  });

  it("connection hints: the knowledge port reaches knowledge nodes only, main and sub agents may attach several", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const byId = (id: string): PlacedNode => nodes.find((n) => n.id === id)!;
    const k: PlacedNode = { id: "knowledge", type: "knowledge", data: defaultNodeData("knowledge"), position: { x: 0, y: 0 } };
    expect(connectionHintOf(byId("agent-main"), k, edges, "knowledge")).toBeNull();
    expect(connectionHintOf(byId("agent-main"), byId("tools-calculator"), edges, "knowledge")).toBe("지식 포트는 MCP 에 연결할 수 없습니다.");
    expect(connectionHintOf(byId("agent-main"), k, edges, "tools")).toBe("Tool 포트는 지식에 연결할 수 없습니다.");
    expect(connectionHintOf(byId("entry"), k, edges)).toContain("연결할 수 없습니다");
    const sub: PlacedNode = { id: "s", type: "agent", data: { ...defaultNodeData("agent", "sub"), description: "x" }, position: { x: 0, y: 0 } };
    const withOne: PlacedEdge[] = [...edges, { id: "e1", source: "s", target: "knowledge", kind: "knowledge" }];
    expect(connectionHintOf(sub, { ...k, id: "knowledge-2" }, withOne, "knowledge")).toBeNull();
    expect(connectionHintOf(sub, k, withOne, "knowledge")).toBe("이미 연결돼 있습니다.");
  });

  it("auto layout keeps knowledge nodes in the right column with tools", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const k: PlacedNode = { id: "knowledge", type: "knowledge", data: defaultNodeData("knowledge"), position: { x: 0, y: 0 } };
    const placed = new Map(autoLayout(toCanvas([...nodes, k], [...edges, { id: "e-k", source: "agent-main", target: "knowledge", kind: "knowledge" }])).map((n) => [n.id, n.position]));
    expect(placed.get("agent-main")!.x + NODE_WIDTH).toBeLessThanOrEqual(placed.get("knowledge")!.x);
    expect(placed.get("model-main")!.y + NODE_HEIGHT).toBeLessThan(placed.get("agent-main")!.y);
  });
});

/**
 * `checkNodeData` 가 내는 문장이 서버와 글자까지 같은지 본다. 기대값은 저장소 HEAD 의
 * `platform/daegam/services/agent_graph.py` 의 *Data 모델을 실제로 `model_validate` 해서 받아 적은 것이다
 * (pydantic 의 첫 오류를 `loc: msg` 로 편 것이고, `agent_graph.py` 의 `_parse_nodes` 가 쓰는 방식과 같다).
 *
 * 이 표가 있는 이유는 `agentGraph.ts` 머리가 "메시지까지 서버와 같다" 고 약속하기 때문이다. 약속이 깨지면
 * 사용자는 편집 중에 본 문장과 저장 뒤에 받은 문장이 달라서 같은 문제를 두 번 만난다.
 * `platform/tests/test_agent_graph_parity.py` 는 노드 data 의 허용 키만 보므로 문장은 여기가 지킨다.
 */
describe("checkNodeData messages are the server's, word for word", () => {
  const CASES: [NodeType, unknown, string][] = [
    // 노드 data 자체가 dict 가 아니다
    ["agent", 5, "data: Input should be a valid dictionary or instance of AgentData"],
    // entry
    ["entry", { session_ttl_seconds: 0 }, "session_ttl_seconds: Input should be greater than or equal to 1"],
    ["entry", { max_turns: 1.5 }, "max_turns: Input should be a valid integer, got a number with a fractional part"],
    ["entry", { rate_limit: 5 }, "rate_limit: Input should be a valid dictionary or instance of RateLimit"],
    ["entry", { rate_limit: {} }, "rate_limit.per_user_per_minute: Field required"],
    ["entry", { rate_limit: { per_user_per_minute: 0 } }, "rate_limit.per_user_per_minute: Input should be greater than or equal to 1"],
    // agent: model_validator 갈래는 loc 이 모델 단위이고 `Value error, ` 가 붙는다
    ["agent", { role: "nope", name: "a" }, "role: Input should be 'main' or 'sub'"],
    ["agent", { name: "a" }, "role: Field required"],
    ["agent", { role: "main", name: 5 }, "name: Input should be a valid string"],
    ["agent", { role: "main", name: "a", model: "x" }, "model: Extra inputs are not permitted"],
    ["agent", { role: "main", name: "a", retries: -1 }, "retries: Input should be greater than or equal to 0"],
    ["agent", { role: "main", name: "a", context: 5 }, "context: Input should be a valid dictionary or instance of ContextData"],
    ["agent", { role: "main", name: "a", context: { compaction: 5 } }, "context.compaction: Input should be a valid dictionary or instance of Compaction"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: 5 } } }, "context.compaction.trigger: Input should be a valid dictionary or instance of CompactionTrigger"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: {} } } }, "context.compaction.trigger: Value error, trigger must have exactly one of tokens, messages, fraction"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: { tokens: 1, messages: 2 } } } }, "context.compaction.trigger: Value error, trigger must have exactly one of tokens, messages, fraction"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: { tokens: 0 } } } }, "context.compaction.trigger.tokens: Input should be greater than or equal to 1"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: { fraction: 2 } } } }, "context.compaction.trigger.fraction: Input should be less than or equal to 1"],
    ["agent", { role: "main", name: "a", context: { compaction: { trigger: { fraction: 0 } } } }, "context.compaction.trigger.fraction: Input should be greater than 0"],
    ["agent", { role: "main", name: "a", context: { compaction: { keep: 5 } } }, "context.compaction.keep: Input should be a valid dictionary or instance of CompactionKeep"],
    ["agent", { role: "main", name: "a", context: { compaction: { keep: {} } } }, "context.compaction.keep: Value error, keep must have exactly one of messages, fraction"],
    ["agent", { role: "main", name: "a", context: { compaction: { keep: { messages: 1, fraction: 0.5 } } } }, "context.compaction.keep: Value error, keep must have exactly one of messages, fraction"],
    ["agent", { role: "main", name: "a", context: { compaction: { keep: { messages: 0 } } } }, "context.compaction.keep.messages: Input should be greater than or equal to 1"],
    // agent.skills (D64): 항목의 필드가 모르는 키보다 먼저이고, 중복은 목록 단위의 AfterValidator 다
    ["agent", { role: "main", name: "a", skills: 5 }, "skills: Input should be a valid list"],
    ["agent", { role: "main", name: "a", skills: null }, "skills: Input should be a valid list"],
    ["agent", { role: "main", name: "a", skills: [5] }, "skills.0: Input should be a valid dictionary or instance of SkillPin"],
    ["agent", { role: "main", name: "a", skills: [{}] }, "skills.0.id: Field required"],
    ["agent", { role: "main", name: "a", skills: [{ id: "", revision: 1 }] }, "skills.0.id: String should have at least 1 character"],
    ["agent", { role: "main", name: "a", skills: [{ id: "s" }] }, "skills.0.revision: Field required"],
    ["agent", { role: "main", name: "a", skills: [{ id: "s", revision: 0 }] }, "skills.0.revision: Input should be greater than or equal to 1"],
    ["agent", { role: "main", name: "a", skills: [{ id: "s", revision: 1.5 }] }, "skills.0.revision: Input should be a valid integer, got a number with a fractional part"],
    ["agent", { role: "main", name: "a", skills: [{ revision: 1, x: 1 }] }, "skills.0.id: Field required"],
    ["agent", { role: "main", name: "a", skills: [{ id: "s", revision: 1, x: 1 }] }, "skills.0.x: Extra inputs are not permitted"],
    ["agent", { role: "main", name: "a", skills: [{ id: "s", revision: 1 }, { id: "s", revision: 2 }] }, "skills: Value error, skills contains duplicates"],
    // model
    ["model", { provider: "p" }, "name: Field required"],
    ["model", { name: "", provider: "p" }, "name: String should have at least 1 character"],
    ["model", { name: "m", reasoning: 5 }, "reasoning: Input should be a valid dictionary or instance of Reasoning"],
    ["model", { name: "m", reasoning: { effort: "nope" } }, "reasoning.effort: Input should be 'off', 'low', 'medium', 'high', 'xhigh' or 'max'"],
    ["model", { name: "m", temperature: -1 }, "temperature: Input should be greater than or equal to 0"],
    ["model", { name: "m", fallback: 5 }, "fallback: Input should be a valid dictionary or instance of Fallback"],
    ["model", { name: "m", fallback: {} }, "fallback.provider: Field required"],
    ["model", { name: "m", fallback: { provider: "" } }, "fallback.provider: String should have at least 1 character"],
    // tools: 필드 검사가 model_validator 보다 먼저다
    ["tools", {}, "source: Field required"],
    ["tools", { source: 5 }, "source: Input should be a valid dictionary or instance of ToolsSource"],
    ["tools", { source: { kind: "nope", id: "x" } }, "source.kind: Input should be 'gateway_instance' or 'mcp_instance'"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, allow_tools: "no" }, "allow_tools: Input should be a valid list"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, allow_tools: [5] }, "allow_tools.0: Input should be a valid string"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, allow_tools: ["a", ""] }, "data: Value error, allow_tools entries must not be empty"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, allow_tools: ["a", "a"] }, "data: Value error, allow_tools contains duplicates"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: 5 }, "approval: Input should be a valid dictionary"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: { t: "x" } }, "approval.t: Input should be a valid list"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: { t: ["nope"] } }, "approval.t.0: Input should be 'approve', 'edit', 'reject' or 'respond'"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: { "": ["approve"] } }, "data: Value error, approval needs a tool name and at least one decision"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: { t: [] } }, "data: Value error, approval needs a tool name and at least one decision"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, approval: { t: ["approve", "approve"] } }, "data: Value error, approval for 't' repeats a decision"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, timeouts: 5 }, "timeouts: Input should be a valid dictionary"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, timeouts: { t: 0 } }, "data: Value error, timeouts need a tool name and seconds >= 1"],
    ["tools", { source: { kind: "mcp_instance", id: "x" }, timeouts: { "": 3 } }, "data: Value error, timeouts need a tool name and seconds >= 1"],
    // 필드 오류가 model_validator 오류를 이긴다 (여기서는 timeouts 가 allow_tools 중복보다 먼저다)
    ["tools", { source: { kind: "mcp_instance", id: "x" }, allow_tools: ["a", "a"], timeouts: { t: "x" } }, "timeouts.t: Input should be a valid integer, unable to parse string as an integer"],
    // memory
    ["memory", { checkpointer: 5 }, "checkpointer: Input should be a valid dictionary or instance of Checkpointer"],
    ["memory", { nope: 1 }, "nope: Extra inputs are not permitted"],
    // guardrail: 규칙 종류별 검사는 model_validator 라 loc 이 rules.N 이다
    ["guardrail", { rules: [{ kind: "max_length", value: 1 }] }, "position: Field required"],
    ["guardrail", { position: "nope", rules: [{ kind: "max_length", value: 1 }] }, "position: Input should be 'input', 'output' or 'tool'"],
    ["guardrail", { position: "input" }, "rules: Field required"],
    ["guardrail", { position: "input", rules: "x" }, "rules: Input should be a valid list"],
    ["guardrail", { position: "input", rules: [] }, "rules: List should have at least 1 item after validation, not 0"],
    ["guardrail", { position: "input", rules: [5] }, "rules.0: Input should be a valid dictionary or instance of GuardrailRule"],
    ["guardrail", { position: "input", rules: [{ value: 1 }] }, "rules.0.kind: Field required"],
    ["guardrail", { position: "input", rules: [{ kind: "nope", value: 1 }] }, "rules.0.kind: Input should be 'max_length', 'deny_pattern', 'pii_mask' or 'injection_check'"],
    ["guardrail", { position: "input", rules: [{ kind: "max_length" }] }, "rules.0.value: Field required"],
    ["guardrail", { position: "input", rules: [{ kind: "max_length", value: 1, nope: 2 }] }, "rules.0.nope: Extra inputs are not permitted"],
    ["guardrail", { position: "input", rules: [{ kind: "max_length", value: 0 }] }, "rules.0: Value error, max_length value must be an integer >= 1"],
    ["guardrail", { position: "input", rules: [{ kind: "max_length", value: "x" }] }, "rules.0: Value error, max_length value must be an integer >= 1"],
    ["guardrail", { position: "input", rules: [{ kind: "deny_pattern", value: "" }] }, "rules.0: Value error, deny_pattern value must be a non-empty regex"],
    // 정규식 모양 검사(check_regex): 길이 상한이 먼저이고, 문법이 맞아도 겹친 반복은 거절한다
    ["guardrail", { position: "input", rules: [{ kind: "deny_pattern", value: "a".repeat(513) }] }, "rules.0: Value error, regex is longer than 512 characters"],
    [
      "guardrail",
      { position: "input", rules: [{ kind: "deny_pattern", value: "(a+)+" }] },
      "rules.0: Value error, regex repeats a group that itself contains a repetition, e.g. (a+)+; this backtracks exponentially on non-matching input. Rewrite it without nesting",
    ],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: 5 }] }, "rules.0: Input should be a valid dictionary or instance of PIIMask"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: {} }] }, "rules.0.types: Field required"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: "x" } }] }, "rules.0.types: Input should be a valid list"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: [] } }] }, "rules.0.types: List should have at least 1 item after validation, not 0"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["nope"] } }] }, "rules.0.types.0: Input should be 'email', 'credit_card', 'ip', 'mac_address', 'url' or 'regex'"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["email"], nope: 1 } }] }, "rules.0.nope: Extra inputs are not permitted"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["email"], regex: 5 } }] }, "rules.0.regex: Input should be a valid string"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["email", "email"] } }] }, "rules.0: Value error, types contains duplicates"],
    ["guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["regex"] } }] }, "rules.0: Value error, types has regex, so regex is required"],
    ["guardrail", { position: "input", rules: [{ kind: "max_length", value: 1 }], on_violation: "nope" }, "on_violation: Input should be 'block' or 'flag'"],
    // injection_check: 서버 InjectionCheck 와 GuardrailData._check (P43 7절)
    ["guardrail", { position: "tool", rules: [{ kind: "injection_check", value: 5 }] }, "rules.0: Input should be a valid dictionary or instance of InjectionCheck"],
    ["guardrail", { position: "tool", rules: [{ kind: "injection_check", value: { model: null } }] }, "rules.0.model: Input should be a valid string"],
    ["guardrail", { position: "tool", rules: [{ kind: "injection_check", value: { model: "m", max_chars: 100 } }] }, "rules.0.max_chars: Input should be greater than or equal to 200"],
    ["guardrail", { position: "tool", rules: [{ kind: "injection_check", value: { model: "m", max_chars: null } }] }, "rules.0.max_chars: Input should be a valid integer"],
    ["guardrail", { position: "tool", rules: [{ kind: "injection_check", value: { model: "m", max_calls: 1001 } }] }, "rules.0.max_calls: Input should be less than or equal to 1000"],
    ["guardrail", { position: "output", rules: [{ kind: "injection_check", value: { model: "m" } }] }, "data: Value error, injection_check runs only at position tool"],
    [
      "guardrail",
      { position: "tool", rules: [{ kind: "injection_check", value: { model: "m" } }, { kind: "injection_check", value: { model: "m" } }] },
      "data: Value error, only one injection_check rule is allowed in a guardrail node",
    ],
    // knowledge
    ["knowledge", { tool_name: 5 }, "tool_name: Input should be a valid string"],
    ["knowledge", { tool_name: "Bad Name" }, "tool_name: String should match pattern '^[A-Za-z0-9_]{1,64}$'"],
    ["knowledge", { top_k: 0 }, "top_k: Input should be greater than or equal to 1"],
    ["knowledge", { top_k: 21 }, "top_k: Input should be less than or equal to 20"],
    ["knowledge", { min_score: 2 }, "min_score: Input should be less than or equal to 1"],
    ["knowledge", { min_score: -1 }, "min_score: Input should be greater than or equal to 0"],
    ["knowledge", { knowledge_base_id: 5 }, "knowledge_base_id: Input should be a valid string"],
    ["knowledge", { description: 5 }, "description: Input should be a valid string"],
  ];

  it.each(CASES)("%s %j -> %s", (type, data, expected) => {
    expect(checkNodeData(type, data)).toBe(expected);
  });

  it("keeps the two places the server cannot be copied, and says so", () => {
    // 정규식 오류 본문은 엔진이 달라서 (JS 와 파이썬 re) 같을 수 없다. loc 과 `Value error, invalid regex: ` 까지만 같다
    const deny = checkNodeData("guardrail", { position: "input", rules: [{ kind: "deny_pattern", value: "(" }] });
    expect(deny).toMatch(/^rules\.0: Value error, invalid regex: /);
    const pii = checkNodeData("guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["regex"], regex: "(" } }] });
    expect(pii).toMatch(/^rules\.0: Value error, invalid regex: /);
    // response_format 은 서버가 pydantic 내부 문장을 내므로 여기서는 사람이 읽을 수 있는 쪽을 쓴다
    expect(checkNodeData("agent", { role: "main", name: "a", response_format: "nope" })).toBe("response_format: Input should be 'text' or {json_schema}");
  });

  it("rejects and accepts the same regex shapes as platform/tests/test_guardrail_regex.py", () => {
    const deny = (value: string) => checkNodeData("guardrail", { position: "input", rules: [{ kind: "deny_pattern", value }] });
    for (const p of ["(a+)+", "(\\w+\\s?)+$", "^(([a-z])+.)+[A-Z]([a-z])+$", "(x*)*", "(a{2,}){3,}", "(?:[a-z]+)+", "((a|b)+)*c", "(a?){5}"]) {
      expect(deny(p), p).toContain("repeats a group");
    }
    // 서버 목록에서 (?i)·(?P<name>) 은 뺐다. 파이썬 re 문법이라 JS 가 문법 오류로 먼저 거절한다
    for (const p of ["\\b(?:ssn|password|비밀번호)\\b", "^\\d{3}-\\d{2}-\\d{4}$", "(?:ab|a)+b", "[a-z]+@[a-z]+\\.[a-z]{2,}", "(\\d+)-(\\d+)", "\\(\\d+\\)+", "[(+)]+"]) {
      expect(deny(p), p).toBeNull();
    }
  });

  it("accepts what the server accepts", () => {
    expect(checkNodeData("model", { name: "m" })).toBeNull();
    expect(checkNodeData("tools", { source: { kind: "mcp_instance" } })).toBeNull();
    expect(checkNodeData("agent", { role: "main", name: "a", response_format: { json_schema: { a: 1 } } })).toBeNull();
    expect(checkNodeData("agent", { role: "main", name: "a", skills: [{ id: "s", revision: 1 }, { id: "t", revision: 3 }] })).toBeNull();
    expect(checkNodeData("guardrail", { position: "input", rules: [{ kind: "pii_mask", value: { types: ["email"] } }] })).toBeNull();
  });
});
