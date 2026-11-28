/**
 * Agent 그래프 (D26·D28, resource-model.md 5절). 캔버스가 그리는 노드·간선과 서버 저장 형식
 * `graph{nodes, edges}` + `layout{node_id: {x, y}}` 사이를 옮기고, 서버 `services/agent_graph.py::validate`
 * 와 같은 규칙·코드로 클라이언트 검증을 한다. 노드 `data` 의 타입은 5절 노드 `data` 표와 1:1 이다.
 *
 * 모델은 노드다(D28). provider·model·출력 토큰·추론 강도·temperature·폴백은 `model` 노드의 설정이고 에이전트는
 * `model` 간선으로 모델 노드에 붙는다(에이전트마다 정확히 하나, 여러 에이전트가 공유 가능). 에이전트의 포트는
 * 간선 종류별로 자리가 다르고(PORTS 표) 핸들 id 는 간선 종류 그대로다.
 *
 * 지식은 노드다(D32). `knowledge` 노드가 Knowledge Base 하나를 검색 도구 하나로 에이전트(메인·서브)에 붙고 한 에이전트에
 * 여럿 붙을 수 있다. 검증 문맥의 `knowledgeBases` 로 존재·삭제 중·임베딩 provider 키·집계(빈·실패)를 서버와 같은 코드로 본다.
 *
 * Skill 은 노드가 아니라 에이전트 노드의 `skills` 다(D64). 규칙은 서버처럼 따로 둔 `agentSkills.ts` 에 있고 여기서는 이슈로 옮긴다.
 *
 * 서버가 K8s Secret 을 읽어야만 아는 것(provider 키 유무)은 여기서 `api_key_secret` 이 비었는가로 대신
 * 본다. 그 밖의 규칙은 코드·표식 자리까지 서버와 같아서 저장 전에 같은 표식을 노드에 달 수 있다. 같은 판정을
 * 내는지는 `src/test/agent-graph.test.ts` 가 code 로 지킨다.
 *
 * 이슈는 문장을 들지 않고 code·자리와 상세의 인자만 든다. 화면 문장은 그릴 때 `issueText(t, issue)` 가 만들고, 정본은
 * 사전의 `canvas.issues` 다(code 별 문장 text 와 이슈마다 다른 상세 detail). 서버는 같은 code 에 영어 문장을 내므로 서버가 준
 * 이슈도 `issueText` 가 code 로 그 표를 찾아 보인다. 노드·간선 종류의 화면 이름과 연결할 수 없는 까닭(`connectionProblem`)도
 * 사전에 있고 여기는 키와 인자만 낸다. 노드 data 의 형식 오류 상세(`checkNodeData`)만
 * 서버 pydantic 문장 그대로다. 저장이 거부됐을 때 받는 것과 같은 필드를 가리키게 하려는 것이고, 글자까지 같은지는
 * `src/test/agent-graph-nodes.test.ts` 의 "checkNodeData messages are the server's, word for word" 가 지킨다.
 * 글자까지 같을 수 없는 자리는 `checkNodeData` docstring 에 둘로 적어 뒀다.
 */
import type { AgentGraphEdge, AgentGraphNode, AgentGraphOut, AgentIssue, AgentLayout, AgentServiceSettings, AgentTemplate, KnowledgeBase, LLMProvider } from "@/api/types";
import type { Messages } from "@/i18n";

import { pinsOf, skillFindings, type PinnedSkill, type SkillPin } from "./agentSkills";
import { SANDBOX_COMMAND_MAX_SECONDS } from "./codeExecution";
import { canonicalJson, isRecord } from "./json";
import { supportsEmbedding } from "./knowledge";
import { slugify } from "./names";
import { sdkKind } from "./providers";
import { ownValue } from "./utils";

// -- 노드·간선 종류 -----------------------------------------------------------------------

export type NodeType = "entry" | "agent" | "model" | "tools" | "memory" | "guardrail" | "knowledge";
export type EdgeKind = "entry" | "model" | "tools" | "delegate" | "memory" | "guardrail" | "knowledge";

export const NODE_TYPES: readonly NodeType[] = ["entry", "agent", "model", "tools", "memory", "guardrail", "knowledge"];
export const EDGE_KINDS: readonly EdgeKind[] = ["entry", "model", "tools", "delegate", "memory", "guardrail", "knowledge"];

/** (source 종류, target 종류) → 간선 종류. 방향은 항상 "붙이는 쪽 → 붙는 것" 이다 (서버 ALLOWED_EDGES 와 같다). */
const ALLOWED_EDGES: Record<string, EdgeKind> = {
  "entry->agent": "entry",
  "agent->model": "model",
  "agent->tools": "tools",
  "agent->agent": "delegate",
  "agent->memory": "memory",
  "agent->guardrail": "guardrail",
  "agent->knowledge": "knowledge",
};

export function edgeKindFor(source: NodeType, target: NodeType): EdgeKind | null {
  return ALLOWED_EDGES[`${source}->${target}`] ?? null;
}

/**
 * 두 노드 사이에 방향이 하나뿐인가. 표에 `A->B` 와 `B->A` 가 함께 있는 쌍은 `agent->agent` 뿐이고 그것도
 * 메인에서 서브로만 간다(`connectionProblem`). 그래서 어느 쪽에서 끌어도 맞는 방향이 하나로 정해진다.
 * 화면이 방향을 대신 맞춰 주는 근거이며, 표에 쌍을 더하면 이 테스트가 먼저 깨진다.
 */
export function hasSingleDirection(a: NodeType, b: NodeType): boolean {
  return !(edgeKindFor(a, b) !== null && edgeKindFor(b, a) !== null && a !== b);
}

// -- 포트 (docs/guide/agents.md 1절 포트 표, D28) ---------------------------------------------

export type PortSide = "left" | "right" | "top" | "bottom";

export interface Port {
  /** 이 포트로 드나드는 간선 종류. 핸들 id 로도 쓴다. */
  kind: EdgeKind;
  direction: "source" | "target";
  side: PortSide;
  /** 같은 변에 포트가 여럿일 때 그 변에서의 자리 (0 = 왼쪽·위, 1 = 오른쪽·아래). */
  at: number;
  /** 메인 에이전트에만 있는 포트 (입구 도착, 위임 출발). */
  mainOnly?: boolean;
  /** 서브 에이전트에만 있는 포트 (위임 도착). */
  subOnly?: boolean;
}

/**
 * 노드 종류별 포트 자리. 에이전트는 왼쪽이 들어오는 입구(메인)·위임(서브), 위 왼쪽이 메모리(메인만), 위 오른쪽이 모델,
 * 오른쪽이 위에서부터 도구·지식·위임(메인만), 아래가 가드레일(메인만)이다. 상대 노드 쪽은 모델·메모리가 아래, 도구·지식·서브가
 * 왼쪽, 가드레일이 위, 입구가 오른쪽(에이전트)·아래(가드레일) 이다.
 */
const PORTS: Record<NodeType, readonly Port[]> = {
  entry: [{ kind: "entry", direction: "source", side: "right", at: 0.5 }],
  agent: [
    { kind: "entry", direction: "target", side: "left", at: 0.5, mainOnly: true },
    { kind: "delegate", direction: "target", side: "left", at: 0.5, subOnly: true },
    { kind: "memory", direction: "source", side: "top", at: 0.28, mainOnly: true },
    { kind: "model", direction: "source", side: "top", at: 0.72 },
    { kind: "tools", direction: "source", side: "right", at: 0.28 },
    { kind: "knowledge", direction: "source", side: "right", at: 0.5 },
    { kind: "delegate", direction: "source", side: "right", at: 0.72, mainOnly: true },
    { kind: "guardrail", direction: "source", side: "bottom", at: 0.5, mainOnly: true },
  ],
  model: [{ kind: "model", direction: "target", side: "bottom", at: 0.5 }],
  memory: [{ kind: "memory", direction: "target", side: "bottom", at: 0.5 }],
  tools: [{ kind: "tools", direction: "target", side: "left", at: 0.5 }],
  guardrail: [{ kind: "guardrail", direction: "target", side: "top", at: 0.5 }],
  knowledge: [{ kind: "knowledge", direction: "target", side: "left", at: 0.5 }],
};

export type NodePort = Port & { hidden: boolean };

/**
 * 노드의 포트 전부. 역할에 맞지 않는 포트(서브의 위임 출발, 메인의 위임 도착)는 `hidden` 이다. 숨은 포트도 핸들로는
 * 남겨 두어 역할을 바꾼 뒤에도 간선이 그 자리에 그려지고 검증이 표식을 달 수 있게 한다.
 */
export function nodePorts(type: NodeType, role: "main" | "sub" = "main"): NodePort[] {
  return PORTS[type].map((p) => ({ ...p, hidden: Boolean((p.mainOnly && role !== "main") || (p.subOnly && role !== "sub")) }));
}

// -- 노드 data (resource-model 5절 표) ------------------------------------------------------

export interface RateLimit {
  per_user_per_minute: number;
}

export interface EntryData {
  session_ttl_seconds: number;
  max_turns: number;
  rate_limit: RateLimit | null;
  max_concurrent_sessions: number;
  max_input_chars: number;
  greeting: string | null;
}

export type Effort = "off" | "low" | "medium" | "high" | "xhigh" | "max";
export const EFFORTS: readonly Effort[] = ["off", "low", "medium", "high", "xhigh", "max"];
/** 모델 노드의 추론 강도 기본값. 값이 빠진 data 를 화면에 보일 때도 이것으로 읽는다. */
export const DEFAULT_EFFORT: Effort = "medium";

/**
 * 런타임이 reasoning_effort 로 바꿔 보내는 GLM 모델의 이름이다. 정본은 agent-runtime/runtime/models.py 의 `GLM_EFFORTS`
 * 이고 `src/test/agent-graph.test.ts` 가 맞댄다. Z.AI 의 이름 그대로여야 하고 OpenRouter 이름이나 날짜·-air 가 붙은 이름은 아니다.
 */
export const GLM_EFFORT_MODELS: readonly string[] = ["glm-5.3", "glm-5.2"];

/**
 * provider kind 와 쓸 모델 별로 받아 주는 effort. openai SDK 로 부르는 kind(openrouter·zai 포함)는 max 가 없다
 * (docs/guide/agents.md 3절 모델 표, D26). 다만 런타임이 값을 바꿔 보내는 GLM 모델은 max 를 받으므로 모든 값을 연다.
 * 모델이 받지 않는 값은 런타임이 가장 가까운 값으로 보낸다 (docs/guide/llm-providers.md "Z.AI and GLM").
 */
export function effortsFor(kind: LLMProvider["kind"] | null | undefined, model?: string | null): readonly Effort[] {
  if (sdkKind(kind) !== "openai") return EFFORTS;
  if (model != null && GLM_EFFORT_MODELS.includes(model.toLowerCase())) return EFFORTS;
  return EFFORTS.filter((e) => e !== "max");
}

/** `{tokens: n}` | `{messages: n}` | `{fraction: f}` 중 하나만 갖는다. */
export interface CompactionTrigger {
  tokens?: number;
  messages?: number;
  fraction?: number;
}

/** `{messages: n}` | `{fraction: f}` 중 하나만 갖는다. */
export interface CompactionKeep {
  messages?: number;
  fraction?: number;
}

export interface Compaction {
  trigger: CompactionTrigger;
  keep: CompactionKeep;
}

export interface Fallback {
  provider: string;
  model: string | null;
}

export type ResponseFormat = "text" | { json_schema: Record<string, unknown> };

/** 에이전트의 턴 타임아웃 기본값(초)이다. 서버 `AgentData.turn_timeout_seconds` 의 기본값과 같다. */
export const DEFAULT_TURN_TIMEOUT_SECONDS = 120;

/** 에이전트에는 프롬프트·실행 한도·컨텍스트 압축·응답 형식·언어·내장 도구만 남는다 (D28). */
export interface AgentData {
  role: "main" | "sub";
  name: string;
  description: string;
  system_prompt: string;
  context: { compaction: Compaction };
  max_tool_calls_per_turn: number;
  turn_timeout_seconds: number;
  retries: number;
  response_format: ResponseFormat;
  language: string | null;
  builtin_tools: { filesystem: boolean };
  /** 붙인 Skill 과 고정한 리비전(D64). 붙인 것이 없으면 키를 두지 않는다. 서버 기본값(빈 목록)과 같은 뜻이다. */
  skills?: SkillPin[];
  /** 코드 실행(P40 9절). 켜면 키가 있고 끄면 키를 지운다. approval 이 있으면 명령 실행 전에 그 결정으로 묻는다. 메인의 것만 컴파일이 읽는다. */
  code_execution?: CodeExecution;
}

/** 코드 실행의 승인 결정(서버 `agent_graph.CodeExecution`). 비면 묻지 않는다. */
export interface CodeExecution {
  approval: Decision[];
}

