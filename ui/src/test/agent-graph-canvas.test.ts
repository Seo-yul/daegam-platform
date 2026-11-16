import { dictionary } from "@/i18n";
import {
  connectionText,
  defaultNodeData,
  fromTemplate,
  hasSingleDirection,
  resolveConnection,
  type NodeType,
  type PlacedEdge,
  type PlacedNode,
} from "@/lib/agentGraph";
import { autoLayout, NODE_HEIGHT, NODE_WIDTH, placeUnplaced } from "@/components/canvas/autoLayout";
import { toCanvas } from "@/components/canvas/types";

import { connectionHintOf } from "./graph";
import { agentTemplateSupport } from "./msw";

describe("connection hints", () => {
  const { nodes, edges } = fromTemplate(agentTemplateSupport);
  const byId = (id: string): PlacedNode => nodes.find((n) => n.id === id)!;
  const es: PlacedEdge[] = edges;

  it("rejects incompatible ports and duplicates, accepts valid ones", () => {
    expect(connectionHintOf(byId("tools-calculator"), byId("agent-main"), es)).toContain("연결할 수 없습니다");
    expect(connectionHintOf(byId("entry"), byId("agent-main"), es)).toBe("이미 연결돼 있습니다.");
    expect(connectionHintOf(byId("agent-main"), byId("agent-main"), es)).toContain("자기 자신");
    expect(connectionHintOf(byId("agent-main"), byId("guardrail-input"), es)).toBe("이미 연결돼 있습니다.");
    expect(connectionHintOf(byId("entry"), byId("guardrail-input"), es)).toContain("연결할 수 없습니다");
    // 메인의 memory 는 하나
    expect(connectionHintOf(byId("agent-main"), { ...byId("memory"), id: "memory-2" }, es)).toContain("메모리는 하나만");
    // 위임 대상은 서브
    const sub: PlacedNode = { id: "s", type: "agent", data: { ...defaultNodeData("agent", "sub"), description: "x" }, position: { x: 0, y: 0 } };
    expect(connectionHintOf(byId("agent-main"), sub, es)).toBeNull();
    expect(connectionHintOf(sub, byId("tools-calculator"), es)).toBeNull();
    expect(connectionHintOf(sub, byId("memory"), [])).toContain("메인 에이전트에만");
    expect(connectionHintOf(byId("entry"), sub, [])).toBe("입구는 메인 에이전트에만 연결합니다.");
  });

  it("uses the kind of the port the drag started from (handle id)", () => {
    const model2: PlacedNode = { ...byId("model-main"), id: "model-2" };
    // 모델 포트는 모델 노드에만, 에이전트마다 하나만
    expect(connectionHintOf(byId("agent-main"), byId("tools-calculator"), es, "model")).toBe("모델 포트는 MCP 에 연결할 수 없습니다.");
    expect(connectionHintOf(byId("agent-main"), byId("model-main"), es, "model")).toBe("이미 연결돼 있습니다.");
    expect(connectionHintOf(byId("agent-main"), model2, es, "model")).toBe("모델은 하나만 붙일 수 있습니다.");
    // 도구 포트를 모델 노드에 놓으면 거부, 도구 노드에 놓으면 허용
    expect(connectionHintOf(byId("agent-main"), model2, es, "tools")).toBe("Tool 포트는 모델에 연결할 수 없습니다.");
    expect(connectionHintOf(byId("agent-main"), { ...byId("tools-calculator"), id: "tools-2" }, es, "tools")).toBeNull();
    // 서브 에이전트도 모델 포트로 모델을 붙일 수 있고(공유), 위임 포트에서 모델로는 못 간다
    const sub: PlacedNode = { id: "s", type: "agent", data: { ...defaultNodeData("agent", "sub"), description: "x" }, position: { x: 0, y: 0 } };
    expect(connectionHintOf(sub, byId("model-main"), es, "model")).toBeNull();
    expect(connectionHintOf(byId("agent-main"), byId("model-main"), [], "delegate")).toBe("서브 에이전트 포트는 모델에 연결할 수 없습니다.");
    // 가드레일은 메인 에이전트에서만 나간다. 입구에서는 어느 포트로도 못 간다
    const guard2: PlacedNode = { ...byId("guardrail-input"), id: "guardrail-2" };
    expect(connectionHintOf(byId("agent-main"), guard2, es, "guardrail")).toBeNull();
    expect(connectionHintOf(byId("entry"), guard2, es, "guardrail")).toBe("가드레일 포트는 가드레일에 연결할 수 없습니다.");
    expect(connectionHintOf(byId("entry"), guard2, es, "entry")).toBe("입구 포트는 가드레일에 연결할 수 없습니다.");
  });
});

