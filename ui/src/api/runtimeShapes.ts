/**
 * 런타임이 쓰고 플랫폼이 그대로 건네는 모양을 좁힌다 (P73 2절). 보류 승인 요청, 세션 메시지 요약, 전사 항목은 서버
 * 스키마에서 자유 객체다. null 이 오고 새 종류가 릴리스마다 늘 수 있어서다. 여기서 칸마다 타입 가드로 읽어 null 은 없는
 * 것으로 보고, 모르는 전사 종류는 그대로 넘겨 접는 쪽(`lib/assistant/transcript.ts` `transcriptItem`)이 건너뛰게 한다.
 * 캐스트는 쓰지 않는다.
 */
import type {
  AgentDecisionType,
  AgentProposal,
  ApprovalReason,
  AgentReviewConfig,
  AgentSessionDetail,
  AgentSessionMessage,
  AssistantActionRequest,
  AssistantPending,
  AssistantQuestion,
  AssistantTranscriptItem,
  ProposalAttention,
  ProposalChanges,
  ProposalEdge,
  ProposalNode,
  ProposalRef,
  ProposalTextDiff,
  RememberChoice,
} from "./types";
import { finiteNumber, isRecord } from "@/lib/json";


function records(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.filter(isRecord) : [];
}

function optionalString(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : [];
}

const DECISIONS: readonly AgentDecisionType[] = ["approve", "edit", "reject", "respond"];

function isDecision(v: unknown): v is AgentDecisionType {
  return DECISIONS.some((d) => d === v);
}

const REASONS: readonly ApprovalReason[] = ["rule", "outside"];

function isReason(v: unknown): v is ApprovalReason {
  return REASONS.some((r) => r === v);
}

const REMEMBER_CHOICES: readonly RememberChoice[] = ["approve", "reject"];

function isRememberChoice(v: unknown): v is RememberChoice {
  return REMEMBER_CHOICES.some((c) => c === v);
}

/**
 * 이름이 없는 요청에 다는 자리 표시. 모양이 틀린 요청도 버리지 않는다. 결정은 요청과 같은 수로 보내야 해서
 * (런타임 422 decision_count_mismatch) 하나를 버리면 남은 결정이 모두 거절된다. 카드에 "?" 로 보여 사람이 거절할 수 있다.
 */
export const UNNAMED_REQUEST = "?";

function proposalNode(r: Record<string, unknown>): ProposalNode {
  const name = optionalString(r.name);
  return {
    id: optionalString(r.id) ?? "",
    type: optionalString(r.type) ?? "",
    ...(name ? { name } : {}),
    ...(Array.isArray(r.fields) ? { fields: strings(r.fields) } : {}),
  };
}

function proposalEdge(r: Record<string, unknown>): ProposalEdge {
  return { source: optionalString(r.source) ?? "", target: optionalString(r.target) ?? "", kind: optionalString(r.kind) ?? "" };
}

function textDiff(r: Record<string, unknown>): ProposalTextDiff {
  const lines = records(r.lines).flatMap((l): ProposalTextDiff["lines"] => (l.op === "-" || l.op === "+" ? [{ op: l.op, text: optionalString(l.text) ?? "" }] : []));
  return { node_id: optionalString(r.node_id) ?? "", field: optionalString(r.field) ?? "", lines, cut: r.cut === true };
}

function proposalRef(v: unknown): ProposalRef | undefined {
  if (!isRecord(v) || typeof v.id !== "string") return undefined;
  const kind = optionalString(v.kind);
  const name = optionalString(v.name);
  return { id: v.id, ...(kind ? { kind } : {}), ...(name ? { name } : {}), ...("revision" in v ? { revision: finiteNumber(v.revision) } : {}) };
}

function attentionMark(r: Record<string, unknown>): ProposalAttention {
  const out: ProposalAttention = { code: optionalString(r.code) ?? "" };
  for (const key of ["node_id", "agent", "tool", "position", "provider"] as const) {
    const v = optionalString(r[key]);
    if (v !== undefined) out[key] = v;
  }
  if ("model" in r) out.model = optionalString(r.model) ?? null;
  for (const key of ["source", "knowledge", "skill"] as const) {
    const ref = proposalRef(r[key]);
    if (ref) out[key] = ref;
  }
  if (Array.isArray(r.fields)) out.fields = strings(r.fields);
  return out;
}

function changesOf(v: unknown): ProposalChanges {
  const c = isRecord(v) ? v : {};
  const nodes = isRecord(c.nodes) ? c.nodes : {};
  const edges = isRecord(c.edges) ? c.edges : {};
  const display = isRecord(c.display_name) ? c.display_name : null;
  return {
    nodes: { added: records(nodes.added).map(proposalNode), changed: records(nodes.changed).map(proposalNode), removed: records(nodes.removed).map(proposalNode) },
    edges: { added: records(edges.added).map(proposalEdge), removed: records(edges.removed).map(proposalEdge) },
    service: strings(c.service),
    texts: records(c.texts).map(textDiff),
    ...(display ? { display_name: { before: optionalString(display.before) ?? null, after: optionalString(display.after) ?? null } } : {}),
  };
}

/**
 * Agent 정의 제안의 덩어리(P48 6.4절)를 좁힌다. SSE `interrupt` 의 `action_requests[i].proposal` 과 `GET /proposal` 의
 * `proposal` 이 같은 모양이다. 제안 id 나 종류가 없으면 제안으로 보지 않는다(카드는 인자를 그대로 보인다).
 */
