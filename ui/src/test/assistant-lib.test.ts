import { QueryClient } from "@tanstack/react-query";

import { normalizePending } from "@/api/runtimeShapes";
import type { AgentPending } from "@/api/types";
import { ChatNotice, applyChatEvent, type ChatItem } from "@/components/chat";
import { splitCodeFences } from "@/lib/codeFences";
import { attentionText, buildContext, buildDecisions, canvasSaveReason, findResourceName, nodeText, proposalIdOf, savedAgentOf, invalidateForEvent, itemsFromTranscript, proposalDigest, proposalHref, proposalLink, proposalNodeMarks, proposalTarget, reopenPending, resourceFromPath, transcriptToItems, visibleArgs, writeToolKinds } from "@/lib/assistant";
import { dictionary } from "@/i18n";
import type { ChatEvent } from "@/lib/chatStream";
import { DELETE_RULES } from "@/lib/deletion";
import { viaFilterValue } from "@/pages/audit/Audit";

import { proposalBlockFixture } from "./msw";
import { tokenUsage } from "./msw/sse";
import { CITATIONS_FIXTURE, FALLBACK_FIXTURE, TURN_FIXTURE } from "./runtimeFixtures";

/** 비서 화면의 순수 규칙 (D31): 이벤트 → 항목, 문맥, 결정 id 대응, 쓰기 결과 무효화, 전사 복원, via 필터. */
describe("assistant lib", () => {
  it("applyChatEvent folds resource and question interrupts into items and hands ui.navigate to the hook", () => {
    let items: ChatItem[] = [];
    const navigated: string[] = [];
    const hooks = { onNavigate: (to: string) => navigated.push(to) };
    items = applyChatEvent(items, { event: "token", data: { text: "서버를 " } }, hooks);
    items = applyChatEvent(items, { event: "resource", data: { kind: "mcp-server", id: "ms-calc", name: "calc", url: "/mcp-servers/ms-calc" } }, hooks);
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "assistant", streaming: false });
    expect(items[1]).toEqual({ kind: "resource", resource: { kind: "mcp-server", id: "ms-calc", name: "calc", url: "/mcp-servers/ms-calc" } });
    // ui.navigate 는 항목이 아니라 콜백이다
    items = applyChatEvent(items, { event: "ui", data: { type: "navigate", to: "/mcp-servers/ms-calc" } }, hooks);
    expect(items).toHaveLength(2);
    expect(navigated).toEqual(["/mcp-servers/ms-calc"]);
    items = applyChatEvent(items, { event: "ui", data: { type: "navigate", to: "/x" } });
    expect(navigated).toEqual(["/mcp-servers/ms-calc"]);
    // 질문 인터럽트는 kind·question 을 pending 에 실어 둔다. review_configs 가 없으면 approve·reject 로 채운다
    items = applyChatEvent(items, { event: "interrupt", data: { session_id: "s", kind: "question", action_requests: [{ name: "ask_user", args: { question: "어디에?" } }], question: { text: "어디에?", options: ["a", "b"] } } }, hooks);
    const last = items[items.length - 1];
    expect(last.kind).toBe("interrupt");
    if (last.kind !== "interrupt") throw new Error("unreachable");
    expect(last.pending.kind).toBe("question");
    expect(last.pending.question).toEqual({ text: "어디에?", options: ["a", "b"] });
    expect(last.pending.review_configs).toEqual([{ action_name: "ask_user", allowed_decisions: ["approve", "reject"] }]);
    expect(last.resolved).toBe(false);
  });

  it("승인 뒤 재개가 같은 호출을 다시 알리면 새 줄을 달지 않고 그 줄을 이어 쓴다", () => {
    let items: ChatItem[] = [{ kind: "user", text: "toolhive 는 무슨 언어야?" }];
    items = applyChatEvent(items, { event: "tool_call", data: { id: "call-1", name: "ask_wiki_question", args: { repoName: "stacklok/toolhive" } } });
    items = applyChatEvent(items, { event: "interrupt", data: { session_id: "s", action_requests: [{ name: "ask_wiki_question", args: {} }], review_configs: [] } });
    // 카드가 붙든 호출은 도는 중이 아니라 결정을 기다린다
    expect(items[1]).toMatchObject({ kind: "tool_call", waiting: true, result: null });
    // 재개: 런타임은 턴마다 새로 세므로 같은 id 를 다시 알린다
    items = applyChatEvent(items, { event: "tool_call", data: { id: "call-1", name: "ask_wiki_question", args: { repoName: "stacklok/toolhive", question: "언어" } } });
    items = applyChatEvent(items, { event: "tool_result", data: { id: "call-1", name: "ask_wiki_question", status: "success", content: "Go" } });
    const calls = items.filter((it) => it.kind === "tool_call");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ waiting: false, call: { args: { question: "언어" } }, result: { status: "success", content: "Go" } });
    // 결과를 이미 받은 호출과 id 가 같아도 새 턴의 새 호출이면 새 줄이다
    items = applyChatEvent(items, { event: "tool_call", data: { id: "call-1", name: "ask_wiki_question", args: {} } });
    expect(items.filter((it) => it.kind === "tool_call")).toHaveLength(2);
  });

  it("resourceFromPath and buildContext read the kind and id from the route and the name from the query cache", () => {
    expect(resourceFromPath("/mcp-servers/ms-calc")).toEqual({ kind: "mcp-server", id: "ms-calc" });
    expect(resourceFromPath("/gateways/gw-support")).toEqual({ kind: "gateway", id: "gw-support" });
    expect(resourceFromPath("/agents/new")).toBeNull();
    expect(resourceFromPath("/mcp-servers")).toBeNull();
    expect(resourceFromPath("/admin/settings")).toBeNull();
    // 경로 조각 표는 deletion.ts 의 PATH_SEGMENT 한 벌이다. 정의와 배포가 한 리소스라(D42) 아홉 종이 다 풀려야 한다
    for (const kind of Object.keys(DELETE_RULES) as (keyof typeof DELETE_RULES)[]) {
      expect(resourceFromPath(`${DELETE_RULES[kind].listPath}/x-1`), kind).toEqual({ kind, id: "x-1" });
    }
    expect(buildContext("/hub")).toEqual({ route: "/hub" });
    const qc = new QueryClient();
    qc.setQueryData(["mcp-servers", "default"], [{ id: "ms-calc", name: "calc", display_name: "계산기" }]);
    qc.setQueryData(["data-services", "default", "ds-pg"], { id: "ds-pg", name: "orders-db" });
    expect(findResourceName(qc, "mcp-server", "ms-calc")).toBe("계산기");
    expect(findResourceName(qc, "data-service", "ds-pg")).toBe("orders-db");
    expect(findResourceName(qc, "cluster", "cl-home")).toBeNull();
    expect(buildContext("/mcp-servers/ms-calc", qc)).toEqual({ route: "/mcp-servers/ms-calc", resource: { kind: "mcp-server", id: "ms-calc", name: "계산기" } });
    expect(buildContext("/clusters/cl-home", qc)).toEqual({ route: "/clusters/cl-home", resource: { kind: "cluster", id: "cl-home", name: "cl-home" } });
    // 캔버스에 저장하지 않은 변경이 있을 때만 그 표시를 싣는다. 그 칸을 모르는 앞 판의 플랫폼이 다른 턴을 막지 않게 한다 (P48 6.5절)
    expect(buildContext("/agents/new", qc, true)).toEqual({ route: "/agents/new", unsaved_edits: true });
  });

  it("buildDecisions pairs positional decisions with call ids and rejects edit", () => {
    const plain = [
      { name: "deploy_mcp_server", args: { server_id: "p" } },
      { name: "delete_mcp_server", args: { server_id: "q" } },
    ];
    expect(buildDecisions([{ type: "approve" }, { type: "reject", message: "아직" }], ["c1", "c2"], plain)).toEqual([
      { id: "c1", type: "approve" },
      { id: "c2", type: "reject", message: "아직" },
    ]);
    expect(buildDecisions([{ type: "respond", message: "다른 이름으로" }], ["c1"], plain)).toEqual([{ id: "c1", type: "reject", message: "다른 이름으로" }]);
    // 수가 어긋난 것은 개발자의 잘못이라 영어 그대로이고, edit 거절은 사람에게 보이므로 사전 키를 든 안내다
    expect(() => buildDecisions([{ type: "approve" }], [], plain)).toThrow("do not match");
    expect(() => buildDecisions([{ type: "edit", edited_action: { name: "x", args: {} } }], ["c1"], plain)).toThrow(new ChatNotice("editUnsupported"));
  });

  it("buildDecisions echoes a proposal's digest on approve and reject alike", () => {
    // 지문이 빠진 제안의 결정은 서버가 409 proposal_changed 로 막는다 (P48 6.1절). 제안이 아닌 호출에는 싣지 않는다
    const requests = [
      { name: "create_agent", args: { name: "support", _proposal: { id: "p-1", digest: "p48.1:abc" } } },
      { name: "deploy_mcp_server", args: { server_id: "p" } },
    ];
    expect(buildDecisions([{ type: "approve" }, { type: "approve" }], ["c1", "c2"], requests)).toEqual([
      { id: "c1", type: "approve", proposal_digest: "p48.1:abc" },
      { id: "c2", type: "approve" },
    ]);
    expect(buildDecisions([{ type: "reject", message: "이름을 바꿔" }], ["c1"], requests)).toEqual([{ id: "c1", type: "reject", message: "이름을 바꿔", proposal_digest: "p48.1:abc" }]);
    expect(buildDecisions([{ type: "reject" }], ["c1"], requests)).toEqual([{ id: "c1", type: "reject", proposal_digest: "p48.1:abc" }]);
    expect(visibleArgs(requests[0].args)).toEqual({ name: "support" });
    expect(proposalDigest(requests[1].args)).toBeUndefined();
    // 인자 없이 온 요청도 카드가 그린다. 객체가 아니면 그대로 둔다 (72a3cac0 의 회귀)
    expect(visibleArgs(null as unknown as Record<string, unknown>)).toBeNull();
    expect(proposalDigest({ _proposal: "p48.1:abc" })).toBeUndefined();
  });

  it("a proposal's canvas link carries its id and session, and the canvas reads them back (P48 6.5)", () => {
    const update = proposalBlockFixture();
    expect(proposalHref("as-1", update)).toBe("/agents/ag-support?tab=definition&proposal=p-1&session=as-1");
    expect(proposalLink("?tab=definition&proposal=p-1&session=as-1")).toEqual({ proposalId: "p-1", sessionId: "as-1" });
    expect(proposalLink("?tab=definition&proposal=p-1")).toBeNull();
    const create = proposalBlockFixture({ kind: "create", agent: { id: null, name: "petstore", display_name: null } });
    expect(proposalHref("as-1", create)).toBe("/agents/new?proposal=p-1&session=as-1");
    expect(proposalHref("as-1", { ...update, agent: { ...update.agent, id: null } })).toBeNull();
    // 같은 대상의 나중 제안만 불러오자고 한다
    expect(proposalTarget(update)).toBe("agent:ag-support");
    expect(proposalTarget(create)).toBe("new");
    expect([...proposalNodeMarks(update)]).toEqual([
      ["guardrail-output", "added"],
      ["agent-main", "changed"],
    ]);
  });

  it("attentionText fills platform values and falls back to the node id, and the canvas save reason names what was saved", () => {
    const t = dictionary("ko");
    expect(attentionText(t, { code: "model_changed", node_id: "agent-main", agent: "main", provider: "openai-main", model: "gpt-4o" })).toBe("main의 모델을 openai-main · gpt-4o로 바꿉니다");
    expect(attentionText(t, { code: "knowledge_attached", node_id: "kb-1", knowledge: { id: "kb-faq" } })).toBe("지식 kb-faq를 붙입니다. 이 Agent 와 대화하는 사람은 모두 그 문서를 검색할 수 있습니다");
    // MCP 프로필 꼴이 아니어서 플랫폼이 싣지 않은 Tool 이름은 노드 id 로 대신한다
    expect(attentionText(t, { code: "approval_removed", node_id: "tools-1" })).toBe("tools-1의 승인을 없앱니다");
    expect(attentionText(dictionary("en"), { code: "guardrail_weakened", node_id: "g", position: "output" })).toBe("Removes or loosens the output guardrail");
    expect(canvasSaveReason({ revision: 3 })).toBe("The user edited this proposal on the canvas and saved revision 3 themselves. Acknowledge it in one line and do not propose this change again.");
    expect(canvasSaveReason({ agentId: "ag-9", revision: 1 })).toContain("created the Agent ag-9 at revision 1");
  });

  it("the canvas reads a node's whole text, the saved Agent from a call result, and the proposal id from the reserved key", () => {
    const graph = { nodes: [{ id: "agent-main", type: "agent" as const, data: { system_prompt: "a\nb", name: 3 } }, { id: "entry", type: "entry" as const }] };
    expect(nodeText(graph, "agent-main", "system_prompt")).toBe("a\nb");
    expect(nodeText(graph, "agent-main", "name")).toBe("");
    expect(nodeText(graph, "entry", "greeting")).toBe("");
    expect(nodeText(graph, "agent-sub", "system_prompt")).toBe("");
    expect(nodeText(undefined, "agent-main", "system_prompt")).toBe("");
    expect(savedAgentOf('{"id":"ag-1","revision":3,"name":"x"}')).toEqual({ id: "ag-1", revision: 3 });
    expect(savedAgentOf('{"code":"revision_conflict"}')).toBeNull();
    expect(savedAgentOf("not json")).toBeNull();
    expect(proposalIdOf({ _proposal: { id: "p-1", digest: "d" } })).toBe("p-1");
    expect(proposalIdOf({ _proposal: { digest: "d" } })).toBeUndefined();
    expect(proposalIdOf({ name: "x" })).toBeUndefined();
  });

  it("writeToolKinds maps write tools to deletion.ts kinds and invalidateForEvent skips reads and failed writes", async () => {
    expect(writeToolKinds("list_mcp_servers")).toBeNull();
    expect(writeToolKinds("get_backend_api")).toBeNull();
    // 배포도 쓰기다. 정의와 배포가 한 리소스라 무효화할 kind 도 하나다 (D42)
    expect(writeToolKinds("deploy_mcp_server")).toEqual(["mcp-server"]);
    expect(writeToolKinds("undeploy_mcp_server")).toEqual(["mcp-server"]);
    expect(writeToolKinds("clone_mcp_server")).toEqual(["mcp-server"]);
    expect(writeToolKinds("delete_backend_api")).toEqual(["backend-api"]);
    expect(writeToolKinds("deploy_agent")).toEqual(["agent"]);
    expect(writeToolKinds("redeploy_gateway")).toEqual(["gateway"]);
    expect(writeToolKinds("publish_mcp_server")).toEqual(["mcp-server"]);
    // kind 를 못 고르는 쓰기(설정 변경)는 빈 목록이고 전체 무효화다
    expect(writeToolKinds("update_settings")).toEqual([]);

    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    expect(await invalidateForEvent(qc, { event: "tool_result", data: { id: "1", name: "list_mcp_servers", status: "success", content: "[]" } })).toBe(false);
    expect(await invalidateForEvent(qc, { event: "tool_result", data: { id: "2", name: "deploy_mcp_server", status: "error", content: "403" } })).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    expect(await invalidateForEvent(qc, { event: "tool_result", data: { id: "3", name: "deploy_mcp_server", status: "success", content: "{}" } })).toBe(true);
    expect(spy.mock.calls.map((c) => c[0]?.queryKey)).toEqual(expect.arrayContaining([["mcp-servers"], ["hub"]]));
    spy.mockClear();
    // resource 이벤트는 링크 카드일 뿐이다. 무효화는 쓰기 도구 결과만 한다
    expect(await invalidateForEvent(qc, { event: "resource", data: { kind: "agent", id: "ag-1", name: "a", url: "/agents/ag-1" } })).toBe(false);
    expect(spy).not.toHaveBeenCalled();
    expect(await invalidateForEvent(qc, { event: "tool_result", data: { id: "4", name: "update_settings", status: "success", content: "{}" } })).toBe(true);
    expect(spy).toHaveBeenCalledWith();
  });

  it("transcriptToItems restores ChatItem-shaped and flat entries and reopens the live pending with call ids", () => {
    const items = transcriptToItems({
      id: "as-1",
      title: "새 대화",
      state: "pending",
      updated_at: "2026-09-08T00:00:00Z",
      turns: 1,
      usage: tokenUsage(10, 2),
      pending: { kind: "confirm", calls: [{ id: "call-9", name: "deploy_mcp_server", args: { name: "x" }, risk: "deploy" }] },
      transcript: [
        { kind: "user", text: "배포" },
        { kind: "assistant", content: "확인합니다." },
        { kind: "tool_call", id: "t1", name: "get_mcp_server", args: { id: "p" }, status: "success", content: "{}" },
        { kind: "resource", resource_kind: "mcp-server", id: "p", name: "calc", url: "/mcp-servers/p" },
        { kind: "interrupt", pending: { session_id: "as-1", kind: "confirm", action_requests: [{ name: "deploy_mcp_server", args: { name: "x" } }], review_configs: [] } },
        { kind: "done", usage: { input: 10, output: 2 }, turn: 1, tool_calls: 1 },
        { kind: "mystery" },
      ],
    });
    expect(items.map((i) => i.kind)).toEqual(["user", "assistant", "tool_call", "resource", "interrupt", "done"]);
    expect(items[2]).toEqual({ kind: "tool_call", call: { id: "t1", name: "get_mcp_server", args: { id: "p" } }, result: { id: "t1", name: "get_mcp_server", status: "success", content: "{}" } });
    expect(items[3]).toEqual({ kind: "resource", resource: { kind: "mcp-server", id: "p", name: "calc", url: "/mcp-servers/p" } });
    const interrupt = items[4];
    if (interrupt.kind !== "interrupt") throw new Error("unreachable");
    expect(interrupt.resolved).toBe(false);
    expect(interrupt.pending.action_requests[0].id).toBe("call-9");
    expect(interrupt.pending.review_configs).toEqual([]);
    expect(items[5]).toEqual({ kind: "done", usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 }, turn: 1, tool_calls: 1 });

    // 전사에 인터럽트가 없어도 살아 있는 보류면 카드를 만든다. 질문이면 도구 인자에서 질문을 읽는다
    const asked = transcriptToItems({
      id: "as-2",
      title: "새 대화",
      state: "pending",
      updated_at: "2026-09-08T00:00:00Z",
      turns: 1,
      usage: tokenUsage(0, 0),
      pending: { kind: "question", calls: [{ id: "q1", name: "ask_user", args: { question: "어디에?", options: ["a"], field: "cluster_id" }, risk: "read" }] },
      transcript: [{ kind: "user", text: "배포" }],
    });
    expect(asked.at(-1)).toMatchObject({ kind: "interrupt", resolved: false, pending: { kind: "question", question: { text: "어디에?", options: ["a"], field: "cluster_id" }, action_requests: [{ id: "q1", name: "ask_user" }] } });

    // idle 세션의 남은 인터럽트는 끝난 것이다
    const idle = transcriptToItems({ id: "as-3", title: "새 대화", state: "idle", updated_at: "", turns: 1, usage: tokenUsage(0, 0), pending: null, transcript: [{ kind: "interrupt", pending: { session_id: "as-3", action_requests: [], review_configs: [] } }] });
    expect(idle[0]).toMatchObject({ kind: "interrupt", resolved: true });
  });

  it("reopenPending stands the live pending in place of the restored card and holds its call, as the stream did (P35)", () => {
    const pending: AgentPending = { session_id: "s", action_requests: [{ name: "Divide", args: { a: 8, b: 2 } }], review_configs: [{ action_name: "Divide", allowed_decisions: ["approve", "reject"] }] };
    const restored = itemsFromTranscript([
      { kind: "user", text: "8 나누기 2" },
      { kind: "tool_call", call: { id: "c1", name: "Divide", args: { a: 8, b: 2 } }, result: null },
      { kind: "interrupt", pending: { session_id: "s", action_requests: [], review_configs: [] }, resolved: false },
    ]);
    const reopened = reopenPending(restored, pending);
    // 카드는 하나이고 살아 있는 보류를 든다. 카드가 붙든 호출은 도는 중이 아니라 결정을 기다린다
    expect(reopened.map((it) => it.kind)).toEqual(["user", "tool_call", "interrupt"]);
    expect(reopened[1]).toMatchObject({ kind: "tool_call", waiting: true, result: null });
    expect(reopened[2]).toMatchObject({ kind: "interrupt", resolved: false, pending: { action_requests: [{ name: "Divide" }] } });
    // 전사에 카드가 없으면 끝에 붙는다
    expect(reopenPending([{ kind: "user", text: "8 나누기 2" }], pending).map((it) => it.kind)).toEqual(["user", "interrupt"]);
  });

  it("done keeps the structured result only when the runtime sent one, live and restored alike", () => {
    const usage = { input_tokens: 10, output_tokens: 2, total_tokens: 12 };
    const plain = applyChatEvent([], { event: "done", data: { session_id: "s", turn: 1, usage, tool_calls: 0, text: "" } });
    expect(plain).toStrictEqual([{ kind: "done", usage, turn: 1, tool_calls: 0 }]);
    const live = applyChatEvent([], { event: "done", data: { session_id: "s", turn: 1, usage, tool_calls: 0, text: "{}", structured_response: { answer: "7" } } });
    expect(live).toStrictEqual([{ kind: "done", usage, turn: 1, tool_calls: 0, structured_response: { answer: "7" } }]);
    // 전사에서 되살린 항목도 같은 모양이다. 칸이 없던 항목에는 칸이 생기지 않는다
    expect(itemsFromTranscript([{ kind: "done", usage, turn: 1, tool_calls: 0, structured_response: { answer: "7" } }])).toStrictEqual(live);
    expect(itemsFromTranscript([{ kind: "done", usage, turn: 1, tool_calls: 0 }])).toStrictEqual(plain);
  });

  it("done 과 error 는 그 턴 시도의 turn_id 를 스트림과 되살린 전사 모두에서 든다 (P36)", () => {
    const { events, transcript } = TURN_FIXTURE;
    const restored = itemsFromTranscript(transcript);
    expect(restored.filter((it) => it.kind === "done" || it.kind === "error").map((it) => ("turn_id" in it ? it.turn_id : null))).toEqual([events.done.turn_id, events.error.turn_id]);
    // 스트림으로 접은 항목도 같은 칸을 든다
    const live = applyChatEvent(applyChatEvent([], { event: "done", data: events.done }), { event: "error", data: events.error });
    expect(live).toMatchObject([
      { kind: "done", turn: events.done.turn, turn_id: events.done.turn_id },
      { kind: "error", code: events.error.code, turn_id: events.error.turn_id },
    ]);
    // turn_id 가 없는 옛 전사에는 칸이 생기지 않는다
    expect(itemsFromTranscript([{ kind: "error", code: "turn_failed", message: "x" }])).toStrictEqual([{ kind: "error", code: "turn_failed", message: "x" }]);
  });

  it("도구 호출 카드의 인용 출처는 스트림과 되살린 전사 모두에서 남는다 (P36)", () => {
    const { events, transcript } = CITATIONS_FIXTURE;
    const restored = itemsFromTranscript(transcript);
    const card = restored.find((it) => it.kind === "tool_call");
    expect(card).toMatchObject({ kind: "tool_call", call: events.tool_call, result: events.tool_result, citations: events.citations.items });
    // 스트림으로 접은 카드도 같은 칸을 든다(결과가 오면 승인 대기 표시 waiting 이 풀린 채 붙는다)
    let live: ChatItem[] = [];
    for (const name of ["tool_call", "tool_result", "citations"] as const) live = applyChatEvent(live, { event: name, data: events[name] });
    expect(live).toStrictEqual([{ ...card, waiting: false }]);
    // 출처가 없는 옛 전사의 카드에는 칸이 생기지 않는다
    expect(itemsFromTranscript([{ kind: "tool_call", call: events.tool_call, result: null }])[0]).not.toHaveProperty("citations");
  });

  it("예비 모델 알림은 모델 호출마다 오는 같은 전환을 턴에 하나로 접고 되살린 전사도 같은 항목이다 (P36)", () => {
    const { events, transcript } = FALLBACK_FIXTURE;
    const fallback: ChatEvent = { event: "model_fallback", data: events.model_fallback };
    let live: ChatItem[] = [{ kind: "user", text: String(transcript[0].text) }];
    for (const ev of [fallback, { event: "token", data: { text: events.done.text } }, fallback, { event: "done", data: events.done }]) live = applyChatEvent(live, ev);
    expect(live).toMatchObject(itemsFromTranscript(transcript));
    expect(live[1]).toStrictEqual({ kind: "fallback", from: "gpt-4.1-mini", to: "claude-sonnet-4-5" });
    // 새 사용자 말과 승인 카드 뒤에서는 다시 알리고, 서브 에이전트의 전환은 따로 알린다
    const sub: ChatEvent = { event: "model_fallback", data: { ...events.model_fallback, agent: "researcher" } };
    const interrupt: ChatEvent = { event: "interrupt", data: normalizePending({ action_requests: [] }) };
    let next = applyChatEvent(live, sub);
    next = applyChatEvent(applyChatEvent(next, interrupt), fallback);
    next = applyChatEvent([...next, { kind: "user", text: "다음" }], fallback);
    expect(next.slice(live.length).map((it) => it.kind)).toEqual(["fallback", "interrupt", "fallback", "user", "fallback"]);
  });

  it("greeting stands before this turn's user item, once, live and restored alike (D65)", () => {
    const greeting = { event: "greeting" as const, data: { session_id: "s", text: "어서 오세요" } };
    const live = applyChatEvent([{ kind: "user", text: "안녕" }], greeting);
    expect(live).toStrictEqual([{ kind: "assistant", text: "어서 오세요", streaming: false, greeting: true }, { kind: "user", text: "안녕" }]);
    // 이미 있으면 다시 세우지 않고, 사용자 항목이 없으면 끝에 선다
    expect(applyChatEvent(live, greeting)).toBe(live);
    expect(applyChatEvent([], greeting)).toStrictEqual([live[0]]);
    // 전사에서 되살린 항목도 같은 표시를 든다. 표시가 없던 말풍선에는 칸이 생기지 않는다
    expect(itemsFromTranscript([{ kind: "assistant", text: "어서 오세요", streaming: false, greeting: true }, { kind: "user", text: "안녕" }])).toStrictEqual(live);
    expect(itemsFromTranscript([{ kind: "assistant", text: "답", streaming: false }])).toStrictEqual([{ kind: "assistant", text: "답", streaming: false }]);
  });

  it("splitCodeFences splits ``` fences and treats an unclosed fence as code", () => {
    expect(splitCodeFences("a\n```yaml\nx: 1\n```\nb")).toEqual([
      { kind: "text", text: "a" },
      { kind: "code", text: "x: 1", lang: "yaml" },
      { kind: "text", text: "b" },
    ]);
    expect(splitCodeFences("```\npartial")).toEqual([{ kind: "code", text: "partial", lang: undefined }]);
    expect(splitCodeFences("")).toEqual([{ kind: "text", text: "" }]);
    expect(splitCodeFences("plain\ntext")).toEqual([{ kind: "text", text: "plain\ntext" }]);
  });

  it("viaFilterValue turns the assistant-only check into the assistant/ prefix", () => {
    expect(viaFilterValue("", false)).toBe("");
    expect(viaFilterValue("", true)).toBe("assistant/");
    expect(viaFilterValue("as-1", true)).toBe("assistant/as-1");
    expect(viaFilterValue("assistant/as-1", true)).toBe("assistant/as-1");
    expect(viaFilterValue(" other/x ", false)).toBe("other/x");
  });
});

