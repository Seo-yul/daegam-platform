/**
 * 개인 MCP 토큰 (P32 6절): /me/tokens 와 관리자의 /users/{id}/tokens. 모양은 platform/daegam/api/personal_tokens.py 다.
 *
 * 발급은 local 목의 관리자 비밀번호(`LOCAL_ADMIN_PASSWORD`)만 받고 틀리면 403 invalid_credentials 다. 401 이면 화면의
 * 401 훅이 세션을 지우므로 서버가 403 을 낸다. `localLoginState.locked` 면 로그인처럼 429 login_locked 다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { IssuedPersonalToken, MeOut, PersonalToken, PersonalTokenIssueIn, RevokeReason } from "@/api/types";

import { LOCAL_ADMIN_PASSWORD, localLoginState, lockedResponse, meDeveloper } from "./auth";
import { NOW, apiError, http, record } from "./core";

/** 발급 응답의 평문. 목록 어디에도 다시 나오지 않아야 한다. */
export const PAT_VALUE = "eyJhbGciOiJFUzI1NiJ9.personal.token";

/**
 * local 모드에서 default 와 acme 의 멤버인 사람. 목의 로그인은 admin 하나라 테스트가 GET /me 를 이것으로 덮어
 * 발급 폼의 테넌트와 연결 카드의 소속 판정을 본다. platform-admin 이 아니므로 `*` 가 없다.
 */
export const meTokenHolder: MeOut = {
  ...meDeveloper,
  sub: "u-admin",
  memberships: [
    { tenant: "default", role: "developer", source: "manual" },
    { tenant: "acme", role: "viewer", source: "manual" },
  ],
  scopes_by_tenant: { ...meDeveloper.scopes_by_tenant, acme: ["mcp:read", "gateway:read"] },
};

export function personalTokenFixture(over: Partial<PersonalToken> = {}): PersonalToken {
  return {
    token_id: "pt-laptop",
    label: "laptop",
    tenant: "default",
    group: "daegam-default",
    created_at: NOW,
    expires_at: "2026-10-02T10:00:00+00:00",
    ready_at: null,
    revoked_at: null,
    revoke_reason: null,
    status: "active",
    ...over,
  };
}

interface TokenState {
  /** GET /me/tokens */
  mine: PersonalToken[];
  /** GET /users/{id}/tokens. 없는 사용자는 빈 목록이다 */
  byUser: Record<string, PersonalToken[]>;
  /** 발급을 받는 테넌트. 그 밖은 403 not_a_member */
  tenants: string[];
  /** 발급 응답의 ready_at. 따뜻한 슬롯이 없을 때를 흉내 낸다 */
  readyAt: string | null;
}

export const tokenState: TokenState = { mine: [], byUser: {}, tenants: [], readyAt: null };

export function resetTokenState(): void {
  tokenState.mine = [personalTokenFixture(), personalTokenFixture({ token_id: "pt-old", label: "old", status: "revoked", revoked_at: NOW, revoke_reason: "password" })];
  tokenState.byUser = {};
  tokenState.tenants = ["default", "acme"];
  tokenState.readyAt = null;
}
resetTokenState();

function issue(body: PersonalTokenIssueIn): IssuedPersonalToken {
  const ready = tokenState.readyAt;
  const token = personalTokenFixture({
    token_id: `pt-${tokenState.mine.length + 1}`,
    label: body.label,
    tenant: body.tenant,
    group: `daegam-${body.tenant}`,
    expires_at: body.expires_at,
    ready_at: ready,
    status: ready ? "pending" : "active",
  });
  tokenState.mine = [token, ...tokenState.mine];
  return { ...token, token: PAT_VALUE };
}

/** 본인·관리자 회수. 남의 토큰이나 없는 토큰은 404 다. */
function revoke(list: PersonalToken[], tokenId: string, reason: RevokeReason): Response {
  const token = list.find((t) => t.token_id === tokenId);
  if (!token) return apiError(404, "not_found", `personal token ${tokenId} not found`);
  Object.assign(token, { status: "revoked", revoked_at: NOW, revoke_reason: reason });
  return HttpResponse.json({ token_id: tokenId, revoked: true });
}

export const tokenHandlers: HttpHandler[] = [
  http.get("/me/tokens", async ({ request }) => {
    await record(request);
    return HttpResponse.json(tokenState.mine);
  }),
  http.post("/me/tokens", async ({ request }) => {
    const body = (await record(request)).body as PersonalTokenIssueIn;
    if (localLoginState.locked) return lockedResponse();
    if (body.password !== LOCAL_ADMIN_PASSWORD) return apiError(403, "invalid_credentials", "password is wrong");
    if (!tokenState.tenants.includes(body.tenant)) return apiError(403, "not_a_member", `not a member of tenant ${body.tenant}`, { tenant: body.tenant });
    return HttpResponse.json(issue(body), { status: 201 });
  }),
  http.delete("/me/tokens/:tokenId", async ({ request, params }) => {
    await record(request);
    return revoke(tokenState.mine, String(params.tokenId), "user");
  }),
  http.get("/users/:id/tokens", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json(tokenState.byUser[String(params.id)] ?? []);
  }),
  http.delete("/users/:id/tokens/:tokenId", async ({ request, params }) => {
    await record(request);
    return revoke(tokenState.byUser[String(params.id)] ?? [], String(params.tokenId), "admin");
  }),
];
