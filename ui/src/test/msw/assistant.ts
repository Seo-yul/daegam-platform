/**
 * 플랫폼 비서 (D31, phase-14) 픽스처와 핸들러: /assistant/config, /assistant/sessions (목록·생성·상세·삭제),
 * /assistant/sessions/{id}/turns (SSE), /assistant/sessions/{id}/decisions (SSE). 계약은 phase-14 "이벤트·응답 계약"·"API" 절이다.
 *
 * 턴의 대본은 메시지의 낱말로 고른다.
 *   배포·deploy   token → tool_call(get_mcp_server) → tool_result → resource(mcp-server) → token → interrupt(confirm)
 *                 결정 approve → tool_call(deploy_mcp_server) → tool_result → resource(mcp-server) → token → done
 *                 결정 reject  → token → done
 *   물어·ask      token → interrupt(question). 다음 턴의 메시지가 답이 되어 token → done
 *   이동·navigate token → ui(navigate /mcp-servers/ms-calc) → done
 *   오류·error    error(turn_failed)
 *   비밀·password tool_call(create_data_service) → tool_result(error secret_in_chat) → token → ui(navigate /data-services/new) → done
 *   제안·propose  token → interrupt(confirm, update_agent 의 Agent 정의 제안. 예약 칸 _proposal 과 덩어리 proposal, P48)
 *                 결정 approve(지문) → tool_call(update_agent) → tool_result(저장된 리비전, 기준이 지났으면 409 revision_conflict)
 *                 → resource(agent) → token → done. 결정 reject → token → done. 지문이 없거나 다르면 409 proposal_changed
 *                 GET /sessions/{id}/proposal 은 기다리는 제안을 기준 r2 위에 짜 맞춘 것이고 없으면 404 proposal_not_found
 *   그 밖         token → tool_call(list_mcp_servers) → tool_result → token → done
 * 전사는 components/chat/items.ts 의 같은 접기 규칙으로 쌓아 GET /sessions/{id} 가 복원용 transcript 를 돌려준다.
 * 계약대로 interrupt 의 action_requests 에는 id 가 없고 세션의 pending.calls[i].id 가 결정의 id 다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { AgentProposal, AssistantConfig, AssistantDecisionsBody, AssistantPendingState, AssistantSessionDetail, AssistantSessionSummary, AssistantTurnBody } from "@/api/types";
import { applyChatEvent, resolveInterrupts, type ChatItem } from "@/components/chat/items";
import { toApiGraph, type Graph } from "@/lib/agentGraph";
import { particle } from "@/lib/particle";

import { filledTemplateGraph } from "./agent-templates";
import { agentStore } from "./agents";
import { NOW, apiError, http, record } from "./core";
import { sseResponse, tokenUsage, type SseEvent } from "./sse";

/** 배포 대본이 승인을 묻는 호출의 id 와 인자다. 결정 본문의 id 와 승인 대기 세션의 pending 을 검사하는 데 쓴다. */
export const DEPLOY_CALL_ID = "call-deploy-1";
export const DEPLOY_ARGS = { server_id: "ms-calc", name: "calc-2", display_name: "calc 두 번째", cluster_id: "cl-home", replicas: 1 };
const ASK_CALL_ID = "call-ask-1";

/*
 * Agent 정의 제안 (P48). ag-support 의 r2 위의 바꾸기 하나다. 메인 에이전트의 시스템 프롬프트에 한 줄을 더하고, 입력 가드레일을
 * 빼고 출력 가드레일을 더한다. 덩어리는 플랫폼이 비교한 모양 그대로다(docs/guide/assistant.md 5절).
 */
export const PROPOSAL_CALL_ID = "call-proposal-1";
export const PROPOSAL_ID = "p-1";
export const PROPOSAL_DIGEST = "p48.1:digest-1";
export const PROPOSAL_ADDED_LINE = "환불 요청은 상담원에게 넘긴다.";