describe("연결 방향", () => {
  const { nodes, edges } = fromTemplate(agentTemplateSupport);
  const byId = (id: string): PlacedNode => nodes.find((n) => n.id === id)!;

  it("어느 두 종류 사이에도 양방향인 쌍이 없다", () => {
    // 화면이 방향을 대신 맞춰 주는 근거다. 표에 양방향 쌍을 더하면 여기서 먼저 걸린다
    const types: NodeType[] = ["entry", "agent", "model", "tools", "memory", "guardrail", "knowledge"];
    const both = types.flatMap((a) => types.map((b) => [a, b] as const)).filter(([a, b]) => !hasSingleDirection(a, b));
    expect(both).toEqual([]);
  });

  it("반대로 끌면 맞는 방향으로 뒤집어 잇는다", () => {
    const model2: PlacedNode = { ...byId("model-main"), id: "model-2" };
    // 모델에서 에이전트로 끌어도 뜻은 "이 에이전트가 이 모델을 쓴다" 하나다
    const flipped = resolveConnection(model2, byId("agent-main"), [], null);
    expect(flipped).toEqual({ source: byId("agent-main"), target: model2, kind: "model", flipped: true });

    // 포트에서 끌었으면 그 종류와 맞는 방향만 뒤집는다. 모델 노드의 포트는 model 이다
    expect(resolveConnection(model2, byId("agent-main"), [], "model")).toMatchObject({ kind: "model", flipped: true });
    expect(resolveConnection(model2, byId("agent-main"), [], "tools")).toEqual({ problem: { key: "wrongPort", args: ["tools", "agent"] } });

    // 앞 방향이 되면 뒤집지 않는다
    expect(resolveConnection(byId("agent-main"), model2, [], null)).toMatchObject({ flipped: false });

    // 서브에서 메인으로 끌면 위임 방향을 바로잡는다
    const sub: PlacedNode = { id: "s", type: "agent", data: { ...defaultNodeData("agent", "sub"), description: "x" }, position: { x: 0, y: 0 } };
    expect(resolveConnection(sub, byId("agent-main"), [], null)).toMatchObject({ source: byId("agent-main"), target: sub, kind: "delegate", flipped: true });

    // 둘 다 안 되면 앞 방향의 이유를 준다. 문장은 그릴 때 사전에서 만든다
    const refused = resolveConnection(byId("entry"), byId("guardrail-input"), edges, null);
    expect(refused).toEqual({ problem: { key: "notAllowed", args: ["entry", "guardrail"] } });
    expect("problem" in refused && connectionText(dictionary("ko"), refused.problem)).toBe("Chat 트리거에서 가드레일로는 연결할 수 없습니다.");
  });
});

