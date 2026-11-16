/**
 * 되돌리기와 평가 화면의 상한과 판단 (P45 6절, 7절, 9절, 11절, 12절, D109). 상한은 서버 `services/evaluation/inputs.py` 의 같은
 * 이름 값의 사본이고 platform/tests/test_evaluation_ui_parity.py 가 맞댄다. 사례 수 × 반복 수의 상한은 오류 사전도 읽어
 * lib/limits.ts 에 있다.
 *
 * 화면은 판정을 다시 하지 않는다. 사례와 반복 하나의 통과는 서버의 압축 판정(`CaseVerdict`)을 읽어 보이기만 하고, 통과율은
 * 실행의 합계(`totals`)에서 읽는다. 배포 게이트가 걸리는지는 서버가 판정하지만, 되돌리기 대화상자가 까닭 칸을 미리 보이도록
 * 같은 규칙(새 내용에만 걸린다)을 리비전의 지문과 배포 기록으로 읽는다.
 */
import type {
  Agent,
  AgentFeedback,
  AgentRevision,
  DeployRecord,
  EvaluationCaseContent,
  EvaluationCaseContentIn,
  EvaluationCheckKind,
  EvaluationRun,
  EvaluationVerdict,
} from "@/api/types";
import { attachedModelIds, mainAgentNode, mainJudge, type AgentData, type ModelData } from "@/lib/agentGraph";
import { parseJsonObject } from "@/lib/json";

export { EVALUATION_MAX_CASE_RUNS } from "@/lib/limits";

/** 세트 이름의 길이 상한. */
export const MAX_NAME_CHARS = 100;
/** 사례 하나의 질문 수 상한. */
export const MAX_MESSAGES = 5;
/** 사례 하나의 채점 기준 수 상한. */
export const MAX_CRITERIA = 10;
/** 사례 하나의 확인 항목 수 상한. */
export const MAX_CHECKS = 10;
/** 사례 하나의 쓰기 Tool 가상 결과 수 상한. */
export const MAX_TOOL_RESULTS = 20;
/** 세트의 반복 수 상한. */
export const MAX_REPEATS = 3;
/** 배포 게이트의 나이 한도(일) 기본값과 상한. */
export const DEFAULT_MAX_AGE_DAYS = 7;
export const MAX_AGE_DAYS = 30;
/** 나빠진 사례를 막는 게이트가 받는 세트의 반복 수 하한. 한 번의 아쉬운 답이 배포를 막지 않게 한다. */
export const BASELINE_REPEATS = 2;

/** 확인 항목의 종류. 화면이 보이는 차례다. Tool 확인은 Tool 이름을, 답 확인은 글을 받는다. */
export const CHECK_KINDS = ["tool_called", "tool_not_called", "answer_contains", "answer_not_contains", "answer_matches"] as const satisfies readonly EvaluationCheckKind[];
const TOOL_CHECKS: ReadonlySet<string> = new Set(["tool_called", "tool_not_called"]);

/** 확인 항목이 Tool 이름을 받는가. */
export function toolCheck(kind: string): boolean {
  return TOOL_CHECKS.has(kind);
}

/** 실행의 상태. 화면이 이름을 보이는 값이다. 모르는 값은 그대로 보인다. */
export const RUN_STATES = ["queued", "starting", "running", "grading", "finished", "error", "cancelled"] as const;
export type RunState = (typeof RUN_STATES)[number];
const ACTIVE_STATES: ReadonlySet<string> = new Set(["queued", "starting", "running", "grading"]);

/** 아직 끝나지 않은 실행인가. 도는 동안은 목록과 실행 하나를 짧게 다시 읽는다. */
export function activeRun(state: string): boolean {
  return ACTIVE_STATES.has(state);
}

/** 도는 실행이 있을 때 다시 읽는 간격(ms). */
export const RUN_POLL_MS = 3_000;

/** 통과율(%, 정수). 센 사례가 없으면 null 이다. 실행 하나와 거절에 실린 실행의 요약이 함께 쓴다. */
export function passRate(run: { totals: Pick<EvaluationRun["totals"], "counted" | "passed"> }): number | null {
  const { counted, passed } = run.totals;
  return counted ? Math.round((passed / counted) * 100) : null;
}

/** 사례와 반복 하나의 확인 항목 결과. 돌지 않았거나(`changed`) 확인할 것이 없으면 null 이다. */
export function checksPassed(v: EvaluationVerdict): boolean | null {
  if (v.state === "changed") return null;
  if (v.state === "error") return false;
  return v.checks_passed ?? null;
}

