import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { getAuthState, safeReturnTo } from "@/lib/auth";
import { apiError, http, HttpResponse, lastRequest, meDeveloper, server } from "./msw";
import { openSelect } from "./select";
import { oidcStub } from "./stubs/oidc-client-ts";

const OIDC_CONFIG = {
  auth_mode: "oidc",
  issuer: "https://okta.example.com/oauth2/default",
  client_id: "0oa123",
  audience: "api://daegam",
  scopes: ["openid", "profile", "email", "groups"],
  groups_claim: "groups",
};

describe("auth", () => {
  it("header mode: no login button, /me fills the role nav and the tenant selector lists memberships", async () => {
    render(<TestApp initialPath="/clusters" />);
    expect(await screen.findByTestId("login-placeholder")).toHaveTextContent("헤더 인증 모드");
    expect(screen.queryByTestId("login-button")).not.toBeInTheDocument();
    expect(await screen.findByTestId("nav-admin-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-admin-group-mappings")).toBeInTheDocument();
    // Settings 진입점은 nav-settings 하나다 (Tenant Settings 중복 항목은 없다)
    expect(screen.getByTestId("nav-settings")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-tenant-settings")).not.toBeInTheDocument();
    expect(screen.getByTestId("nav-audit")).toBeInTheDocument();
    await waitFor(() => expect(lastRequest("GET", "/tenants")).toBeDefined());
    // 테넌트 선택기를 열면 /tenants 의 소속(acme) 이 항목으로 보인다
    const user = userEvent.setup();
    const tenants = await openSelect(user, "tenant-select");
    await within(tenants).findByRole("option", { name: /acme/ });
    await user.keyboard("{Escape}");
    expect(screen.getByTestId("tenant-add")).toBeInTheDocument(); // platform-admin 은 직접 입력 가능
    expect(lastRequest("GET", "/me")?.headers.authorization).toBeUndefined();
  });

  it("oidc mode without a session: shows Login, hides role nav, and signinRedirect goes through oidc-client-ts", async () => {
    server.use(
      http.get("/auth/config", () => HttpResponse.json(OIDC_CONFIG)),
      http.get("/me", () => apiError(401, "unauthorized", "Bearer token required")),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    const login = await screen.findByTestId("login-button");
    expect(screen.queryByTestId("role-nav")).not.toBeInTheDocument();
    expect(oidcStub.settings).toMatchObject({
      authority: OIDC_CONFIG.issuer,
      client_id: "0oa123",
      redirect_uri: "http://localhost:3000/callback",
      response_type: "code",
      scope: "openid profile email groups",
      automaticSilentRenew: true,
    });
    await user.click(login);
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
  });

  it("oidc mode without a session on the rail: the login button carries the failure as its description", async () => {
    server.use(
      http.get("/auth/config", () => HttpResponse.json(OIDC_CONFIG)),
      http.get("/me", () => apiError(401, "unauthorized", "Bearer token required")),
    );
    window.localStorage.setItem("daegam.nav.sidebar", "collapsed");
    render(<TestApp initialPath="/clusters" />);
    const login = await screen.findByTestId("login-button");
    expect(login).toHaveAccessibleName("로그인");
    // 실패가 없으면 설명도 없다. 실패가 있으면 title 과 aria-describedby 가 그 까닭을 싣는다 (AccountMenu)
    if (login.getAttribute("aria-describedby")) expect(login).toHaveAccessibleDescription(login.getAttribute("title") ?? "");
    else expect(login).toHaveAttribute("title", "로그인");
  });

  it("oidc mode with a session: injects Authorization on API calls and loads /me", async () => {
    oidcStub.user = { access_token: "tok-123" };
    server.use(
      http.get("/auth/config", () => HttpResponse.json(OIDC_CONFIG)),
      http.get("/me", ({ request }) => {
        if (request.headers.get("authorization") !== "Bearer tok-123") return apiError(401, "unauthorized", "bad token");
        return HttpResponse.json(meDeveloper);
      }),
    );
    render(<TestApp initialPath="/backend-apis" />);
    expect(await screen.findByTestId("auth-user")).toHaveTextContent("dev@example.com");
    await waitFor(() => expect(getAuthState().token).toBe("tok-123"));
    await screen.findByTestId("backend-api-row-orders");
    expect(lastRequest("GET", "/backend-apis")?.headers.authorization).toBe("Bearer tok-123");
    expect(lastRequest("GET", "/backend-apis")?.headers["x-daegam-tenant"]).toBe("default");
    // developer: audit 만 보이고, 테넌트는 소속만 고를 수 있다 (직접 입력 없음)
    expect(await screen.findByTestId("nav-audit")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-admin-settings")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tenant-add")).not.toBeInTheDocument();
  });

  it("/callback completes the code exchange and returns to the saved path", async () => {
    server.use(
      http.get("/auth/config", () => HttpResponse.json(OIDC_CONFIG)),
      http.get("/me", () => HttpResponse.json(meDeveloper)),
    );
    render(<TestApp initialPath="/callback?code=abc&state=xyz" />);
    expect(await screen.findByTestId("clusters-page")).toBeInTheDocument();
    await waitFor(() => expect(getAuthState().token).toBe("tok-from-callback"));
  });

  it("/callback that fails offers a way to sign in again and a way home, with the library's message", async () => {
    // header 모드에는 OIDC 가 없어 콜백이 실패한다. 라이브러리의 원문은 작은 글자로 남는다
    const text = dictionary("ko").auth.callback;
    render(<TestApp initialPath="/callback?code=abc&state=xyz" />);
    const error = await screen.findByTestId("callback-error");
    expect(error).toHaveTextContent(text.failed);
    expect(error).toHaveTextContent("oidc not configured");
    expect(screen.getByTestId("callback-retry")).toHaveTextContent(text.retry);
    expect(screen.getByTestId("callback-home")).toHaveTextContent(text.home);
    expect(screen.getByTestId("callback-home")).toHaveAttribute("href", "/");
  });
});


describe("safeReturnTo", () => {
  it("같은 origin 의 절대 경로만 받는다", () => {
    expect(safeReturnTo("/mcp-servers/abc?x=1#y")).toBe("/mcp-servers/abc?x=1#y");
    expect(safeReturnTo("/")).toBe("/");
  });

  it("다른 origin 으로 가는 것과 경로가 아닌 것은 / 로 돌린다", () => {
    // state 는 IdP 를 한 바퀴 돌아오므로 우리가 만든 값이라도 밖을 지난 값으로 다룬다
    for (const bad of ["//evil.example/x", "/\\evil.example", "https://evil.example/", "javascript:alert(1)", "mcp-servers", "", undefined, null, 42]) {
      expect(safeReturnTo(bad)).toBe("/");
    }
  });
});
