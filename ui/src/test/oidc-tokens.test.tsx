/**
 * oidc 모드의 오래 가는 자격 (P53b 6.3·6.4·6.5·6.7절, F0 3번).
 *
 * 조용한 갱신은 새 토큰으로 /api/me 를 부르고(플랫폼이 사람의 마지막 IdP 확인을 거기서 배운다), /callback 은 한 페이지 로드에
 * 한 번만 처리한다. 토큰 폼과 동의 화면은 IdP 로그인이 10분보다 오래면 입력을 남기고 max_age 로 다시 로그인하러 떠나고, 돌아온
 * 로그인이 여전히 오래면 prompt=login 과 max_age 로 한 번 더 떠나며, 그래도 안 되면 멈춰 알린다. 돌아오면 입력과 핸들이
 * 그대로다. 로그인 라이브러리는 test/stubs/oidc-client-ts.ts 의 대역이다.
 */
import { StrictMode } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type MockInstance, vi } from "vitest";

import { TestApp } from "@/App";
import type { ServiceAccount, ServiceAccounts } from "@/api/types";
import { dictionary } from "@/i18n";
import { getAuthState, resetAuthState } from "@/lib/auth";
import { leave } from "@/lib/leave";
import { pausedReasonText, runReasonText } from "@/lib/runs";
import {
  CONSENT_HANDLE,
  HttpResponse,
  NOW,
  PAT_VALUE,
  apiError,
  http,
  lastRequest,
  meAdmin,
  meTokenHolder,
  personalTokenFixture,
  record,
  server,
  tokenState,
} from "./msw";
import { submitConfirm } from "./confirm";
import { oidcConfig, signedInUser } from "./oidcSignIn";
import { oidcStub } from "./stubs/oidc-client-ts";

const ko = dictionary("ko");
const OIDC = oidcConfig("idp");
const fresh = () => signedInUser(30);
const stale = () => signedInUser(3600);
/** 다시 로그인하는 max_age. 창(600초)에서 여유 60초를 뺀다. */
const MAX_AGE = 540;

/** 받은 요청을 기록하고 답하는 핸들러. 기록은 lastRequest 가 읽는다. */
function replying(answer: () => Response) {
  return async ({ request }: { request: Request }) => {
    await record(request);
    return answer();
  };
}

function oidcPlatform(me = meTokenHolder) {
  server.use(
    http.get("/auth/config", replying(() => HttpResponse.json(OIDC))),
    http.get("/me", replying(() => HttpResponse.json(me))),
  );
}

/** 페이지를 새로 읽은 것처럼 화면과 인증 상태를 지운다. 이 탭의 sessionStorage 는 남는다. */
function reload(): void {
  cleanup();
  resetAuthState();
}

describe("oidc sign-in plumbing", () => {
  it("a silent renewal calls GET /api/me with the new token", async () => {
    oidcPlatform();
    oidcStub.user = fresh();
    render(<TestApp initialPath="/clusters" />);
    await waitFor(() => expect(lastRequest("GET", "/me")?.headers.authorization).toBe("Bearer tok-1"));
    expect(oidcStub.userLoaded).toHaveLength(1);
    oidcStub.userLoaded[0]({ access_token: "tok-2" });
    await waitFor(() => expect(lastRequest("GET", "/me")?.headers.authorization).toBe("Bearer tok-2"));
    expect(getAuthState().token).toBe("tok-2");
  });

  it("/callback exchanges the code once per page load, even under StrictMode", async () => {
    oidcPlatform();
    render(
      <StrictMode>
        <TestApp initialPath="/callback?code=abc&state=xyz" />
      </StrictMode>,
    );
    expect(await screen.findByTestId("clusters-page")).toBeInTheDocument();
    expect(oidcStub.callbackCalls).toBe(1);
  });
});

