/**
 * 에이전트 노드에 붙인 Skill 의 캔버스 검사와 핀 다루기 (D64, P39 5절).
 *
 * 검사 규칙은 서버 `services/agent_skills.py` 의 `check_skills` 와 같은 code 를 같은 순서로 낸다. 아래 경우는
 * `platform/tests/test_agent_skills.py` 의 순수 검사와 짝이다. 상한과 code 가 서버와 같은지는 파리티 테스트가 따로 본다.
 */
import { CONTEXT_ISSUE_CODES, defaultNodeData, fromTemplate, serializeGraph, validateGraph, DEFAULT_SERVICE, type AgentData, type Graph } from "@/lib/agentGraph";
import { attachSkill, behindSkillIds, detachSkill, graphSkillPins, pinnedSkills, skillFindings, upgradeSkill, type AgentSkills, type PinnedSkill, type SkillFinding } from "@/lib/agentSkills";
import { MIB } from "@/lib/utils";

import { codes, textOf } from "./graph";
import { agentTemplateSupport, mockValidationContext, reportRevisions, sharedRevisions, skillFixture } from "./msw";

function skill(id: string, over: Partial<PinnedSkill> = {}): PinnedSkill {
  return { id, name: id, revision: 1, latest: 1, bytes: 100, scripts: false, ...over };
}

function agent(nodeId: string, ...pins: [string, number][]): AgentSkills {
  return { nodeId, pins: pins.map(([id, revision]) => ({ id, revision })) };
}

const found = (findings: { code: string; level: string; nodeId: string | null }[]) => findings.map((f) => [f.code, f.level, f.nodeId]);

/** 검사 결과 한 줄이 캔버스 이슈가 되어 화면에 보이는 문장. */
const shown = (f: SkillFinding) => textOf({ code: f.code, level: f.level, node_id: f.nodeId, edge_id: null, detail: f.detail });

