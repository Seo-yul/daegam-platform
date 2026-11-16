/**
 * Agent 캔버스 (D26 F4, D28). ReactFlow 위에 노드 7종 카드·종류별 색 간선·줌 컨트롤·범례를 그린다.
 * 상태는 부모(useCanvasHistory)가 갖고 이 컴포넌트는 변경을 `onChange(updater, commit)` 로 올린다.
 *  - 선택·드래그 중 위치는 commit 없이, 노드·간선 추가·삭제와 드래그 끝은 commit 이다.
 *  - 연결은 lib/agentGraph.connectionProblem 이 허용하는 것만 된다. 종류별 포트 점(핸들 id = 간선 종류)에서 끌어 대상
 *    노드 카드 아무 곳에나 놓으면 그 종류의 간선이 붙는다(카드를 덮는 target 핸들 + connectionMode loose + onConnectEnd 의
 *    카드 탐지). 끌기 시작하면 그 종류로 붙을 수 있는 노드만 밝아지고 나머지는 흐려진다.
 *  - 팔레트에서 끌어다 놓으면 onDropNode(type, 위치) 를 부른다. 위치는 흐름 좌표다.
 *  - Delete/Backspace 는 ReactFlow 가 선택된 노드·간선을 지운다.
 *  - fitViewKey 가 바뀌면 화면을 다시 맞춘다 (자동 배치·불러오기 뒤). 맞추는 줌에는 바닥이 있어(FIT_VIEW)
 *    큰 그래프는 다 들어오지 않는다.
 */
import "@xyflow/react/dist/style.css";

import { Background, BackgroundVariant, ConnectionMode, Controls, Panel, ReactFlow, ReactFlowProvider, applyEdgeChanges, applyNodeChanges, useReactFlow, type Connection, type EdgeChange, type FinalConnectionState, type NodeChange, type OnSelectionChangeParams } from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, type DragEvent } from "react";

import { issuesByEdge, issuesByNode, resolveConnection, uniqueEdgeId, type ConnectionProblem, type EdgeKind, type Issue, type NodeType } from "@/lib/agentGraph";
import { cn } from "@/lib/utils";

import { NODE_HEIGHT, NODE_WIDTH } from "./autoLayout";
import { CanvasContext } from "./context";
import { EdgeLegend, edgeTypes, nodeTypes } from "./nodes";
import { EMPTY_LOOKUP, handleKind, makeEdge, placedEdge, placedNode, type CanvasEdge, type CanvasLookup, type CanvasNode, type CanvasState, type ProposalMark } from "./types";

export const NODE_DRAG_MIME = "application/x-daegam-agent-node";

const NO_MARKS: ReadonlyMap<string, ProposalMark> = new Map();

/**
 * 전체 보기의 줌 (D44 다섯째, P24 F4). padding 만으로는 줌이 안 오른다. 줌은 그래프 크기가 정하고
 * padding 은 그 둘레를 깎을 뿐이라, 노드 열 개짜리 그래프는 1440px 에서 0.5 언저리로 눌려 카드 제목
 * 13px 이 6px 로 보였다. 그래서 바닥을 0.95 로 준다. 제목이 12.35px 라 읽힌다.
 *
 * 큰 그래프는 다 보이기와 글자 읽히기를 동시에 못 한다. 글자가 읽히는 쪽을 택했으므로 그래프가
 * 화면보다 넓으면 잘려 보이고 끌어서 본다. 다 보고 싶으면 아래 줌 컨트롤로 내리면 되고, 사용자가
 * 직접 내리는 한계(ReactFlow 의 minZoom)는 0.25 그대로다.
 */
const FIT_VIEW = { padding: 0.08, minZoom: 0.95 } as const;