/** 제안을 기준 r2 에 짜 맞춘 그래프. */
export function proposedGraphFixture(): Graph {
  const g = filledTemplateGraph();
  const agent = g.nodes.find((n) => n.id === "agent-main");
  if (agent) agent.data = { ...agent.data, system_prompt: `${(agent.data as { system_prompt: string }).system_prompt}\n${PROPOSAL_ADDED_LINE}` } as typeof agent.data;
  return {
    nodes: [
      ...g.nodes.filter((n) => n.id !== "guardrail-input"),
      { id: "guardrail-output", type: "guardrail", data: { position: "output", rules: [{ kind: "max_length", value: 2000 }], on_violation: "block", message: null } },
    ],
    edges: [...g.edges.filter((e) => e.target !== "guardrail-input"), { id: "e-guardrail-out", source: "agent-main", target: "guardrail-output", kind: "guardrail" }],
  };
}

export function proposalBlockFixture(over: Partial<AgentProposal> = {}): AgentProposal {
  return {
    proposal_id: PROPOSAL_ID,
    kind: "update",
    agent: { id: "ag-support", name: "support", display_name: "상담 에이전트" },
    base_revision: 2,
    deployed_revision: 2,
    changes: {
      nodes: { added: [{ id: "guardrail-output", type: "guardrail" }], changed: [{ id: "agent-main", type: "agent", name: "Agent", fields: ["system_prompt"] }], removed: [{ id: "guardrail-input", type: "guardrail" }] },
      edges: { added: [{ source: "agent-main", target: "guardrail-output", kind: "guardrail" }], removed: [{ source: "agent-main", target: "guardrail-input", kind: "guardrail" }] },
      service: [],
      texts: [{ node_id: "agent-main", field: "system_prompt", lines: [{ op: "+", text: PROPOSAL_ADDED_LINE }], cut: false }],
    },
    attention: [
      { code: "system_prompt_changed", node_id: "agent-main", agent: "Agent" },
      { code: "guardrail_weakened", node_id: "guardrail-input", position: "input" },
    ],
    warnings: [],
    ...over,
  };
}

export interface MockAssistantSession {
  id: string;
  tenant: string;
  title: string;
  /** 이 대화에 붙은 provider (D45). 첫 턴이 보내면 목이 여기에 적어 둔다. */
  provider: string | null;
  state: "idle" | "running" | "pending";
  pending: AssistantPendingState | null;
  /** 이 세션에서 낸 Agent 정의 제안의 수(P48). 다음 제안의 id 가 된다. */
  proposals?: number;
  transcript: ChatItem[];
  turns: number;
  usage: ReturnType<typeof tokenUsage>;
  updated_at: string;
}

export const assistantConfigFixture: AssistantConfig = {
  enabled: true,
  provider: "openai-main",
  // 홈의 모델 chip 이 고르는 목록 (D45). 이름·종류·모델만 온다
  providers: [
    { name: "openai-main", kind: "openai", model: "gpt-4o" },
    { name: "ant-main", kind: "anthropic", model: "claude-opus-5" },
  ],
  confirm_deploy: true,
  limits: { max_tool_calls_per_turn: 24, turn_timeout_seconds: 300 },
};

/** 세션의 n 번째 제안의 id 와 지문. 다듬을 때마다 새 제안이 오므로 세션마다 센다. 첫 제안은 PROPOSAL_ID 다. */
export function proposalIds(n = 1): { id: string; digest: string } {
  return { id: `p-${n}`, digest: `p48.1:digest-${n}` };
}

/** 제안 대본이 기다리게 하는 호출의 인자. 예약 칸에 제안 id·지문·기준 리비전이 있다. */
export function proposalArgs(n = 1): Record<string, unknown> {
  return {
    agent_id: "ag-support",
    set_nodes: [{ id: "agent-main" }, { id: "guardrail-output" }],
    remove_nodes: ["guardrail-input"],
    _proposal: { ...proposalIds(n), base_revision: 2, attention: ["system_prompt_changed", "guardrail_weakened"] },
  };
}

