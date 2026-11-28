/**
 * Agent 픽스처와 핸들러 (D26, D42). 정의와 배포가 한 리소스다.
 *   /agents (templates·validate·summary·revisions·deploy·redeploy·rollback·evaluation-gate·undeploy·publish·clone·manifest·overrides·drift).
 * 허브 게시(P46 6.1절)는 hub.ts 의 `hubState` 에 `agent` 항목을 더하고 뺀다.
 * 배포, 재배포, 되돌리기는 배포 기록 줄을 더하고 배포 게이트(agent-evaluation.ts `gateRefusal`)를 지난다 (P45 6절, 12절).
 * 채팅·세션·승인·메트릭·이벤트 프록시는 agent-chat.ts, 그 SSE 대본과 런타임 세션 색인은 agent-sse.ts, 템플릿과
 * 검증 문맥은 agent-templates.ts 에 있다.
 *
 * validate 는 클라이언트 검증(lib/agentGraph.ts)을 목 문맥으로 돌린다. 서버와 같은 규칙·코드이므로 화면이
 * 서버 응답으로 표식을 다는 경로를 그대로 지나간다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { Agent, AgentExposureInput, AgentIssue, AgentRevision, AgentSleep, DeployRecord, EvaluationGateBody, ManifestDocument } from "@/api/types";
import { dictionary } from "@/i18n";
import { canonicalJson, graphRemembers, issueText, toApiGraph, toServiceSettings, validateGraph, type Graph, type Issue } from "@/lib/agentGraph";
import { hasRuntimeFeature } from "@/lib/runtimeFeatures";

import { gateRefusal } from "./agent-evaluation";
import { agentSessionState, resetAgentSessionState } from "./agent-sse";
import { agentTemplateSupport, filledTemplateGraph, mockValidationContext } from "./agent-templates";
import { NOW, apiError, docStore, http, record } from "./core";
import { agentExposureOf } from "./exposure";
import { hubAgentFixture, hubState } from "./hub";

/** 배포까지 끝난 Agent. 대부분의 테스트가 이 모양에서 출발한다. */
export function agentFixture(over: Partial<Agent> = {}): Agent {
  return {
    id: "ag-support",
    tenant: "default",
    name: "support",
    display_name: "상담 에이전트",
    state: "deployed",
    revision: 2,
    deployed_revision: 2,
    graph: toApiGraph(filledTemplateGraph()),
    service: { budget: { session_tokens: 200000, daily_tokens: null }, observability: { record_bodies: false }, evaluation: null },
    layout: { ...agentTemplateSupport.layout },
    revisions: [1, 2],
    cluster_id: "cl-home",
    k8s: { name: "agent-support", namespace: "daegam-default", token_secret: "agent-support-token" },
    replicas: 1,
    resources: { limits: { cpu: "500m", memory: "512Mi" } },
    scale_to_zero: true,
    bindings: [{ data_service_id: "ds-pg", env_prefix: "DAEGAM_DB_0" }],
    token_id: "atok-1",
    llm_secrets: ["agent-support-llm-openai-main"],
    llm_route: { route: "direct", reason: "gateway_off" },
    exposure: { enabled: false, host: null, ingress_class: null, tls: { mode: "none", secret_name: null, issuer: null }, cors_origins: [], allowed_groups: [] },
    k8s_uid: null,
    svc_token_rev: "0",
    effective_config: { namespace: { name: "daegam-default" }, images: { agent_image: "daegam-agent-runtime:0.1.0" }, replicas: 1, bindings: [{ data_service_id: "ds-pg", env_prefix: "DAEGAM_DB_0" }] },
    overrides: {},
    // status() 가 값이 바뀔 때만 적어 두는 마지막 phase. 가벼운 고르개 목록(P18 F2)이 이것만 읽는다
    last_phase: "Ready",
    last_error: null,
    last_spec_change: NOW,
    // 배포 기록 (P45 6절). 이 목의 r1 배포와 r2 재배포다
    deploy_history: [
      { id: "dh-1", action: "deploy", revision: 1, restored_from: null, spec_digest: "v1:r1", image: "daegam-agent-runtime:0.1.0", at: NOW, by: "header", evaluation_run: null, skipped_reason: null, outcome: "deployed" },
      { id: "dh-2", action: "redeploy", revision: 2, restored_from: null, spec_digest: "v1:r2", image: "daegam-agent-runtime:0.1.0", at: NOW, by: "header", evaluation_run: null, skipped_reason: null, outcome: "deployed" },
    ],
    // 배포 게이트 (P45 12절). 꺼져 있다
    evaluation_gate: null,
    status: {
      phase: "Ready",
      active_sessions: 2,
      pending_approvals: 1,
      chat_url: "/api/agents/ag-support/chat",
      message: null,
      conditions: [],
      ready_replicas: 1,
      warnings: [],
      observed_at: NOW,
      since: NOW,
      error: null,
      // 플랫폼 설정이 정한 기록 보존 기간 (D46). 화면의 안내문이 세션 TTL 과 함께 읽는다
      transcript_retention_days: 30,
      // 핀된 리비전 입구의 인사말 (D65). 첫 말 전의 말풍선이고 새 세션의 첫 SSE 이벤트가 같은 글이다
      greeting: "안녕하세요. 무엇을 도와드릴까요?",
      // 핀된 리비전의 채팅 사실 (P36). 기억 서랍 단추와 인용 창의 원문 다운로드가 읽는다
      long_term_memory: false,
      sources_masked: false,
      // 쉬는 상태 (P44 10절). 깨어 있고 잠들 수 있으며 설치의 쉬는 창은 30분이다
      sleep: sleepOf(),
    },
    created_at: NOW,
    updated_at: NOW,
    updated_by: null,
    ...over,
  };
}

