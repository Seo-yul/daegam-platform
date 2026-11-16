/**
 * 에이전트 노드에 붙인 Skill (D64, P39 5절). 서버 `services/agent_skills.py` 와 짝이다.
 *
 * 에이전트 노드(메인과 서브)의 data 에 `skills: [{id, revision}]` 로 붙고 붙일 때의 최신 리비전이 고정된다. 노드 종류는
 * 더하지 않는다. 여기에는 검사 규칙(`skillFindings`)과 그 규칙이 읽는 리비전 메타, 캔버스가 핀을 붙이고 빼고 올리는 순수
 * 함수를 둔다. 화면 문장은 다른 그래프 이슈와 같이 사전의 `canvas.issues` 가 정본이고 여기는 code 와 상세(skillDetail
 * 의 키와 인자)만 낸다.
 * 상한이 서버와 같은지는 `platform/tests/test_agent_graph_parity.py` 가, code 가 화면 문장 표에 다 있는지는
 * `src/test/agent-graph.test.ts` 가 본다.
 */
import type { Skill, SkillRevisionMeta } from "@/api/types";
import type { Messages } from "@/i18n";

import { detail, type IssueCode, type IssueDetail } from "./agentGraph";
import { displayName, unresolvedLabel } from "./names";
import { MIB } from "./utils";

/** 에이전트 하나에 붙는 수. 이름과 설명이 모든 모델 호출에 실린다(서버 MAX_PER_AGENT). */
export const MAX_SKILLS_PER_AGENT = 10;
/** 그래프 전체의 서로 다른 Skill 수(서버 MAX_PER_GRAPH). */
export const MAX_SKILLS_PER_GRAPH = 20;
/** 서로 다른 핀의 본문 합(서버 MAX_DISTINCT_BYTES). */
export const MAX_SKILLS_DISTINCT_BYTES = 4 * MIB;
/** init 이 에이전트마다 펼쳐 쓰는 합(서버 MAX_EXPANDED_BYTES). */
export const MAX_SKILLS_EXPANDED_BYTES = 16 * MIB;

/** 에이전트 노드에 붙인 Skill 하나. 붙일 때의 리비전이 고정된다. */
export interface SkillPin {
  id: string;
  revision: number;
}

/**
 * 그래프가 고정한 Skill 리비전 하나의 메타다(서버 PinnedSkill). 본문은 없고 `latest` 는 그 Skill 의 최신 번호다.
 * 검증 문맥이 Skill 목록(최신 리비전)과 뒤처진 핀의 리비전 목록에서 모은다.
 */
export interface PinnedSkill {
  id: string;
  name: string;
  revision: number;
  latest: number;
  bytes: number;
  scripts: boolean;
}

/** 검사에 넘기는 에이전트 노드 하나와 그 노드가 붙인 Skill. graph.nodes 순서다. */
export interface AgentSkills {
  nodeId: string;
  pins: readonly SkillPin[];
}

/**
 * Skill 검사 결과가 드는 상세. 사전 `canvas.issues.skillDetail` 의 키와 인자이고, 문장은 `agentGraph.ts` 의 issueText 가 그릴 때
 * 그래프 검사의 상세와 합쳐 만든다.
 */
type SkillDetail = IssueDetail<keyof Messages["canvas"]["issues"]["skillDetail"]>;

/** 검사 결과 한 줄. `agentGraph.ts` 가 이슈로 옮기고, 화면은 문장 뒤에 `detail` 의 상세를 붙인다. */
export interface SkillFinding {
  code: IssueCode;
  level: "error" | "warning";
  nodeId: string | null;
  detail?: SkillDetail;
}

/** 그 노드가 붙인 Skill. 키가 없는 옛 노드는 빈 목록이다(서버 기본값과 같다). */
export function pinsOf(data: { skills?: readonly SkillPin[] } | null | undefined): readonly SkillPin[] {
  return data?.skills ?? [];
}

function pinKey(id: string, revision: number): string {
  return `${id}@${revision}`;
}

// -- 검증 (5절 표) -------------------------------------------------------------------------

/**
 * 5절 표의 규칙 전부다. 서버 `check_skills` 와 같은 순서로 같은 code 를 낸다. `filesystem` 은 메인의 파일 도구 스위치이고
 * `revisions` 는 문맥이 모은 메타다. 표식은 그 Skill 을 붙인 에이전트 노드에 붙고, 그래프 전체의 상한은 표식이 없다.
 */
export function skillFindings(agents: readonly AgentSkills[], filesystem: boolean, revisions: readonly PinnedSkill[]): SkillFinding[] {
  const byPin = new Map(revisions.map((r) => [pinKey(r.id, r.revision), r]));
  const names = new Map(revisions.map((r) => [r.id, r.name]));
  const attached = agents.filter((a) => a.pins.length);
  const first = new Map<string, { revision: number; nodeId: string }>();
  const owners = new Map<string, string>();
  const out: SkillFinding[] = [];
  for (const agent of attached) {
    out.push(...agentFindings(agent, filesystem));
    for (const pin of agent.pins) out.push(...pinFindings(agent.nodeId, pin, { byPin, names, first, owners }));
  }
  out.push(...totalFindings(attached, byPin));
  return out;
}