export function normalizeProposal(v: unknown): AgentProposal | undefined {
  if (!isRecord(v) || typeof v.proposal_id !== "string" || !v.proposal_id || (v.kind !== "create" && v.kind !== "update")) return undefined;
  const agent = isRecord(v.agent) ? v.agent : {};
  return {
    proposal_id: v.proposal_id,
    kind: v.kind,
    agent: { id: optionalString(agent.id) ?? null, name: optionalString(agent.name) ?? "", display_name: optionalString(agent.display_name) ?? null },
    base_revision: finiteNumber(v.base_revision),
    deployed_revision: finiteNumber(v.deployed_revision),
    changes: changesOf(v.changes),
    attention: records(v.attention).map(attentionMark),
    warnings: records(v.warnings).map((w) => ({ code: optionalString(w.code) ?? "", node_id: optionalString(w.node_id) ?? null, message: optionalString(w.message) ?? null })),
  };
}

function actionRequest(r: Record<string, unknown>): AssistantActionRequest {
  const files = records(r.files)
    .filter((f) => typeof f.name === "string")
    .map((f) => ({ name: String(f.name), url: optionalString(f.url) ?? null }));
  const proposal = normalizeProposal(r.proposal);
  return {
    name: typeof r.name === "string" ? r.name : UNNAMED_REQUEST,
    args: isRecord(r.args) ? r.args : {},
    ...(typeof r.description === "string" ? { description: r.description } : {}),
    ...(Array.isArray(r.files) ? { files } : {}),
    ...(typeof r.id === "string" ? { id: r.id } : {}),
    ...(isReason(r.reason) ? { reason: r.reason } : {}),
    ...(proposal ? { proposal } : {}),
  };
}

function rememberOptions(v: unknown): RememberChoice[][] | undefined {
  if (!Array.isArray(v)) return undefined;
  return v.map((choices) => (Array.isArray(choices) ? choices.filter(isRememberChoice) : []));
}

function reviewConfig(r: Record<string, unknown>): AgentReviewConfig {
  return { action_name: String(r.action_name), allowed_decisions: Array.isArray(r.allowed_decisions) ? r.allowed_decisions.filter(isDecision) : [] };
}

function question(v: unknown): AssistantQuestion | null | undefined {
  if (v === null) return null;
  if (!isRecord(v)) return undefined;
  return {
    text: optionalString(v.text) ?? "",
    ...(Array.isArray(v.options) ? { options: strings(v.options) } : v.options === null ? { options: null } : {}),
    ...(typeof v.field === "string" || v.field === null ? { field: v.field } : {}),
  };
}

/**
 * 보류 승인 요청(`GET /sessions/{sid}/pending` 과 SSE `interrupt` 의 data)을 승인 카드가 읽는 모양으로 좁힌다. 런타임
 * 계약이 다른 두 곳(에이전트·비서)이 같은 카드를 쓴다. review_configs 가 없으면 요청마다 승인·거절을 허락한다.
 */
export function normalizePending(raw: unknown): AssistantPending {
  const r = isRecord(raw) ? raw : {};
  const action_requests = Array.isArray(r.action_requests) ? r.action_requests.map((a) => actionRequest(isRecord(a) ? a : {})) : [];
  const review_configs = Array.isArray(r.review_configs)
    ? records(r.review_configs)
        .filter((c) => typeof c.action_name === "string")
        .map(reviewConfig)
    : action_requests.map((a): AgentReviewConfig => ({ action_name: a.name, allowed_decisions: ["approve", "reject"] }));
  const q = question(r.question);
  const remember = rememberOptions(r.remember_options);
  return {
    session_id: optionalString(r.session_id) ?? "",
    action_requests,
    review_configs,
    ...(remember && typeof r.remember_days === "number" ? { remember_options: remember, remember_days: r.remember_days } : {}),
    ...(r.kind === "confirm" || r.kind === "question" ? { kind: r.kind } : {}),
    ...(q !== undefined ? { question: q } : {}),
  };
}

function isTranscriptItem(v: unknown): v is AssistantTranscriptItem {
  return isRecord(v) && typeof v.kind === "string";
}

/** 전사 항목 배열. `kind` 가 글자가 아닌 항목은 버린다. 배열이 아니면(null 포함) 빈 배열이다. */
export function transcriptItems(raw: unknown): AssistantTranscriptItem[] {
  return Array.isArray(raw) ? raw.filter(isTranscriptItem) : [];
}

/** Agent 세션의 화면용 전사 봉투 `{items, limit}` (D35 F1). 봉투가 아니면 없다. */
export function agentTranscript(raw: unknown): AgentSessionDetail["transcript"] {
  if (!isRecord(raw)) return undefined;
  return { items: transcriptItems(raw.items), ...(typeof raw.limit === "number" ? { limit: raw.limit } : {}) };
}

/** 세션 상세의 메시지 요약 (런타임 `summarize_messages`). role·text 가 없으면 빈 글자다. */
export function sessionMessages(raw: unknown): AgentSessionMessage[] {
  return records(raw).map((m) => ({
    role: optionalString(m.role) ?? "",
    text: optionalString(m.text) ?? "",
    ...(Array.isArray(m.tool_calls) ? { tool_calls: strings(m.tool_calls) } : {}),
    ...(typeof m.tool === "string" ? { tool: m.tool } : {}),
    ...(typeof m.status === "string" ? { status: m.status } : {}),
  }));
}
