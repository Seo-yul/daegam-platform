/** 자동 실행의 화면 규칙 (lib/runs.ts). 쓰기 단추를 세우는 권한과 까닭 칸의 말이다. */
import { describe, expect, it } from "vitest";

import { dictionary } from "@/i18n";
import { canWriteRuns, runReasonText, runSourceText } from "@/lib/runs";

import { meAdmin, meDeveloper } from "./msw";

const ko = dictionary("ko");

describe("canWriteRuns", () => {
  it("테넌트 소속이 없는 platform-admin 은 header 모드에서만 쓴다", () => {
    // 서버는 묶을 때 소속 줄을 본다(rbac.holds_tenant). `*` 줄만으로는 403 이다
    expect(canWriteRuns(meAdmin, "default", "local")).toBe(false);
    expect(canWriteRuns(meAdmin, "default", "oidc")).toBe(false);
    expect(canWriteRuns(meAdmin, "default", "header")).toBe(true);
    const member = { ...meAdmin, memberships: [...meAdmin.memberships, { tenant: "default", role: "developer", source: "manual" }] };
    expect(canWriteRuns(member, "default", "local")).toBe(true);
  });

  it("소속이 있어도 agent:write 와 agent:chat 이 둘 다 있어야 한다", () => {
    const writer = { ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:write", "agent:chat"] } };
    expect(canWriteRuns(writer, "default", "oidc")).toBe(true);
    const reader = { ...meDeveloper, scopes_by_tenant: { default: ["agent:read", "agent:chat"] } };
    expect(canWriteRuns(reader, "default", "oidc")).toBe(false);
    expect(canWriteRuns(null, "default", "header")).toBe(false);
  });
});

describe("runReasonText", () => {
  it.each(["stopped", "approval_deadline", "source_deleted", "agent_deleted", "session_closed", null])("%s 는 상태가 말하므로 비운다", (reason) => {
    expect(runReasonText(ko, reason, 7)).toBeNull();
  });

  it("런타임의 5xx 는 응답하지 않은 것이고 모르는 코드는 코드 그대로다", () => {
    expect(runReasonText(ko, "http_503", 7)).toEqual({ text: ko.runs.reason.unavailable, code: false });
    expect(runReasonText(ko, "something_new", 7)).toEqual({ text: "something_new", code: true });
  });
});

describe("runSourceText", () => {
  it("아는 출처는 사전의 말이고 뒤 판이 더한 출처는 코드 그대로다 (P46 A0)", () => {
    expect(runSourceText(ko, "schedule")).toBe(ko.runs.schedules.title);
    expect(runSourceText(ko, "webhook")).toBe(ko.runs.source.webhook);
    expect(runSourceText(ko, "manual")).toBe(ko.runs.source.manual);
    expect(runSourceText(ko, "agent")).toBe("Agent 호출");
    expect(runSourceText(ko, "later_source")).toBe("later_source");
    // 사전의 상속 키는 출처가 아니다
    expect(runSourceText(ko, "toString")).toBe("toString");
  });
});