describe("skillFindings (서버 check_skills 와 같은 code·순서)", () => {
  it("파일 도구가 꺼져 있으면 Skill 을 붙인 에이전트마다 오류다", () => {
    const agents = [agent("main", ["s-a", 1]), agent("sub", ["s-a", 1]), agent("bare")];
    expect(found(skillFindings(agents, false, [skill("s-a")]))).toEqual([
      ["skills_filesystem_disabled", "error", "main"],
      ["skills_filesystem_disabled", "error", "sub"],
    ]);
    expect(skillFindings(agents, true, [skill("s-a")])).toEqual([]);
  });

  it("에이전트 하나에 열한 개, 그래프 전체에 스물한 개면 too_many_skills 다", () => {
    const many = Array.from({ length: 21 }, (_, i) => skill(`s-${String(i).padStart(2, "0")}`));
    const pins = many.map((s): [string, number] => [s.id, 1]);
    expect(found(skillFindings([agent("main", ...pins.slice(0, 11))], true, many))).toEqual([["too_many_skills", "error", "main"]]);
    const spread = [agent("main", ...pins.slice(0, 10)), agent("one", ...pins.slice(10, 20)), agent("two", pins[20])];
    const graphWide = skillFindings(spread, true, many);
    expect(found(graphWide)).toEqual([["too_many_skills", "error", null]]);
    expect(graphWide[0].detail).toEqual({ key: "skillsDistinct", args: [21] });
    expect(shown(graphWide[0])).toContain("(서로 다른 Skill: 21개)");
  });

  it("한 Skill 은 한 리비전이고 서로 다른 Skill 의 이름은 겹치지 않는다", () => {
    const agents = [agent("main", ["s-a", 1], ["s-b", 1]), agent("sub", ["s-a", 2])];
    const revisions = [skill("s-a", { name: "report", latest: 2 }), skill("s-a", { name: "report", revision: 2, latest: 2 }), skill("s-b", { name: "report" })];
    const findings = skillFindings(agents, true, revisions);
    expect(found(findings)).toEqual([
      ["skill_behind", "warning", "main"],
      ["skill_name_conflict", "error", "main"],
      ["skill_revision_mismatch", "error", "sub"],
    ]);
    expect(findings.map((f) => f.detail)).toEqual([
      { key: "skillBehind", args: ["report", 1, 2] },
      { key: "skillNameTaken", args: ["report"] },
      { key: "skillRevisions", args: ["report", 2, "main", 1] },
    ]);
    expect(shown(findings[2])).toContain("(Skill report: 이 에이전트 r2, main r1)");
  });

  it("없는 리비전과 보이지 않는 Skill 은 unknown_skill 이고 이름을 아는 것은 이름으로 말한다", () => {
    const findings = skillFindings([agent("main", ["s-a", 3], ["s-gone", 1])], true, [skill("s-a", { name: "report" })]);
    expect(found(findings)).toEqual([
      ["unknown_skill", "error", "main"],
      ["unknown_skill", "error", "main"],
    ]);
    expect(findings.map((f) => f.detail)).toEqual([
      { key: "skillMissing", args: ["report", 3] },
      { key: "skillMissing", args: ["s-gone", 1] },
    ]);
    expect(shown(findings[0])).toContain("(찾지 못한 Skill: report r3)");
  });

  it("서로 다른 핀의 합과 에이전트마다 펼친 합을 따로 묶는다", () => {
    const big = [0, 1, 2].map((i) => skill(`s-${i}`, { bytes: 2 * MIB }));
    const all = big.map((s): [string, number] => [s.id, 1]);
    const distinct = skillFindings([agent("main", ...all)], true, big);
    expect(found(distinct)).toEqual([["skills_too_large", "error", null]]);
    expect(distinct[0].detail).toEqual({ key: "skillsDistinctBytes", args: [6 * MIB] });
    expect(shown(distinct[0])).toContain("(서로 다른 Skill 의 합: 6.0 MiB)");
    // 서로 다른 핀의 합은 4 MiB 이지만 다섯 에이전트에 펼치면 20 MiB 다
    const spread = ["main", "a", "b", "c", "d"].map((id) => agent(id, ...all.slice(0, 2)));
    const expanded = skillFindings(spread, true, big);
    expect(found(expanded)).toEqual([["skills_too_large", "error", null]]);
    expect(expanded[0].detail).toEqual({ key: "skillsExpandedBytes", args: [20 * MIB] });
    expect(shown(expanded[0])).toContain("(에이전트마다 쓰는 합: 20.0 MiB)");
  });

  it("스크립트와 새 리비전은 경고뿐이다", () => {
    expect(found(skillFindings([agent("main", ["s-a", 1])], true, [skill("s-a", { scripts: true, latest: 3 })]))).toEqual([
      ["skill_scripts_inert", "warning", "main"],
      ["skill_behind", "warning", "main"],
    ]);
  });
});

describe("그래프 검증에서의 Skill", () => {
  /** 메인에 Skill 을 붙인 템플릿 그래프. 템플릿의 빈 자리 표식은 보지 않는다. */
  function graphWith(pins: { id: string; revision: number }[], filesystem = true): Graph {
    const { nodes, edges } = fromTemplate(agentTemplateSupport);
    const main = nodes.find((n) => n.id === "agent-main")!.data as AgentData;
    main.skills = pins;
    main.builtin_tools = { filesystem };
    return serializeGraph(nodes, edges).graph;
  }
  const skillIssues = (g: Graph) => validateGraph(g, DEFAULT_SERVICE, mockValidationContext()).filter((i) => i.code.includes("skill") || i.code === "invalid_node_data");

  it("이슈를 에이전트 노드에 달고 화면 문장 뒤에 상세를 붙인다", () => {
    // 목의 sk-report 는 r2 가 최신이고 스크립트가 있다. r1 은 스크립트가 없다
    const issues = skillIssues(graphWith([{ id: "sk-report", revision: 1 }], false));
    expect(issues.map((i) => [i.code, i.level, i.node_id])).toEqual([
      ["skills_filesystem_disabled", "error", "agent-main"],
      ["skill_behind", "warning", "agent-main"],
    ]);
    expect(textOf(issues[0])).toContain("'내장 파일 Tool 노출' 을 켜거나");
    expect(textOf(issues[1])).toBe("고정한 리비전보다 새 리비전이 있습니다. 새 리비전을 쓰려면 '최신으로' 를 누르고 저장한 뒤 재배포해 주세요. (Skill: report-style, 고정 r1, 최신 r2)");
    expect(codes(skillIssues(graphWith([{ id: "sk-report", revision: 2 }])))).toEqual(["skill_scripts_inert"]);
  });

  it("문맥이 있어야 아는 표식은 목록을 못 읽었을 때 화면이 보류할 수 있다", () => {
    for (const code of ["unknown_skill", "skill_name_conflict", "skills_too_large", "skill_scripts_inert", "skill_behind"]) expect(CONTEXT_ISSUE_CODES.has(code), code).toBe(true);
    // 그래프만으로 아는 것은 보류하지 않는다
    for (const code of ["skills_filesystem_disabled", "too_many_skills", "skill_revision_mismatch"]) expect(CONTEXT_ISSUE_CODES.has(code), code).toBe(false);
  });

  it("모양이 틀린 skills 는 invalid_node_data 이고 서버 pydantic 문장을 그대로 낸다", () => {
    const issues = skillIssues(graphWith([{ id: "sk-report", revision: 1 }, { id: "sk-report", revision: 2 }]));
    expect(issues.map((i) => [i.code, i.node_id])).toEqual([["invalid_node_data", "agent-main"]]);
    expect(textOf(issues[0])).toContain("(skills: Value error, skills contains duplicates)");
  });
});