/**
 * 상태의 쉬는 상태 (P44 10절). 기본은 깨어 있고 잠들 수 있으며 쉬는 창이 30분인 설치다. 까닭(ineligible)만 주면 서버처럼 그 하나를
 * 까닭 목록(ineligible_all)에도 둔다.
 */
export function sleepOf(over: Partial<AgentSleep> = {}): AgentSleep {
  const all = over.ineligible_all ?? (over.ineligible ? [over.ineligible] : []);
  return { state: "awake", since: null, reason: null, ineligible: null, window_seconds: 1800, ...over, ineligible_all: all };
}

/** 상태의 sleep 만 바꾼 배포 Agent. 기본은 agentFixture 다. */
export function withSleep(over: Partial<AgentSleep>, agent: Agent = agentFixture()): Agent {
  return { ...agent, status: { ...agent.status!, sleep: sleepOf(over) } };
}

/**
 * 쓰지 않아 0 으로 줄인 Agent (P44 10절). base 의 배포 위에 상태만 바꾼다. 상태는 런타임을 읽지 않고 잠들 때 남긴 세션·보류 수를
 * 보인다. phase 가 Waking 이면 다시 띄우는 중이다.
 */
export function asleepAgentFixture(base: Agent = agentFixture(), phase: "Sleeping" | "Waking" = "Sleeping"): Agent {
  const state = phase === "Sleeping" ? "asleep" : "waking";
  const sleep = sleepOf({ state, since: NOW, reason: phase === "Sleeping" ? "idle" : "wake" });
  return { ...base, status: { ...base.status!, phase, ready_replicas: 0, active_sessions: 3, pending_approvals: 2, sleep } };
}

/**
 * 런타임이 이 능력들을 알린 배포 Agent 다(D65). 기본 픽스처는 기록이 없다(첫 배포, 옛 이미지). 이름은 lib/runtimeFeatures.ts 의
 * `RUNTIME_*` 상수로 준다.
 */
export function agentWithRuntimeFeatures(...names: string[]): Agent {
  const base = agentFixture();
  return agentFixture({ status: { ...base.status!, runtime: { image: "daegam-agent-runtime:0.1.0", features: names } } });
}

/** 메인 에이전트에 Skill 을 붙인 배포된 Agent (D64). 목의 sk-report 는 r2 가 최신이다. */
export function agentWithSkill(pin: { id: string; revision: number }): Agent {
  const agent = agentFixture();
  const main = agent.graph.nodes.find((n) => n.id === "agent-main")!;
  main.data = { ...main.data, skills: [pin] };
  return agent;
}

