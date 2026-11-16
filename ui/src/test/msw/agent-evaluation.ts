/**
 * Agent 평가의 세트, 사례, 실행, 배포 게이트 (P45 7절~13절, D109). 플랫폼 `api/agent_evaluation.py` 를 흉내 낸다. 되돌리기와 배포
 * 기록, 게이트의 판정은 agents.ts 의 배포 핸들러가 이 모듈의 `gateRefusal` 로 한다.
 *
 * 목의 규칙은 셋이다.
 * - 세트의 지문은 사례의 지문(내용의 JSON)과 반복 수로 짓는다. 사례를 바꾸면 지문이 바뀌어 시작이 409 `evaluation_set_changed` 다.
 * - 개인정보는 전화번호(010-1234-5678 꼴)와 이메일만 찾는다. 복사한 사례에 남으면 `acknowledgeable: false`, 손으로 쓴 사례는
 *   `pii_acknowledged` 가 없으면 `acknowledgeable: true` 로 422 다. 복사는 동의한 평가(`agentFeedbackState`)의 턴만 받고 질문의
 *   전화번호를 `[phone]` 으로 바꾼다.
 * - 실행은 시작한 뒤 `finishAfterMs` 가 지나면 끝난다(vitest 는 바로, 브라우저 목 서버는 진행을 보이려고 늦춘다). 첫 질문에
 *   `FAIL` 이 든 사례는 확인 항목이 지나지 못한다. 쓰기 Tool 의 가상 결과가 있으면 그 Tool 호출을 `simulated` 로 적는다.
 * 부르는 사람은 `evaluationState.me` 이고 실행의 결과 내용은 시작한 사람에게만 준다.
 */
import type { HttpHandler } from "msw";

import type {
  Agent,
  EvaluationCase,
  EvaluationCaseBody,
  EvaluationGate,
  EvaluationResult,
  EvaluationRun,
  EvaluationRunDetail,
  EvaluationSet,
  EvaluationSetBody,
  EvaluationVerdict,
} from "@/api/types";

import { agentFeedbackState } from "./agent-feedback";
import { agentStore, digestOf } from "./agents";
import { meAdmin } from "./auth";
import { HttpResponse, NOW, apiError, http, record } from "./core";

export interface EvaluationMockState {
  sets: EvaluationSet[];
  cases: EvaluationCase[];
  runs: EvaluationRunDetail[];
  /** 부르는 사람의 sub 와 이름. */
  me: string;
  meName: string;
  /** 시작한 뒤 끝나기까지(ms). */
  finishAfterMs: number;
  /** 실행 id 마다 끝나는 때(ms). */
  finishAt: Record<string, number>;
}

export const evaluationState: EvaluationMockState = { sets: [], cases: [], runs: [], me: meAdmin.sub, meName: meAdmin.sub, finishAfterMs: 0, finishAt: {} };

export function resetEvaluationState(): void {
  evaluationState.sets = [];
  evaluationState.cases = [];
  evaluationState.runs = [];
  evaluationState.me = meAdmin.sub;
  evaluationState.meName = meAdmin.sub;
  evaluationState.finishAfterMs = 0;
  evaluationState.finishAt = {};
}

/** 세트 하나. 기본은 Agent ag-support 의 반복 1회, 채점 모델 없는 세트다. */
export function evaluationSetFixture(over: Partial<EvaluationSet> = {}): EvaluationSet {
  return {
    id: "es-1",
    tenant: "default",
    agent_id: "ag-support",
    name: "환불 문의",
    grader: null,
    repeats: 1,
    case_count: 0,
    created_at: NOW,
    created_by: "dev@example.com",
    updated_at: NOW,
    updated_by: "dev@example.com",
    digest: "",
    ...over,
  };
}

/** 사례 하나. 기본은 다른 사람(dev@example.com)이 손으로 쓴 사례다. */
export function evaluationCaseFixture(over: Partial<EvaluationCase> = {}): EvaluationCase {
  const content = over.content ?? { messages: ["환불 기한이 언제인가요?"], reference: null, criteria: [], checks: [{ kind: "answer_contains", tool: null, args: {}, text: "7일" }], expect_done: true, tool_results: {} };
  return {
    id: "ec-1",
    tenant: "default",
    agent_id: "ag-support",
    set_id: "es-1",
    content,
    digest: JSON.stringify(content),
    origin: null,
    expires_at: null,
    pii_ack: null,
    created_at: NOW,
    created_by: "dev@example.com",
    updated_at: NOW,
    updated_by: "dev@example.com",
    ...over,
  };
}

