/**
 * dagre 로 왼쪽에서 오른쪽으로 자동 배치. 노드 카드 크기는 고정폭(w-72)이라 상수로 둔다.
 *
 * dagre 는 한 방향(LR)만 놓으므로 입구에서 에이전트를 거쳐 도구·서브에이전트로 가는 흐름만 dagre 에 맡기고, 포트 방향(D28)을 따르는
 * 위성 노드는 붙는 노드 기준 상대 위치로 보정한다. 모델·메모리는 붙는 에이전트 위(메모리가 왼쪽, 모델이 오른쪽. 위 포트
 * 순서와 같다), 가드레일은 붙는 입구·에이전트 아래다. 겹치지 않도록 붙는 노드의 dagre 크기에 위성 줄의 폭·높이를 더해
 * 자리를 미리 잡는다. 여러 에이전트가 공유하는 모델은 간선이 먼저 나오는 에이전트 위에 둔다.
 */
import dagre from "@dagrejs/dagre";

import type { EdgeKind } from "@/lib/agentGraph";

import type { CanvasNode, CanvasState } from "./types";

export const NODE_WIDTH = 288;
export const NODE_HEIGHT = 112;
/** 같은 줄의 위성 사이 간격. dagre 의 nodesep 과 같다. */
const GAP_X = 48;
/** 위성 줄과 붙는 노드 사이의 세로 간격. */
const GAP_Y = 64;
const RANK_SEP = 96;

type Side = "top" | "bottom";

/** 위성이 되는 간선 종류와 자리. order 는 같은 줄에서의 왼쪽부터 순서다. */
const SATELLITE: Partial<Record<EdgeKind, { side: Side; order: number }>> = {
  memory: { side: "top", order: 0 },
  model: { side: "top", order: 1 },
  guardrail: { side: "bottom", order: 0 },
};

interface Row {
  top: string[];
  bottom: string[];
}

export function autoLayout(state: CanvasState): CanvasNode[] {
  const byId = new Map(state.nodes.map((n) => [n.id, n]));
  const size = (id: string) => {
    const n = byId.get(id);
    return { w: n?.measured?.width ?? NODE_WIDTH, h: n?.measured?.height ?? NODE_HEIGHT };
  };

  // 위성 → 붙는 노드. 처음 나오는 간선이 이긴다
  const hostOf = new Map<string, { host: string; side: Side; order: number; seq: number }>();
  state.edges.forEach((e, seq) => {
    const sat = e.data?.kind ? SATELLITE[e.data.kind] : undefined;
    if (!sat || e.source === e.target || !byId.has(e.source) || !byId.has(e.target) || hostOf.has(e.target)) return;
    hostOf.set(e.target, { host: e.source, seq, ...sat });
  });
  // 붙는 노드가 스스로 위성이면(형식이 틀린 간선) 그 위성은 보통 노드로 둔다
  for (const [sat, h] of [...hostOf]) if (hostOf.has(h.host)) hostOf.delete(sat);

  const rows = new Map<string, Row>();
  for (const [sat, h] of hostOf) {
    const row = rows.get(h.host) ?? { top: [], bottom: [] };
    row[h.side].push(sat);
    rows.set(h.host, row);
  }
  for (const row of rows.values()) {
    for (const side of ["top", "bottom"] as const) {
      row[side].sort((a, b) => hostOf.get(a)!.order - hostOf.get(b)!.order || hostOf.get(a)!.seq - hostOf.get(b)!.seq);
    }
  }
  const rowWidth = (ids: string[]) => ids.reduce((acc, id, i) => acc + size(id).w + (i ? GAP_X : 0), 0);
  const rowHeight = (ids: string[]) => Math.max(0, ...ids.map((id) => size(id).h));

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: GAP_X, ranksep: RANK_SEP, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));
  const boxes = new Map<string, { w: number; h: number; topH: number; bottomH: number; W: number; H: number }>();
  for (const n of state.nodes) {
    if (hostOf.has(n.id)) continue;
    const { w, h } = size(n.id);
    const row = rows.get(n.id) ?? { top: [], bottom: [] };
    const topH = row.top.length ? rowHeight(row.top) + GAP_Y : 0;
    const bottomH = row.bottom.length ? rowHeight(row.bottom) + GAP_Y : 0;
    const W = Math.max(w, rowWidth(row.top), rowWidth(row.bottom));
    const H = h + topH + bottomH;
    boxes.set(n.id, { w, h, topH, bottomH, W, H });
    g.setNode(n.id, { width: W, height: H });
  }
  for (const e of state.edges) {
    if (e.source !== e.target && g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);

  const pos = new Map<string, { x: number; y: number }>();
  for (const [id, b] of boxes) {
    // dagre 는 중심 좌표를 주고 ReactFlow 는 왼쪽 위 좌표를 쓴다
    const p = g.node(id);
    const cx = p.x;
    const boxTop = p.y - b.H / 2;
    pos.set(id, { x: cx - b.w / 2, y: boxTop + b.topH });
    const row = rows.get(id);
    if (!row) continue;
    const placeRow = (ids: string[], bottomY: number, alignBottom: boolean) => {
      let x = cx - rowWidth(ids) / 2;
      for (const sat of ids) {
        const s = size(sat);
        pos.set(sat, { x, y: alignBottom ? bottomY - s.h : bottomY });
        x += s.w + GAP_X;
      }
    };
    // 위 줄은 아래 끝을 붙는 노드 위 GAP_Y 에 맞추고, 아래 줄은 위 끝을 붙는 노드 아래 GAP_Y 에 맞춘다
    if (row.top.length) placeRow(row.top, boxTop + b.topH - GAP_Y, true);
    if (row.bottom.length) placeRow(row.bottom, boxTop + b.topH + b.h + GAP_Y, false);
  }
  return state.nodes.map((n) => {
    const p = pos.get(n.id) ?? n.position;
    return { ...n, position: { x: Math.round(p.x), y: Math.round(p.y) } };
  });
}

/**
 * 저장된 자리가 없는 노드만 자동 배치로 놓는다 (P48 6.5절). 청지기의 제안은 좌표를 싣지 않으므로 캔버스가 놓는다.
 * 저장된 자리가 하나도 없으면(새 Agent) 전부 자동 배치다. 아니면 전체를 자동 배치한 자리를 새 노드에만 쓰고, 그 무리를
 * 저장된 노드들 아래로 옮겨 사람이 맞춰 둔 배치와 겹치지 않게 한다. placed 는 저장된 자리가 있는 노드 id 다.
 */
export function placeUnplaced(state: CanvasState, placed: ReadonlySet<string>): CanvasNode[] {
  const fresh = state.nodes.filter((n) => !placed.has(n.id));
  if (!fresh.length) return state.nodes;
  const auto = new Map(autoLayout(state).map((n) => [n.id, n.position]));
  if (fresh.length === state.nodes.length) return state.nodes.map((n) => ({ ...n, position: auto.get(n.id) ?? n.position }));
  const kept = state.nodes.filter((n) => placed.has(n.id));
  const bottom = Math.max(...kept.map((n) => n.position.y + (n.measured?.height ?? NODE_HEIGHT)));
  const top = Math.min(...fresh.map((n) => auto.get(n.id)?.y ?? n.position.y));
  const dy = bottom + GAP_Y - top;
  return state.nodes.map((n) => {
    if (placed.has(n.id)) return n;
    const p = auto.get(n.id) ?? n.position;
    return { ...n, position: { x: p.x, y: p.y + dy } };
  });
}