/** 모델 노드 (D28). 에이전트마다 하나가 붙고 여러 에이전트가 공유할 수 있다. */
export interface ModelData {
  name: string;
  provider: string;
  model: string | null;
  max_output_tokens: number;
  reasoning: { effort: Effort };
  temperature: number | null;
  fallback: Fallback | null;
}

/**
 * 모델 노드의 필드. 에이전트 data 에 있으면 서버의 AgentData(services/agent_graph.py)가 extra 로 거절하므로
 * checkNodeData 도 같은 메시지로 거절한다.
 */
export const LEGACY_MODEL_FIELDS = ["provider", "model", "max_output_tokens", "reasoning", "temperature", "fallback"] as const;

export type SourceKind = "gateway_instance" | "mcp_instance" | "agent";
/** 게시한 Agent 를 Tool 로 부르는 소스 (P46 6.2절). 소스 id 는 그 Agent 의 허브 항목 id 다. 서버 `AGENT_SOURCE`. */
export const AGENT_SOURCE = "agent" satisfies SourceKind;
/** MCP 를 말하는 소스 종류. 검증 문맥(ValidationContext)에 목록이 있다. */
export type McpSourceKind = Exclude<SourceKind, "agent">;
export const SOURCE_KIND_VALUES: readonly SourceKind[] = ["gateway_instance", "mcp_instance", "agent"];

/**
 * 도구 소스 종류별 화면 이름·화면 경로·목록 이름. `listKey` 는 InspectorOptions 의 목록 이름이고, MCP 를 말하는 두 종류의 것은
 * ValidationContext 도 함께 쓴다. 종류를 가르는 삼항을 곳곳에 두지 않고 이 표를 찾는다. Agent 소스의 목록은 허브의 `agent` 항목이고
 * 여는 화면은 그 항목이 가리키는 Agent 의 상세다.
 */
export const SOURCE_KINDS: {
  [K in SourceKind]: { label: "Gateway" | "MCP Server" | "Agent"; route: "gateways" | "mcp-servers" | "agents"; listKey: K extends McpSourceKind ? "gateways" | "mcpServers" : "agentItems" };
} = {
  gateway_instance: { label: "Gateway", route: "gateways", listKey: "gateways" },
  mcp_instance: { label: "MCP Server", route: "mcp-servers", listKey: "mcpServers" },
  agent: { label: "Agent", route: "agents", listKey: "agentItems" },
};

export type Decision = "approve" | "edit" | "reject" | "respond";
export const DECISIONS: readonly Decision[] = ["approve", "edit", "reject", "respond"];

export interface ToolsData {
  source: { kind: SourceKind; id: string };
  allow_tools: string[];
  approval: Record<string, Decision[]>;
  timeouts: Record<string, number>;
  /** 읽기 전용으로 표시한 write Tool 이다(P39b 14절 4부). `tool_safety` 는 image·remote·entry Tool 의 표시만 읽기로 센다(P43 4절). */
  read_only?: string[];
  /** 웹 fetch 가 가도 되는 도메인이다(P39b 14절 2부). 소문자 A-label 이고 공개 접미사는 서버가 거절한다. */
  allow_domains?: string[];
  /** 웹 fetch 가 가지 않는 도메인이다. */
  block_domains?: string[];
  /** 바깥 내용 표시를 백엔드 종류의 기본값에서 바꾼 Tool 이다(P43 2절). 참이면 결과가 바깥 내용이다. */
  outside?: Record<string, boolean>;
  /** 사람이 승인을 기억해도 되는 Tool 이다(P43 5절, 기본 꺼짐). */
  rememberable?: string[];
}

/**
 * 장기 기억 (D34). 세션이 끝나도 남는 사용자별 기억이다. 경로는 `/memories` 고정이고 저장소는 이 memory 노드가
 * 고른 Database 다. `retention_days` 가 null 이면 지우지 않고, 값이 있으면 1 이상이다.
 */
export interface LongTermData {
  /** null 이면 만료가 없다. 켬·끔은 이 키의 존재로 말하므로 enabled 필드를 두지 않는다(서버 LongTerm 과 같다). */
  retention_days: number | null;
}

/**
 * memory 노드 (D33·D34). 체크포인터가 본체이고 장기 기억은 켤 때만 붙는 절이다. 꺼져 있으면 `long_term` 키를
 * 아예 두지 않는다. 켠 적 없는 프로필의 저장 본문이 지금과 한 글자도 달라지지 않게 하려는 것이고, 서버 컴파일이
 * agent.json 에 하는 것과 같은 규칙이다.
 */
export interface MemoryData {
  checkpointer: { data_service_id: string };
  long_term?: LongTermData;
}

/** 이 memory 노드에서 장기 기억이 켜져 있는가. **키가 있으면 켠 것**이고 끄면 키를 지운다(서버 LongTerm 과 같다). */
export function longTermEnabled(data: MemoryData | null | undefined): boolean {
  return data?.long_term != null;
}

/** 그래프에 장기 기억을 켠 memory 노드가 있는가. Agent 개요의 메모리 줄이 읽는다. 홈 챗의 기억 서랍 단추는 도는 리비전의 사실(상태의 `long_term_memory`)로 가른다. */
export function graphRemembers(graph: { nodes?: readonly { type: string; data?: unknown }[] } | null | undefined): boolean {
  return (graph?.nodes ?? []).some((n) => n.type === "memory" && longTermEnabled(n.data as MemoryData | undefined));
}

export type PIIType = "email" | "credit_card" | "ip" | "mac_address" | "url" | "regex";
export const PII_TYPES: readonly PIIType[] = ["email", "credit_card", "ip", "mac_address", "url", "regex"];

/** 판정 모델 규칙의 값(P43 7절). model 은 같은 그래프의 모델 노드 id 다. 서버 `InjectionCheck`. */
export interface InjectionCheck {
  model: string;
  max_chars: number;
  max_calls: number;
}
export const INJECTION_CHECK = "injection_check";
/** 판정 모델 규칙의 기본값과 범위. 서버 `InjectionCheck` 의 Field 와 같다. */
export const JUDGE_MAX_CHARS = { default: 8000, min: 200, max: 200000 } as const;
export const JUDGE_MAX_CALLS = { default: 20, min: 1, max: 1000 } as const;

export type GuardrailRule =
  | { kind: "max_length"; value: number }
  | { kind: "deny_pattern"; value: string }
  | { kind: "pii_mask"; value: { types: PIIType[]; regex?: string | null } }
  | { kind: typeof INJECTION_CHECK; value: InjectionCheck };
export type GuardrailRuleKind = GuardrailRule["kind"];
export const GUARDRAIL_RULE_KINDS: readonly GuardrailRuleKind[] = ["max_length", "deny_pattern", "pii_mask", INJECTION_CHECK];

/** 가드레일 노드의 판정 모델 규칙이다. 없으면 null 이다. 서버 `GuardrailData.judge`. */
export function judgeOf(d: GuardrailData): InjectionCheck | null {
  const rule = (d.rules ?? []).find((r) => r.kind === INJECTION_CHECK);
  return rule ? (rule.value as InjectionCheck) : null;
}

/**
 * 메인 에이전트가 붙인 guardrail 노드 가운데 첫 판정 모델 규칙이다. 그래프 순서이고 없으면 null 이다. 서버 `Analysis.judges` 의 첫
 * 것이고, 채점 모델 선택지(lib/evaluation.ts graderChoices)가 판정 모델의 provider 를 넣을 때 쓴다.
 */
export function mainJudge(mainId: string, nodes: readonly { id: string; type: string; data?: unknown }[], edges: readonly { source: string; target: string; kind: string }[]): InjectionCheck | null {
  const attached = new Set(edges.filter((e) => e.source === mainId && e.kind === "guardrail").map((e) => e.target));
  for (const n of nodes) {
    if (n.type !== "guardrail" || !attached.has(n.id)) continue;
    const judge = judgeOf((n.data ?? {}) as GuardrailData);
    if (judge) return judge;
  }
  return null;
}

export interface GuardrailData {
  position: "input" | "output" | "tool";
  rules: GuardrailRule[];
  on_violation: "block" | "flag";
  message: string | null;
}

/** 도구 이름 규칙. 서버 `services/toolnames.py` 의 TOOL_NAME_RE 와 같다. */
export const TOOL_NAME_RE = /^[A-Za-z0-9_]{1,64}$/;

/**
 * deepagents·langchain 이 자동으로 붙이는 도구 이름(구조화 출력 도구 포함). 검색 도구가 이 이름이면 런타임의 ToolNode 가
 * 조용히 덮으므로 서버가 reserved_tool_name 으로 막는다(agent_graph.py 의 RESERVED_TOOL_NAMES 와 같다).
 */