/** 끝난 실행 하나. 기본은 dev@example.com 이 r2 로 시작해 사례 ec-1 이 지난 것이다. */
export function evaluationRunFixture(over: Partial<EvaluationRunDetail> = {}): EvaluationRunDetail {
  return {
    id: "er-1",
    set_id: "es-1",
    set_digest: "",
    revision: 2,
    spec_digest: "v1:r2",
    grader: null,
    state: "finished",
    reason: null,
    cancelled_by: null,
    started_by: "dev@example.com",
    started_by_name: "dev@example.com",
    header_mode: false,
    cases: 1,
    repeats: 1,
    done_steps: 1,
    graded_steps: 0,
    verdicts: [{ case_id: "ec-1", case_digest: "", repeat: 0, state: "done", counted: true, checks_passed: true, criteria: 0, criteria_passed: null, grader_error: false }],
    totals: { cases: 1, counted: 1, passed: 1, tokens: 120, cost_usd: null },
    created_at: NOW,
    running_at: NOW,
    ended_at: NOW,
    results: [],
    content_hidden: true,
    baseline: { worse: [], missing: [] },
    ...over,
  };
}

/** 세트의 지금 지문. 반복 수와 사례마다의 지문이다. */
export function setDigest(setId: string): string {
  const set = evaluationState.sets.find((s) => s.id === setId);
  const cases = evaluationState.cases.filter((c) => c.set_id === setId).map((c) => c.digest);
  return `sd:${set?.repeats ?? 0}:${cases.join("|")}`;
}

function setOut(s: EvaluationSet): EvaluationSet {
  return { ...s, case_count: evaluationState.cases.filter((c) => c.set_id === s.id).length, digest: setDigest(s.id) };
}

function findSet(agentId: unknown, setId: unknown): EvaluationSet | Response {
  const s = evaluationState.sets.find((x) => x.id === String(setId) && x.agent_id === String(agentId));
  return s ?? apiError(404, "not_found", `evaluation set ${String(setId)} not found`);
}

const PHONE = /01[016789][-. ]?\d{3,4}[-. ]?\d{4}/g;
const EMAIL = /[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9-]+\.[A-Za-z]{2,}/g;

/** 사례 내용에서 개인정보로 보이는 것의 자리와 종류. */
function piiOf(content: EvaluationCaseBody["content"]): { kind: string; place: string }[] {
  const found: { kind: string; place: string }[] = [];
  const look = (text: string | null | undefined, place: string) => {
    if (!text) return;
    if (text.match(PHONE)) found.push({ kind: "phone", place });
    if (text.match(EMAIL)) found.push({ kind: "email", place });
  };
  content.messages.forEach((m, i) => look(m, `messages[${i}]`));
  look(content.reference?.text, "reference");
  (content.checks ?? []).forEach((c, i) => look(c.text, `checks[${i}].text`));
  Object.entries(content.tool_results ?? {}).forEach(([name, text]) => look(text, `tool_results["${name}"]`));
  return found;
}

/** 저장 모델의 꼴로. 입력에서 비운 칸의 기본값을 채운다. */
function storedContent(content: EvaluationCaseBody["content"]): EvaluationCase["content"] {
  return {
    messages: content.messages,
    reference: content.reference ? { text: content.reference.text, good: content.reference.good ?? true } : null,
    criteria: content.criteria ?? [],
    checks: (content.checks ?? []).map((c) => ({ kind: c.kind, tool: c.tool ?? null, args: c.args ?? {}, text: c.text ?? null })),
    expect_done: content.expect_done,
    tool_results: content.tool_results ?? {},
  };
}

function consents(ratingId: string): boolean {
  return agentFeedbackState.items.some((f) => f.id === ratingId && f.evaluation_use && !f.superseded);
}

/** 사례 저장의 거절. 없으면 null 이다. */
function caseRefusal(body: { content: EvaluationCaseBody["content"]; pii_acknowledged?: boolean }, origin: EvaluationCase["origin"]): Response | null {
  if (origin && !consents(origin.rating_id)) return apiError(409, "evaluation_consent_missing", "the rating does not consent");
  const pii = piiOf(body.content);
  if (pii.length && (origin || !body.pii_acknowledged)) {
    return apiError(422, "evaluation_case_pii", "the case holds text that looks like personal information", { matches: pii.map((m) => ({ ...m, start: 0, end: 0 })), acknowledgeable: !origin });
  }
  return null;
}

