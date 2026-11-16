/**
 * Agent 그래프 갈래의 색 (D28). 캔버스의 노드·팔레트·간선과 Agent 목록 카드의 태그가 같은 색을 본다. 노드 종류와
 * 간선 종류가 이름으로 같은 토큰(index.css 의 `--ui-kind-*`)을 가리키므로 표 없이 이름에서 만든다. 테마를 따라 바뀐다.
 */
import type { EdgeKind, NodeType } from "@/lib/agentGraph";

/** 노드나 간선 갈래의 색. */
export function kindColor(kind: NodeType | EdgeKind): string {
  return `var(--ui-kind-${kind})`;
}

/** 갈래 색의 옅은 바탕. 아이콘 칸과 태그가 이 한 비율(`--ui-kind-tint`)을 쓴다. */
export function kindTint(color: string): string {
  return `color-mix(in srgb, ${color} var(--ui-kind-tint), transparent)`;
}
