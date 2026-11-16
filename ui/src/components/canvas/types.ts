/**
 * 캔버스(@xyflow/react) 가 다루는 노드·간선 모양과 lib/agentGraph 의 PlacedNode·PlacedEdge 사이의 변환.
 * ReactFlow 노드의 `type` 은 노드 종류이고 `data` 는 resource-model 5절 표의 노드 data 그대로다. 간선의 `data.kind` 가 간선 종류이고
 * `sourceHandle`·`targetHandle` 은 그 종류다(핸들 id = 간선 종류, D28). 저장 형식에는 종류만 남는다.
 */
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { LLMProvider } from "@/api/types";

import { EDGE_KINDS, NODE_TYPES, type ConnectionProblem, type EdgeKind, type Issue, type NodeData, type NodeDataByType, type NodeType, type PlacedEdge, type PlacedNode } from "@/lib/agentGraph";
import type { SkillInfo } from "@/lib/agentSkills";
import { kindColor } from "@/lib/kindColor";

export type CanvasNode = Node<Record<string, unknown>, NodeType>;

/** 청지기의 제안이 노드에 다는 표시(P48 6.5절). 더하는 노드와 바꾸는 노드다. 지우는 노드는 캔버스에 없다. */
export type ProposalMark = "added" | "changed";
export type CanvasEdge = Edge<{ kind: EdgeKind }, "kind">;

/** 노드 종류별 `data` 타입. `agentGraph.ts` 의 표를 캔버스 쪽 이름으로 쓴다. */
export type NodeDataOf<T extends NodeType> = NodeDataByType[T];

/**
 * 노드의 `data` 를 종류에 맞는 타입으로 읽는다. xyflow 의 `Node<T>` 는 data 가 `Record<string, unknown>` 이라
 * 캔버스 곳곳에서 좁히기가 필요한데, 캐스팅을 이 두 함수에만 두어 "여기서 타입을 믿는다" 가 grep 되는 한
 * 낱말이 되게 한다. `type` 은 타입을 고르는 증거일 뿐 런타임에는 쓰지 않는다. 노드의 실제 `type` 과
 * 어긋나게 부르면 호출부의 버그다.
 */
export function nodeData<T extends NodeType>(n: CanvasNode, _type: T): NodeDataOf<T> {
  return n.data as unknown as NodeDataOf<T>;
}

/** 노드 컴포넌트처럼 노드가 아니라 `data` 만 받는 자리용. */
export function asNodeData<T extends NodeType>(data: CanvasNode["data"], _type: T): NodeDataOf<T> {
  return data as unknown as NodeDataOf<T>;
}

export interface CanvasState {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
}

export const EMPTY_CANVAS: CanvasState = { nodes: [], edges: [] };

/**
 * 간선 종류별 색 (디자인 목업: 입구 cyan·모델 rose·도구 violet·위임 fuchsia·메모리 green·가드레일 amber·지식 teal).
 * 포트 점도 같은 색이다. 값은 테마 토큰이라 라이트에서는 진한 계열로 바뀐다(src/index.css `--ui-kind-*`).
 */

/** 핸들 id 가 간선 종류이면 그것, 아니면(카드 핸들 "card", 없음) null. */
export function handleKind(handleId: string | null | undefined): EdgeKind | null {
  return handleId && EDGE_KINDS.includes(handleId as EdgeKind) ? (handleId as EdgeKind) : null;
}

/** 핸들 id 는 간선 종류 그대로다. 저장된 간선은 종류만 갖고 핸들은 여기서 파생한다. */
export function makeEdge(id: string, source: string, target: string, kind: EdgeKind): CanvasEdge {
  return {
    id,
    source,
    target,
    sourceHandle: kind,
    targetHandle: kind,
    type: "kind",
    data: { kind },
    markerEnd: { type: MarkerType.ArrowClosed, color: kindColor(kind), width: 18, height: 18 },
  };
}

export function makeNode(id: string, type: NodeType, data: NodeData, position: { x: number; y: number }, selected = false): CanvasNode {
  return { id, type, data: data as unknown as Record<string, unknown>, position, selected };
}

