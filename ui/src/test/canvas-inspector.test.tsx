/**
 * 캔버스 인스펙터의 네 규칙.
 *  - 참조 id 를 이름으로 풀 때 "고르지 않음", "지워짐", "아직 모름" 을 다른 말로 보인다 (D43 넷째). 에이전트 패널의
 *    붙은 지식 목록도 노드 카드·다른 패널과 같은 규칙이다.
 *  - 응답 형식 JSON schema 편집기는 JSON 객체만 받고, 객체가 아닌 원문으로 형식을 바꾸면 기본 스키마로 되돌리며 이유를 남긴다.
 *  - 서브 에이전트는 컴파일이 읽지 않는 네 칸(응답 언어·응답 형식·컨텍스트 압축·내장 파일 도구)을 숨기고 안내 한 줄을 보인다.
 *  - Skills 절은 붙일 때 최신 리비전으로 고정하고, "최신으로" 는 같은 Skill 을 붙인 모든 에이전트 노드를 함께 올린다 (D64).
 *    누른 단추가 사라져도 초점이 body 로 떨어지지 않는다.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { Inspector, type InspectorOptions } from "@/components/canvas/Inspector";
import { knowledgeBaseLabel, sourceLabel } from "@/components/canvas/inspector/types";
import { makeEdge, makeNode, type CanvasState } from "@/components/canvas/types";
import { dictionary } from "@/i18n";
import { DEFAULT_SERVICE, defaultNodeData, type AgentData, type KnowledgeData } from "@/lib/agentGraph";
import { MAX_SKILLS_PER_AGENT, type SkillPin } from "@/lib/agentSkills";
import { LOADING_LABEL } from "@/lib/names";

import { kbFaq, reportRevisions, sharedRevisions, skillFixture } from "./msw";
import { renderWithProviders } from "./render";
import { openSelect, selectValue } from "./select";

const ko = dictionary("ko");
const DELETED_LABEL = ko.ui.names.deleted;

function options(over: Partial<InspectorOptions> = {}): InspectorOptions {
  return { providers: [], gateways: [], mcpServers: [], dataServices: [], knowledgeBases: [kbFaq], skills: [], ready: true, ...over };
}

function knowledge(kbId: string): KnowledgeData {
  return { ...defaultNodeData("knowledge"), knowledge_base_id: kbId };
}

/** 메인 에이전트 하나와 거기 붙은 지식 노드들. */
function graphWithKnowledge(kbIds: string[]): CanvasState {
  const nodes = [makeNode("agent", "agent", defaultNodeData("agent"), { x: 0, y: 0 }), ...kbIds.map((kb, i) => makeNode(`kn-${i}`, "knowledge", knowledge(kb), { x: 300, y: i * 150 }))];
  const edges = kbIds.map((_, i) => makeEdge(`e-kn-${i}`, "agent", `kn-${i}`, "knowledge"));
  return { nodes, edges };
}

/** 서브 에이전트 하나뿐인 그래프. 인스펙터가 고르는 노드 id 는 graphWithKnowledge 와 같은 "agent" 다. */
function graphWithSub(): CanvasState {
  return { nodes: [makeNode("agent", "agent", defaultNodeData("agent", "sub"), { x: 0, y: 0 })], edges: [] };
}

/**
 * 인스펙터를 에이전트 노드를 고른 채로 띄운다. 바뀐 data 는 상태로 되돌려 다시 그린다. `onChange` 는 다른 노드의 바뀜과
 * undo 지점까지 받는다. Skills 절의 이름이 Skill 상세로 가는 링크라 renderWithProviders 로 라우터 안에 띄운다.
 */