/** 그때의 모습. 끝날 때가 지났으면 판정과 결과를 채워 끝낸다. */
function view(run: EvaluationRunDetail): EvaluationRunDetail {
  const at = evaluationState.finishAt[run.id];
  if (at === undefined || !["queued", "starting", "running", "grading"].includes(run.state)) return run;
  if (Date.now() < at) return { ...run, state: "running", done_steps: 0 };
  const finished = finish(run);
  evaluationState.runs = evaluationState.runs.map((r) => (r.id === run.id ? finished : r));
  delete evaluationState.finishAt[run.id];
  return finished;
}

function finish(run: EvaluationRunDetail): EvaluationRunDetail {
  const cases = evaluationState.cases.filter((c) => c.set_id === run.set_id);
  const verdicts: EvaluationVerdict[] = [];
  const results: EvaluationResult[] = [];
  for (const c of cases) {
    for (let repeat = 0; repeat < run.repeats; repeat += 1) {
      const failed = c.content.messages[0]?.includes("FAIL") ?? false;
      const counted = (c.content.checks ?? []).length > 0;
      verdicts.push({ case_id: c.id, case_digest: c.digest, repeat, state: "done", counted, checks_passed: !failed, criteria: c.content.criteria?.length ?? 0, criteria_passed: c.content.criteria?.length ? !failed : null, grader_error: false });
      const calls = Object.entries(c.content.tool_results ?? {}).map(([name, result]) => ({ id: `call-${name}`, name, args: {}, answer: "simulated", status: "success", result }));
      results.push({
        id: `${run.id}-${c.id}-${repeat}`,
        tenant: "default",
        agent_id: "ag-support",
        run_id: run.id,
        set_id: run.set_id,
        case_id: c.id,
        case_digest: c.digest,
        repeat,
        started_by: run.started_by,
        session_id: `s-eval-${c.id}`,
        answer: `결과: ${calls[0]?.result ?? c.content.messages.join(" ")}`,
        tool_calls: calls,
        checks: (c.content.checks ?? []).map((k) => ({ kind: k.kind, passed: !failed, gap: failed ? "the answer lacks the text" : null })),
        criteria: (c.content.criteria ?? []).map((criterion) => ({ criterion, passed: !failed, gap: failed ? "missing" : null })),
        grader_call: null,
        grader_error: null,
        turns: ["t-1"],
        tokens: { input: 10, output: 5 },
        cost_usd: null,
        created_at: NOW,
        expires_at: NOW,
      });
    }
  }
  const counted = cases.filter((c) => (c.content.checks ?? []).length > 0);
  const passed = counted.filter((c) => !c.content.messages[0]?.includes("FAIL"));
  return {
    ...run,
    state: "finished",
    done_steps: verdicts.length,
    verdicts,
    results,
    totals: { cases: cases.length, counted: counted.length, passed: passed.length, tokens: 100, cost_usd: null },
    ended_at: NOW,
    baseline: { worse: [], missing: [] },
  };
}

function runOut(run: EvaluationRunDetail): EvaluationRun {
  const { results: _results, content_hidden: _hidden, baseline: _baseline, ...rest } = run;
  return rest;
}

/**
 * 배포 게이트의 판정 (12절). 게이트가 걸리지 않거나 지나면 null 과 받아들인 실행의 id 이고, 막으면 409 응답이다. 새 내용(지문이
 * 배포된 리비전의 것도, 결과가 deployed 인 기록 줄의 것도 아니다)에만 걸린다.
 */
export function gateRefusal(agent: Agent, digest: string, deployedDigest: string | null, skip: boolean): { refusal: Response | null; run: string | null } {
  const gate = agent.evaluation_gate;
  if (!gate || skip) return { refusal: null, run: null };
  if (digest === deployedDigest || agent.deploy_history.some((r) => r.spec_digest === digest && r.outcome === "deployed")) return { refusal: null, run: null };
  const runs = evaluationState.runs.map(view).filter((r) => r.set_id === gate.set_id && r.spec_digest === digest);
  const finished = runs.find((r) => r.state === "finished" && r.set_digest === setDigest(gate.set_id));
  const summary = (r: EvaluationRunDetail | undefined) => (r ? { id: r.id, state: r.state, totals: r.totals } : null);
  if (!finished) {
    const reason = runs.some((r) => r.state === "finished") ? "stale" : "no_run";
    return { refusal: apiError(409, "evaluation_required", `the deploy needs a passed evaluation run of its content (${reason})`, { reason, run: summary(runs[0]), judged: null }), run: null };
  }
  const rate = finished.totals.counted ? finished.totals.passed / finished.totals.counted : 0;
  if (!finished.totals.counted) {
    return { refusal: apiError(409, "evaluation_required", "nothing counted", { reason: "nothing_counted", run: summary(finished), judged: summary(finished) }), run: null };
  }
  if (rate < gate.min_pass_rate) {
    return {
      refusal: apiError(409, "evaluation_required", "pass rate", { reason: "pass_rate", run: summary(finished), judged: summary(finished), pass_rate: rate, min_pass_rate: gate.min_pass_rate }),
      run: null,
    };
  }
  return { refusal: null, run: finished.id };
}