// xyflow 의 `type` 과 간선 `data` 는 선택이라 기본값이 필요하다. 캔버스의 노드·간선은 전부 makeNode·makeEdge 를
// 거치므로 실제로 비는 일은 없고, 비면 아무 종류나 되면 되므로 첫 값을 쓴다.
export function placedNode(n: CanvasNode): PlacedNode {
  return { id: n.id, type: n.type ?? NODE_TYPES[0], data: n.data as unknown as NodeData, position: n.position };
}

export function placedEdge(e: CanvasEdge): PlacedEdge {
  return { id: e.id, source: e.source, target: e.target, kind: e.data?.kind ?? EDGE_KINDS[0] };
}

export function toCanvas(nodes: PlacedNode[], edges: PlacedEdge[]): CanvasState {
  return {
    nodes: nodes.map((n) => makeNode(n.id, n.type, n.data, n.position)),
    edges: edges.map((e) => makeEdge(e.id, e.source, e.target, e.kind)),
  };
}

export function toPlaced(state: CanvasState): { nodes: PlacedNode[]; edges: PlacedEdge[] } {
  return { nodes: state.nodes.map(placedNode), edges: state.edges.map(placedEdge) };
}

/** 노드 카드·인스펙터가 이름을 풀어 보이는 데 쓰는 조회. 페이지가 쿼리 결과로 만든다. */
export interface CanvasLookup {
  /**
   * 이름을 찾을 목록을 다 받았는가. 못 받았으면 아래 조회가 돌려주는 null 은 "없다" 가 아니라
   * "아직 모른다" 다. 둘을 같은 말로 그리면 여는 동안 멀쩡한 참조가 지워진 것처럼 보인다.
   */
  ready: boolean;
  providerKind: (name: string) => LLMProvider["kind"] | null;
  providerModel: (name: string) => string | null;
  sourceName: (kind: "gateway_instance" | "mcp_instance", id: string) => string | null;
  dataServiceName: (id: string) => string | null;
  /** Database 의 kind. 메모리 카드가 체크포인터가 postgres 인지 redis 인지 보이는 데 쓴다 (D33). */
  dataServiceKind: (id: string) => string | null;
  /** Knowledge Base 표시 이름. 없는 id 는 null 이다 (D32). */
  knowledgeBaseName: (id: string) => string | null;
  /** knowledge 카드의 `indexed/documents` 칩. */
  knowledgeBaseStatus: (id: string) => { indexed: number; documents: number } | null;
  /** Skill 의 표시 이름·최신 리비전·소유 테넌트·consume 여부·스크립트 여부. 보이지 않는 id 는 null 이다 (D64). */
  skill: (id: string) => SkillInfo | null;
}

export const EMPTY_LOOKUP: CanvasLookup = {
  // 아무것도 받지 않은 자리다. 여기서 ready 를 참으로 두면 빈 목록을 "다 지워졌다" 로 읽는다
  ready: false,
  providerKind: () => null,
  providerModel: () => null,
  sourceName: () => null,
  dataServiceName: () => null,
  dataServiceKind: () => null,
  knowledgeBaseName: () => null,
  knowledgeBaseStatus: () => null,
  skill: () => null,
};

/** 노드 컴포넌트가 컨텍스트로 받는 것. 그래프 상태는 ReactFlow 스토어에 있으므로 이슈·조회·연결 힌트만 둔다. */
export interface CanvasContextValue {
  nodeIssues: Map<string, Issue[]>;
  edgeIssues: Map<string, Issue[]>;
  lookup: CanvasLookup;
  /**
   * 지금 끌고 있는 연결(출발 노드와 출발 포트의 간선 종류)을 이 노드에 이을 수 있는가. null 이면 된다. 아니면 안 되는
   * 까닭이고 문장은 그릴 때 `connectionText` 가 만든다. kind 가 null 이면 노드 종류 대응표로 정한다.
   */
  connectionHint: (fromId: string, kind: EdgeKind | null, toId: string) => ConnectionProblem | null;
  /** 청지기의 제안을 불러온 캔버스가 노드 카드에 다는 표시(P48 6.5절). 더하는 노드와 바꾸는 노드다. */
  proposalMarks: ReadonlyMap<string, ProposalMark>;
}
