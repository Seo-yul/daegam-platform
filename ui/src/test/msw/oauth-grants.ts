/**
 * MCP 클라이언트의 OAuth 연결 (P33 12절): /me/oauth-grants 와 관리자의 /users/{id}/oauth-grants. 모양은
 * platform/daegam/api/oauth_grants.py 다. 서버는 OAuth 가 꺼지면 404 이지만 목은 늘 답한다. 화면이 꺼진 때 부르지 않는지는
 * 테스트가 요청 기록으로 본다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { GrantRevokeReason, OAuthGrant } from "@/api/types";

import { NOW, apiError, http, record } from "./core";

/** Claude Code 가 CIMD 로 default 의 Gateway 에 맺은 연결. */
export function oauthGrantFixture(over: Partial<OAuthGrant> = {}): OAuthGrant {
  return {
    grant_id: "og-claude",
    client_name: "Claude Code",
    client_ref: "https://claude.ai/oauth/claude-code-client-metadata",
    client_kind: "cimd",
    client_host: "claude.ai",
    tenant: "default",
    resource: "https://gw-support.apps.example/mcp",
    resource_kind: "gateway",
    resource_id: "gw-support",
    scope: ["mcp", "offline_access"],
    created_at: NOW,
    last_refresh_at: NOW,
    expires_at: "2026-10-02T10:00:00+00:00",
    revoked_at: null,
    revoked_reason: null,
    status: "active",
    ...over,
  };
}

interface GrantState {
  /** GET /me/oauth-grants */
  mine: OAuthGrant[];
  /** GET /users/{id}/oauth-grants. 없는 사용자는 빈 목록이다 */
  byUser: Record<string, OAuthGrant[]>;
}

export const grantState: GrantState = { mine: [], byUser: {} };

export function resetGrantState(): void {
  grantState.mine = [oauthGrantFixture()];
  grantState.byUser = {};
}
resetGrantState();

/** 본인·관리자 끊기. 남의 연결이나 없는 연결은 404 다. */
function disconnect(list: OAuthGrant[], grantId: string, reason: GrantRevokeReason): Response {
  const grant = list.find((g) => g.grant_id === grantId);
  if (!grant) return apiError(404, "not_found", `oauth grant ${grantId} not found`);
  Object.assign(grant, { status: "revoked", revoked_at: NOW, revoked_reason: reason });
  return HttpResponse.json({ grant_id: grantId, revoked: true });
}

export const grantHandlers: HttpHandler[] = [
  http.get("/me/oauth-grants", async ({ request }) => {
    await record(request);
    return HttpResponse.json(grantState.mine);
  }),
  http.delete("/me/oauth-grants/:grantId", async ({ request, params }) => {
    await record(request);
    return disconnect(grantState.mine, String(params.grantId), "user");
  }),
  http.get("/users/:id/oauth-grants", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json(grantState.byUser[String(params.id)] ?? []);
  }),
  http.delete("/users/:id/oauth-grants/:grantId", async ({ request, params }) => {
    await record(request);
    return disconnect(grantState.byUser[String(params.id)] ?? [], String(params.grantId), "admin");
  }),
];