/** 사례와 반복 하나의 채점 기준 결과. 기준이 없거나 아직 채점하지 않았으면 null 이다. 채점 오류는 지나지 못한 것이다. */
export function criteriaPassed(v: EvaluationVerdict): boolean | null {
  if (!v.criteria || v.state === "changed") return null;
  if (v.grader_error) return false;
  return v.criteria_passed ?? null;
}

// -- 되돌리기와 배포 기록 (6절, 12절) --------------------------------------------------------------

/** 배포 기록의 결과 가운데 객체가 적용된 것. 파드가 준비됐다는 뜻은 아니다. */
export const DEPLOYED_OUTCOME = "deployed";

/** 리비전 번호 → 내용의 지문. */
export function digestsOf(revisions: readonly AgentRevision[] | undefined): ReadonlyMap<number, string> {
  return new Map((revisions ?? []).map((r) => [r.revision, r.spec_digest]));
}

/** 그 내용이 마지막으로 `deployed` 에 이른 기록 줄. 없으면 undefined 다. */
export function lastDeployedRow(history: readonly DeployRecord[], digest: string | undefined): DeployRecord | undefined {
  if (!digest) return undefined;
  return history.filter((r) => r.spec_digest === digest && r.outcome === DEPLOYED_OUTCOME).at(-1);
}

/**
 * 되돌릴 수 있는 리비전인가. 배포된 내용도, 지금 리비전의 내용도 아니어야 한다. 배포된 내용이면 서버가 409 이고, 지금 리비전의
 * 내용이면 되돌리기가 리비전을 만들지 않고 재배포와 같다(재배포 단추가 그 일을 한다). 지문을 모르면(목록을 아직 못 읽었다) 묻지
 * 않는다.
 */
export function canRollBackTo(agent: Pick<Agent, "revision" | "deployed_revision">, digests: ReadonlyMap<number, string>, revision: number): boolean {
  const digest = digests.get(revision);
  if (!digest || agent.deployed_revision === null) return false;
  return digest !== digests.get(agent.deployed_revision) && digest !== digests.get(agent.revision);
}

/**
 * 그 내용을 핀하는 동작에 배포 게이트가 걸리는가 (12절). 게이트가 켜져 있고, 배포된 리비전의 내용도 결과가 `deployed` 인 기록
 * 줄의 내용도 아닐 때다. 서버가 같은 규칙으로 다시 판정한다.
 */
export function gateApplies(agent: Pick<Agent, "evaluation_gate" | "deployed_revision" | "deploy_history">, digests: ReadonlyMap<number, string>, digest: string | undefined): boolean {
  if (!agent.evaluation_gate || !digest) return false;
  if (agent.deployed_revision !== null && digests.get(agent.deployed_revision) === digest) return false;
  return !lastDeployedRow(agent.deploy_history, digest);
}

// -- 사례 편집 (7절, 10절, 11절) -------------------------------------------------------------------

/** 편집기가 쥐는 확인 항목 하나. 인자는 JSON 글이고 저장할 때 푼다. */
export interface CheckDraft {
  kind: EvaluationCheckKind;
  tool: string;
  args: string;
  text: string;
}

/** 편집기가 쥐는 쓰기 Tool 의 가상 결과 하나. */
export interface ToolResultDraft {
  tool: string;
  result: string;
}

/** 편집기가 쥐는 사례의 내용. 빈 줄은 저장할 때 뺀다. */
export interface CaseDraft {
  messages: string[];
  reference: string;
  referenceGood: boolean;
  criteria: string[];
  checks: CheckDraft[];
  expectDone: boolean;
  toolResults: ToolResultDraft[];
}

export const EMPTY_CHECK: CheckDraft = { kind: "answer_contains", tool: "", args: "", text: "" };

export function emptyCase(): CaseDraft {
  return { messages: [""], reference: "", referenceGood: true, criteria: [], checks: [], expectDone: true, toolResults: [] };
}

/** 저장된 내용(또는 복사본)을 편집기의 꼴로. */
export function caseDraftOf(content: EvaluationCaseContent): CaseDraft {
  return {
    messages: content.messages.length ? [...content.messages] : [""],
    reference: content.reference?.text ?? "",
    referenceGood: content.reference?.good ?? true,
    criteria: [...(content.criteria ?? [])],
    checks: (content.checks ?? []).map((c) => ({
      kind: (CHECK_KINDS as readonly string[]).includes(c.kind) ? (c.kind as EvaluationCheckKind) : "answer_contains",
      tool: c.tool ?? "",
      args: c.args && Object.keys(c.args).length ? JSON.stringify(c.args) : "",
      text: c.text ?? "",
    })),
    expectDone: content.expect_done,
    toolResults: Object.entries(content.tool_results ?? {}).map(([tool, result]) => ({ tool, result })),
  };
}