const SETS = "/agents/:id/evaluation-sets";
const SET = `${SETS}/:sid`;

export const agentEvaluationHandlers: HttpHandler[] = [
  http.get(SETS, async ({ request, params }) => {
    await record(request);
    return HttpResponse.json(evaluationState.sets.filter((s) => s.agent_id === params.id).map(setOut));
  }),
  http.post(SETS, async ({ request, params }) => {
    const r = await record(request);
    const body = r.body as EvaluationSetBody;
    const made = evaluationSetFixture({ id: `es-${evaluationState.sets.length + 1}`, agent_id: String(params.id), name: body.name, grader: body.grader ?? null, repeats: body.repeats ?? 1, created_by: evaluationState.me, updated_by: evaluationState.me });
    evaluationState.sets = [...evaluationState.sets, made];
    return HttpResponse.json(setOut(made), { status: 201 });
  }),
  http.put(SET, async ({ request, params }) => {
    const r = await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    const body = r.body as EvaluationSetBody;
    const next = { ...set, name: body.name, grader: body.grader ?? null, repeats: body.repeats ?? 1, updated_by: evaluationState.me };
    evaluationState.sets = evaluationState.sets.map((s) => (s.id === set.id ? next : s));
    return HttpResponse.json(setOut(next));
  }),
  http.delete(SET, async ({ request, params }) => {
    await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    evaluationState.sets = evaluationState.sets.filter((s) => s.id !== set.id);
    evaluationState.cases = evaluationState.cases.filter((c) => c.set_id !== set.id);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get(`${SET}/cases`, async ({ request, params }) => {
    await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    return HttpResponse.json({ items: evaluationState.cases.filter((c) => c.set_id === set.id), set_digest: setDigest(set.id) });
  }),
  http.post(`${SET}/cases/from-turn`, async ({ request, params }) => {
    const r = await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    const body = r.body as { session_id: string; turn_id: string };
    const rating = agentFeedbackState.items.find((f) => f.agent_id === params.id && f.session_id === body.session_id && f.turn_id === body.turn_id);
    if (!rating || rating.superseded) return apiError(404, "turn_not_found", "no rated turn");
    if (!rating.evaluation_use) return apiError(409, "evaluation_consent_missing", "the rating does not consent");
    const question = "제 번호는 [phone] 입니다. 환불해 주세요.";
    return HttpResponse.json({
      content: { messages: [question], reference: { text: "환불은 7일 안에 신청할 수 있습니다.", good: rating.rating === "up" }, criteria: [], checks: [], expect_done: true, tool_results: {} },
      origin: { rating_id: rating.id, session_id: rating.session_id, turn_id: rating.turn_id },
      replaced: { phone: 1 },
    });
  }),
  http.post(`${SET}/cases`, async ({ request, params }) => {
    const r = await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    const body = r.body as EvaluationCaseBody;
    const refused = caseRefusal(body, body.origin ?? null);
    if (refused) return refused;
    const content = storedContent(body.content);
    const made = evaluationCaseFixture({
      id: `ec-${evaluationState.cases.length + 1}`,
      agent_id: String(params.id),
      set_id: set.id,
      content,
      digest: JSON.stringify(content),
      origin: body.origin ?? null,
      pii_ack: body.pii_acknowledged ? { by: evaluationState.me, at: NOW } : null,
      created_by: evaluationState.me,
      updated_by: evaluationState.me,
    });
    evaluationState.cases = [...evaluationState.cases, made];
    return HttpResponse.json(made, { status: 201 });
  }),
  http.put(`${SET}/cases/:cid`, async ({ request, params }) => {
    const r = await record(request);
    const found = evaluationState.cases.find((c) => c.id === params.cid && c.set_id === params.sid);
    if (!found) return apiError(404, "not_found", "evaluation case not found");
    const body = r.body as EvaluationCaseBody;
    const refused = caseRefusal(body, found.origin);
    if (refused) return refused;
    const content = storedContent(body.content);
    const next = { ...found, content, digest: JSON.stringify(content), updated_by: evaluationState.me };
    evaluationState.cases = evaluationState.cases.map((c) => (c.id === found.id ? next : c));
    return HttpResponse.json(next);
  }),
  http.delete(`${SET}/cases/:cid`, async ({ request, params }) => {
    await record(request);
    evaluationState.cases = evaluationState.cases.filter((c) => c.id !== params.cid);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get(`${SET}/runs`, async ({ request, params }) => {
    await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    const runs = evaluationState.runs.map(view).filter((r) => r.set_id === set.id);
    const mine = runs.find((r) => r.started_by === evaluationState.me);
    // 시작한 적이 없으면 다른 사람이 쓰거나 고친 모든 사례를, 있으면 그 뒤에 바뀐 사례를 확인한다
    const cases = evaluationState.cases.filter((c) => c.set_id === set.id && (c.created_by !== evaluationState.me || c.updated_by !== evaluationState.me));
    const review = mine ? cases.filter((c) => !mine.verdicts.some((v) => v.case_id === c.id && v.case_digest === c.digest)) : cases;
    return HttpResponse.json({ items: [...runs].reverse().map(runOut), review: { set_digest: setDigest(set.id), since: mine ? mine.created_at : null, cases: review.map((c) => c.id) } });
  }),
  http.post(`${SET}/runs`, async ({ request, params }) => {
    const r = await record(request);
    const set = findSet(params.id, params.sid);
    if (set instanceof Response) return set;
    const body = r.body as { set_digest: string; revision?: number };
    if (body.set_digest !== setDigest(set.id)) return apiError(409, "evaluation_set_changed", "the set changed");
    if (evaluationState.runs.map(view).some((x) => ["queued", "starting", "running", "grading"].includes(x.state))) return apiError(409, "evaluation_run_active", "a run is active");
    const agent = agentStore.find(params.id);
    const revision = body.revision ?? agent?.revision ?? 2;
    const run = evaluationRunFixture({
      id: `er-${evaluationState.runs.length + 1}`,
      set_id: set.id,
      set_digest: setDigest(set.id),
      revision,
      spec_digest: digestOf({ id: String(params.id) }, revision),
      state: "queued",
      started_by: evaluationState.me,
      started_by_name: evaluationState.meName,
      cases: evaluationState.cases.filter((c) => c.set_id === set.id).length,
      repeats: set.repeats,
      done_steps: 0,
      verdicts: [],
      totals: { cases: 0, counted: 0, passed: 0, tokens: 0, cost_usd: null },
      ended_at: null,
      results: [],
      baseline: null,
    });
    evaluationState.runs = [...evaluationState.runs, run];
    evaluationState.finishAt[run.id] = Date.now() + evaluationState.finishAfterMs;
    return HttpResponse.json(runOut(run), { status: 202 });
  }),
  http.get(`${SET}/runs/:rid`, async ({ request, params }) => {
    await record(request);
    const found = evaluationState.runs.find((x) => x.id === params.rid && x.set_id === params.sid);
    if (!found) return apiError(404, "not_found", "evaluation run not found");
    const run = view(found);
    const mine = run.started_by === evaluationState.me;
    return HttpResponse.json({ ...run, results: mine ? run.results : [], content_hidden: !mine });
  }),
  http.delete(`${SET}/runs/:rid`, async ({ request, params }) => {
    await record(request);
    const found = evaluationState.runs.find((x) => x.id === params.rid);
    if (!found) return apiError(404, "not_found", "evaluation run not found");
    const run = view(found);
    if (!["queued", "starting", "running", "grading"].includes(run.state)) return apiError(409, "state_conflict", "the run has ended");
    evaluationState.runs = evaluationState.runs.map((x) => (x.id === run.id ? { ...run, state: "cancelled", cancelled_by: evaluationState.me, ended_at: NOW } : x));
    delete evaluationState.finishAt[run.id];
    return new HttpResponse(null, { status: 204 });
  }),
];

/** 게이트 하나. 기본은 세트 es-1 의 통과율 80%, 나빠진 사례는 보지 않는 7일이다. */
export function evaluationGateFixture(over: Partial<EvaluationGate> = {}): EvaluationGate {
  return { set_id: "es-1", min_pass_rate: 0.8, no_worse_cases: false, max_age_days: 7, updated_by: "dev@example.com", updated_at: NOW, ...over };
}