export interface AgentCanvasProps {
  state: CanvasState;
  onChange: (updater: (prev: CanvasState) => CanvasState, commit: boolean) => void;
  issues: Issue[];
  lookup?: CanvasLookup;
  colorMode: "light" | "dark";
  onDropNode: (type: NodeType, position: { x: number; y: number }) => void;
  onSelectionChange?: (selection: { nodeId: string | null; edgeId: string | null }) => void;
  /** 바뀌면 fitView 한다. */
  fitViewKey?: number;
  /** 바뀌면 이 노드를 화면 가운데로 옮긴다 (검증 이슈 클릭, 팔레트로 노드 추가). */
  focusNodeId?: string | null;
  focusKey?: number;
  /** 지금 배율을 그대로 두고 옮기기만 한다. 검증 이슈는 읽어야 하므로 1 까지 당기지만, 노드 추가는
   *  사용자가 일부러 맞춰 둔 배율을 건드리지 않는다. */
  focusKeepZoom?: boolean;
  /** 청지기의 제안을 불러왔으면 노드마다의 표시(더함·바꿈). P48 6.5절. */
  proposalMarks?: ReadonlyMap<string, ProposalMark>;
  className?: string;
}

function CanvasInner(props: AgentCanvasProps) {
  const { state, onChange, issues, lookup = EMPTY_LOOKUP, colorMode, onDropNode, onSelectionChange, fitViewKey, focusNodeId, focusKey, focusKeepZoom, className, proposalMarks = NO_MARKS } = props;
  const rf = useReactFlow();
  const latest = useRef(state);
  latest.current = state;

  const nodeIssues = useMemo(() => issuesByNode(issues), [issues]);
  const edgeIssues = useMemo(() => issuesByEdge(issues), [issues]);

  const connectionHint = useCallback((fromId: string, kind: EdgeKind | null, toId: string): ConnectionProblem | null => {
    const s = latest.current;
    const source = s.nodes.find((n) => n.id === fromId);
    const target = s.nodes.find((n) => n.id === toId);
    if (!source || !target) return { key: "nodeMissing", args: [] };
    const r = resolveConnection(placedNode(source), placedNode(target), s.edges.map(placedEdge), kind);
    return "problem" in r ? r.problem : null;
  }, []);

  const ctxValue = useMemo(() => ({ nodeIssues, edgeIssues, lookup, connectionHint, proposalMarks }), [nodeIssues, edgeIssues, lookup, connectionHint, proposalMarks]);

  const onNodesChange = useCallback(
    (changes: NodeChange<CanvasNode>[]) => {
      const commit = changes.some((c) => c.type === "remove" || c.type === "add" || (c.type === "position" && c.dragging === false));
      onChange((prev) => ({ ...prev, nodes: applyNodeChanges<CanvasNode>(changes, prev.nodes) }), commit);
    },
    [onChange],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange<CanvasEdge>[]) => {
      const commit = changes.some((c) => c.type === "remove" || c.type === "add");
      onChange((prev) => ({ ...prev, edges: applyEdgeChanges<CanvasEdge>(changes, prev.edges) }), commit);
    },
    [onChange],
  );

  const isValidConnection = useCallback(
    (c: Connection | { source: string; target: string; sourceHandle?: string | null }) => {
      if (!c.source || !c.target) return false;
      return connectionHint(c.source, handleKind(c.sourceHandle), c.target) === null;
    },
    [connectionHint],
  );

  /** 간선 종류는 출발 포트(sourceHandle)가 정하고, 핸들 정보가 없으면 노드 종류 대응표가 정한다.
   *  반대로 끌었으면 `resolveConnection` 이 방향을 뒤집어 준다(쌍마다 맞는 방향이 하나뿐이다). */
  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      onChange((prev) => {
        const from = prev.nodes.find((n) => n.id === c.source);
        const to = prev.nodes.find((n) => n.id === c.target);
        if (!from || !to) return prev;
        const r = resolveConnection(placedNode(from), placedNode(to), prev.edges.map(placedEdge), handleKind(c.sourceHandle));
        if ("problem" in r) return prev;
        const id = uniqueEdgeId(r.source.id, r.target.id, prev.edges.map((e) => e.id));
        return { ...prev, edges: [...prev.edges, makeEdge(id, r.source.id, r.target.id, r.kind)] };
      }, true);
    },
    [onChange],
  );

  /** 핸들 밖에서 놓아도 연결한다. 포인터 아래의 노드 카드(data-testid="node-<id>")를 찾아 onConnect 와 같은 규칙으로 잇는다.
   *  핸들 위에서 놓은 경우는 ReactFlow 가 이미 onConnect 를 불렀으므로(connectionState.toNode 가 있다) 건너뛴다. */
  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent, connectionState: FinalConnectionState) => {
      if (connectionState.isValid || connectionState.toNode) return;
      const from = connectionState.fromNode?.id;
      if (!from) return;
      const point = "changedTouches" in event ? event.changedTouches[0] : event;
      const el = document.elementFromPoint(point.clientX, point.clientY)?.closest<HTMLElement>("[data-testid^='node-']");
      const to = el?.dataset.testid?.replace(/^node-/, "");
      if (!to || to === from) return;
      onConnect({ source: from, target: to, sourceHandle: connectionState.fromHandle?.id ?? null, targetHandle: null });
    },
    [onConnect],
  );

  const onDragOver = useCallback((e: DragEvent) => {
    if (e.dataTransfer.types.includes(NODE_DRAG_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    }
  }, []);

  const onDrop = useCallback(
    (e: DragEvent) => {
      const type = e.dataTransfer.getData(NODE_DRAG_MIME) as NodeType;
      if (!type) return;
      e.preventDefault();
      onDropNode(type, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
    },
    [onDropNode, rf],
  );

  const handleSelection = useCallback(
    ({ nodes, edges }: OnSelectionChangeParams) => {
      onSelectionChange?.({ nodeId: nodes[0]?.id ?? null, edgeId: nodes.length ? null : (edges[0]?.id ?? null) });
    },
    [onSelectionChange],
  );

  useEffect(() => {
    if (fitViewKey === undefined) return;
    const t = window.setTimeout(() => void rf.fitView({ ...FIT_VIEW, duration: 250 }), 30);
    return () => window.clearTimeout(t);
  }, [fitViewKey, rf]);

  useEffect(() => {
    if (!focusNodeId || focusKey === undefined) return;
    const node = latest.current.nodes.find((n) => n.id === focusNodeId);
    if (!node) return;
    const w = node.measured?.width ?? NODE_WIDTH;
    const h = node.measured?.height ?? NODE_HEIGHT;
    const zoom = focusKeepZoom ? rf.getZoom() : Math.max(rf.getZoom(), 1);
    void rf.setCenter(node.position.x + w / 2, node.position.y + h / 2, { zoom, duration: 250 });
  }, [focusNodeId, focusKey, focusKeepZoom, rf]);

  return (
    <CanvasContext.Provider value={ctxValue}>
      <div className={cn("relative h-full w-full", className)} data-testid="agent-canvas" onDragOver={onDragOver} onDrop={onDrop}>
        <ReactFlow<CanvasNode, CanvasEdge>
          nodes={state.nodes}
          edges={state.edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectEnd={onConnectEnd}
          isValidConnection={isValidConnection}
          connectionMode={ConnectionMode.Loose}
          connectionRadius={48}
          onSelectionChange={handleSelection}
          colorMode={colorMode}
          deleteKeyCode={["Delete", "Backspace"]}
          multiSelectionKeyCode={["Meta", "Control"]}
          fitView
          fitViewOptions={FIT_VIEW}
          minZoom={0.25}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          defaultEdgeOptions={{ type: "kind" }}
          nodesConnectable
          elevateEdgesOnSelect
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="var(--ui-canvas-dot)" />
          <Controls showInteractive={false} position="bottom-right" className="daegam-flow-controls" />
          {/* 범례는 읽기만 하는 글이다. 캔버스가 좁으면 여러 줄로 접히며 오른쪽 아래 줌 컨트롤까지 상자가
              뻗으므로, 포인터를 통과시키고 폭도 컨트롤 앞에서 멈춘다 */}
          <Panel position="bottom-left" className="pointer-events-none max-w-[calc(100%-5rem)]">
            <EdgeLegend showSelectHint={state.nodes.length > 1} />
          </Panel>
        </ReactFlow>
      </div>
    </CanvasContext.Provider>
  );
}

export function AgentCanvas(props: AgentCanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