/** 그리기만 하고 아직 안 올린 Agent. 배포 쪽이 비어 있다 (D42). */
export function agentDraftFixture(over: Partial<Agent> = {}): Agent {
  return agentFixture({
    id: "ag-draft",
    name: "draft",
    display_name: "draft",
    state: "draft",
    deployed_revision: null,
    cluster_id: null,
    k8s: null,
    effective_config: null,
    token_id: null,
    llm_secrets: [],
    last_phase: null,
    status: null,
    ...over,
  });
}

export const agentState: { items: Agent[] } = { items: [agentFixture()] };

/** 업로드·기억·세션 파일·피드백 목도 이것으로 Agent 를 찾는다. */
export const agentStore = docStore(agentState, "agent");

/** 되돌리기가 만든 리비전의 내용 지문. `${Agent id}:${리비전}` 마다 가져온 리비전의 지문이다. 없으면 `v1:r{n}` 이다. */
const restoredDigests = new Map<string, string>();

/** 리비전 내용의 지문. 목은 리비전마다 다른 내용으로 보고, 되돌리기가 만든 리비전만 가져온 리비전과 같은 내용이다. */
export function digestOf(a: Pick<Agent, "id">, n: number): string {
  return restoredDigests.get(`${a.id}:${n}`) ?? `v1:r${n}`;
}

function revisionOf(a: Agent, n: number): AgentRevision {
  // 목은 리비전마다 그래프를 따로 두지 않는다. r1 은 도구 노드가 없던 초안으로 보인다
  const graph = n === a.revision ? a.graph : { nodes: a.graph.nodes.filter((x) => x.type !== "tools"), edges: a.graph.edges.filter((e) => e.kind !== "tools") };
  return { revision: n, created_at: NOW, graph, service: a.service, spec_digest: digestOf(a, n) };
}

/** 배포 기록 줄 하나. 목의 배포는 바로 끝난 것으로 본다. */
function historyRow(a: Agent, action: string, revision: number, extra: Partial<DeployRecord> = {}): DeployRecord {
  return {
    id: `dh-${a.deploy_history.length + 1}`,
    action,
    revision,
    restored_from: null,
    spec_digest: digestOf(a, revision),
    image: "daegam-agent-runtime:0.1.0",
    at: NOW,
    by: "header-admin",
    evaluation_run: null,
    skipped_reason: null,
    outcome: "deployed",
    ...extra,
  };
}

/** 본문의 `skip_evaluation` 까닭. */
function skipOf(body: unknown): string | null {
  return ((body as { skip_evaluation?: { reason?: string } | null } | null)?.skip_evaluation?.reason ?? null) || null;
}

/** 핀할 리비전에 게이트를 판정하고, 지나면 기록 줄에 남길 칸(받아들인 실행이나 건너뛴 까닭)을 준다. */
function judged(a: Agent, revision: number, body: unknown): { refusal: Response | null; extra: Partial<DeployRecord> } {
  const skip = skipOf(body);
  const deployed = a.deployed_revision !== null ? digestOf(a, a.deployed_revision) : null;
  const { refusal, run } = gateRefusal(a, digestOf(a, revision), deployed, skip !== null);
  if (refusal) return { refusal, extra: {} };
  const applied = a.evaluation_gate && digestOf(a, revision) !== deployed && !a.deploy_history.some((r) => r.spec_digest === digestOf(a, revision) && r.outcome === "deployed");
  return { refusal: null, extra: applied ? (skip ? { skipped_reason: skip } : { evaluation_run: run }) : {} };
}

/**
 * 서버가 보내는 꼴의 이슈. 서버처럼 상세 없이 code 와 문장 하나(message)를 싣는다. 서버 문장은 영어지만 목은 캔버스가
 * 그리는 한국어 문장을 싣는다. 화면은 code 로 문장을 찾으므로 message 는 표에 없는 code 와 배포 경고 줄에서만 쓰인다.
 */
function serverIssue(issue: Issue): AgentIssue {
  return { code: issue.code, level: issue.level, node_id: issue.node_id, edge_id: issue.edge_id, message: issueText(dictionary("ko"), issue) };
}