function Harness({ initial, opts, onData, onChange }: { initial: CanvasState; opts: InspectorOptions; onData?: (data: AgentData) => void; onChange?: (id: string, data: AgentData, commit?: boolean) => void }) {
  const [state, setState] = useState(initial);
  return (
    <Inspector
      state={state}
      selection={{ nodeId: "agent", edgeId: null }}
      issues={[]}
      options={opts}
      onChangeNodeData={(id, data, commit) => {
        if (id === "agent") onData?.(data as AgentData);
        onChange?.(id, data as AgentData, commit);
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

describe("canvas inspector reference labels", () => {
  it("knowledgeBaseLabel 은 고르지 않음·찾음·지워짐·아직 모름을 가른다", () => {
    expect(knowledgeBaseLabel(ko, "", options())).toBe("Knowledge Base 미선택");
    expect(knowledgeBaseLabel(ko, "kb-faq", options())).toBe("제품 FAQ");
    expect(knowledgeBaseLabel(ko, "kb-gone", options())).toBe(DELETED_LABEL);
    expect(knowledgeBaseLabel(ko, "kb-gone", options({ ready: false }))).toBe(LOADING_LABEL);
    // 도구 소스도 같은 규칙이다
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "" }, options())).toBe("미선택");
    expect(sourceLabel(ko, { kind: "mcp_instance", id: "gone" }, options())).toBe(DELETED_LABEL);
    expect(sourceLabel(ko, { kind: "gateway_instance", id: "gone" }, options({ ready: false }))).toBe(LOADING_LABEL);
  });

  it("에이전트 패널의 붙은 지식 목록이 지워진 Knowledge Base 를 '미선택' 이라 하지 않는다", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithProviders(<Harness initial={graphWithKnowledge(["kb-faq", "kb-gone", ""])} opts={options()} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(within(screen.getByTestId("attached-kn-0")).getByText("search_knowledge · 제품 FAQ")).toBeInTheDocument();
    expect(within(screen.getByTestId("attached-kn-1")).getByText(`search_knowledge · ${DELETED_LABEL}`)).toBeInTheDocument();
    expect(within(screen.getByTestId("attached-kn-2")).getByText("search_knowledge · Knowledge Base 미선택")).toBeInTheDocument();
    unmount();

    // 목록을 아직 못 받았으면 지워진 것이 아니라 모르는 것이다
    renderWithProviders(<Harness initial={graphWithKnowledge(["kb-gone"])} opts={options({ ready: false })} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(within(screen.getByTestId("attached-kn-0")).getByText(`search_knowledge · ${LOADING_LABEL}`)).toBeInTheDocument();
  });
});

describe("canvas inspector response format", () => {
  const DEFAULT_SCHEMA = { type: "object", properties: {} };

  it("JSON 객체만 받고, 객체가 아닌 원문으로 형식을 바꾸면 기본 스키마로 되돌리며 이유를 남긴다", async () => {
    const user = userEvent.setup();
    const seen: AgentData[] = [];
    renderWithProviders(<Harness initial={graphWithKnowledge([])} opts={options()} onData={(d) => seen.push(d)} />);
    await user.click(screen.getByTestId("inspector-tabs-prompt"));

    // text 에서 바꾸면 기본 스키마다
    await selectValue(user, "agent-response-format", "json_schema");
    expect(seen.at(-1)?.response_format).toEqual({ json_schema: DEFAULT_SCHEMA });
    const editor = await screen.findByTestId("monaco");
    expect((editor as HTMLTextAreaElement).value).toBe(JSON.stringify(DEFAULT_SCHEMA, null, 2));

    // 객체가 아닌 JSON 은 반영하지 않는다
    fireEvent.change(editor, { target: { value: "[]" } });
    expect(screen.getByTestId("agent-json-schema-error")).toHaveTextContent("JSON 객체여야 합니다.");
    expect(seen.at(-1)?.response_format).toEqual({ json_schema: DEFAULT_SCHEMA });
    fireEvent.change(editor, { target: { value: '{"type": "object", "required": ["a"]}' } });
    expect(screen.queryByTestId("agent-json-schema-error")).not.toBeInTheDocument();
    expect(seen.at(-1)?.response_format).toEqual({ json_schema: { type: "object", required: ["a"] } });

    // 편집기에 남은 원문이 객체가 아니면 형식을 바꿀 때 기본 스키마로 되돌린다
    fireEvent.change(editor, { target: { value: "[1]" } });
    await selectValue(user, "agent-response-format", "text");
    expect(seen.at(-1)?.response_format).toBe("text");
    await selectValue(user, "agent-response-format", "json_schema");
    expect(seen.at(-1)?.response_format).toEqual({ json_schema: DEFAULT_SCHEMA });
    await waitFor(() => expect(screen.getByTestId("agent-json-schema-error")).toHaveTextContent("JSON 객체여야 합니다. 기본 스키마로 되돌렸습니다."));

    // 문법 오류도 같다. 이유 문장 뒤에 되돌렸다는 문장이 붙는다
    fireEvent.change(await screen.findByTestId("monaco"), { target: { value: "{" } });
    expect(screen.getByTestId("agent-json-schema-error").textContent).toMatch(/^JSON 오류: .+\.$/);
    await selectValue(user, "agent-response-format", "text");
    await selectValue(user, "agent-response-format", "json_schema");
    await waitFor(() => expect(screen.getByTestId("agent-json-schema-error").textContent).toMatch(/^JSON 오류: .+[^.]\. 기본 스키마로 되돌렸습니다\.$/));
    expect(seen.at(-1)?.response_format).toEqual({ json_schema: DEFAULT_SCHEMA });
  });
});

describe("canvas inspector subagent fields", () => {
  it("sub 에는 응답 언어·응답 형식·컨텍스트 압축·내장 파일 도구 칸이 없고 안내 한 줄이 있다", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithProviders(<Harness initial={graphWithSub()} opts={options()} />);
    expect(screen.getByTestId("agent-sub-notice")).toHaveTextContent("응답 언어와 응답 형식은 시스템 프롬프트에 적습니다. 컨텍스트 압축은 메인의 압축 정책을 따릅니다.");
    expect(screen.queryByTestId("agent-language")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-tabs-prompt"));
    expect(screen.getByTestId("agent-system-prompt")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-response-format")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-advanced")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.queryByTestId("agent-builtin-filesystem")).not.toBeInTheDocument();
    // 턴 제한 셋은 그대로다. 서브의 턴 타임아웃은 런타임이 메인의 task 호출에 건다
    expect(screen.getByTestId("agent-turn-timeout")).toBeInTheDocument();
    unmount();

    // 메인은 네 칸이 다 있고 안내가 없다
    renderWithProviders(<Harness initial={graphWithKnowledge([])} opts={options()} />);
    expect(screen.queryByTestId("agent-sub-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-language")).toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-tabs-prompt"));
    expect(screen.getByTestId("agent-response-format")).toBeInTheDocument();
    expect(screen.getByTestId("agent-advanced")).toBeInTheDocument();
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-builtin-filesystem")).toBeInTheDocument();
  });
});

describe("canvas inspector Skills", () => {
  // 목의 sk-report 는 r2 가 최신이고 스크립트가 있다. sk-shared 는 acme 테넌트가 연 r1 이다
  const report = skillFixture(reportRevisions()[1], { display_name: "보고서 문체" });
  const shared = skillFixture(sharedRevisions()[0], { tenant: "acme", display_name: "웹 조사" });

  /** 고른 메인("agent")과 서브("sub")가 같은 Skill 을 붙인 그래프. */
  function graphWithSkills(main: SkillPin[], sub: SkillPin[] = []): CanvasState {
    const subData = { ...defaultNodeData("agent", "sub"), name: "researcher", description: "조사", skills: sub };
    return {
      nodes: [makeNode("agent", "agent", { ...defaultNodeData("agent"), skills: main }, { x: 0, y: 0 }), makeNode("sub", "agent", subData, { x: 300, y: 0 })],
      edges: [makeEdge("e-del", "agent", "sub", "delegate")],
    };
  }

  it("최신으로는 그 Skill 을 붙인 모든 에이전트 노드를 올리고 마지막에 한 번 undo 지점을 만든다", async () => {
    const user = userEvent.setup();
    const changes: [string, SkillPin[] | undefined, boolean | undefined][] = [];
    renderWithProviders(<Harness initial={graphWithSkills([{ id: "sk-report", revision: 1 }], [{ id: "sk-report", revision: 1 }])} opts={options({ skills: [report, shared] })} onChange={(id, d, commit) => changes.push([id, d.skills, commit])} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    const row = screen.getByTestId("agent-skill-sk-report");
    expect(within(row).getByTestId("agent-skill-sk-report-name")).toHaveTextContent("보고서 문체 · r1");
    expect(within(row).getByTestId("agent-skill-sk-report-name")).toHaveAttribute("href", "/skills/sk-report");
    expect(within(row).getByTestId("agent-skill-sk-report-behind")).toHaveTextContent("새 리비전 r2");

    expect(within(row).getByTestId("agent-skill-sk-report-upgrade")).toHaveAccessibleName("보고서 문체 최신으로");
    await user.click(within(row).getByTestId("agent-skill-sk-report-upgrade"));
    expect(changes).toEqual([
      ["agent", [{ id: "sk-report", revision: 2 }], false],
      ["sub", [{ id: "sk-report", revision: 2 }], true],
    ]);
    expect(screen.getByTestId("agent-skill-sk-report-name")).toHaveTextContent("보고서 문체 · r2");
    expect(screen.queryByTestId("agent-skill-sk-report-upgrade")).not.toBeInTheDocument();
    // 사라진 단추 대신 절 제목이 초점을 받는다
    expect(screen.getByTestId("agent-skills-heading")).toHaveFocus();
  });

  it("붙이면 그때의 최신 리비전으로 고정하고, 마지막 하나를 빼면 skills 키가 없어진다", async () => {
    const user = userEvent.setup();
    const seen: AgentData[] = [];
    renderWithProviders(<Harness initial={graphWithSkills([])} opts={options({ skills: [report, shared] })} onData={(d) => seen.push(d)} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-skills")).toHaveTextContent("붙인 Skill 이 없습니다.");
    // 다른 테넌트의 것은 소유 테넌트를, 스크립트가 든 것은 그렇다고 고르기 전에 알린다
    const listbox = await openSelect(user, "agent-skill-add");
    expect(within(listbox).getAllByRole("option").map((o) => o.textContent)).toEqual(["보고서 문체 · r2 · 스크립트 포함", "웹 조사 · r1 · acme"]);
    await user.keyboard("{Escape}");
    await selectValue(user, "agent-skill-add", "sk-shared");
    expect(seen.at(-1)?.skills).toEqual([{ id: "sk-shared", revision: 1 }]);
    expect(screen.getByTestId("agent-skill-sk-shared")).toHaveTextContent("acme");
    // 남은 후보를 마저 붙여 고를 것이 없어져도 고르개는 disabled 가 아니라 aria-disabled 라 초점을 지킨다
    await selectValue(user, "agent-skill-add", "sk-report");
    const picker = screen.getByTestId("agent-skill-add");
    expect(picker).toHaveAttribute("aria-disabled", "true");
    expect(picker).not.toBeDisabled();
    expect(picker).toHaveFocus();

    expect(screen.getByTestId("agent-skill-sk-shared-detach")).toHaveAccessibleName("웹 조사 빼기");
    await user.click(screen.getByTestId("agent-skill-sk-report-detach"));
    await user.click(screen.getByTestId("agent-skill-sk-shared-detach"));
    expect(seen.at(-1)).not.toHaveProperty("skills");
    // 줄과 함께 사라진 빼기 단추 대신 고르개가 초점을 받는다
    expect(screen.getByTestId("agent-skill-add")).toHaveFocus();
    expect(screen.getByTestId("agent-skill-add")).not.toHaveAttribute("aria-disabled");
  });

  it("열 개를 붙이면 고르개가 잠기고, consume 이 빠진 Skill 과 못 찾은 Skill 을 따로 말한다", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: MAX_SKILLS_PER_AGENT }, (_, i) => ({ id: `sk-${i}`, revision: 1 }));
    const { unmount } = renderWithProviders(<Harness initial={graphWithSkills(many)} opts={options({ skills: [report] })} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-skill-add")).toHaveAttribute("aria-disabled", "true");
    // 잠긴 고르개는 눌러도 열리지 않는다
    await user.click(screen.getByTestId("agent-skill-add"));
    expect(screen.queryByTestId("agent-skill-add-listbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-skills")).toHaveTextContent(`에이전트 하나에는 Skill 을 ${MAX_SKILLS_PER_AGENT}개까지 붙일 수 있습니다.`);
    expect(screen.getByTestId("agent-skill-sk-0-name")).toHaveTextContent(`${DELETED_LABEL} · r1`);
    unmount();

    // 정책이 좁아져 consume 이 빠진 Skill 은 붙어 있어도 표시하고 고르개에는 나오지 않는다. 목록을 아직 못 받았으면 모른다
    const revoked = { ...shared, permissions: { view: true, consume: false, manage: false } };
    renderWithProviders(<Harness initial={graphWithSkills([{ id: "sk-shared", revision: 1 }, { id: "sk-gone", revision: 1 }])} opts={options({ skills: [revoked], ready: false })} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-skill-sk-shared-not-consumable")).toHaveTextContent("사용 권한 없음");
    expect(screen.getByTestId("agent-skill-sk-gone-name")).toHaveTextContent(`${LOADING_LABEL} · r1`);
    expect(screen.getByTestId("agent-skill-add")).toHaveAttribute("aria-disabled", "true");
  });

  it("목록을 아직 못 받았으면 붙일 Skill 이 없다고 하지 않고 불러오는 중이라고 한다", async () => {
    const user = userEvent.setup();
    const { unmount } = renderWithProviders(<Harness initial={graphWithSkills([])} opts={options({ ready: false })} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-skills")).toHaveTextContent("Skill 목록을 불러오는 중입니다.");
    expect(screen.getByTestId("agent-skills")).not.toHaveTextContent("붙일 수 있는 Skill 이 없습니다");
    unmount();

    renderWithProviders(<Harness initial={graphWithSkills([])} opts={options()} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByTestId("agent-skills")).toHaveTextContent("붙일 수 있는 Skill 이 없습니다. Skills 화면에서 먼저 만들어 주세요.");
  });

  it("파일 도구 도움말이 Skills 도 이 도구가 있어야 한다고 말한다", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness initial={graphWithSkills([])} opts={options()} />);
    await user.click(screen.getByTestId("inspector-tabs-tools"));
    expect(screen.getByText(/Skills 와 장기 기억은 이 Tool 이 있어야 합니다/)).toBeInTheDocument();
  });
});