/** `GET /sessions/{id}/proposal` 의 답. latest_revision 은 목 Agent 의 지금 리비전이다. */
function proposalView(proposalId: string): Record<string, unknown> {
  const agent = agentStore.find("ag-support");
  return {
    proposal_id: proposalId,
    kind: "update",
    agent_id: "ag-support",
    name: "support",
    display_name: "상담 에이전트",
    base_revision: 2,
    latest_revision: agent?.revision ?? null,
    graph: proposedGraphFixture(),
    service: agent?.service ?? {},
    proposal: proposalBlockFixture({ proposal_id: proposalId }),
  };
}

/** 끝난 턴 하나가 든 세션. 패널을 다시 열 때 전사 복원을 검사하는 데 쓴다. */
export function assistantSessionFixture(over: Partial<MockAssistantSession> = {}): MockAssistantSession {
  return {
    id: "as-1",
    tenant: "default",
    title: "calc 인스턴스 확인",
    provider: "openai-main",
    state: "idle",
    pending: null,
    transcript: [
      { kind: "user", text: "MCP Server 가 몇 개 있어?" },
      { kind: "assistant", text: "MCP Server 목록을 확인하겠습니다. ", streaming: false },
      { kind: "tool_call", call: { id: "call-list-0", name: "list_mcp_servers", args: {} }, result: { id: "call-list-0", name: "list_mcp_servers", status: "success", content: '[{"id":"ms-calc","name":"calc-1"}]' } },
      { kind: "assistant", text: "인스턴스가 1개 있습니다: calc-1 (Ready).", streaming: false },
      { kind: "done", usage: tokenUsage(300, 40), turn: 1, tool_calls: 1 },
    ],
    turns: 1,
    usage: tokenUsage(300, 40),
    updated_at: NOW,
    ...over,
  };
}

function fixtures(): MockAssistantSession[] {
  return [assistantSessionFixture(), assistantSessionFixture({ id: "as-acme-1", tenant: "acme", title: "acme 백엔드 등록", provider: null, transcript: [{ kind: "user", text: "acme 백엔드를 등록해 줘" }, { kind: "assistant", text: "acme 백엔드를 등록하려면 스펙 URL 이 필요합니다.", streaming: false }, { kind: "done", usage: tokenUsage(120, 20), turn: 1, tool_calls: 0 }] })];
}

export const assistantState: { config: AssistantConfig; sessions: MockAssistantSession[]; nextId: number } = { config: { ...assistantConfigFixture }, sessions: fixtures(), nextId: 2 };

export function resetAssistantState(): void {
  assistantState.config = { ...assistantConfigFixture };
  assistantState.sessions = fixtures();
  assistantState.nextId = 2;
}

function summary(s: MockAssistantSession): AssistantSessionSummary {
  return { id: s.id, title: s.title, state: s.state, provider: s.provider, updated_at: s.updated_at };
}

function detail(s: MockAssistantSession): AssistantSessionDetail {
  return { ...summary(s), pending: s.pending, transcript: s.transcript as unknown as AssistantSessionDetail["transcript"], turns: s.turns, usage: s.usage };
}