function validateBody(body: { graph?: unknown; service?: unknown }) {
  const graph = (body.graph ?? { nodes: [], edges: [] }) as Graph;
  const issues = validateGraph(graph, toServiceSettings(body.service as Agent["service"] | undefined), mockValidationContext()).map(serverIssue);
  return { issues, ok: !issues.some((i) => i.level === "error") };
}

/** 노출을 켠 채 Ready 인 Agent. 상세의 외부 호출 카드·재배포 대화상자 테스트가 쓴다. */
export function exposedAgentFixture(over: Partial<Agent> = {}): Agent {
  const base = agentFixture();
  return agentFixture({
    exposure: { enabled: true, host: "support.default.apps.local", ingress_class: "traefik", tls: { mode: "secret", secret_name: "calc-tls", issuer: null }, cors_origins: ["https://app.example.com"], allowed_groups: [] },
    status: { ...base.status!, external_url: "https://support.default.apps.local", exposure: { front_phase: null, address_assigned: true, tls_secret_present: true, certificate_ready: null } },
    ...over,
  });
}

const deploymentCR = {
  apiVersion: "apps/v1",
  kind: "Deployment",
  metadata: { name: "agent-support", namespace: "daegam-default", labels: { "daegam.devops.ai.kr/tenant": "default" } },
  spec: { replicas: 1, template: { spec: { containers: [{ name: "agent", image: "daegam-agent-runtime:0.1.0" }] } } },
};

export const agentManifestDocs: ManifestDocument[] = [
  { kind: "Deployment", name: "agent-support", namespace: "daegam-default", rendered: deploymentCR, overrides: {}, final: deploymentCR, live: deploymentCR, drift: [], drift_patch: {}, drift_managers: [], locked: ["spec.template.spec.containers[name=agent].image", "metadata.name"], ignored_by_operator: [], error: null },
  { kind: "Service", name: "agent-support", namespace: "daegam-default", rendered: { kind: "Service" }, overrides: {}, final: { kind: "Service" }, live: null, drift: null, drift_patch: null, drift_managers: [], locked: ["*"], ignored_by_operator: [], error: null },
];

export function resetAgentState(): void {
  agentState.items = [agentFixture()];
  restoredDigests.clear();
  resetAgentSessionState();
}

// -- 핸들러 --------------------------------------------------------------------------------------

/** 기록에 그 런타임 능력이 없으면(없는 Agent 도) 플랫폼 프록시처럼 409 runtime_feature_missing 이다. 있으면 null. */
export function featureMissing(agent: Agent | null, name: string): Response | null {
  if (hasRuntimeFeature(agent?.status, name)) return null;
  return apiError(409, "runtime_feature_missing", `the deployed runtime image has not reported the '${name}' capability`, { feature: name });
}