describe("my MCP tokens in oidc mode", () => {
  it("says the data plane still takes IdP tokens and how long a sign-in lasts, and asks no password", async () => {
    oidcPlatform();
    oidcStub.user = fresh();
    tokenState.mine = [personalTokenFixture({ token_id: "pt-u", status: "revoked", revoked_at: NOW, revoke_reason: "unconfirmed" })];
    render(<TestApp initialPath="/account/tokens" />);
    expect(await screen.findByTestId("tokens-idp-issuer")).toHaveTextContent(ko.account.tokens.idpIssuer);
    expect(screen.getByTestId("tokens-confirm-hint")).toHaveTextContent(ko.account.tokens.confirmHint(7));
    const form = await screen.findByTestId("issue-token-form");
    expect(within(form).queryByTestId("it-password")).not.toBeInTheDocument();
    expect(within(form).getByTestId("it-reauth-hint")).toHaveTextContent(ko.account.issue.reauthHint);
    expect(await screen.findByTestId("pat-status-pt-u")).toHaveTextContent("회수됨오래 로그인하지 않음");
    // 잠든 인가 서버의 연결도 보고 끊을 수 있다
    expect(screen.getByTestId("grants-card")).toBeInTheDocument();
  });

  it("signs in again with max_age, then prompt=login with max_age, then stops; the input survives each trip", async () => {
    oidcPlatform();
    oidcStub.user = stale();
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/tokens" />);
    const form = await screen.findByTestId("issue-token-form");
    await user.type(within(form).getByTestId("it-label"), "ci runner");
    await user.click(within(form).getByTestId("it-submit"));
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
    expect(oidcStub.signinRedirectCalls[0]).toEqual({ state: { returnTo: "/account/tokens", reauth: 1 }, max_age: MAX_AGE });
    expect(lastRequest("POST", "/me/tokens")).toBeUndefined();

    // 첫 시도에서 돌아온 ID 토큰의 auth_time 이 여전히 오래다. 콜백이 둘째 시도로 떠난다
    reload();
    oidcStub.callbackState = { returnTo: "/account/tokens", reauth: 1 };
    oidcStub.callbackUser = stale();
    render(<TestApp initialPath="/callback?code=c1&state=s1" />);
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(2));
    expect(oidcStub.signinRedirectCalls[1]).toEqual({
      state: { returnTo: "/account/tokens", reauth: 2 },
      max_age: MAX_AGE,
      prompt: "login",
    });
    expect(screen.getByTestId("callback-page")).toBeInTheDocument();

    // 둘째 시도도 새 로그인을 받지 못했다. 더 떠나지 않고 폼으로 돌아와 멈춘 까닭을 보인다. 입력은 그대로다
    reload();
    oidcStub.callbackState = { returnTo: "/account/tokens", reauth: 2 };
    render(<TestApp initialPath="/callback?code=c2&state=s2" />);
    const back = await screen.findByTestId("issue-token-form");
    expect(within(back).getByTestId("it-label")).toHaveValue("ci runner");
    expect(within(back).getByTestId("it-reauth-failed")).toHaveTextContent(ko.auth.reauthFailed);
    expect(oidcStub.signinRedirectCalls).toHaveLength(2);
  });

  it("sends the ID token beside the access token once the sign-in is recent", async () => {
    oidcPlatform();
    oidcStub.user = fresh();
    server.use(
      http.post(
        "/me/tokens",
        replying(() => HttpResponse.json({ ...personalTokenFixture({ token_id: "pt-new", label: "ci" }), token: PAT_VALUE }, { status: 201 })),
      ),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/tokens" />);
    const form = await screen.findByTestId("issue-token-form");
    await user.type(within(form).getByTestId("it-label"), "ci");
    await user.click(within(form).getByTestId("it-submit"));
    await waitFor(() => expect(lastRequest("POST", "/me/tokens")).toBeDefined());
    const sent = lastRequest("POST", "/me/tokens")!;
    expect(sent.headers["x-daegam-id-token"]).toBe(oidcStub.user?.id_token);
    expect(sent.body).not.toHaveProperty("password");
    expect(oidcStub.signinRedirectCalls).toHaveLength(0);
  });

  it("an expiring ID token counts as an old sign-in", async () => {
    oidcPlatform();
    oidcStub.user = signedInUser(30, { expiresIn: 20 });
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/tokens" />);
    const form = await screen.findByTestId("issue-token-form");
    await user.type(within(form).getByTestId("it-label"), "ci");
    await user.click(within(form).getByTestId("it-submit"));
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
  });

  it("a 403 reauth_required on a recent sign-in stops instead of signing in again", async () => {
    // 다시 로그인해도 풀리지 않는 까닭(IdP 와 플랫폼의 시계 차이 등)이다. 누를 때마다 IdP 를 돌지 않는다
    oidcPlatform();
    oidcStub.user = fresh();
    server.use(http.post("/me/tokens", replying(() => apiError(403, "reauth_required", "sign in again"))));
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/tokens" />);
    const form = await screen.findByTestId("issue-token-form");
    await user.type(within(form).getByTestId("it-label"), "ci");
    await user.click(within(form).getByTestId("it-submit"));
    expect(await within(form).findByTestId("it-reauth-failed")).toHaveTextContent(ko.auth.reauthFailed);
    expect(oidcStub.signinRedirectCalls).toHaveLength(0);
  });

  it("header mode has no personal tokens", async () => {
    render(<TestApp initialPath="/account/tokens" />);
    expect(await screen.findByTestId("tokens-header-mode")).toHaveTextContent(ko.account.tokens.headerMode);
  });

  it("the account menu links to the token page in oidc mode and has no password link", async () => {
    oidcPlatform(meAdmin);
    oidcStub.user = fresh();
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await user.click(await screen.findByTestId("account-menu"));
    const panel = screen.getByTestId("account-menu-panel");
    expect(within(panel).getByTestId("tokens-link")).toHaveAttribute("href", "/account/tokens");
    expect(within(panel).queryByTestId("password-link")).not.toBeInTheDocument();
  });
});