function agentFindings(agent: AgentSkills, filesystem: boolean): SkillFinding[] {
  const out: SkillFinding[] = [];
  if (!filesystem) out.push({ code: "skills_filesystem_disabled", level: "error", nodeId: agent.nodeId });
  if (agent.pins.length > MAX_SKILLS_PER_AGENT) {
    out.push({ code: "too_many_skills", level: "error", nodeId: agent.nodeId, detail: detail("skillsAttached", agent.pins.length) });
  }
  return out;
}

interface PinScan {
  byPin: ReadonlyMap<string, PinnedSkill>;
  names: ReadonlyMap<string, string>;
  /** Skill id → 처음 본 리비전과 노드 */
  first: Map<string, { revision: number; nodeId: string }>;
  /** Skill 이름 → 그 이름을 먼저 쓴 Skill id */
  owners: Map<string, string>;
}

function pinFindings(nodeId: string, pin: SkillPin, scan: PinScan): SkillFinding[] {
  const out: SkillFinding[] = [];
  const at = (code: IssueCode, d: SkillDetail, level: SkillFinding["level"] = "error") => out.push({ code, level, nodeId, detail: d });
  const label = scan.names.get(pin.id) ?? pin.id;
  const seen = scan.first.get(pin.id) ?? { revision: pin.revision, nodeId };
  scan.first.set(pin.id, seen);
  if (seen.revision !== pin.revision) at("skill_revision_mismatch", detail("skillRevisions", label, pin.revision, seen.nodeId, seen.revision));
  const skill = scan.byPin.get(pinKey(pin.id, pin.revision));
  if (!skill) {
    at("unknown_skill", detail("skillMissing", label, pin.revision));
    return out;
  }
  const owner = scan.owners.get(skill.name) ?? pin.id;
  scan.owners.set(skill.name, owner);
  if (owner !== pin.id) at("skill_name_conflict", detail("skillNameTaken", skill.name));
  if (skill.scripts) at("skill_scripts_inert", detail("skill", skill.name), "warning");
  if (skill.latest > pin.revision) at("skill_behind", detail("skillBehind", skill.name, pin.revision, skill.latest), "warning");
  return out;
}

/** 그래프 전체의 상한이다. 문서의 최상위 `skills[]` 개수와 설정 볼륨에 쓰는 양을 묶는다. */
function totalFindings(attached: readonly AgentSkills[], byPin: ReadonlyMap<string, PinnedSkill>): SkillFinding[] {
  const keys = attached.flatMap((a) => a.pins.map((p) => pinKey(p.id, p.revision)));
  const distinct = new Set(keys);
  const size = (key: string) => byPin.get(key)?.bytes ?? 0;
  const out: SkillFinding[] = [];
  const count = new Set(attached.flatMap((a) => a.pins.map((p) => p.id))).size;
  if (count > MAX_SKILLS_PER_GRAPH) out.push({ code: "too_many_skills", level: "error", nodeId: null, detail: detail("skillsDistinct", count) });
  const total = [...distinct].reduce((n, key) => n + size(key), 0);
  const expanded = keys.reduce((n, key) => n + size(key), 0);
  if (total > MAX_SKILLS_DISTINCT_BYTES) out.push({ code: "skills_too_large", level: "error", nodeId: null, detail: detail("skillsDistinctBytes", total) });
  if (expanded > MAX_SKILLS_EXPANDED_BYTES) out.push({ code: "skills_too_large", level: "error", nodeId: null, detail: detail("skillsExpandedBytes", expanded) });
  return out;
}

// -- 검증 문맥 --------------------------------------------------------------------------------

/**
 * Skill 목록과 리비전 목록을 검증 문맥의 메타로 옮긴다(서버 `pinned_skills`). 목록의 행은 최신 리비전의 메타이고, 그보다
 * 낮게 고정한 핀은 그 Skill 의 리비전 목록이 채운다. 목록에 없는 Skill 의 리비전은 뺀다(보이지 않는 Skill 이다).
 */
export function pinnedSkills(skills: readonly Skill[], older: readonly SkillRevisionMeta[]): PinnedSkill[] {
  const byId = new Map(skills.map((s) => [s.id, s]));
  const latest = skills.map((s) => ({ id: s.id, name: s.name, revision: s.revision, latest: s.revision, bytes: s.latest.bytes, scripts: s.latest.files.some((f) => f.script) }));
  const rest = older.flatMap((m) => {
    const skill = byId.get(m.skill_id);
    if (!skill || m.revision === skill.revision) return [];
    return [{ id: m.skill_id, name: m.frontmatter.name, revision: m.revision, latest: skill.revision, bytes: m.bytes, scripts: m.files.some((f) => f.script) }];
  });
  return [...latest, ...rest];
}