/** 한 턴의 대본. 세션의 pending 도 여기서 세운다(interrupt 를 내기 전에 저장한다는 계약). */
function turnScript(s: MockAssistantSession, message: string): SseEvent[] {
  if (/배포|deploy/i.test(message)) {
    s.pending = { kind: "confirm", calls: [{ id: DEPLOY_CALL_ID, name: "deploy_mcp_server", args: DEPLOY_ARGS, risk: "deploy" }] };
    return [
      { event: "token", data: { text: "calc 프로필로 MCP Server 를 만들겠습니다. " } },
      { event: "tool_call", data: { id: "call-profile-1", name: "get_mcp_server", args: { server_id: "ms-calc" } } },
      { event: "tool_result", data: { id: "call-profile-1", name: "get_mcp_server", status: "success", content: '{"id":"ms-calc","name":"calc","revision":3}' } },
      { event: "resource", data: { kind: "mcp-server", id: "ms-calc", name: "calc", url: "/mcp-servers/ms-calc" } },
      { event: "token", data: { text: "배포는 승인이 필요합니다." } },
      {
        event: "interrupt",
        data: {
          session_id: s.id,
          kind: "confirm",
          action_requests: [
            {
              name: "deploy_mcp_server",
              args: DEPLOY_ARGS,
              description: "MCP Server 를 만듭니다 (deploy 등급).",
              // 가져온 문서를 쓰는 호출이면 서버가 출처를 싣는다 (D31 T10)
              files: [{ name: "fetched-1.yaml", url: "https://api.example.com/spec.yaml" }],
            },
          ],
          review_configs: [{ action_name: "deploy_mcp_server", allowed_decisions: ["approve", "reject"] }],
        },
      },
    ];
  }
  if (/제안|propose/i.test(message)) {
    s.proposals = (s.proposals ?? 0) + 1;
    const args = proposalArgs(s.proposals);
    s.pending = { kind: "confirm", calls: [{ id: PROPOSAL_CALL_ID, name: "update_agent", args, risk: "write" }] };
    return [
      { event: "token", data: { text: "메인 에이전트에 환불 안내를 더하고 출력 가드레일을 두는 제안입니다." } },
      {
        event: "interrupt",
        data: {
          session_id: s.id,
          kind: "confirm",
          action_requests: [{ name: "update_agent", args, description: "Agent 정의를 바꿉니다.", proposal: proposalBlockFixture({ proposal_id: proposalIds(s.proposals).id }) }],
          review_configs: [{ action_name: "update_agent", allowed_decisions: ["approve", "reject"] }],
        },
      },
    ];
  }
  if (/물어|ask|질문/i.test(message)) {
    const args = { question: "어느 클러스터에 배포할까요?", options: ["cl-home", "cl-edge"], field: "cluster_id" };
    s.pending = { kind: "question", calls: [{ id: ASK_CALL_ID, name: "ask_user", args, risk: "read" }] };
    return [
      { event: "token", data: { text: "클러스터를 정해야 합니다. " } },
      { event: "interrupt", data: { session_id: s.id, kind: "question", action_requests: [{ name: "ask_user", args }], review_configs: [], question: { text: args.question, options: args.options, field: args.field } } },
    ];
  }
  // 실제 provider 는 말풍선 하나를 토큰 여러 개로 나눠 보낸다. 그때 전사 항목 수는 늘지 않는다.
  if (/길게|자세히/i.test(message)) {
    const chunks = ["MCP Server 는 ", "MCP Server 을 클러스터에 ", "올린 것입니다. ", "상태는 phase 로 봅니다. ", "Ready 면 호출할 수 있습니다."];
    return [
      ...chunks.map((text) => ({ event: "token", data: { text } }) as SseEvent),
      { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(80, 40), tool_calls: 0, text: chunks.join("") } },
    ];
  }
  if (/이동|navigate|열어/i.test(message)) {
    return [
      { event: "token", data: { text: "MCP Server calc-1 화면을 엽니다." } },
      { event: "ui", data: { type: "navigate", to: "/mcp-servers/ms-calc" } },
      { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(80, 12), tool_calls: 1, text: "MCP Server calc-1 화면을 엽니다." } },
    ];
  }
  // viewer 가 쓰기를 시키면 도구가 403 을 돌려주고 모델이 그 이유를 말한다 (D31·D35 완료 조건 3).
  // 인가는 청지기 안에서도 그대로 돌기 때문에 사용자보다 큰 권한을 갖지 못한다
  if (/권한|viewer|403/i.test(message)) {
    return [
      { event: "tool_call", data: { id: "call-deploy-403", name: "deploy_mcp_server", args: DEPLOY_ARGS } },
      { event: "tool_result", data: { id: "call-deploy-403", name: "deploy_mcp_server", status: "error", content: '{"code":"forbidden","detail":"missing scope mcp:deploy"}' } },
      { event: "token", data: { text: "이 작업에는 배포 권한이 필요한데 지금 계정에는 없습니다. 관리자에게 developer 역할을 요청해 주세요." } },
      { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(200, 30), tool_calls: 1, text: "이 작업에는 배포 권한이 필요한데 지금 계정에는 없습니다. 관리자에게 developer 역할을 요청해 주세요." } },
    ];
  }
  if (/오류|error/i.test(message)) {
    return [{ event: "error", data: { session_id: s.id, code: "turn_failed", message: "provider 가 오류를 돌려줬습니다." } }];
  }
  if (/비밀|password/i.test(message)) {
    return [
      { event: "tool_call", data: { id: "call-ds-1", name: "create_data_service", args: { name: "orders", kind: "postgres", host: "db.local", password: "***" } } },
      { event: "tool_result", data: { id: "call-ds-1", name: "create_data_service", status: "error", content: '{"code":"secret_in_chat","detail":"password must not be sent through chat"}' } },
      { event: "token", data: { text: "비밀번호는 채팅으로 받지 않습니다. 나머지를 채운 등록 화면을 열어 드릴 테니 비밀번호만 넣어 주세요." } },
      { event: "ui", data: { type: "navigate", to: "/data-services/new" } },
      { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(200, 40), tool_calls: 2, text: "" } },
    ];
  }
  return [
    { event: "token", data: { text: "MCP Server 목록을 확인하겠습니다. " } },
    { event: "tool_call", data: { id: `call-list-${s.turns + 1}`, name: "list_mcp_servers", args: {} } },
    { event: "tool_result", data: { id: `call-list-${s.turns + 1}`, name: "list_mcp_servers", status: "success", content: '[{"id":"ms-calc","name":"calc-1","status":{"phase":"Ready"}}]' } },
    { event: "token", data: { text: "인스턴스가 1개 있습니다: calc-1 (Ready)." } },
    { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(300, 40), tool_calls: 1, text: "MCP Server 목록을 확인하겠습니다. 인스턴스가 1개 있습니다: calc-1 (Ready)." } },
  ];
}