describe("MCP client consent in oidc mode", () => {
  let assign: MockInstance<(url: string) => void>;
  const consentPath = `/oauth/consent?request=${CONSENT_HANDLE}`;

  beforeEach(() => {
    assign = vi.spyOn(leave, "assign").mockImplementation(() => undefined);
  });

  it("without a session it signs in at the IdP and comes back to the same handle", async () => {
    oidcPlatform(meAdmin);
    render(<TestApp initialPath={consentPath} />);
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
    expect(oidcStub.signinRedirectCalls[0]).toEqual({ state: { returnTo: consentPath } });
  });

  it("an old sign-in signs in again before approving and keeps the handle; a recent one sends the ID token", async () => {
    oidcPlatform(meAdmin);
    oidcStub.user = stale();
    const user = userEvent.setup();
    render(<TestApp initialPath={consentPath} />);
    await user.click(await screen.findByTestId("consent-approve"));
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
    expect(oidcStub.signinRedirectCalls[0]).toEqual({ state: { returnTo: consentPath, reauth: 1 }, max_age: MAX_AGE });
    expect(lastRequest("POST", "/oauth/consent/approve")).toBeUndefined();

    reload();
    oidcStub.user = fresh();
    render(<TestApp initialPath={consentPath} />);
    await user.click(await screen.findByTestId("consent-approve"));
    await waitFor(() => expect(assign).toHaveBeenCalled());
    expect(lastRequest("POST", "/oauth/consent/approve")?.headers["x-daegam-id-token"]).toBe(oidcStub.user?.id_token);
  });
});

describe("run causes of the long bound", () => {
  it("names the days for a run and a paused source of a person past the bound", () => {
    expect(runReasonText(ko, "acts_as_unconfirmed", 7)).toEqual({ text: ko.runs.reason.actsAsUnconfirmed(7), code: false });
    const paused = { paused: true, paused_reason: "acts_as_lost_access", paused_cause: "unconfirmed", expires_at: "2099-01-01T00:00:00Z" };
    expect(pausedReasonText(ko, paused, 7)).toBe(ko.runs.pausedReason.actsAsUnconfirmed(7));
    expect(pausedReasonText(ko, { ...paused, paused_cause: "role" }, 7)).toBe(ko.runs.pausedReason.actsAsLostAccess);
  });
});

