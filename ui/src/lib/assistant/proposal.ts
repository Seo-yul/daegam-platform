/**
 * 청지기의 Agent 정의 제안(P48 6.4·6.5절)을 화면이 읽는 규칙. 카드와 캔버스는 플랫폼이 한 번 비교한 덩어리를 그리기만
 * 하고 스스로 비교하지 않는다. 여기에는 캔버스로 가는 링크, 제목과 먼저 볼 것의 문장, 노드 표시처럼 덩어리를 읽는 것만 있다.
 */
import type { AgentGraphOut, AgentProposal, ProposalAttention, ProposalRef } from "@/api/types";
import type { ProposalMark } from "@/components/canvas/types";
import type { Messages } from "@/i18n";
import { parseJsonObject } from "@/lib/json";
import { ownValue } from "@/lib/utils";

/** 캔버스 주소가 싣는 제안 id 와 그 제안이 기다리는 세션. 캔버스는 이 둘로 제 제안과 같은 세션의 나중 제안을 가른다. */
export const PROPOSAL_PARAM = "proposal";
export const PROPOSAL_SESSION_PARAM = "session";

/** 캔버스의 대상. 저장된 Agent 의 캔버스면 그 id, 새 Agent 캔버스면 null 이다. 같은 대상의 나중 제안만 불러오자고 한다. */
export function canvasTarget(agentId: string | null): string {
  return agentId === null ? "new" : `agent:${agentId}`;
}

/** 제안이 고치는 대상. 바꾸기는 그 Agent, 만들기는 새 Agent 다. */
export function proposalTarget(p: Pick<AgentProposal, "kind" | "agent">): string {
  return canvasTarget(p.kind === "update" ? (p.agent.id ?? "") : null);
}

/**
 * "캔버스에서 보기" 의 주소. 만들기는 새 Agent 캔버스이고 바꾸기는 그 Agent 의 정의 탭이다. 바꾸기인데 Agent id 가 없으면
 * 열 캔버스가 없어 null 이다.
 */
export function proposalHref(sessionId: string, p: Pick<AgentProposal, "proposal_id" | "kind" | "agent">): string | null {
  const q = new URLSearchParams();
  if (p.kind === "update") {
    if (!p.agent.id) return null;
    q.set("tab", "definition");
  }
  q.set(PROPOSAL_PARAM, p.proposal_id);
  q.set(PROPOSAL_SESSION_PARAM, sessionId);
  return p.kind === "update" ? `/agents/${encodeURIComponent(p.agent.id ?? "")}?${q}` : `/agents/new?${q}`;
}

/** 주소의 제안 id 와 세션. 둘 중 하나라도 없으면 제안을 연 캔버스가 아니다. */
export function proposalLink(search: string): { proposalId: string; sessionId: string } | null {
  const q = new URLSearchParams(search);
  const proposalId = q.get(PROPOSAL_PARAM);
  const sessionId = q.get(PROPOSAL_SESSION_PARAM);
  return proposalId && sessionId ? { proposalId, sessionId } : null;
}

export function proposalTitle(t: Messages, p: AgentProposal): string {
  const text = t.shared.approvalCard.proposal;
  const name = p.agent.display_name || p.agent.name;
  return p.kind === "update" ? text.titleUpdate(name, p.base_revision ?? 0) : text.titleNew(name);
}

export function proposalCounts(p: AgentProposal): { added: number; changed: number; removed: number } {
  const { nodes } = p.changes;
  return { added: nodes.added.length, changed: nodes.changed.length, removed: nodes.removed.length };
}

/** 노드 종류의 화면 이름. 이 화면이 모르는 종류는 그대로다. */
export function nodeTypeLabel(t: Messages, type: string): string {
  return ownValue(t.canvas.nodeLabels, type) ?? type;
}

/** 간선 종류의 화면 이름. 이 화면이 모르는 종류는 그대로다. */
export function edgeKindLabel(t: Messages, kind: string): string {
  return ownValue(t.canvas.edgeLabels, kind) ?? kind;
}

/** 제안이 바꾸는 글 칸의 화면 이름. 인스펙터가 그 칸에 다는 이름과 같고, 모르는 칸은 칸 이름 그대로다. */
export function textFieldLabel(t: Messages, field: string): string {
  switch (field) {
    case "system_prompt":
      return t.canvas.agent.systemPrompt;
    case "description":
      return t.canvas.fields.description;
    case "greeting":
      return t.shared.chatPanel.greeting;
    case "message":
      return t.canvas.guardrail.message;
    default:
      return field;
  }
}

