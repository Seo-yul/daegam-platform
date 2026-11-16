/**
 * 런타임이 쓴 모양을 좁히는 접기 (P73 2절). 서버는 이 모양을 자유 객체로 건네므로 null 칸, 모르는 전사 종류,
 * 빠진 봉투가 그대로 온다. 접기는 오류 없이 읽고 모르는 종류는 건너뛴다.
 */
import { describe, expect, it } from "vitest";

import { UNNAMED_REQUEST, agentTranscript, normalizePending, normalizeProposal, sessionMessages, transcriptItems } from "@/api/runtimeShapes";
import { itemsFromTranscript } from "@/lib/assistant";

import { proposalBlockFixture } from "./msw";

describe("런타임이 쓴 모양", () => {
  it("null 칸과 모르는 종류가 든 전사를 오류 없이 접고 모르는 종류는 건너뛴다", () => {
    const raw = [
      { kind: "user", text: null },
      { kind: "error", code: null, message: "boom", turn_id: null },
      { kind: "fallback", from: null, to: null, agent: null },
      { kind: "tool_call", id: "t1", name: null, args: null, status: null },
      { kind: "rewind", turn: 2 },
      { kind: 3 },
      null,
      "text",
    ];
    expect(transcriptItems(raw).map((it) => it.kind)).toEqual(["user", "error", "fallback", "tool_call", "rewind"]);
    expect(itemsFromTranscript(raw).map((it) => it.kind)).toEqual(["user", "error", "fallback", "tool_call"]);
  });

  it("전사가 null 이거나 없으면 빈 목록이다", () => {
    expect(itemsFromTranscript(null)).toEqual([]);
    expect(itemsFromTranscript(undefined)).toEqual([]);
    expect(agentTranscript(null)).toBeUndefined();
    expect(agentTranscript({ items: null, limit: null })).toEqual({ items: [] });
    expect(agentTranscript({ items: [{ kind: "user", text: "hi" }], limit: 500 })).toEqual({ items: [{ kind: "user", text: "hi" }], limit: 500 });
  });

  it("보류 승인 요청의 null 칸은 없는 것으로 보고, 모양이 틀린 요청도 결정 수가 맞게 자리 표시로 남긴다", () => {
    const pending = normalizePending({
      session_id: null,
      action_requests: [{ name: "Divide", args: null, description: null, files: null }, { name: null }, null],
      review_configs: [{ action_name: "Divide", allowed_decisions: ["approve", "later", null] }, { action_name: null }],
      kind: "other",
      question: null,
    });
    expect(pending).toEqual({
      session_id: "",
      action_requests: [
        { name: "Divide", args: {} },
        { name: UNNAMED_REQUEST, args: {} },
        { name: UNNAMED_REQUEST, args: {} },
      ],
      review_configs: [{ action_name: "Divide", allowed_decisions: ["approve"] }],
      question: null,
    });
    expect(normalizePending(null)).toEqual({ session_id: "", action_requests: [], review_configs: [] });
  });

  it("review_configs 가 없으면 요청마다 승인·거절을 허락한다", () => {
    expect(normalizePending({ action_requests: [{ name: "Add", args: { a: 1 }, id: "c1" }], kind: "confirm" })).toEqual({
      session_id: "",
      action_requests: [{ name: "Add", args: { a: 1 }, id: "c1" }],
      review_configs: [{ action_name: "Add", allowed_decisions: ["approve", "reject"] }],
      kind: "confirm",
    });
  });

  it("메시지 요약의 null 칸은 빈 글자이거나 없다", () => {
    expect(sessionMessages([{ role: "tool", text: null, tool: null, status: "error", tool_calls: null }, { role: "ai", text: "hi", tool_calls: ["Add", null] }, 1])).toEqual([
      { role: "tool", text: "", status: "error" },
      { role: "ai", text: "hi", tool_calls: ["Add"] },
    ]);
    expect(sessionMessages(null)).toEqual([]);
  });
  it("요청의 이유와 플랫폼이 알린 기억할 수 있는 선택을 남기고 모르는 값은 버린다 (P43)", () => {
    const pending = normalizePending({
      session_id: "s1",
      action_requests: [
        { name: "Divide", args: {}, reason: "outside" },
        { name: "Add", args: {}, reason: "later-kind" },
      ],
      review_configs: [],
      remember_options: [["approve", "reject", "edit"], null],
      remember_days: 30,
    });
    expect(pending.action_requests.map((a) => a.reason)).toEqual(["outside", undefined]);
    expect(pending.remember_options).toEqual([["approve", "reject"], []]);
    expect(pending.remember_days).toBe(30);
    // 날 수가 없으면 고를 것도 없다
    expect(normalizePending({ action_requests: [], remember_options: [[]] }).remember_options).toBeUndefined();
  });

  it("청지기의 Agent 정의 제안 덩어리를 요청에 남기고, null 칸과 모르는 줄은 버리며, 제안 id 가 없으면 제안이 아니다 (P48)", () => {
    const block = proposalBlockFixture();
    const pending = normalizePending({ session_id: "as-1", action_requests: [{ name: "update_agent", args: {}, proposal: block }, { name: "deploy_agent", args: {} }] });
    expect(pending.action_requests[0].proposal).toEqual(block);
    expect(pending.action_requests[1].proposal).toBeUndefined();
    const loose = normalizeProposal({
      proposal_id: "p-2",
      kind: "create",
      agent: { id: null, name: "petstore", display_name: null },
      base_revision: null,
      deployed_revision: null,
      changes: { nodes: { added: [{ id: "agent-main", type: "agent", name: null }], changed: null }, edges: null, service: ["budget", 3], texts: [{ node_id: "agent-main", field: "system_prompt", lines: [{ op: "+", text: "a" }, { op: "=", text: "b" }], cut: null }] },
      attention: [{ code: "model_changed", node_id: "agent-main", agent: "main", provider: "openai-main", model: null, source: "not-a-ref" }],
      warnings: [{ code: "provider_no_key", node_id: null, message: null }],
    });
    expect(loose?.changes.nodes).toEqual({ added: [{ id: "agent-main", type: "agent" }], changed: [], removed: [] });
    expect(loose?.changes.edges).toEqual({ added: [], removed: [] });
    expect(loose?.changes.service).toEqual(["budget"]);
    expect(loose?.changes.texts).toEqual([{ node_id: "agent-main", field: "system_prompt", lines: [{ op: "+", text: "a" }], cut: false }]);
    expect(loose?.attention).toEqual([{ code: "model_changed", node_id: "agent-main", agent: "main", provider: "openai-main", model: null }]);
    expect(loose?.warnings).toEqual([{ code: "provider_no_key", node_id: null, message: null }]);
    expect(normalizeProposal({ kind: "create" })).toBeUndefined();
    expect(normalizeProposal({ proposal_id: "p-3", kind: "rename" })).toBeUndefined();
  });
});