export const agentHandlers: HttpHandler[] = [
  // 고정 경로는 `/:id` 보다 먼저 와야 id 로 읽히지 않는다
  http.get("/agents/templates", async ({ request }) => {
    await record(request);
    return HttpResponse.json([agentTemplateSupport]);
  }),
  http.post("/agents/validate", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json(validateBody(r.body as Record<string, unknown>));
  }),
  // 홈 챗의 고르개가 쓰는 가벼운 목록 (D35 F2). 상태 왕복이 없어 phase 는 마지막으로 알려진 값이다
  http.get("/agents/summary", async ({ request }) => {
    await record(request);
    return HttpResponse.json(agentState.items.map((a) => ({ id: a.id, name: a.name, display_name: a.display_name, phase: a.status?.phase ?? a.last_phase ?? null })));
  }),
  http.get("/agents", async ({ request }) => {
    await record(request);
    return HttpResponse.json(agentState.items);
  }),
  // 만들기는 저장이다. 올리는 것은 배포가 따로 한다 (D42)
  http.post("/agents", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { name: string; display_name?: string | null; graph?: Agent["graph"]; service?: Agent["service"]; layout?: Agent["layout"] };
    if (agentState.items.some((a) => a.name === body.name)) return apiError(409, "name_conflict", `agent '${body.name}' already exists`);
    const { issues, ok } = validateBody(body);
    if (!ok) {
      return apiError(422, "invalid_graph", `graph has ${issues.filter((i) => i.level === "error").length} error(s)`, { issues });
    }
    const made = agentDraftFixture({
      id: "ag-new",
      name: body.name,
      display_name: body.display_name || body.name,
      revision: 1,
      revisions: [1],
      graph: body.graph ?? { nodes: [], edges: [] },
      service: toServiceSettings(body.service) as Agent["service"],
      layout: body.layout ?? {},
    });
    agentState.items = [...agentState.items, made];
    return HttpResponse.json({ ...made, issues, revision_created: true }, { status: 201 });
  }),
  http.get("/agents/:id", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    return a instanceof Response ? a : HttpResponse.json(a);
  }),
  http.put("/agents/:id", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const body = r.body as { name: string; display_name?: string | null; graph?: Agent["graph"]; service?: Agent["service"]; layout?: Agent["layout"]; expect_revision?: number | null };
    if (body.name !== a.name) return apiError(422, "name_immutable", "name cannot be changed after creation");
    // 본 리비전이 최신일 때만 저장한다 (P48 6.7절)
    if (body.expect_revision != null && body.expect_revision !== a.revision) return apiError(409, "revision_conflict", "another revision was saved first", { revision: a.revision });
    const nextService = toServiceSettings(body.service) as Agent["service"];
    const changed = canonicalJson({ graph: body.graph, service: nextService }) !== canonicalJson({ graph: a.graph, service: toServiceSettings(a.service) });
    let issues: ReturnType<typeof validateBody>["issues"] = [];
    if (changed) {
      const v = validateBody(body);
      issues = v.issues;
      if (!v.ok) return apiError(422, "invalid_graph", `graph has ${issues.filter((i) => i.level === "error").length} error(s)`, { issues });
      a.graph = body.graph ?? { nodes: [], edges: [] };
      a.service = nextService;
      a.revision += 1;
      a.revisions = [...a.revisions, a.revision];
    }
    a.display_name = body.display_name || a.display_name;
    a.layout = body.layout ?? {};
    return HttpResponse.json({ ...a, issues, revision_created: changed });
  }),
  http.delete("/agents/:id", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    agentStore.remove(a.id);
    return HttpResponse.json({ id: a.id, deleted: true, deleted_at: NOW, revoked_tokens: a.token_id ? 1 : 0 });
  }),
  http.get("/agents/:id/revisions", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    return HttpResponse.json(a.revisions.map((n) => revisionOf(a, n)));
  }),
  http.get("/agents/:id/revisions/:n", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const n = Number(params.n);
    if (!a.revisions.includes(n)) return apiError(404, "not_found", `revision ${n} not found`);
    return HttpResponse.json(revisionOf(a, n));
  }),

  // -- 배포 동작 (D42) ---------------------------------------------------------------
  http.post("/agents/:id/deploy", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    if (a.state !== "draft") return apiError(409, "state_conflict", `agent is ${a.state}, not draft`);
    const body = (r.body ?? {}) as { cluster_id?: string | null; namespace?: string | null; replicas?: number; resources?: Record<string, unknown> | null; exposure?: AgentExposureInput | null };
    if (a.name === "taken")
      return apiError(409, "k8s_name_conflict", "agent-taken is already used in daegam-default", { k8s_name: "agent-taken", namespace: "daegam-default" });
    const { issues, ok } = validateBody({ graph: a.graph, service: a.service });
    if (!ok) return apiError(422, "invalid_graph", "graph has errors", { issues });
    const ex = agentExposureOf(body.exposure, a.name, false);
    if ("error" in ex) return ex.error;
    const gate = judged(a, a.revision, r.body);
    if (gate.refusal) return gate.refusal;
    const base = agentFixture();
    const next = agentStore.replace({
      ...a,
      deploy_history: [...a.deploy_history, historyRow(a, "deploy", a.revision, gate.extra)],
      state: "deploying",
      deployed_revision: a.revision,
      cluster_id: body.cluster_id ?? "cl-home",
      k8s: { name: `agent-${a.name}`, namespace: body.namespace ?? "daegam-default", token_secret: `agent-${a.name}-token` },
      replicas: body.replicas ?? 1,
      resources: body.resources ?? null,
      token_id: "atok-new",
      llm_secrets: base.llm_secrets,
      effective_config: base.effective_config,
      exposure: ex.exposure,
      status: {
        phase: "Pending",
        active_sessions: null,
        pending_approvals: null,
        chat_url: null,
        external_url: ex.external_url,
        exposure: ex.exposure_status,
        message: "Deployment not yet observed",
        conditions: [],
        warnings: [],
        observed_at: NOW,
        since: NOW,
        error: null,
        greeting: (a.graph.nodes.find((n) => n.type === "entry")?.data as { greeting?: string | null } | undefined)?.greeting ?? null,
        long_term_memory: graphRemembers(a.graph),
        sources_masked: false,
      },
    });
    return HttpResponse.json({ ...next, warnings: [...issues.map((i) => `${i.code}: ${i.message}`), "network_policy_unenforced"] });
  }),
  // 본문 exposure 가 있으면 노출 설정을 바꿔 재배포하고, 없으면 저장된 스냅샷을 유지한다 (D27)
  http.post("/agents/:id/redeploy", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const body = (r.body ?? {}) as { exposure?: AgentExposureInput | null };
    const gate = judged(a, a.revision, r.body);
    if (gate.refusal) return gate.refusal;
    let next = { ...a, deploy_history: [...a.deploy_history, historyRow(a, "redeploy", a.revision, gate.extra)] };
    if (body.exposure) {
      const ex = agentExposureOf(body.exposure, a.name, true);
      if ("error" in ex) return ex.error;
      next = { ...next, exposure: ex.exposure, status: { ...next.status!, external_url: ex.external_url, exposure: ex.exposure_status } };
    }
    const pending = agentSessionState.items.filter((s) => s.status === "pending").length;
    // 코드는 서버가 실제로 내는 것과 같아야 한다 (services/agents.py 의 redeploy)
    const warnings = pending ? [`pending_sessions_revision_changed: ${pending} session(s) await approval on revision 1; they resume on revision 2`] : [];
    next = { ...next, deployed_revision: a.revision, status: { ...next.status!, phase: "Rolling", warnings } };
    return HttpResponse.json({ ...agentStore.replace(next), warnings });
  }),
  // 되돌리기 (P45 6절). 그 리비전의 내용을 새 리비전으로 저장하고 재배포한다. 배포된 내용과 같으면 409 다
  http.post("/agents/:id/rollback", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const { revision } = r.body as { revision: number };
    if (!a.revisions.includes(revision)) return apiError(404, "not_found", `revision ${revision} not found`);
    const digest = digestOf(a, revision);
    if (a.deployed_revision !== null && digest === digestOf(a, a.deployed_revision)) return apiError(409, "nothing_to_roll_back", "the revision has the deployed content");
    const gate = gateRefusal(a, digest, a.deployed_revision !== null ? digestOf(a, a.deployed_revision) : null, skipOf(r.body) !== null);
    if (gate.refusal) return gate.refusal;
    const made = a.revision + 1;
    restoredDigests.set(`${a.id}:${made}`, digest);
    const extra = a.evaluation_gate && !a.deploy_history.some((x) => x.spec_digest === digest && x.outcome === "deployed") ? (skipOf(r.body) ? { skipped_reason: skipOf(r.body) } : { evaluation_run: gate.run }) : {};
    const row = historyRow(a, "rollback", made, { restored_from: revision, ...extra });
    const next = agentStore.replace({ ...a, revision: made, revisions: [...a.revisions, made], deployed_revision: made, deploy_history: [...a.deploy_history, row] });
    return HttpResponse.json({ ...next, warnings: [] });
  }),
  // 배포 게이트 (P45 12절). 본문이 null 이면 끈다
  http.put("/agents/:id/evaluation-gate", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const body = r.body as EvaluationGateBody | null;
    const gate = body ? { ...body, no_worse_cases: body.no_worse_cases ?? false, max_age_days: body.max_age_days ?? 7, updated_by: "header-admin", updated_at: NOW } : null;
    agentStore.replace({ ...a, evaluation_gate: gate });
    return HttpResponse.json({ gate });
  }),
  http.post("/agents/:id/undeploy", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    return HttpResponse.json(agentStore.replace({ ...a, state: "draft", deployed_revision: null, cluster_id: null, k8s: null, effective_config: null, token_id: null, llm_secrets: [], status: null }));
  }),
  // 허브 게시와 해제 (P46 6.1절). 이미 게시돼 있으면 200 으로 그 항목이고, 게시돼 있지 않은 것의 해제는 404 다
  http.post("/agents/:id/publish", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const existing = hubState.items.find((i) => i.source.agent_id === a.id);
    if (existing) return HttpResponse.json(existing);
    const body = r.body as { name?: string | null; display_name?: string; description: string; calls_trusted?: boolean };
    const name = body.name ?? a.name;
    const item = hubAgentFixture({ id: `hub-${name}`, name, display_name: body.display_name ?? a.display_name, description: body.description, calls_trusted: body.calls_trusted ?? false });
    item.source = { ...item.source, agent_id: a.id };
    hubState.items = [...hubState.items, item];
    return HttpResponse.json(item, { status: 201 });
  }),
  http.delete("/agents/:id/publish", async ({ request, params }) => {
    await record(request);
    const item = hubState.items.find((i) => i.source.agent_id === params.id);
    if (!item) return apiError(404, "not_published", `agent ${String(params.id)} is not published`);
    hubState.items = hubState.items.filter((i) => i !== item);
    return HttpResponse.json({ id: item.id, deleted: true });
  }),
  // 쓰지 않는 Agent 를 0 으로 줄이기 (P44 7절, 8절). 지금 재우면 Sleeping 이고, 깨우면 Waking 이다(깨어 있으면 그대로다)
  http.post("/agents/:id/sleep", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const sleep = a.status?.sleep ?? sleepOf();
    if (sleep.ineligible) return apiError(409, "agent_sleep_ineligible", `agent cannot sleep: ${sleep.ineligible}`, { ineligible: sleep.ineligible });
    // 세션·보류 수는 잠들 때의 값 그대로 남는다
    return HttpResponse.json(agentStore.replace({ ...a, status: { ...a.status!, phase: "Sleeping", ready_replicas: 0, sleep: { ...sleep, state: "asleep", since: NOW, reason: "request" } } }));
  }),
  http.post("/agents/:id/wake", async ({ request, params }) => {
    await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    if (a.status?.phase !== "Sleeping") return HttpResponse.json(a, { status: 202 });
    const sleep = a.status.sleep ?? sleepOf();
    // 서버처럼 깨우기 시작한 때가 since 다. 화면의 진행 배너가 이때부터 잰다
    const since = new Date().toISOString();
    return HttpResponse.json(agentStore.replace({ ...a, status: { ...a.status, phase: "Waking", sleep: { ...sleep, state: "waking", since, reason: "wake" } } }), { status: 202 });
  }),
  http.post("/agents/:id/clone", async ({ request, params }) => {
    const r = await record(request);
    const a = agentStore.findOr404(params.id);
    if (a instanceof Response) return a;
    const body = r.body as { name: string; display_name?: string | null };
    if (agentState.items.some((x) => x.name === body.name)) return apiError(409, "name_conflict", `agent '${body.name}' already exists`);
    const made = agentDraftFixture({ id: "ag-clone", name: body.name, display_name: body.display_name || body.name, graph: a.graph, service: a.service, layout: a.layout, revision: 1, revisions: [1] });
    agentState.items = [...agentState.items, made];
    return HttpResponse.json(made, { status: 201 });
  }),

  // -- Advanced (MCP Server 와 같은 문서 묶음 계약)
  http.get("/agents/:id/manifest", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ documents: agentManifestDocs });
  }),
  http.put("/agents/:id/overrides", async ({ request }) => {
    const r = await record(request);
    const text = (r.body as { yaml?: string }).yaml ?? "";
    const m = /replicas: (\d+)/.exec(text);
    const replicas = m ? Number(m[1]) : 1;
    const final = { ...deploymentCR, spec: { ...deploymentCR.spec, replicas } };
    return HttpResponse.json({ overrides: { spec: { replicas } }, final, diff: ["spec.replicas"], validation: "dry_run" });
  }),
  http.post("/agents/:id/drift/:action", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ documents: agentManifestDocs });
  }),
];