/** 저장 본문을 만들지 못한 까닭. 인자 JSON 이 객체가 아니면 그 확인 항목의 차례(0부터)다. */
export type DraftProblem = { kind: "args"; index: number };

/**
 * 편집기의 꼴을 저장 본문으로. 빈 질문, 빈 기준, Tool 이름이 빈 가상 결과는 뺀다. 확인 항목은 종류에 맞는 칸만 싣는다. 인자
 * JSON 을 풀지 못하면 problem 이다. 상한과 꼴은 서버가 다시 본다.
 */
export function caseBodyOf(draft: CaseDraft): { content: EvaluationCaseContentIn } | { problem: DraftProblem } {
  const checks: EvaluationCaseContentIn["checks"] = [];
  for (const [index, c] of draft.checks.entries()) {
    if (toolCheck(c.kind)) {
      const parsed = c.kind === "tool_called" ? parseJsonObject(c.args) : { value: null, error: null };
      if (parsed.error) return { problem: { kind: "args", index } };
      const args = parsed.value ?? {};
      checks.push({ kind: c.kind, tool: c.tool.trim(), ...(Object.keys(args).length ? { args } : {}) });
    } else {
      checks.push({ kind: c.kind, text: c.text });
    }
  }
  const toolResults = Object.fromEntries(draft.toolResults.filter((r) => r.tool.trim()).map((r) => [r.tool.trim(), r.result]));
  return {
    content: {
      messages: draft.messages.filter((m) => m.trim()),
      reference: draft.reference.trim() ? { text: draft.reference, good: draft.referenceGood } : null,
      criteria: draft.criteria.filter((c) => c.trim()),
      checks,
      expect_done: draft.expectDone,
      tool_results: toolResults,
    },
  };
}

/** 사례를 한 줄로 가리키는 글. 첫 질문이다. */
export function caseLabel(content: Pick<EvaluationCaseContent, "messages"> | undefined, fallback: string): string {
  return content?.messages[0]?.trim() || fallback;
}

// -- 채점 모델 (11절) ------------------------------------------------------------------------------

/** 채점 모델로 고를 수 있는 provider 하나와 그 자리의 모델 노드가 정한 모델(비었으면 null). */
export interface GraderChoice {
  provider: string;
  model: string | null;
}

type GraphLike = {
  nodes?: readonly { id: string; type: string; data?: unknown }[];
  edges?: readonly { source: string; target: string; kind: string }[];
};

/**
 * 채점 모델의 provider 선택지. 서버 `services/agent_graph.py` `compiled_model_providers` 와 같은 자리다: 메인 에이전트의 모델과 그
 * 폴백, 서브에이전트의 모델, 메인이 붙인 가드레일의 판정 모델(P43 7절). 떠 있는 모델 노드와 서브에이전트·판정 모델의 폴백은 문서에
 * 실리지 않으므로 넣지 않는다. 시작과 채점이 같은
 * 규칙으로 다시 본다(409 `evaluation_grader_provider`). 같은 provider 는 처음 자리의 모델을 쓴다.
 */
export function graderChoices(graph: GraphLike): GraderChoice[] {
  const nodes = graph.nodes ?? [];
  const edges = graph.edges ?? [];
  const modelOf = (agentId: string) => {
    const id = attachedModelIds(agentId, edges)[0];
    const node = nodes.find((n) => n.id === id && n.type === "model");
    return node ? ((node.data ?? {}) as Partial<ModelData>) : null;
  };
  const found: GraderChoice[] = [];
  const add = (provider: string | null | undefined, model: string | null | undefined) => {
    if (provider && !found.some((c) => c.provider === provider)) found.push({ provider, model: model || null });
  };
  const main = mainAgentNode(nodes);
  const mainModel = main ? modelOf(main.id) : null;
  if (mainModel) {
    add(mainModel.provider, mainModel.model);
    add(mainModel.fallback?.provider, mainModel.fallback?.model);
  }
  for (const n of nodes) {
    if (n.type === "agent" && (n.data as Partial<AgentData> | undefined)?.role === "sub") {
      const sub = modelOf(n.id);
      if (sub) add(sub.provider, sub.model);
    }
  }
  const judge = main ? mainJudge(main.id, nodes, edges) : null;
  const judgeModel = judge ? nodes.find((n) => n.id === judge.model && n.type === "model") : undefined;
  if (judgeModel) {
    const d = (judgeModel.data ?? {}) as Partial<ModelData>;
    add(d.provider, d.model);
  }
  return found;
}

// -- 대화에서 복사 (8절) ---------------------------------------------------------------------------