/** 플랫폼이 문서에서 읽은 이름. 못 읽었으면 id 다. */
function refLabel(ref: ProposalRef | undefined): string | undefined {
  return ref ? ref.name || ref.id : undefined;
}

/**
 * 먼저 볼 것 하나의 문장. 값은 플랫폼이 싣는 것(문서의 이름, provider 와 모델 id, MCP 프로필 꼴의 Tool 이름)이고, 싣지 않은
 * 값(이름을 못 읽었거나 Tool 이름이 그 꼴이 아니다)은 그 노드 id 로 대신한다. 이 화면이 모르는 code 는 code 그대로 보인다.
 */
export function attentionText(t: Messages, mark: ProposalAttention): string {
  const text = t.shared.approvalCard.proposal.attention;
  const where = mark.node_id ?? "?";
  switch (mark.code) {
    case "system_prompt_changed":
      return text.system_prompt_changed(mark.agent || where);
    case "tool_source_added":
      return text.tool_source_added(refLabel(mark.source) ?? where);
    case "write_without_approval":
      return text.write_without_approval(mark.tool || where);
    case "approval_removed":
      return text.approval_removed(mark.tool || where);
    case "guardrail_weakened": {
      // 가드레일에는 이름이 없어 캔버스의 위치 낱말로 가리킨다
      const position = mark.position ? (ownValue(t.canvas.guardrail.positions, mark.position) ?? mark.position) : where;
      return text.guardrail_weakened(position);
    }
    case "knowledge_attached":
      return text.knowledge_attached(refLabel(mark.knowledge) ?? where);
    case "model_changed": {
      const model = [mark.provider, mark.model].filter(Boolean).join(" · ") || where;
      return text.model_changed(mark.agent || where, model);
    }
    case "skill_attached":
      return text.skill_attached(refLabel(mark.skill) ?? where);
    case "web_reach_widened":
    case "rate_limit_removed":
    case "code_execution":
    case "service_changed":
    case "memory_changed":
      return text[mark.code];
    default:
      return mark.code;
  }
}

/** 캔버스가 노드 카드에 다는 표시. 더하는 노드와 바꾸는 노드다. 지우는 노드는 캔버스에 없어 따로 적는다. */
export function proposalNodeMarks(p: AgentProposal): Map<string, ProposalMark> {
  const marks = new Map<string, ProposalMark>();
  for (const n of p.changes.nodes.added) marks.set(n.id, "added");
  for (const n of p.changes.nodes.changed) marks.set(n.id, "changed");
  return marks;
}

/**
 * 그래프에서 노드 하나의 글 칸 전체. 캔버스의 인스펙터가 제안이 바꾸는 글의 앞(기준 리비전)과 뒤(짜 맞춘 정의)를 보이는 데
 * 쓴다. 노드나 칸이 없거나 글이 아니면 빈 글이다.
 */
export function nodeText(graph: Pick<AgentGraphOut, "nodes"> | undefined, nodeId: string, field: string): string {
  const value = graph?.nodes.find((n) => n.id === nodeId)?.data?.[field];
  return typeof value === "string" ? value : "";
}

/** 결정한 제안 호출의 성공 결과에서 저장된 Agent 의 id 와 리비전. 4 KB 로 잘려도 둘은 남는다. 못 읽으면 null 이다. */
export function savedAgentOf(content: string): { id: string; revision: number } | null {
  const v = parseJsonObject(content).value;
  return v && typeof v.id === "string" && typeof v.revision === "number" ? { id: v.id, revision: v.revision } : null;
}

/**
 * 사람이 캔버스에서 제 판을 저장한 뒤 제안 호출을 닫는 거절 사유(P48 6.5절). 사람이 아니라 청지기가 읽는 글이라 화면 언어를
 * 따르지 않고 늘 같은 문장이며, 저장된 리비전이나 새 Agent 의 id 를 적어 청지기가 그 저장에서 이어 가게 한다. 플랫폼은 이 거절에
 * `canvas_saved` 를 보고 카드 거절이 아닌 제 설명을 붙인다.
 */
export function canvasSaveReason({ revision, agentId }: { revision: number; agentId?: string }): string {
  const what = agentId ? `created the Agent ${agentId} at revision ${revision}` : `saved revision ${revision}`;
  return `The user edited this proposal on the canvas and ${what} themselves. Acknowledge it in one line and do not propose this change again.`;
}
