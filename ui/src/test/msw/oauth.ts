/**
 * MCP 클라이언트 연결 동의 (P33 7절): /oauth/consent/{inspect,approve,deny}. 모양은 platform/daegam/api/oauth.py 다.
 *
 * 목은 핸들 `CONSENT_HANDLE` 하나만 알고 다른 핸들은 서명이 틀린 것처럼 404 다. 허용·거절에 한 번 답하면 그 뒤로는 409
 * request_used 다. `consentState.approveErrors` 는 허용이 차례로 낼 오류 코드다(403 reauth_required 뒤 성공 같은 흐름).
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { ConsentView } from "@/api/types";

import { apiError, http, record } from "./core";

export const CONSENT_HANDLE = "v1.consent-body.consent-mac";
export const CONSENT_ISS = "https://daegam.example.com";
export const APPROVED_REDIRECT = `http://localhost:53112/callback?code=code-1&state=st-1&iss=${encodeURIComponent(CONSENT_ISS)}`;
export const DENIED_REDIRECT = `http://localhost:53112/callback?error=access_denied&state=st-1&iss=${encodeURIComponent(CONSENT_ISS)}`;

/** DCR 클라이언트가 소속 테넌트의 Gateway 에 연결하려는 요청. */
export function consentViewFixture(over: Partial<ConsentView> = {}): ConsentView {
  return {
    client: { name: "Script Client", ref: "dcr:0123456789abcdef", kind: "dcr", host: null, name_collides: false },
    redirect: { host: "localhost", port: 53112 },
    member: true,
    resource: { url: "https://gw.acme.example/mcp", kind: "gateway", name: "calc-gw", display_name: "계산 Gateway", tenant: "acme" },
    scope: ["mcp", "offline_access"],
    expires_at: "2026-09-28T10:10:00+00:00",
    access_token_seconds: 900,
    grant_days: 30,
    ...over,
  };
}

const STATUS: Record<string, number> = {
  reauth_required: 403,
  not_a_member: 403,
  consent_limit: 429,
  request_used: 409,
  request_expired: 410,
};

interface ConsentState {
  view: ConsentView;
  /** inspect 가 낼 오류 코드. null 이면 view 를 준다 */
  inspectError: string | null;
  approveErrors: string[];
  answered: boolean;
}

export const consentState: ConsentState = { view: consentViewFixture(), inspectError: null, approveErrors: [], answered: false };

export function resetConsentState(): void {
  consentState.view = consentViewFixture();
  consentState.inspectError = null;
  consentState.approveErrors = [];
  consentState.answered = false;
}

function failure(code: string): Response {
  return apiError(STATUS[code] ?? 400, code, code);
}

/** 핸들 확인. 모르는 핸들은 404, 답한 핸들은 409 다. */
async function opened(request: Request): Promise<Response | null> {
  const body = (await record(request)).body as { request: string };
  if (body.request !== CONSENT_HANDLE) return apiError(404, "not_found", "the consent request is not valid");
  return consentState.answered ? failure("request_used") : null;
}

function answer(redirectTo: string): Response {
  consentState.answered = true;
  return HttpResponse.json({ redirect_to: redirectTo });
}

export const oauthHandlers: HttpHandler[] = [
  http.post("/oauth/consent/inspect", async ({ request }) => {
    const refused = await opened(request);
    if (refused) return refused;
    if (consentState.inspectError) return failure(consentState.inspectError);
    return HttpResponse.json(consentState.view);
  }),
  http.post("/oauth/consent/approve", async ({ request }) => {
    const refused = await opened(request);
    if (refused) return refused;
    const code = consentState.approveErrors.shift();
    return code ? failure(code) : answer(APPROVED_REDIRECT);
  }),
  http.post("/oauth/consent/deny", async ({ request }) => {
    return (await opened(request)) ?? answer(DENIED_REDIRECT);
  }),
];