describe("guardrail item", () => {
  it("keeps the runtime's rule and position both live and on reload", () => {
    // 런타임은 규칙 이름을 rule 로 싣는다(runtime/guards.py). 전사도 같은 이름으로 남는다(runtime/transcript.py). 안내문이 없으면
    // 항목에도 없고 그릴 때 사전의 기본 문장을 쓴다
    const live = applyChatEvent([], { event: "guardrail", data: { position: "input", rule: "deny_pattern", action: "flag" } });
    expect(live).toEqual([{ kind: "guardrail", action: "flag", position: "input", rule: "deny_pattern" }]);
    const reloaded = itemsFromTranscript([{ kind: "guardrail", position: "input", rule: "deny_pattern", action: "flag", message: "막혔습니다." }]);
    expect(reloaded).toEqual([{ kind: "guardrail", action: "flag", message: "막혔습니다.", position: "input", rule: "deny_pattern" }]);
  });
});

describe("transcriptToItems pending merge", () => {
  it("uses the live pending wholesale when the tool names differ from the last interrupt in the transcript", () => {
    const items = transcriptToItems({
      id: "as-3",
      title: "새 대화",
      state: "pending",
      updated_at: "2026-09-08T00:00:00Z",
      turns: 2,
      usage: tokenUsage(0, 0),
      pending: { kind: "confirm", calls: [{ id: "call-new", name: "deploy_agent", args: { name: "b" }, risk: "deploy" }] },
      transcript: [
        { kind: "user", text: "배포" },
        { kind: "interrupt", pending: { session_id: "as-3", kind: "confirm", action_requests: [{ name: "deploy_mcp_server", args: { name: "a" } }], review_configs: [] }, resolved: true },
      ],
    });
    const interrupt = items.at(-1);
    if (!interrupt || interrupt.kind !== "interrupt") throw new Error("unreachable");
    expect(interrupt.resolved).toBe(false);
    expect(interrupt.pending.action_requests).toEqual([{ id: "call-new", name: "deploy_agent", args: { name: "b" } }]);
  });
});