export const RESERVED_TOOL_NAMES: ReadonlySet<string> = new Set(["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "task", "write_todos", "execute", "structured_response"]);

/** knowledge 노드의 도구 이름 기본값 (서버 KnowledgeData.tool_name 의 기본값과 같다). */
export const DEFAULT_KNOWLEDGE_TOOL_NAME = "search_knowledge";

/** knowledge 노드 (D32). Knowledge Base 하나를 검색 도구 하나로 붙인다. description 이 비면 서버가 KB 이름으로 기본 문장을 만든다. */
export interface KnowledgeData {
  knowledge_base_id: string;
  tool_name: string;
  description: string;
  top_k: number;
  min_score: number | null;
  /** 검색 결과를 바깥 내용으로 다루는가(P43 2절). 없으면 참이다. */
  outside?: boolean;
}

export type NodeData = EntryData | AgentData | ModelData | ToolsData | MemoryData | GuardrailData | KnowledgeData;

export interface NodeDataByType {
  entry: EntryData;
  agent: AgentData;
  model: ModelData;
  tools: ToolsData;
  memory: MemoryData;
  guardrail: GuardrailData;
  knowledge: KnowledgeData;
}

// -- 그래프 문서 ---------------------------------------------------------------------------

export interface GraphNode {
  id: string;
  type: NodeType;
  data: NodeData;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export type Point = { x: number; y: number };
export type Layout = AgentLayout;

export interface ServiceSettings {
  budget: { session_tokens: number | null; daily_tokens: number | null };
  observability: { record_bodies: boolean };
  evaluation?: Record<string, unknown> | null;
}

export const DEFAULT_SERVICE: ServiceSettings = {
  budget: { session_tokens: null, daily_tokens: null },
  observability: { record_bodies: false },
  evaluation: null,
};

export const DEFAULT_COMPACTION: Compaction = { trigger: { fraction: 0.85 }, keep: { fraction: 0.1 } };

/** 서버 pydantic 모델의 기본값과 같다. */
export function defaultNodeData(type: "entry"): EntryData;
export function defaultNodeData(type: "agent", role?: "main" | "sub"): AgentData;
export function defaultNodeData(type: "model"): ModelData;
export function defaultNodeData(type: "tools"): ToolsData;
export function defaultNodeData(type: "memory"): MemoryData;
export function defaultNodeData(type: "guardrail"): GuardrailData;
export function defaultNodeData(type: "knowledge"): KnowledgeData;
export function defaultNodeData(type: NodeType, role?: "main" | "sub"): NodeData;
export function defaultNodeData(type: NodeType, role: "main" | "sub" = "main"): NodeData {
  switch (type) {
    case "entry":
      return { session_ttl_seconds: 3600, max_turns: 50, rate_limit: null, max_concurrent_sessions: 100, max_input_chars: 4000, greeting: null };
    case "agent":
      return {
        role,
        name: role === "main" ? "main" : "sub",
        description: "",
        system_prompt: "",
        context: { compaction: { trigger: { ...DEFAULT_COMPACTION.trigger }, keep: { ...DEFAULT_COMPACTION.keep } } },
        max_tool_calls_per_turn: 8,
        turn_timeout_seconds: DEFAULT_TURN_TIMEOUT_SECONDS,
        retries: 2,
        response_format: "text",
        language: null,
        builtin_tools: { filesystem: true },
      };
    case "model":
      return { name: "model", provider: "", model: null, max_output_tokens: 2048, reasoning: { effort: DEFAULT_EFFORT }, temperature: null, fallback: null };
    case "tools":
      return { source: { kind: "gateway_instance", id: "" }, allow_tools: [], approval: {}, timeouts: {} };
    case "memory":
      return { checkpointer: { data_service_id: "" } };
    case "guardrail":
      return { position: "input", rules: [{ kind: "max_length", value: 4000 }], on_violation: "block", message: null };
    case "knowledge":
      return { knowledge_base_id: "", tool_name: DEFAULT_KNOWLEDGE_TOOL_NAME, description: "", top_k: 5, min_score: null };
  }
}

/** id 후보. `base` 가 비어 있으면 그것, 아니면 `<base>-2`, `<base>-3` … 이다. */
function uniqueId(base: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    const candidate = `${base}-${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/** 노드 id 후보. 노드 종류 이름에서 시작한다 (`tools`, `tools-2` …). */
export function uniqueNodeId(type: NodeType, existing: Iterable<string>): string {
  return uniqueId(type, existing);
}

/** 간선 id 후보. `e-<source>-<target>` 에서 시작한다. */
export function uniqueEdgeId(source: string, target: string, existing: Iterable<string>): string {
  return uniqueId(`e-${source}-${target}`, existing);
}

/** 에이전트 이름 슬러그의 상한. K8s 이름이 아니라서 NAME_MAX(32)보다 길다. */
const AGENT_NAME_MAX = 48;

/**
 * 에이전트 이름 슬러그. deepagents 는 서브에이전트 이름을 `task` 도구의 인자로 쓰므로 공백·기호 없이 소문자
 * 영문·숫자·하이픈으로 만든다. 한글만이면 빈 문자열이라 호출부가 기본값을 둔다.
 */
export function agentSlug(display: string): string {
  return slugify(display, AGENT_NAME_MAX);
}

// -- 직렬화 --------------------------------------------------------------------------------

/** 캔버스가 다루는 노드: 문서의 노드 + 배치. */
export interface PlacedNode {
  id: string;
  type: NodeType;
  data: NodeData;
  position: Point;
}

/** 간선은 종류만 저장한다. 어느 포트에 그리는지는 종류에서 나온다 (핸들 id = 종류). */
export interface PlacedEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
}

export interface GraphDocument {
  graph: Graph;
  layout: Layout;
}

/** 캔버스 상태 → 저장 본문의 graph·layout. 노드 순서는 캔버스 순서 그대로다(컴파일 결과의 결정적 순서). */
export function serializeGraph(nodes: PlacedNode[], edges: PlacedEdge[]): GraphDocument {
  const layout: Layout = {};
  for (const n of nodes) layout[n.id] = { x: Math.round(n.position.x), y: Math.round(n.position.y) };
  return {
    graph: {
      nodes: nodes.map((n) => ({ id: n.id, type: n.type, data: n.data })),
      edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, kind: e.kind })),
    },
    layout,
  };
}

/** 서버 응답·요청의 그래프 모양 (생성 타입). nodes·edges 가 선택인 요청 본문도 받는다. */
export type ApiGraph = { nodes?: AgentGraphNode[]; edges?: AgentGraphEdge[] };

/** 노드 data 는 생성 타입에서 자유 dict 라 여기의 타입을 그대로 보낸다. */
export function toApiGraph(graph: Graph): AgentGraphOut {
  return graph as unknown as AgentGraphOut;
}

/** 에이전트에 붙이는 모델 노드를 그 에이전트의 위 오른쪽 모델 포트 근처에 둘 때의 어긋남 (카드 폭 240 기준). */
export const MODEL_ATTACH_OFFSET: Point = { x: 140, y: -170 };

/** 배치가 없는 노드는 격자에 둔다. 서버 layout 은 노드 id 별 선택 사항이다. D26 모델 필드의 승격은 서버가 읽을 때 한다. */
export function deserializeGraph(graph: ApiGraph | Graph, layout: Layout | null | undefined): { nodes: PlacedNode[]; edges: PlacedEdge[] } {
  // data 는 복사한다. 캔버스가 편집하는 객체가 쿼리 캐시·템플릿과 얽히지 않게
  const copied: Graph = {
    nodes: (graph.nodes ?? []).map((n) => ({ id: n.id, type: n.type as NodeType, data: JSON.parse(JSON.stringify(n.data ?? {})) as NodeData })),
    edges: (graph.edges ?? []).map((e) => ({ id: e.id, source: e.source, target: e.target, kind: e.kind as EdgeKind })),
  };
  const nodes: PlacedNode[] = copied.nodes.map((n, i) => ({
    ...n,
    position: layout?.[n.id] ? { x: layout[n.id].x, y: layout[n.id].y } : { x: 80 + (i % 4) * 320, y: 80 + Math.floor(i / 4) * 200 },
  }));
  return { nodes, edges: copied.edges };
}

/** 리비전 동일성 비교와 같은 기준(키 순서 무관, layout 제외)의 문자열. 캔버스·목이 이 모듈에서 가져가므로 다시 내보낸다. */
export { canonicalJson };

/** 응답(ServiceSettingsOut)·요청(ServiceSettings, 필드 선택)·캔버스 상태 어느 것이든 완전한 모양으로 채운다. */
export type ApiServiceSettings = {
  budget?: { session_tokens?: number | null; daily_tokens?: number | null } | null;
  observability?: { record_bodies?: boolean } | null;
  evaluation?: Record<string, unknown> | null;
};

export function toServiceSettings(s: AgentServiceSettings | ApiServiceSettings | ServiceSettings | null | undefined): ServiceSettings {
  return {
    budget: { session_tokens: s?.budget?.session_tokens ?? null, daily_tokens: s?.budget?.daily_tokens ?? null },
    observability: { record_bodies: s?.observability?.record_bodies ?? false },
    evaluation: (s?.evaluation as Record<string, unknown> | null | undefined) ?? null,
  };
}

/** 템플릿을 캔버스 상태로. provider·MCP·Database 는 비어 있어 검증이 그 자리를 표시한다. */
export function fromTemplate(t: AgentTemplate): { nodes: PlacedNode[]; edges: PlacedEdge[]; service: ServiceSettings } {
  const { nodes, edges } = deserializeGraph(t.graph, t.layout);
  return { nodes, edges, service: toServiceSettings(t.service) };
}

/** 에이전트에서 kind 간선으로 붙은 노드 id (그래프 순서). */
export function attachedIds(agentId: string, edges: readonly { source: string; target: string; kind: string }[], kind: string): string[] {
  return edges.filter((e) => e.source === agentId && e.kind === kind).map((e) => e.target);
}

/** 에이전트에 붙은 모델 노드 id (그래프 순서). 카드·인스펙터·목록이 쓴다. */
export function attachedModelIds(agentId: string, edges: readonly { source: string; target: string; kind: string }[]): string[] {
  return attachedIds(agentId, edges, "model");
}

/**
 * 메인 에이전트가 `model` 간선으로 붙은 모델 노드의 provider·model. 빈 값은 null 이고, 메인이나 그 모델 노드가 없으면
 * 통째로 null 이다. 목록과 홈 챗이 "이 Agent 가 어떤 모델로 답하는가" 를 같은 규칙으로 보인다. 서브에이전트의 모델
 * 노드가 그래프 앞쪽에 있어도 메인 것을 고른다. 모델 필드를 에이전트 노드에 두던 옛 문서(D26)는 서버가 읽을 때
 * 승격하므로 모델 노드만 본다.
 */
export function mainModel(graph: {
  nodes?: readonly { id: string; type: string; data?: unknown }[];
  edges?: readonly { source: string; target: string; kind: string }[];
}): { provider: string | null; model: string | null } | null {
  const nodes = graph.nodes ?? [];
  const main = mainAgentNode(nodes);
  if (!main) return null;
  const modelId = attachedModelIds(main.id, graph.edges ?? [])[0];
  const node = nodes.find((n) => n.id === modelId && n.type === "model");
  if (!node) return null;
  const data = (node.data ?? {}) as Partial<ModelData>;
  return { provider: data.provider || null, model: data.model || null };
}

/** 메인 에이전트 노드. 없으면 undefined 다. 저장된 그래프와 캔버스의 노드(`type` 이 선택값) 둘 다 받는다. */
export function mainAgentNode<N extends { type?: string; data?: unknown }>(nodes: readonly N[]): N | undefined {
  return nodes.find((n) => n.type === "agent" && (n.data as Partial<AgentData> | undefined)?.role === "main");
}

/**
 * 메인 에이전트에 `memory` 간선으로 memory 노드가 붙었는가. 서버 `main_has_memory` 와 같다. 없으면 대화와 승인 대기가 파드에
 * 살아 0 으로 줄일 수 없다 (P44 6절).
 */
export function mainHasMemory(graph: {
  nodes?: readonly { id: string; type: string; data?: unknown }[];
  edges?: readonly { source: string; target: string; kind: string }[];
}): boolean {
  const nodes = graph.nodes ?? [];
  const main = mainAgentNode(nodes);
  if (!main) return false;
  const targets = new Set(attachedIds(main.id, graph.edges ?? [], "memory"));
  return nodes.some((n) => n.type === "memory" && targets.has(n.id));
}

/** 메인 에이전트의 설명. 목록 카드가 "무엇을 하는 Agent 인지" 로 보인다. 비었으면 null 이다. */
export function mainAgentDescription(graph: { nodes?: readonly { type: string; data?: unknown }[] }): string | null {
  const data = mainAgentNode(graph.nodes ?? [])?.data as Partial<AgentData> | undefined;
  return data?.description?.trim() || null;
}

// -- 검증 -----------------------------------------------------------------------------------

/**
 * 검증 이슈 한 건. 캔버스 검증기가 낸 것과 서버가 준 것(`AgentIssueOut`)을 함께 담는다. 문장은 들지 않는다. 화면 문장은
 * 그릴 때 `issueText` 가 code 와 상세의 인자로 만든다.
 */
export interface Issue extends Pick<AgentIssue, "code" | "level" | "node_id" | "edge_id" | "data"> {
  /** 캔버스 검증기가 문장 뒤에 붙일 상세의 표 키와 인자. 서버가 준 이슈에는 없다. */
  detail?: IssueDetail;
  /** 서버가 준 영어 문장. 표에 없는 code 일 때만 화면에 나온다. 캔버스 검증기가 낸 이슈에는 없다. */
  message?: string;
}

/**
 * Gateway 도구 소스에 Tenant Settings 의 agent_oauth 가 있어야 하는 모드인가. oidc 모드만 그렇다.
 *
 * local 모드도 토큰 인증이지만 vMCP 를 플랫폼이 낸 토큰으로 부른다(D41, 서버의 `gateway_auth_kind` 가
 * bearer). 토큰 인증 여부(`tokenAuth`)로 가르면 local 배포본의 캔버스가 서버는 통과시키는 그래프에
 * agent_oauth_missing 오류를 달고 저장을 막는다.
 */
export function gatewayNeedsAgentOauth(authMode: string | null | undefined): boolean {
  return authMode === "oidc";
}

export interface ValidationContext {
  /** Platform Settings llm_providers. api_key_secret 이 비면 provider_no_key 로 본다 (stub 면제). */
  providers: LLMProvider[];
  /** 플랫폼이 oidc 모드인가 (auth_mode == oidc). Gateway 소스의 agent_oauth 규칙이 이때만 켜진다. */
  oidcMode: boolean;
  agentOauth: { client_id: string } | null;
  agentClientIds: string[];
  /**
   * 도구 소스 후보. id 만이 아니라 상태를 함께 든다 (D42).
   *
   * 정의와 배포가 한 리소스가 되면서 "있는 것" 과 "쓸 수 있는 것" 이 갈렸다. 안 올린 `draft` 는 문서로는
   * 있지만 부를 주소가 없어 도구 소스가 되지 못하므로, 존재 검사(unknown_source)와 배포 검사
   * (source_not_deployed)를 서버와 같은 순서로 따로 낸다.
   */
  gateways: { id: string; state: string }[];
  mcpServers: { id: string; state: string }[];
  /** 테넌트의 Database. memory 검사가 kind 와 verify 가 남긴 `capabilities.checkpointer`(redis) 를 본다 (D26, D33). */
  dataServices: { id: string; name: string; kind: string; db?: number | null; capabilities?: Record<string, string> }[];
  /** 테넌트의 Knowledge Base (status 집계 포함). knowledge 노드의 존재·삭제 중·임베딩 provider·빈·실패 검사가 본다 (D32). */
  knowledgeBases: KnowledgeBase[];
  /** 그래프가 고정한 Skill 리비전의 메타 (D64). 보이는 Skill 의 최신 리비전과 뒤처진 핀의 리비전이다. */
  skillRevisions: PinnedSkill[];
}

/**
 * ValidationContext 의 목록(provider·Gateway·MCP Server·Database·Knowledge Base·Tenant Settings)이 있어야만
 * 낼 수 있는 표식. 목록을 아직 못 읽었거나 조회가 실패했을 때 이 코드들을 그대로 내면 "참조가 지워졌다" 는
 * 거짓말이 되므로 화면이 이것으로 걸러 보류한다 (규칙 자체는 서버가 저장 때 다시 본다).
 */
export const CONTEXT_ISSUE_CODES: ReadonlySet<string> = new Set([
  "unknown_provider",
  "provider_no_key",
  "unsupported_param",
  "unknown_source",
  "source_not_deployed",
  "agent_oauth_missing",
  "agent_client_id_not_allowed",
  "unknown_data_service",
  "invalid_data_service_kind",
  "redis_modules_missing",
  "unknown_knowledge_base",
  "embedding_not_supported",
  "knowledge_base_deleting",
  "knowledge_base_empty",
  "knowledge_base_failed",
  "unknown_skill",
  "skill_name_conflict",
  "skills_too_large",
  "skill_scripts_inert",
  "skill_behind",
]);

/** verify 가 `capabilities.checkpointer` 에 남기는 값. 없으면 아직 verify 전이다 (none). */
export type RedisCheckpointerState = "none" | "modules_missing" | "unknown";

/**
 * redis memory 의 `redis_modules_missing` detail. 정본은 platform/daegam/services/agent_graph.py 의
 * REDIS_CHECKPOINTER_DETAIL 이고 글자대로 같다 (D33). 키는 `capabilities.checkpointer` 값이고 없으면 verify 전이다.
 * 화면에는 이 영어가 아니라 사전의 `canvas.issues.redisCheckpointerHint` 를 보인다. 이 표는 서버가 무엇을 내는지 적어 둔 정본이다.
 */
export const REDIS_CHECKPOINTER_DETAIL: Readonly<Record<RedisCheckpointerState, string>> = {
  none: "run verify first; the checkpointer needs Redis 8 with RediSearch and RedisJSON",
  modules_missing: "Redis has no RediSearch/RedisJSON (Redis 8 bundles them)",
  unknown: "the verify account cannot run FT._LIST; grant it or verify with another account",
};

/** `capabilities.checkpointer` 값을 세 갈래 중 하나로. 모르는 값과 빈 값은 verify 전이다. */
function redisCheckpointerState(capability: string | undefined): RedisCheckpointerState {
  return capability === "modules_missing" || capability === "unknown" ? capability : "none";
}

function isInt(v: unknown): v is number {
  return typeof v === "number" && Number.isInteger(v);
}
function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** 가드레일 정규식의 길이 상한. 서버 `MAX_REGEX_LENGTH` 와 같다. */
const MAX_REGEX_LENGTH = 512;

/** 반복 기호 하나와 뒤따르는 게으름·소유 표시. 서버 `_QUANTIFIER` 와 같은 식이고 pos 자리에서만 맞춘다(sticky). */
const QUANTIFIER = /(?:[*+?]|\{\d+(?:,\d*)?\})[?+]?/y;

function quantifierAfter(pattern: string, pos: number): number {
  QUANTIFIER.lastIndex = pos;
  return QUANTIFIER.exec(pattern)?.[0].length ?? 0;
}

/**
 * 반복되는 그룹 안에 또 반복이 있는가, 곧 (a+)+ 꼴인가. 서버 `regex_safety.has_nested_repetition` 을 그대로 옮겼다.
 * 괄호를 스택으로 따라가며 그룹 안에 반복이 있었는지 적어 두고, 그룹이 닫힌 뒤 반복이 붙으면 그 표시를 본다.
 * 이스케이프와 문자 클래스는 문법이 아니라서 건너뛴다. `(a|ab)+` 같은 겹치는 대안은 서버처럼 잡지 못한다.
 */
function hasNestedRepetition(pattern: string): boolean {
  const stack: boolean[] = [];
  const n = pattern.length;
  let i = 0;
  while (i < n) {
    const c = pattern[i];
    if (c === "(") {
      stack.push(false);
      i += 1;
      continue;
    }
    if (c === ")") {
      const inner = stack.pop() ?? false;
      i += 1;
      const q = quantifierAfter(pattern, i);
      if (q && inner) return true;
      if (stack.length && (q || inner)) stack[stack.length - 1] = true;
      i += q;
      continue;
    }
    // 원자 하나(이스케이프, 문자 클래스, 글자)의 끝으로 간 뒤 반복이 붙었으면 감싼 그룹에 적는다
    if (c === "\\") {
      i += 2;
    } else if (c === "[") {
      i += 1;
      if (i < n && pattern[i] === "^") i += 1;
      if (i < n && pattern[i] === "]") i += 1;
      while (i < n && pattern[i] !== "]") i += pattern[i] === "\\" ? 2 : 1;
      i += 1;
    } else {
      i += 1;
    }
    const q = quantifierAfter(pattern, i);
    if (q && stack.length) stack[stack.length - 1] = true;
    i += q;
  }
  return false;
}

/**
 * 서버 `check_regex` 처럼 길이 상한, 문법, 겹친 반복 순으로 본다. 문장은 서버 것 그대로이고 부르는 쪽이
 * `Value error, ` 로 감싼다. 문법 오류의 본문만은 엔진이 달라(JS 와 파이썬 re) 같을 수 없다.
 */
function regexError(pattern: string): string | null {
  // 파이썬 len 과 같게 코드 포인트로 센다
  if ([...pattern].length > MAX_REGEX_LENGTH) return `regex is longer than ${MAX_REGEX_LENGTH} characters`;
  try {
    new RegExp(pattern);
  } catch (e) {
    return `invalid regex: ${(e as Error).message}`;
  }
  if (hasNestedRepetition(pattern)) {
    return "regex repeats a group that itself contains a repetition, e.g. (a+)+; this backtracks exponentially on non-matching input. Rewrite it without nesting";
  }
  return null;
}

/**
 * 노드 종류별로 서버가 받는 키. `agent_graph.py` 의 *Data 모델이 전부 `extra="forbid"` 라
 * 여기 없는 키가 하나라도 있으면 저장이 422 invalid_node_data 다. 캔버스가 먼저 잡아야
 * "화면은 멀쩡한데 저장만 거부되는" 상태가 안 생긴다.
 * 서버 모델과 어긋나면 platform/tests/test_agent_graph_parity.py 가 막는다.
 */
/** 모르는 키를 버리는 노드 종류(서버의 ToolsData 와 같다). 뒤 판이 칸을 더해도 앞 판이 그
 *  그래프를 검증한다. */
export const EXTRA_IGNORED: ReadonlySet<NodeType> = new Set<NodeType>(["tools"]);

export const NODE_DATA_KEYS: Record<NodeType, readonly string[]> = {
  entry: ["session_ttl_seconds", "max_turns", "rate_limit", "max_concurrent_sessions", "max_input_chars", "greeting"],
  agent: ["role", "name", "description", "system_prompt", "context", "max_tool_calls_per_turn", "turn_timeout_seconds", "retries", "response_format", "language", "builtin_tools", "skills", "code_execution"],
  model: ["name", "provider", "model", "max_output_tokens", "reasoning", "temperature", "fallback"],
  tools: ["source", "allow_tools", "approval", "timeouts", "read_only", "allow_domains", "block_domains", "outside", "rememberable"],
  memory: ["checkpointer", "long_term"],
  guardrail: ["position", "rules", "on_violation", "message"],
  knowledge: ["knowledge_base_id", "tool_name", "description", "top_k", "min_score", "outside"],
};

/**
 * 서버 pydantic 오류 문장을 그대로 만드는 헬퍼들. `checkNodeData` 는 이것만 써서 문장을 만든다.
 * 서버가 어떤 문장을 내는지는 `platform/daegam/services/agent_graph.py` 의 *Data 모델을 실제로
 * `model_validate` 해서 확인했고, `platform/tests/test_agent_graph_parity.py` 가 드리프트를 막는다.
 */

/** `model_validator` 갈래. loc 이 모델 단위이고 메시지 앞에 `Value error, ` 가 붙는다. */
function valueError(loc: string, msg: string): string {
  return `${loc}: Value error, ${msg}`;
}

/** 서브 모델 자리에 dict 가 아닌 것이 온 갈래. 모델 이름이 문장에 그대로 나온다. */
function notDict(loc: string, model: string): string {
  return `${loc}: Input should be a valid dictionary or instance of ${model}`;
}

/** 정수 자리. pydantic 은 값의 종류에 따라 꼬리를 다르게 붙인다. */
function intError(loc: string, v: unknown): string {
  if (typeof v === "string") return `${loc}: Input should be a valid integer, unable to parse string as an integer`;
  if (typeof v === "number") return `${loc}: Input should be a valid integer, got a number with a fractional part`;
  return `${loc}: Input should be a valid integer`;
}

/** 실수 자리. */
function numError(loc: string, v: unknown): string {
  if (typeof v === "string") return `${loc}: Input should be a valid number, unable to parse string as a number`;
  return `${loc}: Input should be a valid number`;
}

/** Literal 열거. 값은 작은따옴표이고 마지막 앞에 or 가 붙는다. */
function oneOf(loc: string, values: readonly string[]): string {
  const quoted = values.map((v) => `'${v}'`);
  const last = quoted.pop() ?? "";
  return `${loc}: Input should be ${quoted.length ? `${quoted.join(", ")} or ${last}` : last}`;
}

/** 필수 문자열 자리 (`str`, min_length=1). 없음·문자열 아님·빈 문자열 순으로 본다. */
function requiredNonEmptyStr(loc: string, v: unknown): string | null {
  if (v === undefined || v === null) return `${loc}: Field required`;
  if (typeof v !== "string") return `${loc}: Input should be a valid string`;
  if (!v) return `${loc}: String should have at least 1 character`;
  return null;
}

/** 정수 하한 자리 (`int`, ge). 값이 없으면 `required` 일 때만 오류이고, 아니면 서버 기본값을 쓰므로 보지 않는다. */
function intAtLeast(loc: string, v: unknown, min: number, { required = false }: { required?: boolean } = {}): string | null {
  if (v === undefined || v === null) return required ? `${loc}: Field required` : null;
  if (!isInt(v)) return intError(loc, v);
  if (v < min) return `${loc}: Input should be greater than or equal to ${min}`;
  return null;
}

/** 실수 범위 자리 (`float`, gt·ge·le). 값이 없으면 보지 않는다. 하한을 먼저, 상한을 나중에 본다. */
function numInRange(loc: string, v: unknown, { gt, ge, le }: { gt?: number; ge?: number; le?: number }): string | null {
  if (v === undefined || v === null) return null;
  if (!isNum(v)) return numError(loc, v);
  if (gt !== undefined && v <= gt) return `${loc}: Input should be greater than ${gt}`;
  if (ge !== undefined && v < ge) return `${loc}: Input should be greater than or equal to ${ge}`;
  if (le !== undefined && v > le) return `${loc}: Input should be less than or equal to ${le}`;
  return null;
}

/** 서버 모델 이름. `notDict` 와 노드 data 자체가 dict 가 아닐 때 쓴다. */
const NODE_DATA_MODEL_NAMES: Record<NodeType, string> = {
  entry: "EntryData",
  agent: "AgentData",
  model: "ModelData",
  tools: "ToolsData",
  memory: "MemoryData",
  guardrail: "GuardrailData",
  knowledge: "KnowledgeData",
};

/**
 * 노드 data 의 형식 검사. 서버 pydantic 모델(extra=forbid, ge 제약, 배타 필드)과 같은 조건이고 첫 문제만
 * `loc: msg` 로 돌려준다. 통과하면 null 이다. 문장은 서버가 내는 것과 글자까지 같아서, 편집 중에 보는 표식과
 * 저장이 거부됐을 때 받는 문장이 다르지 않다.
 *
 * pydantic 은 필드 검사를 전부 마친 뒤에야 `model_validator` 를 돌리므로 여기서도 필드 검사가 먼저다.
 * 두 갈래만 서버와 글자가 다를 수 있다. 정규식 오류 본문은 엔진이 달라서 (JS 와 파이썬 re) 서로 다르고,
 * `response_format` 은 서버가 `response_format.literal['text']: Input should be 'text'` 라는 pydantic 내부
 * 문장을 내므로 여기서는 사람이 읽을 수 있는 쪽을 쓴다.
 */
export function checkNodeData(type: NodeType, data: unknown): string | null {
  if (!isRecord(data)) return notDict("data", NODE_DATA_MODEL_NAMES[type]);
  const d = data;
  // extra=forbid. 옛 예약 knowledge 노드의 임의 키도 여기서 걸린다(서버와 같다)
  const allowed = NODE_DATA_KEYS[type];
  const extra = Object.keys(d).find((k) => !allowed.includes(k));
  if (extra && !EXTRA_IGNORED.has(type)) return `${extra}: Extra inputs are not permitted`;
  /** 서버에 기본값이 있는 최상위 정수 필드. 값이 없으면 검사하지 않는다. */
  const ge = (key: string, min: number): string | null => intAtLeast(key, d[key], min);
  switch (type) {
    case "entry": {
      for (const k of ["session_ttl_seconds", "max_turns", "max_concurrent_sessions", "max_input_chars"]) {
        const e = ge(k, 1);
        if (e) return e;
      }
      if (d.rate_limit != null) {
        if (!isRecord(d.rate_limit)) return notDict("rate_limit", "RateLimit");
        return intAtLeast("rate_limit.per_user_per_minute", d.rate_limit.per_user_per_minute, 1, { required: true });
      }
      return null;
    }
    case "agent": {
      if (d.role === undefined || d.role === null) return "role: Field required";
      if (d.role !== "main" && d.role !== "sub") return oneOf("role", ["main", "sub"]);
      const nameErr = requiredNonEmptyStr("name", d.name);
      if (nameErr) return nameErr;
      for (const k of LEGACY_MODEL_FIELDS) {
        if (k in d) return `${k}: Extra inputs are not permitted`;
      }
      for (const [k, min] of [
        ["max_tool_calls_per_turn", 1],
        ["turn_timeout_seconds", 1],
        ["retries", 0],
      ] as const) {
        const e = ge(k, min);
        if (e) return e;
      }
      const contextErr = checkContext(d.context);
      if (contextErr) return contextErr;
      if (d.response_format !== undefined && d.response_format !== "text") {
        if (!isRecord(d.response_format) || !isRecord(d.response_format.json_schema)) return "response_format: Input should be 'text' or {json_schema}";
      }
      const skillsErr = d.skills === undefined ? null : checkSkillPins(d.skills);
      if (skillsErr) return skillsErr;
      return d.code_execution === undefined || d.code_execution === null ? null : checkCodeExecutionData(d.code_execution);
    }
    case "model": {
      const nameErr = requiredNonEmptyStr("name", d.name);
      if (nameErr) return nameErr;
      const e = ge("max_output_tokens", 1);
      if (e) return e;
      if (d.reasoning != null) {
        if (!isRecord(d.reasoning)) return notDict("reasoning", "Reasoning");
        const effort = d.reasoning.effort;
        if (effort !== undefined && effort !== null && !EFFORTS.includes(effort as Effort)) return oneOf("reasoning.effort", EFFORTS);
      }
      const tempErr = numInRange("temperature", d.temperature, { ge: 0 });
      if (tempErr) return tempErr;
      if (d.fallback != null) {
        if (!isRecord(d.fallback)) return notDict("fallback", "Fallback");
        return requiredNonEmptyStr("fallback.provider", d.fallback.provider);
      }
      return null;
    }
    case "tools": {
      // 필드 검사 먼저다
      if (d.source === undefined || d.source === null) return "source: Field required";
      if (!isRecord(d.source)) return notDict("source", "ToolsSource");
      const sourceKind = d.source.kind;
      if (!SOURCE_KIND_VALUES.some((kind) => kind === sourceKind)) return oneOf("source.kind", SOURCE_KIND_VALUES);
      const allow = (d.allow_tools ?? []) as unknown;
      if (!Array.isArray(allow)) return "allow_tools: Input should be a valid list";
      const badEntry = allow.findIndex((t) => typeof t !== "string");
      if (badEntry >= 0) return `allow_tools.${badEntry}: Input should be a valid string`;
      const approval = (d.approval ?? {}) as unknown;
      if (!isRecord(approval)) return "approval: Input should be a valid dictionary";
      for (const [tool, decisions] of Object.entries(approval)) {
        if (!Array.isArray(decisions)) return `approval.${tool}: Input should be a valid list`;
        const bad = decisions.findIndex((x) => !DECISIONS.includes(x as Decision));
        if (bad >= 0) return oneOf(`approval.${tool}.${bad}`, DECISIONS);
      }
      const timeouts = (d.timeouts ?? {}) as unknown;
      if (!isRecord(timeouts)) return "timeouts: Input should be a valid dictionary";
      for (const [tool, seconds] of Object.entries(timeouts)) {
        if (!isInt(seconds)) return intError(`timeouts.${tool}`, seconds);
      }
      for (const key of ["read_only", "allow_domains", "block_domains", "rememberable"] as const) {
        const list = (d[key] ?? []) as unknown;
        if (!Array.isArray(list)) return `${key}: Input should be a valid list`;
        const bad = list.findIndex((t) => typeof t !== "string");
        if (bad >= 0) return `${key}.${bad}: Input should be a valid string`;
      }
      const outside = (d.outside ?? {}) as unknown;
      if (!isRecord(outside)) return "outside: Input should be a valid dictionary";
      for (const [tool, on] of Object.entries(outside)) {
        if (typeof on !== "boolean") return `outside.${tool}: Input should be a valid boolean`;
      }
      // ToolsData._check (model_validator). 서버의 검사 순서를 그대로 따른다
      if ((allow as string[]).some((t) => !t)) return valueError("data", "allow_tools entries must not be empty");
      if (new Set(allow).size !== allow.length) return valueError("data", "allow_tools contains duplicates");
      for (const [tool, decisions] of Object.entries(approval)) {
        const list = decisions as Decision[];
        if (!tool || !list.length) return valueError("data", "approval needs a tool name and at least one decision");
        if (new Set(list).size !== list.length) return valueError("data", `approval for '${tool}' repeats a decision`);
      }
      for (const [tool, seconds] of Object.entries(timeouts)) {
        if (!tool || (seconds as number) < 1) return valueError("data", "timeouts need a tool name and seconds >= 1");
      }
      return null;
    }
    case "memory": {
      if (d.checkpointer !== undefined && d.checkpointer !== null && !isRecord(d.checkpointer)) return notDict("checkpointer", "Checkpointer");
      if (d.long_term !== undefined && d.long_term !== null) {
        if (!isRecord(d.long_term)) return notDict("long_term", "LongTerm");
        const extraKey = Object.keys(d.long_term).find((k) => k !== "retention_days");
        if (extraKey) return `long_term.${extraKey}: Extra inputs are not permitted`;
        return intAtLeast("long_term.retention_days", d.long_term.retention_days, 1);
      }
      return null;
    }
    case "guardrail": {
      if (d.position === undefined || d.position === null) return "position: Field required";
      if (d.position !== "input" && d.position !== "output" && d.position !== "tool") return oneOf("position", ["input", "output", "tool"]);
      if (d.rules === undefined || d.rules === null) return "rules: Field required";
      if (!Array.isArray(d.rules)) return "rules: Input should be a valid list";
      if (!d.rules.length) return "rules: List should have at least 1 item after validation, not 0";
      for (let i = 0; i < d.rules.length; i++) {
        const e = checkGuardrailRule(`rules.${i}`, d.rules[i] as unknown);
        if (e) return e;
      }
      if (d.on_violation !== undefined && d.on_violation !== null && d.on_violation !== "block" && d.on_violation !== "flag") return oneOf("on_violation", ["block", "flag"]);
      // GuardrailData._check (model_validator): 판정 모델은 position tool 에만, 노드 하나에 하나까지다
      const judges = (d.rules as { kind?: unknown }[]).filter((r) => r.kind === INJECTION_CHECK).length;
      if (judges && d.position !== "tool") return valueError("data", "injection_check runs only at position tool");
      if (judges > 1) return valueError("data", "only one injection_check rule is allowed in a guardrail node");
      return null;
    }
    case "knowledge": {
      if (d.knowledge_base_id !== undefined && d.knowledge_base_id !== null && typeof d.knowledge_base_id !== "string") return "knowledge_base_id: Input should be a valid string";
      if (d.tool_name !== undefined && d.tool_name !== null) {
        if (typeof d.tool_name !== "string") return "tool_name: Input should be a valid string";
        if (!TOOL_NAME_RE.test(d.tool_name)) return `tool_name: String should match pattern '${TOOL_NAME_RE.source}'`;
      }
      if (d.description !== undefined && d.description !== null && typeof d.description !== "string") return "description: Input should be a valid string";
      const e = ge("top_k", 1);
      if (e) return e;
      if (isInt(d.top_k) && d.top_k > 20) return "top_k: Input should be less than or equal to 20";
      if (d.outside !== undefined && typeof d.outside !== "boolean") return "outside: Input should be a valid boolean";
      return numInRange("min_score", d.min_score, { ge: 0, le: 1 });
    }
  }
}

/**
 * 에이전트의 `context` (ContextData → Compaction → trigger·keep). 중첩은 서버 모델의 중첩 그대로이고, 빠진
 * 자리는 서버 기본값을 쓰므로 보지 않는다.
 */
function checkContext(context: unknown): string | null {
  if (context === undefined || context === null) return null;
  if (!isRecord(context)) return notDict("context", "ContextData");
  const compaction = context.compaction;
  if (compaction === undefined || compaction === null) return null;
  if (!isRecord(compaction)) return notDict("context.compaction", "Compaction");
  const { trigger, keep } = compaction;
  if (trigger !== undefined && trigger !== null) {
    if (!isRecord(trigger)) return notDict("context.compaction.trigger", "CompactionTrigger");
    const e = exactlyOne("context.compaction.trigger", "trigger", trigger, ["tokens", "messages", "fraction"]);
    if (e) return e;
  }
  if (keep !== undefined && keep !== null) {
    if (!isRecord(keep)) return notDict("context.compaction.keep", "CompactionKeep");
    return exactlyOne("context.compaction.keep", "keep", keep, ["messages", "fraction"]);
  }
  return null;
}

/**
 * `CompactionTrigger`·`CompactionKeep` 의 "정확히 하나" 검사. 필드 범위가 먼저이고 배타 조건이 나중이다.
 * 배타 조건은 model_validator 라 loc 이 모델 자신이고 `Value error, ` 가 붙는다.
 */
function exactlyOne(loc: string, what: string, obj: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const k of keys) {
    const e = k === "fraction" ? numInRange(`${loc}.fraction`, obj[k], { gt: 0, le: 1 }) : intAtLeast(`${loc}.${k}`, obj[k], 1);
    if (e) return e;
  }
  const given = keys.filter((k) => obj[k] != null);
  if (given.length !== 1) return valueError(loc, `${what} must have exactly one of ${keys.join(", ")}`);
  return null;
}

/**
 * 에이전트의 `skills` (`SkillPins`). 항목마다 필드(id·revision)가 먼저이고 모르는 키가 그다음이다(pydantic 의 순서). 중복은
 * 목록을 다 읽은 뒤의 AfterValidator 라 loc 이 목록이다.
 */
/** 코드 실행 칸(서버 `CodeExecution`). approval 은 결정 목록이고 겹치지 않는다. */
function checkCodeExecutionData(value: unknown): string | null {
  if (!isRecord(value)) return notDict("code_execution", "CodeExecution");
  const extra = Object.keys(value).find((k) => k !== "approval");
  if (extra) return `code_execution.${extra}: Extra inputs are not permitted`;
  const approval = value.approval ?? [];
  if (!Array.isArray(approval)) return "code_execution.approval: Input should be a valid list";
  for (const [i, decision] of approval.entries()) {
    if (!DECISIONS.includes(decision as Decision)) return oneOf(`code_execution.approval.${i}`, DECISIONS);
  }
  return new Set(approval).size === approval.length ? null : "code_execution: Value error, code_execution.approval repeats a decision";
}

function checkSkillPins(skills: unknown): string | null {
  if (!Array.isArray(skills)) return "skills: Input should be a valid list";
  for (const [i, pin] of skills.entries()) {
    const at = `skills.${i}`;
    if (!isRecord(pin)) return notDict(at, "SkillPin");
    const extra = Object.keys(pin).find((k) => k !== "id" && k !== "revision");
    const e = requiredNonEmptyStr(`${at}.id`, pin.id) ?? intAtLeast(`${at}.revision`, pin.revision, 1, { required: true }) ?? (extra ? `${at}.${extra}: Extra inputs are not permitted` : null);
    if (e) return e;
  }
  const ids = skills.map((pin) => (pin as SkillPin).id);
  return new Set(ids).size === ids.length ? null : "skills: Value error, skills contains duplicates";
}

/**
 * 가드레일 규칙 하나. `GuardrailRule` 의 필드(kind·value)가 먼저이고, 종류별 검사는 model_validator 라
 * loc 이 `rules.N` 이다. pii_mask 는 그 안에서 `PIIMask` 를 검증하므로 PIIMask 의 필드 오류는
 * `rules.N.types` 처럼 한 단계만 깊어진다.
 */
function checkGuardrailRule(at: string, r: unknown): string | null {
  if (!isRecord(r)) return notDict(at, "GuardrailRule");
  const extra = Object.keys(r).find((k) => k !== "kind" && k !== "value");
  if (extra) return `${at}.${extra}: Extra inputs are not permitted`;
  if (r.kind === undefined || r.kind === null) return `${at}.kind: Field required`;
  if (!GUARDRAIL_RULE_KINDS.includes(r.kind as GuardrailRuleKind)) return oneOf(`${at}.kind`, GUARDRAIL_RULE_KINDS);
  if (!("value" in r)) return `${at}.value: Field required`;
  if (r.kind === INJECTION_CHECK) return checkInjectionCheck(at, r.value);
  if (r.kind === "max_length") {
    if (!isInt(r.value) || r.value < 1) return valueError(at, "max_length value must be an integer >= 1");
    return null;
  }
  if (r.kind === "deny_pattern") {
    if (typeof r.value !== "string" || !r.value) return valueError(at, "deny_pattern value must be a non-empty regex");
    const e = regexError(r.value);
    return e ? valueError(at, e) : null;
  }
  // pii_mask: 서버는 여기서 PIIMask 를 통째로 검증한다
  const v = r.value;
  if (!isRecord(v)) return notDict(at, "PIIMask");
  const extraPii = Object.keys(v).find((k) => k !== "types" && k !== "regex");
  if (extraPii) return `${at}.${extraPii}: Extra inputs are not permitted`;
  if (v.types === undefined || v.types === null) return `${at}.types: Field required`;
  if (!Array.isArray(v.types)) return `${at}.types: Input should be a valid list`;
  if (!v.types.length) return `${at}.types: List should have at least 1 item after validation, not 0`;
  const badType = v.types.findIndex((t) => !PII_TYPES.includes(t as PIIType));
  if (badType >= 0) return oneOf(`${at}.types.${badType}`, PII_TYPES);
  if (v.regex !== undefined && v.regex !== null && typeof v.regex !== "string") return `${at}.regex: Input should be a valid string`;
  // PIIMask._check (model_validator)
  if (new Set(v.types).size !== v.types.length) return valueError(at, "types contains duplicates");
  if (v.types.includes("regex") && !v.regex) return valueError(at, "types has regex, so regex is required");
  if (typeof v.regex === "string") {
    const e = regexError(v.regex);
    if (e) return valueError(at, e);
  }
  return null;
}

/** 판정 모델 규칙의 값. 서버는 `InjectionCheck` 를 통째로 검증하므로 필드 오류가 `rules.N.max_chars` 처럼 한 단계만 깊어진다. */
function checkInjectionCheck(at: string, v: unknown): string | null {
  if (!isRecord(v)) return notDict(at, "InjectionCheck");
  const extra = Object.keys(v).find((k) => k !== "model" && k !== "max_chars" && k !== "max_calls");
  if (extra) return `${at}.${extra}: Extra inputs are not permitted`;
  // 서버 `InjectionCheck` 는 null 을 기본값으로 바꾸지 않고 거절한다
  if (v.model !== undefined && typeof v.model !== "string") return `${at}.model: Input should be a valid string`;
  for (const [key, range] of [
    ["max_chars", JUDGE_MAX_CHARS],
    ["max_calls", JUDGE_MAX_CALLS],
  ] as const) {
    const value = v[key];
    if (value === null) return intError(`${at}.${key}`, value);
    const e = intAtLeast(`${at}.${key}`, value, range.min);
    if (e) return e;
    if (isInt(value) && value > range.max) return `${at}.${key}: Input should be less than or equal to ${range.max}`;
  }
  return null;
}

// -- 검증 이슈의 화면 문장 ---------------------------------------------------------------------

/** 이슈가 붙은 자리. 같은 code 라도 노드에 붙은 것과 간선에 붙은 것의 뜻이 다를 때 문장 표가 이것으로 가른다. */
export type IssueAt = { node_id?: string | null; edge_id?: string | null; data?: Record<string, unknown> | null };

/** 사전 `canvas.issues.text` 가 덮는 code. 서버가 내는 code 를 하나도 빠뜨리지 않았는지 `src/test/agent-graph.test.ts` 가 본다. */
export type IssueCode = keyof Messages["canvas"]["issues"]["text"];

/** provider 를 고른 자리. 모델 노드의 provider 와 폴백 provider 는 같은 규칙을 따르고 상세의 낱말만 다르다. */
export type ProviderRole = "provider" | "fallback";

/** 키마다 문장을 둔 사전 표. 값을 받지 않는 문장은 문자열이고, 값을 받는 문장은 함수다. */
type TextTable = Record<string, string | ((...args: never[]) => string)>;

/** 키마다 문장에 넘길 인자. 문자열 문장은 인자가 없다. */
type ArgsOf<T extends TextTable> = { [K in keyof T]: T[K] extends (...args: infer A) => string ? A : [] };

/** 표의 키 하나와 그 문장에 넘길 인자. 문장은 그릴 때 표에서 만든다. 키를 주면 그 키의 인자 꼴로 좁혀진다. */
type Keyed<T extends TextTable, K extends keyof T = keyof T> = { [P in K]: { key: P; args: ArgsOf<T>[P] } }[K];

/** 키와 인자로 표의 문장을 만든다. 문자열 문장은 그대로 돌려준다. 키를 모르는 채로 불러도 인자가 그 키의 꼴인지 타입이 본다. */
function keyedText<T extends TextTable, K extends keyof T>(table: { [P in keyof T]: string | ((...args: ArgsOf<T>[P]) => string) }, d: Keyed<T, K>): string {
  const entry = table[d.key];
  return typeof entry === "string" ? entry : entry(...d.args);
}

/**
 * 상세의 표 둘. 그래프 검사의 것은 사전의 `canvas.issues.detail`, Skill 검사(agentSkills.ts 의 skillFindings)의 것은
 * `canvas.issues.skillDetail` 이고 두 표의 키는 겹치지 않는다. 노드·간선 종류와 Redis 상태는 id 로 받고 화면 낱말은 사전이 찾는다.
 */
type GraphDetailTable = Messages["canvas"]["issues"]["detail"];
type SkillDetailTable = Messages["canvas"]["issues"]["skillDetail"];
type DetailTable = GraphDetailTable & SkillDetailTable;

/** 이슈의 상세 하나. 두 표의 키와 그 문장에 넘길 인자다. 키를 주면 그 키의 인자 꼴로 좁혀진다. */
export type IssueDetail<K extends keyof DetailTable = keyof DetailTable> = Keyed<DetailTable, K>;

/** 상세를 키와 인자로 만든다. 문장은 그릴 때 만든다. */
export function detail<K extends keyof DetailTable>(key: K, ...args: ArgsOf<DetailTable>[K]): IssueDetail<K> {
  return { key, args };
}

/** Skill 검사의 상세인가. 키가 그 표에 있는지로 가른다. */
function isSkillDetail(t: Messages, d: IssueDetail): d is IssueDetail<keyof SkillDetailTable> {
  return Object.hasOwn(t.canvas.issues.skillDetail, d.key);
}

/** 상세의 문장. 키가 든 표 하나에서 만든다. */
function detailText(t: Messages, d: IssueDetail): string {
  if (isSkillDetail(t, d)) return keyedText<SkillDetailTable, keyof SkillDetailTable>(t.canvas.issues.skillDetail, d);
  return keyedText<GraphDetailTable, keyof GraphDetailTable>(t.canvas.issues.detail, d);
}

/** 경고 표(warnings.codes)의 그 code 문장. 문장이 값을 받아 만들어지는 code 와 표에 없는 code 는 undefined 다. */
function warningSentence(t: Messages, code: string): string | undefined {
  const entry = ownValue<object>(t.warnings.codes, code);
  return entry && "text" in entry && typeof entry.text === "string" ? entry.text : undefined;
}

/**
 * 이슈 한 건의 화면 문장. code 로 사전의 `canvas.issues.text` 를 찾고, 캔버스 검증기가 낸 이슈면 뒤에 상세를 붙인다. 서버
 * `POST /validate`·저장 응답이 준 영어 이슈도 같은 code 로 찾아 화면 언어로 보인다. 그 표에 없는 code 는 오류 표(errors.codes)와
 * 경고 표(warnings.codes)의 문장이다. 서버만 보는 Agent 소스의 이슈가 거기 있다(P46 6.2절). 어디에도 없으면(서버가 규칙을 더한 때)
 * 서버 문장을 그대로 보인다. `code` 자체는 화면 문장에 넣지 않는다. 배지·목록이 title 이나 작은 글씨로 따로 보인다.
 */
export function issueText(t: Messages, issue: Issue): string {
  const entry = ownValue<string | ((at: IssueAt) => string)>(t.canvas.issues.text, issue.code);
  if (entry === undefined) return ownValue(t.errors.codes, issue.code) ?? warningSentence(t, issue.code) ?? issue.message ?? issue.code;
  const text = typeof entry === "function" ? entry(issue) : entry;
  return issue.detail ? `${text} ${detailText(t, issue.detail)}` : text;
}

interface Analysis {
  nodes: Map<string, GraphNode>;
  order: string[];
  valid: Set<string>;
  edges: GraphEdge[];
  issues: Issue[];
  entry: string | null;
  main: string | null;
  reachable: Set<string>;
}

/** 이슈 하나. 문장은 들지 않는다. `extra` 를 주면 그릴 때 code 별 문장 뒤에 그 상세가 붙는다 (이슈마다 다른 이름·값·이유). */
function issue(a: Analysis, code: IssueCode, level: Issue["level"] = "error", where: IssueAt = {}, extra?: IssueDetail): void {
  a.issues.push({ code, level, node_id: where.node_id ?? null, edge_id: where.edge_id ?? null, detail: extra });
}

function agentOf(a: Analysis, id: string): AgentData | null {
  const n = a.nodes.get(id);
  return n && n.type === "agent" && a.valid.has(id) ? (n.data as AgentData) : null;
}

function ofType(a: Analysis, type: NodeType): string[] {
  return a.order.filter((id) => a.nodes.get(id)!.type === type);
}

/** `type` 노드 중 data 형식 검사를 통과한 것의 id 와 data. 그래프 순서이고 서버 `Analysis.nodes_of` 와 같다. */
function nodesOf<T extends NodeType>(a: Analysis, type: T): [string, NodeDataByType[T]][] {
  return ofType(a, type)
    .filter((id) => a.valid.has(id))
    .map((id) => [id, a.nodes.get(id)!.data as NodeDataByType[T]]);
}

function outEdges(a: Analysis, id: string, kind: EdgeKind): GraphEdge[] {
  return a.edges.filter((e) => e.source === id && e.kind === kind);
}

function targets(a: Analysis, id: string, kind: EdgeKind): string[] {
  const wanted = new Set(outEdges(a, id, kind).map((e) => e.target));
  return a.order.filter((i) => wanted.has(i));
}

/** 형식 검사를 통과한 knowledge 노드의 data. 다른 종류이거나 형식이 틀렸으면 null 이다. */
function knowledgeOf(a: Analysis, id: string): KnowledgeData | null {
  return a.valid.has(id) && a.nodes.get(id)?.type === "knowledge" ? (a.nodes.get(id)!.data as KnowledgeData) : null;
}

/** 검색 도구 이름. 값이 빠졌으면 서버가 채우는 기본값이다. */
function toolNameOf(d: KnowledgeData): string {
  return typeof d.tool_name === "string" ? d.tool_name : DEFAULT_KNOWLEDGE_TOOL_NAME;
}

/**
 * provider 에 키가 있는가. stub 은 면제다. 서버 `key_checker` 는 Secret 을 직접 읽지만 여기서는 `api_key_secret`
 * 이 비었는가로 대신 본다. 모델 노드와 knowledge 노드의 임베딩 provider 가 같은 판정을 쓴다.
 */
function providerHasKey(p: LLMProvider): boolean {
  return p.kind === "stub" || Boolean(p.api_key_secret);
}

/** Chat 트리거와 이어진 에이전트에 붙어야 쓰이는 노드 종류. 서버 ATTACHABLE 과 같다. */
const ATTACHABLE = ["model", "tools", "memory", "guardrail", "knowledge"] as const;

// 아래 check 함수들은 서버 `agent_graph.py` 의 같은 이름(`_check_*`)과 하나씩 짝이다. 검사 순서도 서버의
// `analyze` 와 같아서 이슈가 같은 순서로 나온다. 한쪽 규칙을 고치면 짝이 되는 함수를 함께 본다.

/** 노드 data 형식과 서브 에이전트의 설명. 서버 `_parse_nodes`. */
function parseNodes(graph: Graph, a: Analysis): void {
  for (const node of graph.nodes) {
    if (a.nodes.has(node.id)) {
      issue(a, "duplicate_node_id", "error", { node_id: node.id });
      continue;
    }
    a.nodes.set(node.id, node);
    a.order.push(node.id);
    const err = checkNodeData(node.type, node.data);
    // 상세는 서버 pydantic 이 내는 것과 같은 `필드: 이유` 다. 저장이 거부됐을 때 받는 것과 같은 자리를 가리킨다
    if (err) issue(a, "invalid_node_data", "error", { node_id: node.id }, detail("nodeData", err));
    else a.valid.add(node.id);
    // 모르는 키를 버리는 종류도 저장하는 사람에게는 알린다. `allow_domain` 같은 오타가 안전 규칙을 조용히 빼지 않게 한다
    const allowed = NODE_DATA_KEYS[node.type];
    if (!err && EXTRA_IGNORED.has(node.type) && isRecord(node.data) && Array.isArray(allowed)) {
      if (Object.keys(node.data).some((k) => !allowed.includes(k))) issue(a, "unknown_node_keys", "warning", { node_id: node.id });
    }
  }
  for (const [id, d] of nodesOf(a, "agent")) {
    if (d.role === "sub" && !d.description.trim()) {
      issue(a, "invalid_node_data", "error", { node_id: id }, detail("subAgentDescription"));
    }
  }
}

/** Chat 트리거와 메인 에이전트는 하나씩, memory 노드는 하나까지다. 서버 `_check_counts`. */
function checkCounts(a: Analysis): void {
  const entries = ofType(a, "entry");
  if (!entries.length) issue(a, "entry_count");
  else {
    a.entry = entries[0];
    for (const extra of entries.slice(1)) issue(a, "entry_count", "error", { node_id: extra });
  }
  const mains = nodesOf(a, "agent")
    .filter(([, d]) => d.role === "main")
    .map(([id]) => id);
  if (!mains.length) issue(a, "main_agent_count");
  else {
    a.main = mains[0];
    for (const extra of mains.slice(1)) issue(a, "main_agent_count", "error", { node_id: extra });
  }
  for (const extra of ofType(a, "memory").slice(1)) issue(a, "memory_count", "error", { node_id: extra });
}

/** 간선 id·끝점·종류와 역할에 따른 규칙. 서버 `_check_edges`. */
function checkEdges(graph: Graph, a: Analysis): void {
  const seenEdges = new Set<string>();
  for (const edge of graph.edges) {
    if (seenEdges.has(edge.id)) {
      issue(a, "duplicate_edge_id", "error", { edge_id: edge.id });
      continue;
    }
    seenEdges.add(edge.id);
    const src = a.nodes.get(edge.source);
    const dst = a.nodes.get(edge.target);
    if (!src || !dst) {
      const missing = src ? edge.target : edge.source;
      issue(a, "dangling_edge", "error", { edge_id: edge.id }, detail("missingNode", missing));
      continue;
    }
    a.edges.push(edge);
    const expected = edgeKindFor(src.type, dst.type);
    if (!expected || expected !== edge.kind) {
      issue(a, "invalid_edge_kind", "error", { edge_id: edge.id }, detail("edgeBetween", src.type, dst.type, edge.kind));
      continue;
    }
    const srcAgent = agentOf(a, edge.source);
    const dstAgent = agentOf(a, edge.target);
    if (edge.kind === "entry" && dstAgent && dstAgent.role !== "main") {
      issue(a, "invalid_edge_kind", "error", { edge_id: edge.id }, detail("entryToSub"));
    } else if (edge.kind === "delegate") {
      if (edge.source === edge.target) issue(a, "delegation_cycle", "error", { edge_id: edge.id }, detail("selfDelegation"));
      else if (srcAgent && srcAgent.role !== "main") issue(a, "invalid_edge_kind", "error", { edge_id: edge.id }, detail("delegationFromSub"));
      else if (dstAgent && dstAgent.role !== "sub") issue(a, "invalid_edge_kind", "error", { edge_id: edge.id }, detail("delegationToMain"));
    } else if ((edge.kind === "memory" || edge.kind === "guardrail") && srcAgent && srcAgent.role !== "main") {
      issue(a, "invalid_edge_kind", "error", { edge_id: edge.id }, detail("attachedToSub"));
    }
  }
}

/** 위임 순환. 되돌아가는 간선(back edge)마다 하나다. 자기 위임은 checkEdges 가 봤다. 서버 `_check_delegation_cycles`. */
function checkDelegationCycles(a: Analysis): void {
  const out = groupBy(
    a.edges.filter((e) => e.kind === "delegate" && e.source !== e.target),
    (e) => e.source,
  );
  const state = new Map<string, 1 | 2>();
  const visit = (id: string): void => {
    state.set(id, 1);
    for (const e of out.get(id) ?? []) {
      const s = state.get(e.target);
      if (s === 1) issue(a, "delegation_cycle", "error", { edge_id: e.id }, detail("delegationReturn", e.target));
      else if (!s) visit(e.target);
    }
    state.set(id, 2);
  };
  for (const id of ofType(a, "agent")) if (!state.has(id)) visit(id);
}

/** 도달성: entry 에서 메인(entry 간선), 메인에서 서브(delegate 간선)로 이어진다. 에이전트는 모두 entry 와 이어져 있어야 한다. 서버 `_check_reachability`. */
function checkReachability(a: Analysis): void {
  if (!a.entry) return;
  a.reachable.add(a.entry);
  const stack = targets(a, a.entry, "entry").filter((t) => a.nodes.get(t)!.type === "agent");
  while (stack.length) {
    const id = stack.pop()!;
    if (a.reachable.has(id)) continue;
    a.reachable.add(id);
    stack.push(...targets(a, id, "delegate"));
  }
  for (const id of ofType(a, "agent")) {
    if (!a.reachable.has(id)) {
      const hint = agentOf(a, id)?.role === "main" ? detail("unreachableMain") : detail("unreachableSub");
      issue(a, "unreachable_agent", "error", { node_id: id }, hint);
    }
  }
}

/** 에이전트마다 model 간선은 정확히 하나다 (D28). 서버 `_check_models`. */
function checkModels(a: Analysis): void {
  for (const id of ofType(a, "agent")) {
    // 서버와 같이 모델 노드로 가는 간선만 센다. 종류가 틀린 간선은 invalid_edge_kind 가 따로 본다
    const models = outEdges(a, id, "model").filter((e) => a.nodes.get(e.target)?.type === "model");
    if (!models.length) issue(a, "model_count", "error", { node_id: id });
    // 서버는 넘치는 model 간선에 node_id 와 edge_id 를 함께 단다. 에이전트 카드에도 표식이 붙어야 한다
    for (const extra of models.slice(1)) issue(a, "model_count", "error", { node_id: id, edge_id: extra.id });
  }
}

/** 메인이 붙인 guardrail 노드의 판정 모델 규칙. 그래프 순서다. 서버 `Analysis.judges`. */
function judges(a: Analysis): [string, InjectionCheck][] {
  if (!a.main) return [];
  const attached = new Set(targets(a, a.main, "guardrail"));
  return nodesOf(a, "guardrail").flatMap(([id, d]): [string, InjectionCheck][] => {
    const judge = judgeOf(d);
    return attached.has(id) && judge ? [[id, judge]] : [];
  });
}

/** 판정 모델은 같은 그래프의 모델 노드이고, 메인이 붙인 guardrail 노드 가운데 판정은 하나까지다. 서버 `_check_judges`. */
function checkJudges(a: Analysis): void {
  for (const [id, d] of nodesOf(a, "guardrail")) {
    const judge = judgeOf(d);
    if (!judge) continue;
    if (a.nodes.get(judge.model)?.type !== "model") {
      issue(a, "unknown_judge_model", "error", { node_id: id }, judge.model ? detail("judgeModelMissing", judge.model) : detail("judgeModelUnset"));
    }
  }
  for (const [id] of judges(a).slice(1)) issue(a, "judge_count", "error", { node_id: id });
}

/** 메인의 memory 간선은 하나까지이고, entry 와 이어진 에이전트에 붙지 않은 ATTACHABLE 노드는 경고다. 서버 `_check_attachments`. */
function checkAttachments(a: Analysis): void {
  if (a.main) {
    for (const extra of outEdges(a, a.main, "memory").slice(1)) issue(a, "memory_count", "error", { edge_id: extra.id });
  }
  const attached = new Set<string>();
  for (const id of a.reachable) for (const kind of ATTACHABLE) for (const t of targets(a, id, kind)) attached.add(t);
  // 판정 모델 노드는 간선 없이 메인이 붙인 guardrail 노드의 규칙이 가리킨다(P43 7절)
  for (const [, judge] of judges(a)) attached.add(judge.model);
  for (const type of ATTACHABLE) {
    for (const id of ofType(a, type)) {
      if (!attached.has(id)) issue(a, "unattached_node", "warning", { node_id: id });
    }
  }
}

/** 에이전트 이름은 서로 달라야 한다. 서버 `_check_agent_names`. */
function checkAgentNames(a: Analysis): void {
  const names = new Map<string, string>();
  for (const [id, d] of nodesOf(a, "agent")) {
    const first = names.get(d.name);
    if (first !== undefined) issue(a, "duplicate_agent_name", "error", { node_id: id }, detail("agentNameTaken", first));
    else names.set(d.name, id);
  }
}

type Providers = Map<string, LLMProvider>;

/** provider 하나 (모델 노드의 provider 또는 폴백 provider). 표식은 모델 노드에 붙는다. */
function checkProvider(a: Analysis, providers: Providers, id: string, name: string, temperature: number | null, role: ProviderRole): void {
  const p = providers.get(name);
  if (!p) {
    issue(a, "unknown_provider", "error", { node_id: id }, name ? detail("providerMissing", role, name) : detail("providerUnset", role));
    return;
  }
  if (!providerHasKey(p)) issue(a, "provider_no_key", "error", { node_id: id }, detail("providerNoKey", role, name));
  if (p.kind === "anthropic" && temperature != null) issue(a, "unsupported_param", "warning", { node_id: id }, detail("providerName", role, name));
}

/** 모델 노드의 provider 와 폴백 provider. 붙지 않은 모델 노드도 같은 규칙이다. 서버 `_check_providers`. */
function checkProviders(a: Analysis, providers: Providers): void {
  for (const [id, d] of nodesOf(a, "model")) {
    checkProvider(a, providers, id, d.provider, d.temperature, "provider");
    if (d.fallback) checkProvider(a, providers, id, d.fallback.provider, d.temperature, "fallback");
  }
}

/**
 * MCP 노드의 도구 소스. 테넌트에 있는가, 배포됐는가, oidc 모드면 agent_oauth 가 있는가. 서버 `_check_sources`. 게시한 Agent 를
 * 부르는 소스는 서버만 본다. 항목의 접근 정책과 호출받는 Agent 의 배포를 읽어야 해서다(서버 `agent_calls.call_issues`).
 */
function checkSources(a: Analysis, ctx: ValidationContext): void {
  for (const [id, d] of nodesOf(a, "tools")) {
    if (d.source.kind === AGENT_SOURCE) continue;
    const kind = SOURCE_KINDS[d.source.kind];
    const source = ctx[kind.listKey].find((x) => x.id === d.source.id);
    if (!source) {
      issue(a, "unknown_source", "error", { node_id: id }, d.source.id ? detail("sourceMissing", kind.label, d.source.id) : detail("sourceUnset", kind.label));
      continue;
    }
    // 정의만 있는 것은 부를 주소가 없다. 서버도 같은 순서로 존재 다음에 배포를 본다
    if (source.state !== "deployed") {
      issue(a, "source_not_deployed", "error", { node_id: id }, detail("sourceState", kind.label, d.source.id, source.state));
      continue;
    }
    if (d.source.kind === "gateway_instance" && ctx.oidcMode) {
      if (!ctx.agentOauth) {
        issue(a, "agent_oauth_missing", "error", { node_id: id });
      } else if (!ctx.agentClientIds.includes(ctx.agentOauth.client_id)) {
        issue(a, "agent_client_id_not_allowed", "error", { node_id: id }, detail("clientIdNotAllowed", ctx.agentOauth.client_id));
      }
    }
  }
}

/** memory 노드의 Database. 테넌트에 있고 postgres·redis 여야 하며 redis 는 verify 가 ready 이고 db 0 이어야 한다. 서버 `_check_memory`. */
function checkMemory(a: Analysis, ctx: ValidationContext): void {
  for (const [id, d] of nodesOf(a, "memory")) {
    const dsId = d.checkpointer?.data_service_id ?? "";
    const ds = ctx.dataServices.find((x) => x.id === dsId);
    if (!ds) issue(a, "unknown_data_service", "error", { node_id: id }, dsId ? detail("databaseMissing", dsId) : undefined);
    else if (ds.kind !== "postgres" && ds.kind !== "redis") issue(a, "invalid_data_service_kind", "error", { node_id: id }, detail("databaseKind", ds.name, ds.kind));
    else if (ds.kind === "redis" && ds.capabilities?.checkpointer !== "ready") issue(a, "redis_modules_missing", "error", { node_id: id }, detail("redisCheckpointer", redisCheckpointerState(ds.capabilities?.checkpointer), ds.name));
    // db 0 검사는 modules 검사와 독립이다(서버도 같다). 둘 다 틀리면 두 표식이 함께 붙는다
    if (ds && ds.kind === "redis" && (ds.db ?? 0) !== 0) issue(a, "redis_db_not_zero", "error", { node_id: id }, detail("redisDb", ds.name, ds.db ?? 0));
  }
}

/**
 * 장기 기억을 켠 memory 노드 (D34). 저장소(메인에 붙은 memory)와 메인 에이전트의 파일 도구가 함께 있어야 런타임이
 * 뜬다. 두 조건은 서로 독립이라 둘 다 틀리면 두 표식이 함께 붙는다. 서버 `_check_long_term`.
 */
function checkLongTerm(a: Analysis): void {
  const attached = new Set(a.main ? targets(a, a.main, "memory") : []);
  const main = a.main ? agentOf(a, a.main) : null;
  for (const [id, d] of nodesOf(a, "memory")) {
    if (!longTermEnabled(d)) continue;
    if (!attached.has(id)) issue(a, "long_term_needs_memory", "error", { node_id: id });
    // 키가 없으면 서버 기본값과 같이 켜진 것으로 읽는다. 그러지 않으면 서버는 통과시키는 그래프를 화면만 막는다
    if (main && !(main.builtin_tools?.filesystem ?? true)) issue(a, "long_term_filesystem_disabled", "error", { node_id: id }, detail("filesystemOff", main.name));
  }
}

/**
 * 코드 실행 (P40 9절). 파일 Tool 이 작업 공간을 쓰므로 메인의 파일 Tool 이 켜져 있어야 하고, 메인의 턴 제한이 명령 하나의 가장 긴
 * 시간보다 짧으면 경고한다. 서버 `_check_code_execution`.
 */
function checkCodeExecution(a: Analysis): void {
  const main = a.main ? agentOf(a, a.main) : null;
  if (!a.main || !main?.code_execution) return;
  // 키가 없으면 서버 기본값과 같이 켜진 것으로 읽는다 (long_term 과 같다)
  if (!(main.builtin_tools?.filesystem ?? true)) issue(a, "code_execution_filesystem_disabled", "error", { node_id: a.main });
  // 키가 없으면 서버 기본값(AgentData.turn_timeout_seconds)으로 읽는다
  if ((main.turn_timeout_seconds ?? DEFAULT_TURN_TIMEOUT_SECONDS) < SANDBOX_COMMAND_MAX_SECONDS) issue(a, "code_execution_turn_timeout_short", "warning", { node_id: a.main });
}

/**
 * 메인에 memory 가 없는데 공유 카운터·체크포인터가 필요한 설정(분당 한도·일당 예산·승인)이 있으면 오류다. 그래프
 * 전체의 규칙이라 표식이 붙는 노드는 없다. 서버 `_check_memory_required`.
 */
function checkMemoryRequired(a: Analysis, service: ServiceSettings): void {
  if (!a.main || targets(a, a.main, "memory").length) return;
  const reasons: string[] = [];
  const entry = a.entry && a.valid.has(a.entry) ? (a.nodes.get(a.entry)!.data as EntryData) : null;
  if (entry?.rate_limit) reasons.push("entry.rate_limit");
  if (service.budget.daily_tokens != null) reasons.push("service.budget.daily_tokens");
  for (const agentId of [...a.reachable].sort()) {
    for (const toolsId of targets(a, agentId, "tools")) {
      if (!a.valid.has(toolsId)) continue;
      const d = a.nodes.get(toolsId)!.data as ToolsData;
      if (Object.keys(d.approval ?? {}).length) reasons.push(`tools '${toolsId}' approval`);
    }
  }
  // 이유는 서버가 세는 것과 같은 이름이다. 어느 설정이 공유 카운터를 부르는지 그대로 짚어 준다
  if (reasons.length) issue(a, "memory_required", "error", {}, detail("memoryReasons", reasons));
}

/**
 * knowledge 노드 (D32). 도구 이름이 내장 도구와 겹치지 않고, Knowledge Base 가 있고 지워지는 중이 아니며 임베딩
 * provider 에 키가 있어야 한다. 색인된 문서가 없거나 실패가 있으면 경고다. 표식은 전부 knowledge 노드이고 붙지 않은
 * 노드도 같은 규칙이다(모델 노드와 같다). 서버 `_check_knowledge`.
 */
function checkKnowledge(a: Analysis, ctx: ValidationContext, providers: Providers): void {
  const kbs = new Map(ctx.knowledgeBases.map((k) => [k.id, k]));
  for (const [id, d] of nodesOf(a, "knowledge")) {
    const toolName = toolNameOf(d);
    if (RESERVED_TOOL_NAMES.has(toolName)) issue(a, "reserved_tool_name", "error", { node_id: id }, detail("reservedToolName", toolName));
    const kbId = d.knowledge_base_id ?? "";
    const kb = kbs.get(kbId);
    if (!kb) {
      issue(a, "unknown_knowledge_base", "error", { node_id: id }, kbId ? detail("knowledgeBaseMissing", kbId) : undefined);
      continue;
    }
    if (kb.deleting_at) issue(a, "knowledge_base_deleting", "error", { node_id: id }, detail("knowledgeBaseDeleting", kb.name));
    // 문장이 모델 노드의 provider 와 다르다(어느 Knowledge Base 의 임베딩인지 적는다). 그래서 checkProvider 를 쓰지 않는다
    const p = providers.get(kb.embedding.provider);
    if (!p) issue(a, "unknown_provider", "error", { node_id: id }, detail("embeddingProviderMissing", kb.name, kb.embedding.provider));
    else if (!supportsEmbedding(p.kind)) issue(a, "embedding_not_supported", "error", { node_id: id }, detail("embeddingKind", kb.name, p.name, p.kind));
    else if (!providerHasKey(p)) issue(a, "provider_no_key", "error", { node_id: id }, detail("embeddingNoKey", kb.name, p.name));
    const status = kb.status ?? { indexed: 0, failed: 0 };
    if (!status.indexed) issue(a, "knowledge_base_empty", "warning", { node_id: id }, detail("knowledgeBase", kb.name));
    if (status.failed) issue(a, "knowledge_base_failed", "warning", { node_id: id }, detail("knowledgeBaseFailed", kb.name, status.failed));
  }
}

/** 한 에이전트에 붙은 knowledge 노드끼리 도구 이름이 겹치면 오류다(런타임에서 하나가 다른 하나를 덮는다). 서버 `_check_knowledge_tool_names`. */
function checkKnowledgeToolNames(a: Analysis): void {
  for (const agentId of ofType(a, "agent")) {
    const first = new Map<string, string>();
    for (const kid of targets(a, agentId, "knowledge")) {
      const d = knowledgeOf(a, kid);
      if (!d) continue;
      const toolName = toolNameOf(d);
      const prev = first.get(toolName);
      if (prev) issue(a, "duplicate_tool_name", "error", { node_id: kid }, detail("toolNameTaken", toolName, prev));
      else first.set(toolName, kid);
    }
  }
}

/** 에이전트 노드의 Skill (D64). 규칙은 `agentSkills.ts` 한 곳이고 여기서는 이슈로 옮긴다. 서버 `_check_skills`. */
function checkSkills(a: Analysis, ctx: ValidationContext): void {
  const main = a.main ? agentOf(a, a.main) : null;
  // 키가 없으면 서버 기본값과 같이 켜진 것으로 읽는다 (long_term 과 같다)
  const filesystem = !main || (main.builtin_tools?.filesystem ?? true);
  const agents = nodesOf(a, "agent").map(([id, d]) => ({ nodeId: id, pins: pinsOf(d) }));
  for (const f of skillFindings(agents, filesystem, ctx.skillRevisions)) issue(a, f.code, f.level, { node_id: f.nodeId }, f.detail);
}

/** docs/guide/agents.md 1절의 규칙 전부. 오류와 경고를 함께 돌려주고 순서는 서버와 같은 검사 순서다(`agent_graph.py` 의 `analyze`). */
export function validateGraph(graph: Graph, service: ServiceSettings, ctx: ValidationContext): Issue[] {
  const a: Analysis = { nodes: new Map(), order: [], valid: new Set(), edges: [], issues: [], entry: null, main: null, reachable: new Set() };
  const providers: Providers = new Map(ctx.providers.map((p) => [p.name, p]));
  parseNodes(graph, a);
  checkCounts(a);
  checkEdges(graph, a);
  checkDelegationCycles(a);
  checkReachability(a);
  checkModels(a);
  checkJudges(a);
  checkAttachments(a);
  checkAgentNames(a);
  checkProviders(a, providers);
  checkSources(a, ctx);
  checkMemory(a, ctx);
  checkLongTerm(a);
  checkCodeExecution(a);
  checkMemoryRequired(a, service);
  checkKnowledge(a, ctx, providers);
  checkKnowledgeToolNames(a);
  checkSkills(a, ctx);
  return a.issues;
}

export function countIssues(issues: Issue[]): { errors: number; warnings: number } {
  let errors = 0;
  let warnings = 0;
  for (const i of issues) {
    if (i.level === "error") errors += 1;
    else warnings += 1;
  }
  return { errors, warnings };
}

/** `key` 가 같은 것끼리 입력 순서대로 모은다. key 가 비면 뺀다. */
function groupBy<T>(items: Iterable<T>, key: (item: T) => string | null | undefined): Map<string, T[]> {
  const out = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    if (!k) continue;
    const bucket = out.get(k);
    if (bucket) bucket.push(item);
    else out.set(k, [item]);
  }
  return out;
}

export function issuesByNode(issues: Issue[]): Map<string, Issue[]> {
  return groupBy(issues, (i) => i.node_id);
}

export function issuesByEdge(issues: Issue[]): Map<string, Issue[]> {
  return groupBy(issues, (i) => i.edge_id);
}

/** 연결할 수 없는 까닭. 사전 `canvas.connection` 의 키와 인자이고 문장은 그릴 때 `connectionText` 가 만든다. */
export type ConnectionProblem = Keyed<Messages["canvas"]["connection"]>;

/** 연결할 수 없는 까닭의 화면 문장. 카드 아래와 툴팁이 보인다. */
export function connectionText(t: Messages, problem: ConnectionProblem): string {
  return keyedText<Messages["canvas"]["connection"], ConnectionProblem["key"]>(t.canvas.connection, problem);
}

/**
 * 캔버스가 연결을 허용할지 (포트 호환 힌트). `kind` 는 끌기 시작한 포트의 간선 종류다(핸들 id). 없으면 노드 종류
 * 대응표에서 정한다. 종류 대응표와 간선 규칙 중 노드만 보고 알 수 있는 것(포트 종류와 대상 종류의 대응, entry 는
 * 메인으로만 가는 것, 서브는 위임하지 못하는 것, 위임 대상은 서브라는 것, 자기 연결·중복 연결 금지, entry 간선
 * 하나, 에이전트의 model 하나, 메인의 memory 하나)이다.
 */
export function connectionProblem(source: PlacedNode, target: PlacedNode, edges: PlacedEdge[], kind: EdgeKind | null = null): ConnectionProblem | null {
  if (source.id === target.id) return { key: "self", args: [] };
  const expected = edgeKindFor(source.type, target.type);
  if (kind && expected !== kind) return { key: "wrongPort", args: [kind, target.type] };
  if (!expected) return { key: "notAllowed", args: [source.type, target.type] };
  if (edges.some((e) => e.source === source.id && e.target === target.id)) return { key: "already", args: [] };
  const src = source.type === "agent" ? (source.data as AgentData) : null;
  const dst = target.type === "agent" ? (target.data as AgentData) : null;
  if (expected === "entry") {
    if (dst && dst.role !== "main") return { key: "entryToSub", args: [] };
    if (edges.some((e) => e.source === source.id && e.kind === "entry")) return { key: "entryOnce", args: [] };
  }
  if (expected === "delegate") {
    if (src && src.role !== "main") return { key: "delegateFromSub", args: [] };
    if (dst && dst.role !== "sub") return { key: "delegateToMain", args: [] };
  }
  if (expected === "memory" && src && src.role !== "main") return { key: "memoryToSub", args: [] };
  if (expected === "guardrail" && src && src.role !== "main") return { key: "guardrailToSub", args: [] };
  if (expected === "model" && edges.some((e) => e.source === source.id && e.kind === "model")) return { key: "oneModel", args: [] };
  if (expected === "memory" && edges.some((e) => e.source === source.id && e.kind === "memory")) return { key: "oneMemory", args: [] };
  return null;
}

export interface ResolvedConnection {
  source: PlacedNode;
  target: PlacedNode;
  kind: EdgeKind;
  /** 끈 방향과 반대로 이었는가. */
  flipped: boolean;
}

/**
 * 끌어 놓은 두 노드를 어느 방향으로 이을지 정한다.
 *
 * 쌍마다 맞는 방향은 하나뿐이다(`hasSingleDirection`). 모델을 끌어 에이전트에 놓든 에이전트에서 끌어 모델에
 * 놓든 뜻하는 것은 "이 에이전트가 이 모델을 쓴다" 하나라, 반대로 끌었으면 화면이 뒤집어 준다. 포트에서 끌었으면
 * 그 포트의 종류와 맞는 방향만 뒤집는다. 둘 다 안 되면 앞 방향의 이유를 돌려준다.
 */
export function resolveConnection(from: PlacedNode, to: PlacedNode, edges: PlacedEdge[], portKind: EdgeKind | null): ResolvedConnection | { problem: ConnectionProblem } {
  const forward = connectionProblem(from, to, edges, portKind);
  if (forward === null) {
    const kind = portKind ?? edgeKindFor(from.type, to.type);
    if (kind) return { source: from, target: to, kind, flipped: false };
  }
  const back = edgeKindFor(to.type, from.type);
  if (back && (portKind === null || portKind === back) && connectionProblem(to, from, edges, back) === null) {
    return { source: to, target: from, kind: back, flipped: true };
  }
  return { problem: forward ?? { key: "notAllowed", args: [from.type, to.type] } };
}
