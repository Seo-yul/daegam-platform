/**
 * 캔버스 undo/redo. 스냅샷은 `{nodes, edges}` 전체다. 선택·드래그 중 위치 같은 것은 commit 없이 present 만
 * 바꾸고, 노드 추가·삭제·연결·드래그 끝·인스펙터 편집이 commit 이다. commit 은 "직전 commit 상태" 를 past 에
 * 밀어 넣으므로 드래그 중간 상태는 이력에 남지 않는다. 그래프(노드 data·간선·배치)가 같으면 commit 해도 이력이
 * 늘지 않는다 (선택만 바뀐 commit 이 undo 한 칸을 먹지 않게).
 */
import { useCallback, useMemo, useState } from "react";

import { canonicalJson, serializeGraph } from "@/lib/agentGraph";

import { toPlaced, type CanvasState } from "./types";

const LIMIT = 100;

interface History {
  past: CanvasState[];
  committed: CanvasState;
  present: CanvasState;
  future: CanvasState[];
}

export type CanvasUpdater = CanvasState | ((prev: CanvasState) => CanvasState);

export interface CanvasHistory {
  state: CanvasState;
  /** commit 이 true 면 undo 지점이 된다. */
  set: (updater: CanvasUpdater, commit?: boolean) => void;
  /** present 를 undo 지점으로 만든다 (인스펙터의 텍스트 입력이 blur 될 때). */
  commit: () => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
  /** 프로필·템플릿을 새로 불러온다. 이력을 비운다. */
  reset: (next: CanvasState) => void;
}

function graphKey(s: CanvasState): string {
  const { nodes, edges } = toPlaced(s);
  return canonicalJson(serializeGraph(nodes, edges));
}

/** 되돌린 스냅샷에 지금 선택을 얹는다. 지워진 노드의 선택은 버린다. */
function withSelection(target: CanvasState, from: CanvasState): CanvasState {
  const selectedNodes = new Set(from.nodes.filter((n) => n.selected).map((n) => n.id));
  const selectedEdges = new Set(from.edges.filter((e) => e.selected).map((e) => e.id));
  return {
    nodes: target.nodes.map((n) => ({ ...n, selected: selectedNodes.has(n.id) })),
    edges: target.edges.map((e) => ({ ...e, selected: selectedEdges.has(e.id) })),
  };
}

export function useCanvasHistory(initial: CanvasState): CanvasHistory {
  const [h, setH] = useState<History>({ past: [], committed: initial, present: initial, future: [] });

  const set = useCallback((updater: CanvasUpdater, commit = false) => {
    setH((prev) => {
      const next = typeof updater === "function" ? updater(prev.present) : updater;
      if (!commit) return { ...prev, present: next };
      if (graphKey(prev.committed) === graphKey(next)) return { ...prev, present: next, committed: next };
      return { past: [...prev.past, prev.committed].slice(-LIMIT), committed: next, present: next, future: [] };
    });
  }, []);

  const commit = useCallback(() => set((s) => s, true), [set]);

  const undo = useCallback(() => {
    setH((prev) => {
      if (!prev.past.length) return prev;
      const committed = withSelection(prev.past[prev.past.length - 1], prev.present);
      return { past: prev.past.slice(0, -1), committed, present: committed, future: [prev.committed, ...prev.future] };
    });
  }, []);

  const redo = useCallback(() => {
    setH((prev) => {
      if (!prev.future.length) return prev;
      const committed = withSelection(prev.future[0], prev.present);
      return { past: [...prev.past, prev.committed], committed, present: committed, future: prev.future.slice(1) };
    });
  }, []);

  const reset = useCallback((next: CanvasState) => {
    setH({ past: [], committed: next, present: next, future: [] });
  }, []);

  return useMemo(
    () => ({ state: h.present, set, commit, undo, redo, canUndo: h.past.length > 0, canRedo: h.future.length > 0, reset }),
    [h.present, h.past.length, h.future.length, set, commit, undo, redo, reset],
  );
}
