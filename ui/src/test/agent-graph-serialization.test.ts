import {
  DEFAULT_SERVICE,
  agentSlug,
  canonicalJson,
  defaultNodeData,
  deserializeGraph,
  edgeKindFor,
  fromTemplate,
  mainModel,
  nodePorts,
  serializeGraph,
  uniqueNodeId,
  type Graph,
} from "@/lib/agentGraph";
import { makeEdge } from "@/components/canvas/types";

import { agentTemplateSupport } from "./msw";

describe("agent graph serialization", () => {
  it("round-trips nodes and edges through graph + layout and keeps node order", () => {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const doc = serializeGraph(nodes, edges);
    expect(doc.graph.nodes.map((n) => n.id)).toEqual(["entry", "agent-main", "model-main", "tools-calculator", "memory", "guardrail-input"]);
    expect(doc.graph.edges).toEqual(agentTemplateSupport.graph.edges);
    expect(doc.layout).toEqual(agentTemplateSupport.layout);
    // layout 이 없는 노드는 격자에 놓인다
    const back = deserializeGraph(doc.graph, { entry: { x: 1, y: 2 } });
    expect(back.nodes[0].position).toEqual({ x: 1, y: 2 });
    expect(back.nodes[1].position).toEqual({ x: 400, y: 80 });
    expect(back.edges).toEqual(edges);
  });

  it("canonicalJson ignores key order so layout-only changes compare equal on graph+service", () => {
    const a = canonicalJson({ graph: { nodes: [{ id: "x", type: "entry", data: { b: 1, a: 2 } }] }, service: DEFAULT_SERVICE });
    const b = canonicalJson({ service: DEFAULT_SERVICE, graph: { nodes: [{ type: "entry", data: { a: 2, b: 1 }, id: "x" }] } });
    expect(a).toBe(b);
  });

  it("ids, slugs and defaults", () => {
    expect(uniqueNodeId("agent", ["agent", "agent-2"])).toBe("agent-3");
    expect(uniqueNodeId("tools", [])).toBe("tools");
    expect(agentSlug("고객 Support Agent!")).toBe("support-agent");
    expect(defaultNodeData("agent", "sub").role).toBe("sub");
    expect(defaultNodeData("agent").context.compaction).toEqual({ trigger: { fraction: 0.85 }, keep: { fraction: 0.1 } });
    expect(defaultNodeData("agent")).not.toHaveProperty("provider");
    expect(defaultNodeData("model")).toEqual({ name: "model", provider: "", model: null, max_output_tokens: 2048, reasoning: { effort: "medium" }, temperature: null, fallback: null });
    expect(defaultNodeData("entry").max_input_chars).toBe(4000);
    expect(edgeKindFor("entry", "agent")).toBe("entry");
    expect(edgeKindFor("agent", "model")).toBe("model");
    expect(edgeKindFor("agent", "agent")).toBe("delegate");
    expect(edgeKindFor("agent", "knowledge")).toBe("knowledge");
    expect(edgeKindFor("tools", "agent")).toBeNull();
    expect(edgeKindFor("model", "agent")).toBeNull();
    expect(edgeKindFor("entry", "knowledge")).toBeNull();
  });

  it("ports follow the port table of docs/guide/agents.md section 1: handle id is the edge kind and the side depends on the kind", () => {
    const brief = (type: Parameters<typeof nodePorts>[0], role?: "main" | "sub") => nodePorts(type, role).map((p) => [p.kind, p.direction, p.side, p.hidden]);
    // 가드레일은 메인 에이전트 한 곳에만 붙으므로 입구에는 나가는 포트가 입구 하나다
    expect(brief("entry")).toEqual([["entry", "source", "right", false]]);
    expect(brief("agent", "main")).toEqual([
      ["entry", "target", "left", false],
      ["delegate", "target", "left", true],
      ["memory", "source", "top", false],
      ["model", "source", "top", false],
      ["tools", "source", "right", false],
      ["knowledge", "source", "right", false],
      ["delegate", "source", "right", false],
      ["guardrail", "source", "bottom", false],
    ]);
    // 서브는 위임을 받기만 하고 내보내지 않는다. 입구도 받지 않는다
    const sub = nodePorts("agent", "sub");
    expect(sub.find((p) => p.kind === "delegate" && p.direction === "source")?.hidden).toBe(true);
    expect(sub.find((p) => p.kind === "delegate" && p.direction === "target")?.hidden).toBe(false);
    expect(sub.find((p) => p.kind === "entry")?.hidden).toBe(true);
    // 메모리·가드레일은 메인에만 붙는다. 서브에 포트를 보이면 끌어 봤자 거부당하는 자리를 내주는 것이다
    expect(sub.filter((p) => !p.hidden).map((p) => p.kind)).toEqual(["delegate", "model", "tools", "knowledge"]);
    // 위 변에서 메모리가 왼쪽, 모델이 오른쪽이고 오른쪽 변에서 도구가 위, 위임이 아래다
    const main = nodePorts("agent", "main");
    const at = (kind: string, direction: string) => main.find((p) => p.kind === kind && p.direction === direction)!.at;
    expect(at("memory", "source")).toBeLessThan(at("model", "source"));
    expect(at("tools", "source")).toBeLessThan(at("knowledge", "source"));
    expect(at("knowledge", "source")).toBeLessThan(at("delegate", "source"));
    expect(brief("model")).toEqual([["model", "target", "bottom", false]]);
    expect(brief("memory")).toEqual([["memory", "target", "bottom", false]]);
    expect(brief("tools")).toEqual([["tools", "target", "left", false]]);
    expect(brief("guardrail")).toEqual([["guardrail", "target", "top", false]]);
    expect(brief("knowledge")).toEqual([["knowledge", "target", "left", false]]);
  });

  it("canvas edges derive both handle ids from the kind", () => {
    const e = makeEdge("e-1", "agent-main", "model-main", "model");
    expect(e.sourceHandle).toBe("model");
    expect(e.targetHandle).toBe("model");
    expect(e.data).toEqual({ kind: "model" });
  });
});

describe("mainModel", () => {
  it("follows the main agent's model edge even when a sub-agent's model node comes first", () => {
    const graph: Graph = {
      nodes: [
        { id: "model-sub", type: "model", data: { ...defaultNodeData("model"), provider: "stub", model: "tiny" } },
        { id: "agent-sub", type: "agent", data: { ...defaultNodeData("agent", "sub"), name: "helper" } },
        { id: "agent-main", type: "agent", data: { ...defaultNodeData("agent"), name: "support" } },
        { id: "model-main", type: "model", data: { ...defaultNodeData("model"), provider: "openai-main", model: "" } },
      ],
      edges: [
        { id: "e-sub-model", source: "agent-sub", target: "model-sub", kind: "model" },
        { id: "e-del", source: "agent-main", target: "agent-sub", kind: "delegate" },
        { id: "e-main-model", source: "agent-main", target: "model-main", kind: "model" },
      ],
    };
    // 빈 모델 이름은 null 로 읽는다
    expect(mainModel(graph)).toEqual({ provider: "openai-main", model: null });
    // 메인에 모델이 없으면 서브의 것으로 대신하지 않는다
    expect(mainModel({ nodes: graph.nodes, edges: graph.edges.filter((e) => e.id !== "e-main-model") })).toBeNull();
  });
});
