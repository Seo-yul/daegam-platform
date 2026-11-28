/**
 * 인스펙터가 주고받는 것. 패널 파일들이 순환 import 없이 쓸 수 있게 여기 둔다.
 * 화면 밖에서는 `@/components/canvas/Inspector` 가 다시 내보내는 이름을 쓴다.
 */
import type { AgentCall, DataService, Gateway, HubItem, KnowledgeBase, LLMProvider, MCPServer, Skill, ToolEffect } from "@/api/types";
import type { NameErrors, NameValue } from "@/components/NameFields";
import type { Messages } from "@/i18n";
import { AGENT_SOURCE, SOURCE_KINDS, type Issue, type NodeData, type ServiceSettings, type ToolsData } from "@/lib/agentGraph";
import type { GateReason } from "@/lib/codeExecution";
import type { ToolOrigin } from "@/lib/toolSafety";
import { advertisedTools } from "@/lib/gateway";
import { agentCallToolName } from "@/lib/hub";
import { displayName, resolveName } from "@/lib/names";

import type { CanvasState } from "../types";

export interface InspectorOptions {
  providers: LLMProvider[];
  /** 도구 소스 후보. 정의와 배포가 한 문서라(D42) 옛 Profile·Instance 두 목록이 하나로 줄었다. */
  gateways: Gateway[];
  mcpServers: MCPServer[];
  /** 허브의 `agent` 항목. 게시한 Agent 를 Tool 로 부르는 소스의 후보다 (P46 6.2절). */
  agentItems: HubItem[];
  dataServices: DataService[];
  /** 테넌트의 Knowledge Base. knowledge 패널의 셀렉트와 상태 칩이 쓴다 (D32). */
  knowledgeBases: KnowledgeBase[];
  /** 보이는 Skill. 에이전트 패널의 Skills 절이 붙일 것을 고르고 붙인 것의 이름과 최신 리비전을 푼다 (D64). */
  skills: Skill[];
  /** 위 목록을 다 받았는가. 못 받았으면 "못 찾음" 은 "지워졌다" 가 아니라 "아직 모른다" 다. */
  ready: boolean;
  /** 마지막 검사가 준 tools 노드마다의 Tool 효과다(P39b 14절 4부). 검사 전에는 없다. */
  toolEffects?: Record<string, Record<string, ToolEffect>>;
  /** 마지막 검사가 준 tools 노드마다 Tool 이름의 백엔드 종류다(P43 2절, lib/toolSafety.ts). 검사 전에는 없다. */
  toolOrigins?: Record<string, Record<string, ToolOrigin>>;
  /** 마지막 검사가 준 tools 노드마다 서버나 허브 항목의 읽기 전용 표시가 서는 Tool 이름이다(P52 4절). 검사 전에는 없다. */
  toolMarked?: Record<string, string[]>;
  /** 마지막 검사가 준 tools 노드마다의 호출받는 Agent 다(P46 6.2절). Agent 소스이고 서버가 그 항목과 Agent 를 읽은 노드만 있다. 검사 전에는 없다. */
  agentCalls?: Record<string, AgentCall>;
  /** 배포된 Agent 의 클러스터 코드 실행 게이트가 닫힌 까닭이다(P40 8절). 열렸거나 모르면(배포 전) null 이다. */
  codeExecutionGate?: GateReason | null;
}

/** 청지기의 제안이 바꾸는 글 칸 하나의 앞과 뒤 전체(P48 6.5절). 카드는 바뀐 줄만 보이고 캔버스는 다 보인다. */
export interface ProposalText {
  field: string;
  before: string;
  after: string;
}

export interface InspectorProps {
  state: CanvasState;
  selection: { nodeId: string | null; edgeId: string | null };
  issues: Issue[];
  options: InspectorOptions;
  onChangeNodeData: (id: string, data: NodeData, commit?: boolean) => void;
  onCommit: () => void;
  onDeleteSelection: () => void;
  service: ServiceSettings;
  onChangeService: (next: ServiceSettings) => void;
  names: NameValue;
  onChangeNames: (next: NameValue) => void;
  readOnlyName: boolean;
  nameErrors?: NameErrors;
  /** 고른 노드에서 청지기의 제안이 바꾸는 글 칸. 제안을 불러오지 않았으면 없다. */
  proposalTexts?: ProposalText[];
}

