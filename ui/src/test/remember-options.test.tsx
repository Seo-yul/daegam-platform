/**
 * 기억할 수 있는 선택 (P43 5절). 스트림의 interrupt 에는 없으므로 채팅이 `GET /pending` 을 따로 읽어 카드에 입히고, 카드가 고른
 * 기억하기를 결정에 싣는다. 런타임이 tool_safety 를 알리지 않으면 따로 읽지 않는다.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AgentPending } from "@/api/types";
import { withRememberOptions, type ChatItem } from "@/components/chat";
import { RUNTIME_TOOL_SAFETY } from "@/lib/runtimeFeatures";

import { openAgentChat, sendMessage } from "./chat";
import { HttpResponse, agentSessionState, agentState, agentWithRuntimeFeatures, http, lastRequest, received, server } from "./msw";

const OFFERED = { remember_options: [["approve", "reject"]], remember_days: 30 } satisfies Partial<AgentPending>;

function card(names: string[], resolved = false): ChatItem {
  return { kind: "interrupt", resolved, pending: { session_id: "s1", action_requests: names.map((name) => ({ name, args: {} })), review_configs: [] } };
}

describe("기억할 수 있는 선택", () => {
  it("마지막 열린 카드에만, 요청이 같을 때만 입힌다", () => {
    const pending: AgentPending = { session_id: "s1", action_requests: [{ name: "Divide", args: {} }], review_configs: [], ...OFFERED };
    const [old, open] = withRememberOptions([card(["Divide"], true), card(["Divide"])], pending);
    expect(old.kind === "interrupt" && old.pending.remember_options).toBeFalsy();
    expect(open.kind === "interrupt" && open.pending.remember_options).toEqual([["approve", "reject"]]);
    const other = [card(["Add"])];
    expect(withRememberOptions(other, pending)).toBe(other);
    // 그사이 같은 Tool 의 다른 보류(외부 내용 뒤)가 섰으면 입히지 않는다
    const later = { ...pending, action_requests: [{ name: "Divide", args: {}, reason: "outside" as const }] };
    const rule = [card(["Divide"])];
    expect(withRememberOptions(rule, later)).toBe(rule);
  });

  it("tool_safety 를 알린 런타임의 승인 카드는 기억하기를 보이고 결정에 싣는다", async () => {
    agentState.items = [agentWithRuntimeFeatures(RUNTIME_TOOL_SAFETY)];
    server.use(
      http.get("/agents/:id/sessions/:sid/pending", ({ params }) => HttpResponse.json({ ...agentSessionState.pending[String(params.sid)], ...OFFERED })),
    );
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "8 나누기 2");
    await user.click(await screen.findByTestId("decision-remember-approve"));
    await waitFor(() => expect(lastRequest("POST", "/agents/ag-support/sessions/")?.body).toMatchObject({ decisions: [{ type: "approve", remember: "approve" }] }));
  });

  it("tool_safety 를 모르는 런타임이면 따로 읽지 않는다", async () => {
    const user = userEvent.setup();
    await openAgentChat();
    await sendMessage(user, "8 나누기 2");
    await screen.findByTestId("decision-approve");
    // 다시 읽기는 스트림이 끝난 뒤에 나가므로 보내기 단추가 "응답 중…" 에서 돌아올 때까지 기다린 뒤에 본다
    await waitFor(() => expect(screen.getByTestId("chat-send")).toHaveTextContent("보내기"));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("decision-remember-approve")).not.toBeInTheDocument();
    expect(received.some((r) => r.method === "GET" && r.path.endsWith("/pending"))).toBe(false);
  });
});