describe("Service accounts card", () => {
  const BASE = "/tenants/default/service-accounts";

  function account(over: Partial<ServiceAccount> = {}): ServiceAccount {
    return {
      account_id: "sa-1",
      tenant: "default",
      name: "ci",
      role: "developer",
      created_by: "u-boss",
      created_by_name: "boss@example.com",
      created_at: NOW,
      tokens: [personalTokenFixture({ token_id: "pt-sa", label: "pipeline" })],
      ...over,
    };
  }

  function serve(list: ServiceAccounts) {
    server.use(
      http.get(BASE, replying(() => HttpResponse.json(list))),
      http.post(BASE, replying(() => HttpResponse.json(account({ account_id: "sa-2", name: "nightly", tokens: [] }), { status: 201 }))),
      http.delete(`${BASE}/:id`, replying(() => HttpResponse.json({ account_id: "sa-1", deleted: true }))),
      http.post(
        `${BASE}/:id/tokens`,
        replying(() => HttpResponse.json({ ...personalTokenFixture({ token_id: "pt-new" }), token: PAT_VALUE }, { status: 201 })),
      ),
    );
  }

  it("lists accounts with their creator and tokens, creates one, issues a 90-day token and deletes after confirming", async () => {
    oidcPlatform(meAdmin);
    oidcStub.user = fresh();
    serve({ mode: "tenant-admin", can_manage: true, accounts: [account()] });
    const user = userEvent.setup();
    render(<TestApp initialPath="/tenant/settings" />);
    const card = await screen.findByTestId("service-accounts-card");
    expect(within(card).getByText(ko.tenant.serviceAccounts.help)).toBeInTheDocument();
    expect(await within(card).findByTestId("sa-sa-1-creator")).toHaveTextContent("boss@example.com");
    expect(within(card).getByTestId("pat-row-pt-sa")).toHaveTextContent("pipeline");

    await user.type(within(card).getByTestId("sa-name"), "nightly");
    await user.click(within(card).getByTestId("sa-create"));
    await waitFor(() => expect(lastRequest("POST", BASE)?.body).toEqual({ name: "nightly", role: "developer" }));

    await user.click(within(card).getByTestId("sa-sa-1-new-token"));
    const form = within(card).getByTestId("issue-token-form");
    expect(within(form).getByTestId("it-expiry")).toHaveValue("90");
    await user.type(within(form).getByTestId("it-label"), "deploy");
    await user.click(within(form).getByTestId("it-submit"));
    await waitFor(() => expect(lastRequest("POST", `${BASE}/sa-1/tokens`)).toBeDefined());
    const sent = lastRequest("POST", `${BASE}/sa-1/tokens`)!;
    expect(sent.headers["x-daegam-id-token"]).toBe(oidcStub.user?.id_token);
    expect(sent.body).toMatchObject({ label: "deploy" });

    await user.click(within(card).getByTestId("sa-sa-1-delete"));
    expect(await screen.findByText(ko.tenant.serviceAccounts.deleteBody)).toBeInTheDocument();
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", `${BASE}/sa-1`)).toBeDefined());
  });

  it("shows the off and platform-admin notices and hides what widens access", async () => {
    oidcPlatform(meAdmin);
    oidcStub.user = fresh();
    serve({ mode: "off", can_manage: false, accounts: [account()] });
    render(<TestApp initialPath="/tenant/settings" />);
    const card = await screen.findByTestId("service-accounts-card");
    expect(await within(card).findByTestId("sa-off")).toHaveTextContent(ko.tenant.serviceAccounts.off);
    expect(within(card).queryByTestId("sa-new")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("sa-sa-1-new-token")).not.toBeInTheDocument();
    expect(within(card).getByTestId("sa-sa-1-delete")).toBeInTheDocument();

    reload();
    serve({ mode: "platform-admin", can_manage: false, accounts: [] });
    render(<TestApp initialPath="/tenant/settings" />);
    const again = await screen.findByTestId("service-accounts-card");
    expect(await within(again).findByTestId("sa-platform-admin-only")).toHaveTextContent(ko.tenant.serviceAccounts.platformAdminOnly);
    expect(within(again).getByTestId("sa-empty")).toHaveTextContent(ko.tenant.serviceAccounts.empty);
  });
});