/** ask_user 의 답(다음 턴 메시지)에 딸린 대본. */
function answerScript(s: MockAssistantSession, message: string): SseEvent[] {
  return [
    { event: "token", data: { text: `${message} 로 진행하겠습니다.` } },
    { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(60, 10), tool_calls: 0, text: `${message} 로 진행하겠습니다.` } },
  ];
}

/** 제안 호출의 결정. 승인은 짜 맞춘 정의를 기준 리비전 위에 저장하고, 기준이 지났으면 409 revision_conflict 결과다. */
function proposalDecisionScript(s: MockAssistantSession, body: AssistantDecisionsBody): SseEvent[] {
  const d = body.decisions[0];
  const done = (text: string, calls: number): SseEvent => ({ event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(80, 12), tool_calls: calls, text } });
  if (d.type === "reject") {
    const text = "제안을 거두었습니다.";
    return [{ event: "token", data: { text } }, done(text, 0)];
  }
  const call = { event: "tool_call", data: { id: PROPOSAL_CALL_ID, name: "update_agent", args: { agent_id: "ag-support" } } };
  const agent = agentStore.find("ag-support");
  if (!agent || agent.revision !== 2) {
    const text = "그 사이 새 리비전이 저장되어 다시 읽고 제안하겠습니다.";
    const content = JSON.stringify({ code: "revision_conflict", status: 409, revision: agent?.revision ?? null });
    return [call, { event: "tool_result", data: { id: PROPOSAL_CALL_ID, name: "update_agent", status: "error", content } }, { event: "token", data: { text } }, done(text, 1)];
  }
  agent.graph = toApiGraph(proposedGraphFixture());
  agent.revision += 1;
  agent.revisions = [...agent.revisions, agent.revision];
  const text = `상담 에이전트를 r${agent.revision}${particle(`r${agent.revision}`, "으로/로")} 저장했습니다.`;
  return [
    call,
    { event: "tool_result", data: { id: PROPOSAL_CALL_ID, name: "update_agent", status: "success", content: JSON.stringify({ id: agent.id, revision: agent.revision }) } },
    { event: "resource", data: { kind: "agent", id: agent.id, name: agent.name, url: `/agents/${agent.id}` } },
    { event: "token", data: { text } },
    done(text, 1),
  ];
}