/**
 * 고른 도구 소스의 화면 이름 (D43 넷째). 사람에게 id 를 보이지 않으므로 목록에서 못 찾으면
 * "(삭제된 항목)" 이고, 아직 고르지 않았으면 "미선택" 이다. 목록을 아직 못 받았으면 둘 다 아닌
 * "모른다" 라서 "…" 이다.
 */
export function sourceLabel(t: Messages, source: ToolsData["source"], options: InspectorOptions): string {
  if (!source.id) return t.canvas.unset;
  return resolveName(t, options[SOURCE_KINDS[source.kind].listKey], source.id, options.ready);
}

/** 고른 Database 의 화면 이름. 같은 규칙이다. */
export function dataServiceLabel(t: Messages, id: string | null | undefined, options: InspectorOptions): string {
  if (!id) return t.canvas.databaseUnset;
  return resolveName(t, options.dataServices, id, options.ready);
}

/** 고른 Knowledge Base 의 화면 이름. 같은 규칙이다. */
export function knowledgeBaseLabel(t: Messages, id: string | null | undefined, options: InspectorOptions): string {
  if (!id) return t.canvas.knowledgeBaseUnset;
  return resolveName(t, options.knowledgeBases, id, options.ready);
}

/**
 * 소스 셀렉트의 선택지. 배포 상태를 함께 적는다. 정의와 배포가 한 문서라(D42) 목록에는 안 올린 것도 함께 서는데 도구 소스는 배포된
 * 것만 쓸 수 있어서다. 게시한 Agent 의 항목은 그 Agent 의 phase 를 적는다.
 */
export function sourceChoices(kind: ToolsData["source"]["kind"], options: InspectorOptions): { id: string; label: string }[] {
  if (kind === AGENT_SOURCE) return options.agentItems.map((x) => ({ id: x.id, label: `${displayName(x)} (${x.status?.phase ?? "?"})` }));
  return options[SOURCE_KINDS[kind].listKey].map((x) => ({ id: x.id, label: `${displayName(x)} (${x.state === "deployed" ? (x.status?.phase ?? "?") : x.state})` }));
}

/** 고른 소스를 여는 화면의 경로. Agent 소스는 항목이 가리키는 Agent 의 상세이고, 그 항목을 목록에서 찾지 못하면 null 이다. */
export function sourcePath(source: ToolsData["source"], options: InspectorOptions): string | null {
  const id = source.kind === AGENT_SOURCE ? options.agentItems.find((i) => i.id === source.id)?.source.agent_id : source.id;
  return id ? `/${SOURCE_KINDS[source.kind].route}/${id}` : null;
}

/**
 * MCP 노드의 도구 이름. Gateway 는 배포 시점 스냅샷의 광고 도구이고, MCP Server 는 정의의 도구(오버라이드 이름)다.
 *
 * 옛 모델에서는 인스턴스에서 프로필로 한 번 더 건너뛰어야 도구를 읽을 수 있었다. 이제 문서 하나가 정의를
 * 함께 들고 있어(D42) 그 자리에서 바로 읽는다. `source.kind` 는 저장된 그래프의 값이라 그대로 둔다.
 */
export function toolNamesOfSource(kind: ToolsData["source"]["kind"], id: string, options: InspectorOptions): string[] | null {
  if (!id) return null;
  if (kind === AGENT_SOURCE) {
    // 게시한 Agent 는 항목 이름에서 나온 Tool 하나를 낸다
    const item = options.agentItems.find((i) => i.id === id);
    return item ? [agentCallToolName(item.name)] : null;
  }
  if (kind === "gateway_instance") {
    const gw = options.gateways.find((g) => g.id === id);
    if (!gw) return null;
    return [...new Set(advertisedTools(gw).flatMap((b) => b.tools.map((t) => t.name)))];
  }
  const server = options.mcpServers.find((m) => m.id === id);
  if (!server) return null;
  return [...new Set(server.backends.flatMap((b) => b.tools.map((t) => b.tool_overrides?.[t]?.name ?? t)))];
}