describe("핀 다루기", () => {
  const report = skillFixture(reportRevisions()[1]);

  it("붙이면 최신 리비전으로 고정하고, 마지막 하나를 빼면 키를 지워 붙인 적 없는 노드와 같아진다", () => {
    const fresh = defaultNodeData("agent");
    const attached = attachSkill(fresh, report);
    expect(attached.skills).toEqual([{ id: "sk-report", revision: 2 }]);
    expect(attachSkill(attached, report)).toBe(attached);
    expect(detachSkill(attached, "sk-report")).toEqual(fresh);
    expect("skills" in detachSkill(attached, "sk-report")).toBe(false);
  });

  it("최신으로는 그 Skill 을 붙인 노드만 바꾸고 다른 핀은 그대로 둔다", () => {
    const data: AgentData = { ...defaultNodeData("agent"), skills: [{ id: "sk-report", revision: 1 }, { id: "sk-shared", revision: 1 }] };
    expect(upgradeSkill(data, "sk-report", 2)?.skills).toEqual([{ id: "sk-report", revision: 2 }, { id: "sk-shared", revision: 1 }]);
    expect(upgradeSkill(data, "sk-other", 2)).toBeNull();
    expect(upgradeSkill(upgradeSkill(data, "sk-report", 2)!, "sk-report", 2)).toBeNull();
  });

  it("문맥은 목록의 최신 리비전과 뒤처진 핀의 리비전 목록을 합친다", () => {
    const shared = skillFixture(sharedRevisions()[0]);
    const metas = pinnedSkills([report, shared], [...reportRevisions(), ...sharedRevisions()]);
    expect(metas.map((m) => [m.id, m.revision, m.latest, m.scripts])).toEqual([
      ["sk-report", 2, 2, true],
      ["sk-shared", 1, 1, false],
      ["sk-report", 1, 2, false],
    ]);
    // 목록에 없는(보이지 않는) Skill 의 리비전은 뺀다
    expect(pinnedSkills([], reportRevisions())).toEqual([]);
  });

  it("뒤처진 핀이 있는 Skill 만 리비전 목록을 더 읽고, 고정 핀은 Skill 마다 처음 본 것이다", () => {
    const graph = {
      nodes: [
        { type: "agent", data: { skills: [{ id: "sk-report", revision: 2 }] } },
        { type: "agent", data: { skills: [{ id: "sk-report", revision: 1 }, { id: "sk-shared", revision: 1 }] } },
        { type: "model", data: { skills: [{ id: "nope", revision: 1 }] } },
      ],
    };
    expect(behindSkillIds(graph, [report, skillFixture(sharedRevisions()[0])])).toEqual(["sk-report"]);
    expect(graphSkillPins(graph)).toEqual([
      { id: "sk-report", revision: 2 },
      { id: "sk-shared", revision: 1 },
    ]);
  });
});
