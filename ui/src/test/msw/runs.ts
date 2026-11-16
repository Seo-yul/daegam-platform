/**
 * 자동 실행 (P41): /agents/{id}/runs·schedules·triggers 와 /me/runs-as-me. 모양은 platform/daegam/api/agent_runs.py 다.
 * 기본은 ag-support 의 일정 둘(사용 중, 다른 사람의 배포로 멈춤), 트리거 하나, 실행 기록 넷이고 나에게 묶인 소스는 그 가운데
 * header-admin 의 것이다. 자동 실행이 꺼진 플랫폼은 `runsState.enabled = false` 로 흉내 내고 모든 라우트가 404 다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { AgentRun, RunSchedule, RunTrigger } from "@/api/types";

import { NOW, apiError, http, record } from "./core";

const ME = { sub: "header-admin", username: "header-admin", kind: "person" as const, cause: null };
const DEV = { sub: "dev@example.com", username: "dev", kind: "person" as const, cause: null };
// 만료와 다음 실행은 먼 앞날이다. 지난 만료는 화면이 멈춘 것으로 본다
const LATER = "2030-01-02T00:00:00+00:00";

export function scheduleFixture(over: Partial<RunSchedule> = {}): RunSchedule {
  return {
    id: "sc-daily",
    agent_id: "ag-support",
    acts_as: ME,
    revision: 1,
    expires_at: LATER,
    expires_in_days: 90,
    paused: false,
    paused_reason: null,
    paused_cause: null,
    created_at: NOW,
    updated_at: NOW,
    cron: "0 9 * * 1-5",
    time_zone: "Asia/Seoul",
    message: "daily report",
    next_run_at: LATER,
    last_fired_at: null,
    last_run_id: null,
    ...over,
  };
}

export function triggerFixture(over: Partial<RunTrigger> = {}): RunTrigger {
  return {
    id: "tr-ci",
    agent_id: "ag-support",
    acts_as: ME,
    revision: 1,
    expires_at: LATER,
    expires_in_days: 90,
    paused: false,
    paused_reason: null,
    paused_cause: null,
    created_at: NOW,
    updated_at: NOW,
    name: "ci",
    template: "Build finished: {{payload./status}}",
    path: "/api/hooks/tr-ci",
    previous_until: null,
    ...over,
  };
}

export function runFixture(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "run-1",
    agent_id: "ag-support",
    source: "manual",
    source_id: null,
    scheduled_for: null,
    acts_as: ME,
    input_bytes: 12,
    input_sha256: null,
    session_id: "s-run-1",
    turn_id: "t-1",
    status: "succeeded",
    reason: null,
    attempts: 1,
    stop_requested_at: null,
    last_event_at: NOW,
    created_at: NOW,
    started_at: "2026-09-01T10:00:00+00:00",
    ended_at: "2026-09-01T10:01:30+00:00",
    deadline: null,
    count: null,
    first_at: null,
    last_at: null,
    usage: { input_tokens: 1000, output_tokens: 500 },
    cost_usd: 0.01,
    ...over,
  };
}

function runFixtures(): AgentRun[] {
  return [
    runFixture(),
    // 다른 사람에게 묶여 승인을 기다리는 일정 실행. 대화 보기와 중단은 묶인 사람과 승인자만이다
    runFixture({
      id: "run-2",
      source: "schedule",
      source_id: "sc-daily",
      acts_as: DEV,
      session_id: "s-run-2",
      status: "waiting_approval",
      ended_at: null,
      deadline: LATER,
      usage: null,
      cost_usd: null,
    }),
    runFixture({
      id: "run-3",
      source: "schedule",
      source_id: "sc-daily",
      session_id: null,
      turn_id: null,
      status: "skipped",
      reason: "previous_running",
      started_at: null,
      ended_at: null,
      count: 3,
      first_at: NOW,
      last_at: NOW,
      usage: null,
      cost_usd: null,
    }),
    runFixture({
      id: "run-4",
      source: "webhook",
      source_id: "tr-ci",
      acts_as: { sub: "header-admin", username: null, kind: "agent", cause: "oidc_mode" },
      session_id: null,
      turn_id: null,
      status: "failed",
      reason: "max_input_chars",
      usage: null,
      cost_usd: null,
    }),
  ];
}

export const runsState: { enabled: boolean; schedules: RunSchedule[]; triggers: RunTrigger[]; runs: AgentRun[] } = {
  enabled: true,
  schedules: [],
  triggers: [],
  runs: [],
};

export function resetRunsState(): void {
  runsState.enabled = true;
  runsState.schedules = [
    scheduleFixture(),
    scheduleFixture({ id: "sc-weekly", cron: "0 8 * * 1", acts_as: DEV, paused: true, paused_reason: "agent_changed" }),
  ];
  runsState.triggers = [triggerFixture()];
  runsState.runs = runFixtures();
}
resetRunsState();

const SECRET = "whsec_dGVzdC1zZWNyZXQtdmFsdWUtMzItYnl0ZXMtbG9uZw==";

function off(): Response | null {
  return runsState.enabled ? null : apiError(404, "not_found", "background runs are not enabled");
}

function findIn<T extends { id: string }>(items: T[], id: unknown, noun: string): T | Response {
  return items.find((x) => x.id === String(id)) ?? apiError(404, "not_found", `${noun} '${String(id)}' not found`);
}

function bound<T extends { acts_as: { sub: string } }>(items: T[]): (T & { tenant: string; agent_name: string })[] {
  return items.filter((x) => x.acts_as.sub === ME.sub).map((x) => ({ ...x, tenant: "default", agent_name: "support" }));
}

export const runsHandlers: HttpHandler[] = [
  http.get("/agents/:id/runs", async ({ request }) => {
    await record(request);
    return off() ?? HttpResponse.json({ runs: runsState.runs });
  }),
  http.post("/agents/:id/runs", async ({ request, params }) => {
    const r = await record(request);
    const closed = off();
    if (closed) return closed;
    const message = (r.body as { message?: string }).message ?? "";
    if (message === "limit") return apiError(429, "agent_run_limit", "this agent is at its run limit");
    const run = runFixture({ id: `run-${runsState.runs.length + 1}`, agent_id: String(params.id), status: "starting", session_id: null, started_at: null, ended_at: null, usage: null, cost_usd: null });
    runsState.runs = [run, ...runsState.runs];
    return HttpResponse.json(run, { status: 202 });
  }),
  http.post("/agents/:id/runs/:runId/cancel", async ({ request, params }) => {
    await record(request);
    const run = off() ?? findIn(runsState.runs, params.runId, "run");
    if (run instanceof Response) return run;
    run.status = "cancelled";
    run.reason = "stopped";
    return HttpResponse.json(run, { status: 202 });
  }),
  http.get("/agents/:id/schedules", async ({ request }) => {
    await record(request);
    return off() ?? HttpResponse.json({ schedules: runsState.schedules });
  }),
  http.post("/agents/:id/schedules", async ({ request }) => {
    const r = await record(request);
    const closed = off();
    if (closed) return closed;
    const body = r.body as { cron: string; time_zone: string; message: string; expires_in_days: number };
    if (body.cron === "* * * * *") return apiError(422, "schedule_too_frequent", "two firings are closer than 5 minutes");
    const schedule = scheduleFixture({ id: `sc-${runsState.schedules.length + 1}`, ...body });
    runsState.schedules = [...runsState.schedules, schedule];
    return HttpResponse.json(schedule, { status: 201 });
  }),
  http.patch("/agents/:id/schedules/:sid", async ({ request, params }) => {
    const r = await record(request);
    const schedule = off() ?? findIn(runsState.schedules, params.sid, "schedule");
    if (schedule instanceof Response) return schedule;
    Object.assign(schedule, r.body as object, { acts_as: ME });
    return HttpResponse.json(schedule);
  }),
  http.delete("/agents/:id/schedules/:sid", async ({ request, params }) => {
    await record(request);
    const schedule = off() ?? findIn(runsState.schedules, params.sid, "schedule");
    if (schedule instanceof Response) return schedule;
    runsState.schedules = runsState.schedules.filter((s) => s !== schedule);
    return new HttpResponse(null, { status: 204 });
  }),
  ...(["pause", "resume"] as const).map((action) =>
    http.post(`/agents/:id/schedules/:sid/${action}`, async ({ request, params }) => {
      await record(request);
      const schedule = off() ?? findIn(runsState.schedules, params.sid, "schedule");
      if (schedule instanceof Response) return schedule;
      Object.assign(schedule, action === "pause" ? { paused: true, paused_reason: "paused" } : { paused: false, paused_reason: null, acts_as: ME });
      return HttpResponse.json(schedule);
    }),
  ),
  http.get("/agents/:id/triggers", async ({ request }) => {
    await record(request);
    return off() ?? HttpResponse.json({ triggers: runsState.triggers });
  }),
  http.post("/agents/:id/triggers", async ({ request }) => {
    const r = await record(request);
    const closed = off();
    if (closed) return closed;
    const id = `tr-${runsState.triggers.length + 1}`;
    const trigger = triggerFixture({ id, path: `/api/hooks/${id}`, ...(r.body as object) });
    runsState.triggers = [...runsState.triggers, trigger];
    return HttpResponse.json({ ...trigger, secret: SECRET }, { status: 201 });
  }),
  http.patch("/agents/:id/triggers/:tid", async ({ request, params }) => {
    const r = await record(request);
    const trigger = off() ?? findIn(runsState.triggers, params.tid, "trigger");
    if (trigger instanceof Response) return trigger;
    Object.assign(trigger, r.body as object);
    return HttpResponse.json(trigger);
  }),
  http.delete("/agents/:id/triggers/:tid", async ({ request, params }) => {
    await record(request);
    const trigger = off() ?? findIn(runsState.triggers, params.tid, "trigger");
    if (trigger instanceof Response) return trigger;
    runsState.triggers = runsState.triggers.filter((x) => x !== trigger);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("/agents/:id/triggers/:tid/rotate-secret", async ({ request, params }) => {
    await record(request);
    const trigger = off() ?? findIn(runsState.triggers, params.tid, "trigger");
    if (trigger instanceof Response) return trigger;
    return HttpResponse.json({ ...trigger, previous_until: LATER, secret: `${SECRET}-rotated` });
  }),
  ...(["pause", "resume"] as const).map((action) =>
    http.post(`/agents/:id/triggers/:tid/${action}`, async ({ request, params }) => {
      await record(request);
      const trigger = off() ?? findIn(runsState.triggers, params.tid, "trigger");
      if (trigger instanceof Response) return trigger;
      Object.assign(trigger, action === "pause" ? { paused: true, paused_reason: "paused" } : { paused: false, paused_reason: null, acts_as: ME });
      return HttpResponse.json(trigger);
    }),
  ),
  http.get("/me/runs-as-me", async ({ request }) => {
    await record(request);
    return off() ?? HttpResponse.json({ schedules: bound(runsState.schedules), triggers: bound(runsState.triggers) });
  }),
  ...(["schedules", "triggers"] as const).map((kind) =>
    http.post(`/me/runs-as-me/${kind}/:id/pause`, async ({ request, params }) => {
      await record(request);
      const closed = off();
      if (closed) return closed;
      const items: (RunSchedule | RunTrigger)[] = runsState[kind];
      const source = items.find((x) => x.id === String(params.id) && x.acts_as.sub === ME.sub);
      if (!source) return apiError(404, "not_found", `${kind} '${String(params.id)}' not found`);
      Object.assign(source, { paused: true, paused_reason: "paused" });
      return HttpResponse.json({ ...source, tenant: "default", agent_name: "support" });
    }),
  ),
];
