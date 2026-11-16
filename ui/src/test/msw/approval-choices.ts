/**
 * 기억한 승인 선택 (P43 5절): /me/approval-choices. 모양은 platform/daegam/api/approval_choices.py 다. 기본은 default 의
 * ag-support 에서 기억한 승인 하나와 지운 Agent 의 거부 하나다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { ApprovalChoice } from "@/api/types";

import { NOW, apiError, http, record } from "./core";

export function approvalChoiceFixture(over: Partial<ApprovalChoice> = {}): ApprovalChoice {
  return {
    id: "ac-divide",
    tenant: "default",
    agent_id: "ag-support",
    agent_name: "support",
    tool: "Divide",
    choice: "approve",
    created_at: NOW,
    expires_at: "2026-10-02T10:00:00+00:00",
    ...over,
  };
}

export const approvalChoiceState: { mine: ApprovalChoice[] } = { mine: [] };

export function resetApprovalChoiceState(): void {
  approvalChoiceState.mine = [
    approvalChoiceFixture(),
    approvalChoiceFixture({ id: "ac-gone", agent_id: "ag-gone", agent_name: null, tool: "delete_rows", choice: "reject" }),
  ];
}
resetApprovalChoiceState();

export const approvalChoiceHandlers: HttpHandler[] = [
  http.get("/me/approval-choices", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ choices: approvalChoiceState.mine });
  }),
  http.delete("/me/approval-choices/:choiceId", async ({ request, params }) => {
    await record(request);
    const id = String(params.choiceId);
    if (!approvalChoiceState.mine.some((c) => c.id === id)) return apiError(404, "not_found", `approval choice '${id}' not found`);
    approvalChoiceState.mine = approvalChoiceState.mine.filter((c) => c.id !== id);
    return new HttpResponse(null, { status: 204 });
  }),
];
