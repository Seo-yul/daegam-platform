/**
 * 가드레일 패널의 판정 모델 규칙(injection_check, P43 7절). 규칙을 고르면 위치가 Tool 로 옮겨지고, 판정 모델 셀렉트가 이 그래프의
 * 모델 노드를 이름으로 보이며, 두 한도와 안내문이 보인다. 캔버스 검사기는 판정 모델이 없거나 모델 노드가 아니면 표식을 단다.
 */
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { Inspector } from "@/components/canvas/Inspector";
import { makeEdge, makeNode, toPlaced, type CanvasState } from "@/components/canvas/types";
import { dictionary } from "@/i18n";
import { DEFAULT_SERVICE, defaultNodeData, INJECTION_CHECK, issueText, judgeOf, serializeGraph, validateGraph, type GuardrailData, type Issue, type ModelData } from "@/lib/agentGraph";

import { inspectorOptions } from "./inspector";
import { mockValidationContext } from "./msw";
import { renderWithProviders } from "./render";
import { openSelect, selectValue } from "./select";

const ko = dictionary("ko");

function model(name: string): ModelData {
  return { ...defaultNodeData("model"), name, provider: "stub" };
}

/** 메인 에이전트와 그 모델, 따로 둔 판정용 모델 노드, 메인에 붙은 입력 가드레일 하나다. */
function graph(): CanvasState {
  return {
    nodes: [
      makeNode("entry", "entry", defaultNodeData("entry"), { x: 0, y: 0 }),
      makeNode("agent", "agent", defaultNodeData("agent"), { x: 0, y: 200 }),
      makeNode("model-main", "model", model("기본 모델"), { x: 300, y: 200 }),
      makeNode("model-judge", "model", model("판정용 Haiku"), { x: 300, y: 400 }),
      makeNode("guard", "guardrail", defaultNodeData("guardrail"), { x: 0, y: 400 }),
    ],
    edges: [makeEdge("e-entry", "entry", "agent", "entry"), makeEdge("e-model", "agent", "model-main", "model"), makeEdge("e-guard", "agent", "guard", "guardrail")],
  };
}

function Harness({ onData }: { onData: (data: GuardrailData) => void }) {
  const [state, setState] = useState(graph);
  return (
    <Inspector
      state={state}
      selection={{ nodeId: "guard", edgeId: null }}
      issues={[]}
      options={inspectorOptions()}
      onChangeNodeData={(id, data) => {
        if (id === "guard") onData(data as GuardrailData);
        setState((s) => ({ ...s, nodes: s.nodes.map((n) => (n.id === id ? { ...n, data: data as unknown as Record<string, unknown> } : n)) }));
      }}
      onCommit={() => undefined}
      onDeleteSelection={() => undefined}
      service={DEFAULT_SERVICE}
      onChangeService={() => undefined}
      names={{ displayName: "", name: "" }}
      onChangeNames={() => undefined}
      readOnlyName={false}
    />
  );
}

describe("guardrail panel: injection_check", () => {
  it("규칙을 고르면 위치가 Tool 이 되고, 판정 모델은 이 그래프의 모델 노드를 이름으로 고른다", async () => {
    const user = userEvent.setup();
    let last: GuardrailData | null = null;
    renderWithProviders(<Harness onData={(d) => (last = d)} />);

    await selectValue(user, "guardrail-rule-kind-0", INJECTION_CHECK);
    expect(last!.position).toBe("tool");
    expect(last!.rules[0]).toEqual({ kind: INJECTION_CHECK, value: { model: "", max_chars: 8000, max_calls: 20 } });
    for (const hint of [ko.canvas.guardrail.injectionHint, ko.canvas.guardrail.judgeModelHint, ko.canvas.guardrail.injectionLimitsHint]) {
      expect(screen.getByText(hint)).toBeInTheDocument();
    }

    const listbox = await openSelect(user, "guardrail-judge-model-0");
    expect(within(listbox).getAllByRole("option").map((o) => o.textContent?.trim())).toEqual(["기본 모델", "판정용 Haiku"]);
    await selectValue(user, "guardrail-judge-model-0", "model-judge");
    expect(judgeOf(last!)).toEqual({ model: "model-judge", max_chars: 8000, max_calls: 20 });

    const calls = screen.getByTestId("guardrail-judge-max-calls-0");
    await user.clear(calls);
    await user.type(calls, "5");
    expect(judgeOf(last!)?.max_calls).toBe(5);
  });
});

describe("canvas checks: injection_check", () => {
  // 규칙 자체는 공유 묶음(platform/tests/fixtures/agent-graph-corpus.json)이 캔버스와 서버에서 함께 본다. 여기는 화면 문장이다
  function judgeIssue(target: string): Issue {
    const g = graph();
    const guard: GuardrailData = { position: "tool", rules: [{ kind: INJECTION_CHECK, value: { model: target, max_chars: 8000, max_calls: 20 } }], on_violation: "block", message: null };
    const nodes = g.nodes.map((n) => (n.id === "guard" ? { ...n, data: guard as unknown as Record<string, unknown> } : n));
    const placed = toPlaced({ nodes, edges: g.edges });
    const [issue] = validateGraph(serializeGraph(placed.nodes, placed.edges).graph, DEFAULT_SERVICE, mockValidationContext()).filter((i) => i.code === "unknown_judge_model");
    return issue;
  }

  it("판정 모델을 고르지 않았거나 모델 노드가 아니면 그 까닭을 문장 뒤에 붙인다", () => {
    const text = ko.canvas.issues.text.unknown_judge_model;
    expect(issueText(ko, judgeIssue(""))).toBe(`${text} (판정 모델을 아직 선택하지 않았습니다.)`);
    expect(issueText(ko, judgeIssue("agent"))).toBe(`${text} (이 그래프에서 찾지 못한 모델 노드: agent)`);
  });
});