describe("auto layout (dagre + port-direction correction)", () => {
  it("puts memory and model above the agent, guardrails below their host, tools and subs to the right", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const sub: PlacedNode = { id: "agent-sub", type: "agent", data: { ...defaultNodeData("agent", "sub"), name: "helper", description: "x" }, position: { x: 0, y: 0 } };
    const guard: PlacedNode = { ...nodes.find((n) => n.id === "guardrail-input")!, id: "guardrail-out", data: { ...defaultNodeData("guardrail"), position: "output" } };
    const state = toCanvas(
      [...nodes, sub, guard],
      [
        ...edges,
        { id: "e-del", source: "agent-main", target: "agent-sub", kind: "delegate" },
        { id: "e-sub-model", source: "agent-sub", target: "model-main", kind: "model" },
        { id: "e-guard-out", source: "agent-main", target: "guardrail-out", kind: "guardrail" },
      ],
    );
    const placed = new Map(autoLayout(state).map((n) => [n.id, n.position]));
    const p = (id: string) => placed.get(id)!;
    const cx = (id: string) => p(id).x + NODE_WIDTH / 2;
    // 흐름은 왼쪽에서 오른쪽
    expect(p("entry").x).toBeLessThan(p("agent-main").x);
    expect(p("agent-main").x + NODE_WIDTH).toBeLessThanOrEqual(p("tools-calculator").x);
    expect(p("agent-main").x + NODE_WIDTH).toBeLessThanOrEqual(p("agent-sub").x);
    // 메모리·모델은 메인 위에 나란히, 메모리가 왼쪽
    expect(p("memory").y + NODE_HEIGHT).toBeLessThan(p("agent-main").y);
    expect(p("model-main").y + NODE_HEIGHT).toBeLessThan(p("agent-main").y);
    expect(p("memory").x + NODE_WIDTH).toBeLessThanOrEqual(p("model-main").x);
    expect(Math.abs((cx("memory") + cx("model-main")) / 2 - cx("agent-main"))).toBeLessThan(1);
    // 가드레일은 붙는 노드 아래에 같은 세로줄로
    expect(p("guardrail-input").y).toBeGreaterThan(p("agent-main").y + NODE_HEIGHT);
    expect(p("guardrail-out").y).toBeGreaterThan(p("agent-main").y + NODE_HEIGHT);
    expect(p("guardrail-input").x + NODE_WIDTH).toBeLessThanOrEqual(p("guardrail-out").x);
    expect(Math.abs((cx("guardrail-input") + cx("guardrail-out")) / 2 - cx("agent-main"))).toBeLessThan(1);
    // 어느 두 카드도 겹치지 않는다
    const ids = [...placed.keys()];
    for (const a of ids) {
      for (const b of ids) {
        if (a >= b) continue;
        const overlap = p(a).x < p(b).x + NODE_WIDTH && p(b).x < p(a).x + NODE_WIDTH && p(a).y < p(b).y + NODE_HEIGHT && p(b).y < p(a).y + NODE_HEIGHT;
        expect(overlap, `${a} overlaps ${b}`).toBe(false);
      }
    }
  });

  it("places only the nodes without a stored position, below the stored ones, and all of them when none is stored (P48 6.5)", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const extra: PlacedNode = { ...nodes.find((n) => n.id === "guardrail-input")!, id: "guardrail-out", position: { x: 80, y: 80 } };
    const state = toCanvas([...nodes, extra], [...edges, { id: "e-guard-out", source: "agent-main", target: "guardrail-out", kind: "guardrail" }]);
    const stored = new Set(nodes.map((n) => n.id));
    const out = new Map(placeUnplaced(state, stored).map((n) => [n.id, n.position]));
    // 저장된 자리는 그대로다
    for (const n of nodes) expect(out.get(n.id)).toEqual(n.position);
    const bottom = Math.max(...nodes.map((n) => n.position.y + NODE_HEIGHT));
    expect(out.get("guardrail-out")!.y).toBeGreaterThan(bottom);
    // 저장된 자리가 하나도 없으면 전부 자동 배치다
    const all = placeUnplaced(state, new Set());
    expect(all.map((n) => n.position)).toEqual(autoLayout(state).map((n) => n.position));
    // 다 저장돼 있으면 그대로다
    expect(placeUnplaced(state, new Set([...stored, "guardrail-out"]))).toBe(state.nodes);
  });

  it("leaves unattached satellites to dagre and never loses a node", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const loose: PlacedNode = { id: "model-2", type: "model", data: defaultNodeData("model"), position: { x: 0, y: 0 } };
    const out = autoLayout(toCanvas([...nodes, loose], edges));
    expect(out.map((n) => n.id)).toEqual([...nodes.map((n) => n.id), "model-2"]);
    expect(out.every((n) => Number.isFinite(n.position.x) && Number.isFinite(n.position.y))).toBe(true);
  });
});