function decisionScript(s: MockAssistantSession, body: AssistantDecisionsBody): SseEvent[] {
  const d = body.decisions[0];
  if (d.type === "reject") {
    return [
      { event: "token", data: { text: d.message ? `배포를 취소했습니다. 사유: ${d.message}` : "배포를 취소했습니다." } },
      { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(60, 8), tool_calls: 0, text: "배포를 취소했습니다." } },
    ];
  }
  return [
    { event: "tool_call", data: { id: DEPLOY_CALL_ID, name: "deploy_mcp_server", args: DEPLOY_ARGS } },
    { event: "tool_result", data: { id: DEPLOY_CALL_ID, name: "deploy_mcp_server", status: "success", content: '{"id":"ms-new","name":"calc-2","status":{"phase":"Pending"}}' } },
    { event: "resource", data: { kind: "mcp-server", id: "ms-new-0123456789", name: "calc-2", display_name: "계산기 두 번째", url: "/mcp-servers/ms-new" } },
    { event: "token", data: { text: "MCP Server calc-2 를 만들었습니다. Ready 가 되면 허브에 게시할 수 있습니다." } },
    { event: "done", data: { session_id: s.id, turn: s.turns + 1, usage: tokenUsage(400, 60), tool_calls: 1, text: "MCP Server calc-2 를 만들었습니다." } },
  ];
}

/** 대본을 전사에 접어 둔다. 인터럽트가 없는 대본이면 턴이 끝난 것이다. */
function commit(s: MockAssistantSession, events: SseEvent[]): void {
  let items = s.transcript;
  for (const ev of events) items = applyChatEvent(items, ev as Parameters<typeof applyChatEvent>[1]);
  s.transcript = items;
  const interrupted = events.some((e) => e.event === "interrupt");
  s.state = interrupted ? "pending" : "idle";
  if (!interrupted) s.pending = null;
  s.turns += 1;
  s.updated_at = NOW;
}

function tenantOf(headers: Record<string, string>): string {
  return headers["x-daegam-tenant"] ?? "default";
}

function findSession(id: string): MockAssistantSession | null {
  return assistantState.sessions.find((s) => s.id === id) ?? null;
}