/** 동의한 평가이고 그 턴이 아직 대화에 있는가. 다시 생성으로 걷힌 턴은 전사에 없어 복사할 수 없다. 피드백 탭과 세션 탭이 같이 쓴다. */
export function copyableRating(f: Pick<AgentFeedback, "evaluation_use" | "superseded">): boolean {
  return f.evaluation_use && !f.superseded;
}

// -- 개인정보 (8절) ------------------------------------------------------------------------------

/** 서버가 찾는 개인정보의 종류 (`services/evaluation/pii.py`). 화면이 이름을 보인다. */
export const PII_KINDS = ["email", "phone", "id_number", "card_number", "masked"] as const;
export type PiiKind = (typeof PII_KINDS)[number];

/** 422 `evaluation_case_pii` 의 찾은 것 하나. place 는 `messages[0]`, `reference`, `checks[1].text` 같은 칸의 자리다. */
export interface PiiMatch {
  kind: string;
  place: string;
}

/**
 * 개인정보 검사가 짚은 칸의 자리를 푼 것. 서버 `services/evaluation/pii.py` `_fields` 의 이름이다. index 는 저장 본문의 차례(0부터)이고,
 * 가상 결과는 Tool 이름(name)으로도 가리킨다. 모르는 꼴이면 null 이다.
 */
export type PiiPlace =
  | { field: "message" | "criterion" | "toolResultName"; index: number }
  | { field: "reference" }
  | { field: "checkTool" | "checkText" | "checkArgs"; index: number }
  | { field: "toolResult"; name: string };

/** 편집기의 칸 하나. index 는 편집기의 줄 차례(0부터)이고 가상 결과는 Tool 이름 칸(toolResultName)과 결과 칸(toolResult)이 있다. */
export type DraftField =
  | { field: "reference" }
  | { field: "message" | "criterion" | "checkTool" | "checkText" | "checkArgs" | "toolResultName" | "toolResult"; index: number };

/** 비지 않은 줄의 편집기 차례. 저장 본문은 빈 줄을 빼므로 저장의 k 번째가 편집기의 몇 번째인지 이것으로 찾는다. */
function kept<T>(rows: readonly T[], filled: (row: T) => boolean): number[] {
  return rows.flatMap((row, i) => (filled(row) ? [i] : []));
}

/**
 * 서버가 짚은 자리를 편집기의 칸으로. 질문과 채점 기준, 가상 결과는 빈 줄을 뺀 차례라 편집기의 차례로 옮기고, 가상 결과의 결과는 Tool
 * 이름으로 찾는다. 그 사이 사람이 칸을 고쳐 찾을 수 없으면 null 이다.
 */
export function draftFieldOf(place: string, draft: CaseDraft): DraftField | null {
  const p = parsePlace(place);
  if (!p) return null;
  const at = (rows: number[], index: number) => (index < rows.length ? rows[index] : null);
  switch (p.field) {
    case "reference":
      return p;
    case "message": {
      const index = at(kept(draft.messages, (m) => Boolean(m.trim())), p.index);
      return index === null ? null : { field: "message", index };
    }
    case "criterion": {
      const index = at(kept(draft.criteria, (c) => Boolean(c.trim())), p.index);
      return index === null ? null : { field: "criterion", index };
    }
    case "checkTool":
    case "checkText":
    case "checkArgs":
      return p.index < draft.checks.length ? p : null;
    case "toolResultName": {
      const index = at(kept(draft.toolResults, (r) => Boolean(r.tool.trim())), p.index);
      return index === null ? null : { field: "toolResultName", index };
    }
    case "toolResult": {
      const index = draft.toolResults.findIndex((r) => r.tool.trim() === p.name);
      return index < 0 ? null : { field: "toolResult", index };
    }
  }
}

export function parsePlace(place: string): PiiPlace | null {
  let m: RegExpExecArray | null;
  if (place === "reference") return { field: "reference" };
  if ((m = /^messages\[(\d+)\]$/.exec(place))) return { field: "message", index: Number(m[1]) };
  if ((m = /^criteria\[(\d+)\]$/.exec(place))) return { field: "criterion", index: Number(m[1]) };
  if ((m = /^checks\[(\d+)\]\.(tool|text|args)$/.exec(place))) {
    const field = m[2] === "tool" ? "checkTool" : m[2] === "text" ? "checkText" : "checkArgs";
    return { field, index: Number(m[1]) };
  }
  if ((m = /^tool_results\.names\[(\d+)\]$/.exec(place))) return { field: "toolResultName", index: Number(m[1]) };
  if ((m = /^tool_results\[(".*")\]$/.exec(place))) {
    try {
      const name: unknown = JSON.parse(m[1]);
      return typeof name === "string" ? { field: "toolResult", name } : null;
    } catch {
      return null;
    }
  }
  return null;
}