/** 그래프의 노드 모양. 캔버스 상태와 API 응답(`data` 가 자유 객체) 어느 쪽이든 받는다. */
type LooseGraph = { nodes?: readonly { type: string; data?: unknown }[] } | null | undefined;

/** 에이전트 노드마다의 핀을 graph.nodes 순서로 편다. 같은 Skill 이 여러 번 나올 수 있다. */
function everyPin(graph: LooseGraph): SkillPin[] {
  return (graph?.nodes ?? []).filter((n) => n.type === "agent").flatMap((n) => pinsOf(n.data as { skills?: SkillPin[] }));
}

/** 그래프가 고정한 핀. Skill 마다 처음 본 것이고 graph.nodes 순서다(서버 `skill_pins`). */
export function graphSkillPins(graph: LooseGraph): SkillPin[] {
  const out = new Map<string, SkillPin>();
  for (const pin of everyPin(graph)) if (!out.has(pin.id)) out.set(pin.id, pin);
  return [...out.values()];
}

/**
 * 최신보다 낮게 고정한 핀이 있는 Skill. 검증 문맥이 이 Skill 들의 리비전 목록을 더 읽는다. 노드마다 리비전이 다른 그래프도
 * 있으므로 첫 핀만이 아니라 모든 핀을 본다.
 */
export function behindSkillIds(graph: LooseGraph, skills: readonly Skill[]): string[] {
  const latest = new Map(skills.map((s) => [s.id, s.revision]));
  const behind = everyPin(graph).filter((pin) => pin.revision < (latest.get(pin.id) ?? 0));
  return [...new Set(behind.map((pin) => pin.id))].sort();
}

// -- 화면 ---------------------------------------------------------------------------------------

/** 캔버스가 Skill id 를 풀어 보이는 것. 최신 리비전의 것이다. */
export interface SkillInfo {
  label: string;
  latest: number;
  owner: string;
  consumable: boolean;
  scripts: boolean;
}

export function toSkillInfo(s: Skill): SkillInfo {
  return { label: displayName(s), latest: s.revision, owner: s.tenant, consumable: s.permissions.consume, scripts: s.latest.files.some((f) => f.script) };
}

export function skillInfo(skills: readonly Skill[], id: string): SkillInfo | null {
  const s = skills.find((x) => x.id === id);
  return s ? toSkillInfo(s) : null;
}

/** 고정한 리비전보다 새 리비전이 있는가. 모르는 Skill 은 뒤처졌다고 하지 않는다. */
export function isPinBehind(pin: SkillPin, info: SkillInfo | null): boolean {
  return info !== null && info.latest > pin.revision;
}

/** 붙인 Skill 의 이름. 못 찾았으면 목록을 다 받았는지에 따라 "(삭제된 항목)" 이나 "…" 다. 단추의 낭독기 이름도 이것을 쓴다. */
export function pinName(t: Messages, info: SkillInfo | null, ready: boolean): string {
  return info?.label ?? unresolvedLabel(t, ready);
}

/** 칩과 줄의 글 `표시 이름 · r3`. */
export function pinText(t: Messages, pin: SkillPin, info: SkillInfo | null, ready: boolean): string {
  return `${pinName(t, info, ready)} · r${pin.revision}`;
}

/** 노드 data 에 핀 목록을 쓴다. 비면 키를 지워 붙인 적 없는 노드와 같은 모양으로 돌아간다(장기 기억과 같은 규칙). */
function withSkillPins<T extends { skills?: SkillPin[] }>(data: T, pins: readonly SkillPin[]): T {
  const { skills: _drop, ...rest } = data;
  return (pins.length ? { ...rest, skills: [...pins] } : rest) as T;
}

/** 붙인다. 그때의 최신 리비전으로 고정한다. 이미 붙어 있으면 그대로다. */
export function attachSkill<T extends { skills?: SkillPin[] }>(data: T, skill: { id: string; revision: number }): T {
  const pins = pinsOf(data);
  if (pins.some((p) => p.id === skill.id)) return data;
  return withSkillPins(data, [...pins, { id: skill.id, revision: skill.revision }]);
}

export function detachSkill<T extends { skills?: SkillPin[] }>(data: T, skillId: string): T {
  return withSkillPins(data, pinsOf(data).filter((p) => p.id !== skillId));
}

/** 그 Skill 의 핀을 `revision` 으로 올린다. 붙이지 않은 노드는 null 이라 부르는 쪽이 건드리지 않는다. */
export function upgradeSkill<T extends { skills?: SkillPin[] }>(data: T, skillId: string, revision: number): T | null {
  const pins = pinsOf(data);
  if (!pins.some((p) => p.id === skillId && p.revision !== revision)) return null;
  return withSkillPins(data, pins.map((p) => (p.id === skillId ? { id: p.id, revision } : p)));
}