export const assistantHandlers: HttpHandler[] = [
  http.get("/assistant/config", async ({ request }) => {
    await record(request);
    return HttpResponse.json(assistantState.config);
  }),
  http.get("/assistant/sessions", async ({ request }) => {
    const r = await record(request);
    const tenant = tenantOf(r.headers);
    return HttpResponse.json({ items: assistantState.sessions.filter((s) => s.tenant === tenant).map(summary) });
  }),
  http.post("/assistant/sessions", async ({ request }) => {
    const r = await record(request);
    if (!assistantState.config.enabled) return apiError(404, "assistant_disabled", "assistant is disabled");
    const body = (r.body ?? {}) as { title?: string | null };
    const s: MockAssistantSession = { id: `as-${assistantState.nextId}`, tenant: tenantOf(r.headers), title: body.title ?? "새 대화", provider: null, state: "idle", pending: null, transcript: [], turns: 0, usage: tokenUsage(0, 0), updated_at: NOW };
    assistantState.nextId += 1;
    assistantState.sessions = [s, ...assistantState.sessions];
    return HttpResponse.json({ id: s.id }, { status: 201 });
  }),
  http.get("/assistant/sessions/:id", async ({ request, params }) => {
    const r = await record(request);
    const s = findSession(String(params.id));
    if (!s || s.tenant !== tenantOf(r.headers)) return apiError(404, "session_not_found", `session ${String(params.id)} not found`);
    return HttpResponse.json(detail(s));
  }),
  http.delete("/assistant/sessions/:id", async ({ request, params }) => {
    const r = await record(request);
    const s = findSession(String(params.id));
    if (!s || s.tenant !== tenantOf(r.headers)) return apiError(404, "session_not_found", `session ${String(params.id)} not found`);
    assistantState.sessions = assistantState.sessions.filter((x) => x.id !== s.id);
    return HttpResponse.json({ id: s.id, deleted: true });
  }),
  http.get("/assistant/sessions/:id/proposal", async ({ request, params }) => {
    const r = await record(request);
    const s = findSession(String(params.id));
    if (!s || s.tenant !== tenantOf(r.headers)) return apiError(404, "session_not_found", `session ${String(params.id)} not found`);
    const waiting = s.pending?.kind === "confirm" ? s.pending.calls.find((c) => c.args._proposal) : undefined;
    if (!waiting) return apiError(404, "proposal_not_found", "no proposal is waiting in this session");
    return HttpResponse.json(proposalView(String((waiting.args._proposal as { id?: string }).id ?? PROPOSAL_ID)));
  }),
  http.post("/assistant/sessions/:id/turns", async ({ request, params }) => {
    const r = await record(request);
    if (!assistantState.config.enabled) return apiError(404, "assistant_disabled", "assistant is disabled");
    const s = findSession(String(params.id));
    if (!s) return apiError(404, "session_not_found", `session ${String(params.id)} not found`);
    if (s.tenant !== tenantOf(r.headers)) return apiError(409, "tenant_mismatch", `session belongs to tenant ${s.tenant}`);
    const body = r.body as AssistantTurnBody;
    if (s.state === "running" || body.message === "busy") return apiError(409, "turn_in_progress", "a turn is already running in this session");
    // 고른 provider 는 그 대화에 붙는다 (D45). 등록되지 않은 이름은 422 다
    if (body.provider) {
      if (!(assistantState.config.providers ?? []).some((p) => p.name === body.provider)) {
        return apiError(422, "unknown_provider", `llm provider '${body.provider}' is not configured`);
      }
      s.provider = body.provider;
    }
    let script: SseEvent[];
    if (s.pending?.kind === "question") {
      // 질문의 답은 새 HumanMessage 가 아니라 ask_user 의 ToolMessage 다. 화면에는 사용자 말풍선으로 보인다
      s.pending = null;
      s.transcript = resolveInterrupts(s.transcript);
      script = answerScript(s, body.message);
    } else {
      // 결정 없이 온 새 메시지는 보류를 superseded 로 닫는다
      if (s.pending) {
        s.pending = null;
        s.transcript = resolveInterrupts(s.transcript);
      }
      script = turnScript(s, body.message);
    }
    s.transcript = [...s.transcript, { kind: "user", text: body.message }];
    commit(s, script);
    return sseResponse(script);
  }),
  http.post("/assistant/sessions/:id/decisions", async ({ request, params }) => {
    const r = await record(request);
    const s = findSession(String(params.id));
    if (!s) return apiError(404, "session_not_found", `session ${String(params.id)} not found`);
    if (s.tenant !== tenantOf(r.headers)) return apiError(409, "tenant_mismatch", `session belongs to tenant ${s.tenant}`);
    if (!s.pending || s.pending.kind !== "confirm") return apiError(409, "not_pending", `session ${s.id} has no pending approval`);
    const body = r.body as AssistantDecisionsBody;
    const decisions = Array.isArray(body?.decisions) ? body.decisions : [];
    if (decisions.length !== s.pending.calls.length) {
      return apiError(422, "decision_count_mismatch", `expected ${s.pending.calls.length} decision(s), got ${decisions.length}`);
    }
    const unknown = decisions.find((d) => !s.pending!.calls.some((c) => c.id === d.id));
    if (unknown) return apiError(422, "decision_id_mismatch", `unknown call id ${String(unknown.id)}`);
    // 제안 호출의 결정은 승인이든 거절이든 그 호출의 지문을 싣는다 (P48 6.1절)
    const proposal = s.pending.calls[0].args._proposal as { digest?: string } | undefined;
    if (proposal && decisions[0].proposal_digest !== proposal.digest) return apiError(409, "proposal_changed", "the decision does not carry the waiting proposal's digest");
    s.pending = null;
    s.transcript = resolveInterrupts(s.transcript);
    const script = proposal ? proposalDecisionScript(s, body) : decisionScript(s, body);
    commit(s, script);
    return sseResponse(script);
  }),
];
