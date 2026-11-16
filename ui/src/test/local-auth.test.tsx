import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { getAuthState } from "@/lib/auth";
import { submitConfirm } from "./confirm";
import { signInLocal } from "./localSignIn";
import { CONSENT_HANDLE, LOCAL_ADMIN_PASSWORD, LOCAL_TOKEN, NOW, auditEvents, authConfigState, grantState, http, HttpResponse, lastExact, lastRequest, localLoginState, meDeveloper, oauthConfigHandler, oauthGrantFixture, personalTokenFixture, platformSettings, server, tokenState } from "./msw";

/**
 * auth_mode=local (D40): 로그인 화면, 토큰 헤더, 첫 로그인의 비밀번호 바꾸기, 401 되돌림, Users 화면.
 * 개인 MCP 토큰(P32)의 머리 링크, 비밀번호 화면의 회수 예고, Users 화면의 사용자 토큰 대화상자도 여기서 본다.
 * 강제 비밀번호 변경을 거쳐도 돌아갈 곳을 지키는 것과 다시 로그인(reauth=1), 대화상자의 MCP 클라이언트 연결도 본다 (P33 16절).
 */
describe("local auth", () => {
  beforeEach(() => {
    authConfigState.mode = "local";
  });

  it("sends an unauthenticated visitor to /login and comes back after signing in", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    expect(await screen.findByTestId("login-page")).toBeInTheDocument();
    // 틀린 비밀번호는 한 문장으로만 말한다 (없는 사용자와 구분하지 않는다)
    await signInLocal(user, "not the password 1");
    expect(await screen.findByTestId("login-error")).toHaveTextContent("아이디 또는 비밀번호가 맞지 않습니다.");
    await user.clear(screen.getByTestId("login-password"));
    await user.type(screen.getByTestId("login-password"), LOCAL_ADMIN_PASSWORD);
    await user.click(screen.getByTestId("login-submit"));
    // 원래 가려던 /clusters 로 돌아오고, 로그인 뒤의 요청(/me)에 토큰이 실린다
    expect(await screen.findByTestId("auth-user")).toBeInTheDocument();
    expect(screen.getByTestId("page-title")).toHaveTextContent("Clusters");
    await waitFor(() => expect(lastRequest("GET", "/me")?.headers.authorization).toBe(`Bearer ${LOCAL_TOKEN}`));
    expect(getAuthState().token).toBe(LOCAL_TOKEN);
    // Users 항목은 local 모드의 platform-admin 에게 보인다
    expect(screen.getByTestId("nav-admin-users")).toBeInTheDocument();
    // 로그아웃하면 토큰이 사라지고 로그인 화면으로 간다
    await user.click(screen.getByTestId("account-menu"));
    await user.click(screen.getByTestId("logout-button"));
    expect(await screen.findByTestId("login-page")).toBeInTheDocument();
    expect(getAuthState().token).toBeNull();
    await waitFor(() => expect(lastRequest("POST", "/auth/logout")).toBeDefined());
  });

  it("forces a password change on first sign-in and says how many characters a short new password still needs", async () => {
    localLoginState.mustChange = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);
    await signInLocal(user);
    expect(await screen.findByTestId("password-page")).toBeInTheDocument();
    expect(screen.getByTestId("password-required-notice")).toBeInTheDocument();
    // 강제 변경 중에는 서버가 다른 API 를 막으므로 개인 토큰 수를 묻지 않는다
    expect(lastRequest("GET", "/me/tokens")).toBeUndefined();
    expect(screen.queryByTestId("pw-token-warning")).not.toBeInTheDocument();
    // 강제 변경 화면에서는 현재 비밀번호가 무엇인지 말해 준다
    expect(screen.getByText("처음 받은 비밀번호나 관리자가 재설정해 준 임시 비밀번호입니다.")).toBeInTheDocument();
    expect(screen.getByText(/12자 이상이어야 합니다\./)).toBeInTheDocument();
    await user.type(screen.getByTestId("pw-current"), LOCAL_ADMIN_PASSWORD);
    await user.type(screen.getByTestId("pw-next"), "short pw");
    await user.type(screen.getByTestId("pw-again"), "short pw");
    // 버튼만 꺼지고 끝나지 않는다. 몇 자 더 필요한지 칸 아래에 말한다
    expect(screen.getByText("지금 8자입니다. 4자 더 입력해야 합니다.")).toBeInTheDocument();
    expect(screen.getByTestId("pw-next")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("pw-submit")).toBeDisabled();
    await user.type(screen.getByTestId("pw-next"), " long now");
    expect(screen.queryByText(/자 더 입력해야 합니다/)).not.toBeInTheDocument();

    // 두 칸이 다르면 보내지 않는다. 위에서 친 짧은 비밀번호는 지우고 새로 친다
    await user.clear(screen.getByTestId("pw-next"));
    await user.clear(screen.getByTestId("pw-again"));
    await user.type(screen.getByTestId("pw-next"), "a brand new password 42");
    await user.type(screen.getByTestId("pw-again"), "a brand new password 43");
    expect(await screen.findByTestId("pw-mismatch")).toBeInTheDocument();
    expect(screen.getByTestId("pw-submit")).toBeDisabled();
    await user.clear(screen.getByTestId("pw-again"));
    await user.type(screen.getByTestId("pw-again"), "a brand new password 42");
    await user.click(screen.getByTestId("pw-submit"));
    await waitFor(() => expect(lastRequest("POST", "/auth/password")).toBeDefined());
    // 새 토큰으로 갈아 타고 홈으로 간다
    await waitFor(() => expect(getAuthState().token).toBe("local-token-2"));
    expect(getAuthState().mustChangePassword).toBe(false);
  });

  it("keeps returnTo through the forced password change on first sign-in", async () => {
    // MCP 클라이언트가 연 동의 화면에서 처음 로그인한 사람이 인가 요청을 잃지 않는다 (P33 16절)
    localLoginState.mustChange = true;
    const user = userEvent.setup();
    render(<TestApp initialPath={`/oauth/consent?request=${CONSENT_HANDLE}`} />);
    await signInLocal(user);
    expect(await screen.findByTestId("password-required-notice")).toBeInTheDocument();
    await user.type(screen.getByTestId("pw-current"), LOCAL_ADMIN_PASSWORD);
    await user.type(screen.getByTestId("pw-next"), "a brand new password 42");
    await user.type(screen.getByTestId("pw-again"), "a brand new password 42");
    await user.click(screen.getByTestId("pw-submit"));
    expect(await screen.findByTestId("consent-approve")).toBeInTheDocument();
    expect(lastRequest("POST", "/oauth/consent/inspect")?.body).toEqual({ request: CONSENT_HANDLE });
  });

  it("shows the sign-in form for reauth=1 even with a stored token", async () => {
    window.sessionStorage.setItem("daegam.local.token", LOCAL_TOKEN);
    const user = userEvent.setup();
    render(<TestApp initialPath="/login?returnTo=%2Fclusters&reauth=1" />);
    expect(await screen.findByTestId("login-reauth")).toHaveTextContent("보안을 위해 다시 로그인해 주세요.");
    expect(getAuthState().token).toBe(LOCAL_TOKEN);
    await signInLocal(user);
    expect(await screen.findByTestId("auth-user")).toBeInTheDocument();
    expect(screen.getByTestId("page-title")).toHaveTextContent("Clusters");
  });

  it("manages users: list, a password reset dialog that explains, focuses and submits on Enter, toggle admin, the last-admin guard, and create", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/users" />);
    await signInLocal(user);
    expect(await screen.findByTestId("users-page")).toBeInTheDocument();
    expect(await screen.findByTestId("user-row-alice")).toBeInTheDocument();
    expect(screen.getByTestId("user-row-admin")).toHaveTextContent("platform-admin");

    // 비밀번호 재설정·MCP 토큰·지우기는 줄의 더보기에 있다
    await user.click(await screen.findByTestId("user-menu-alice"));
    await user.click(await screen.findByTestId("user-reset-alice"));
    const input = await screen.findByTestId("reset-password");
    // 열리면 비밀번호 칸에 바로 쓸 수 있다
    expect(input).toHaveFocus();
    await user.type(input, "temp");
    expect(screen.getByText("지금 4자입니다. 8자 더 입력해야 합니다.")).toBeInTheDocument();
    // 짧으면 확인 버튼이 꺼져 있다 (눌러도 아무 일이 없는 버튼을 두지 않는다)
    expect(screen.getByTestId("confirm-submit")).toBeDisabled();
    await user.type(input, "orary pass 1{Enter}");
    await waitFor(() => expect(lastRequest("POST", "/users/u-alice/password")).toBeDefined());
    expect(lastRequest("POST", "/users/u-alice/password")?.body).toEqual({ password: "temporary pass 1" });

    // 목의 사용자 목록은 재설정으로 바뀌지 않는다. 같은 화면에서 관리자 토글로 넘어간다
    await user.click(screen.getByTestId("user-admin-alice"));
    await waitFor(() => expect(lastRequest("PATCH", "/users/u-alice")).toBeDefined());
    expect(lastRequest("PATCH", "/users/u-alice")?.body).toEqual({ platform_admin: true });
    // 마지막 platform-admin 을 내리면 서버가 422 last_admin 을 내고 화면이 그것을 보인다
    await user.click(screen.getByTestId("user-admin-admin"));
    expect(await screen.findByTestId("users-error")).toHaveTextContent(/last_admin|마지막/);
    // 사용자 추가 카드
    await user.click(screen.getByTestId("user-create-open"));
    await user.type(screen.getByTestId("uc-username"), "carol");
    await user.type(screen.getByTestId("uc-password"), "carol password 12345");
    await user.click(screen.getByTestId("uc-submit"));
    // 앞의 재설정이 POST /users/u-alice/password 를 남겼으므로, 앞머리가 아니라 경로 그대로 맞는 요청을 본다
    await waitFor(() => expect(lastExact("POST", "/users")?.body).toMatchObject({ username: "carol", platform_admin: false }));
  });

  it("shows who last changed the platform settings by username, not by user id", async () => {
    // local 모드의 sub 는 사용자 id(UUID)다. 화면은 디렉터리로 아이디를 찾아 보인다
    server.use(http.get("/settings", () => HttpResponse.json({ ...platformSettings, updated_by: "u-admin" })));
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/settings" />);
    await signInLocal(user);
    expect(await screen.findByTestId("platform-settings-page")).toBeInTheDocument();
    expect(await screen.findByText(/by admin$/)).toBeInTheDocument();
    expect(screen.queryByText(/by u-admin/)).not.toBeInTheDocument();
  });

  it("a developer without the user directory still sees their own audit rows by name", async () => {
    // 디렉터리는 members:write 가 있어야 준다. 개발자에게는 부르지 않고, 자기 행은 /me 의 이름으로 보인다
    server.use(
      http.get("/me", () => HttpResponse.json({ ...meDeveloper, sub: "u-dev" })),
      http.get("/audit", () => HttpResponse.json([{ ...auditEvents[0], sub: "u-dev" }, { ...auditEvents[0], id: "ae-other", sub: "u-other" }])),
    );
    const user = userEvent.setup();
    render(<TestApp initialPath="/audit" />);
    await signInLocal(user);
    const page = await screen.findByTestId("audit-page");
    // 머리 줄에도 같은 이름이 있으므로 감사 표 안에서만 찾는다
    expect(await within(page).findByText("김개발")).toBeInTheDocument();
    // 이름은 한 줄이다. 긴 리소스 id 칸에 밀려 한 글자씩 세로로 쌓이지 않게 한다
    expect(within(page).getByText("김개발").closest("td")).toHaveClass("whitespace-nowrap");
    expect(screen.getByText("u-other")).toBeInTheDocument();
    expect(lastRequest("GET", "/users/directory")).toBeUndefined();
  });

  it("shows the /me name in the header corner, Korean included", async () => {
    // 이름은 서버가 정해 /me 로 준다. 토큰을 브라우저에서 atob 로 풀면 한글이 깨지므로 그렇게 읽지 않는다
    server.use(http.get("/me", () => HttpResponse.json({ ...meDeveloper, sub: "u-hong", name: "홍길동" })));
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await signInLocal(user);
    expect(await screen.findByTestId("auth-user-name")).toHaveTextContent("홍길동");
  });

  it("explains a lockout with the remaining minutes", async () => {
    localLoginState.locked = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);
    await signInLocal(user);
    // Retry-After 는 남은 초(840)다
    expect(await screen.findByTestId("login-error")).toHaveTextContent(/잠겼습니다\. 14분 뒤/);
  });

  it("says the same lockout sentence on the password page as on the login page", async () => {
    // 비밀번호 바꾸기의 틀린 현재 비밀번호도 로그인 잠금에 센다 (P32 6절)
    localLoginState.mustChange = true;
    const user = userEvent.setup();
    render(<TestApp initialPath="/" />);
    await signInLocal(user);
    expect(await screen.findByTestId("password-page")).toBeInTheDocument();
    localLoginState.locked = true;
    await user.type(screen.getByTestId("pw-current"), "not the password 1");
    await user.type(screen.getByTestId("pw-next"), "a brand new password 42");
    await user.type(screen.getByTestId("pw-again"), "a brand new password 42");
    await user.click(screen.getByTestId("pw-submit"));
    expect(await screen.findByTestId("pw-error")).toHaveTextContent(/^로그인 시도가 너무 많아 계정이 잠겼습니다\. \d+분 뒤 다시 시도해 주세요\.$/);
    // 세션은 그대로다. 401 훅 밖의 호출이다
    expect(getAuthState().token).toBe(LOCAL_TOKEN);
  });

  it("links to my MCP tokens in the account menu, before the password link", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await signInLocal(user);
    const corner = await screen.findByTestId("auth-user");
    await user.click(within(corner).getByTestId("account-menu"));
    const tokens = within(corner).getByTestId("tokens-link");
    expect(tokens).toHaveTextContent("내 MCP 토큰");
    expect(tokens).toHaveAttribute("href", "/account/tokens");
    expect(tokens.compareDocumentPosition(within(corner).getByTestId("password-link")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(tokens);
    expect(await screen.findByTestId("tokens-page")).toBeInTheDocument();
  });

  it("drops a dead token on 401 and returns to /login", async () => {
    const user = userEvent.setup();
    render(<TestApp initialPath="/clusters" />);
    await signInLocal(user);
    expect(await screen.findByTestId("auth-user")).toBeInTheDocument();
    // 아직 부르지 않은 화면의 첫 요청이 401 로 온다 (/clusters 는 이미 캐시돼 다시 부르지 않는다)
    server.use(
      http.get("/backend-apis", () => HttpResponse.json({ detail: { code: "invalid_token", detail: "token expired" } }, { status: 401 })),
    );
    await user.click(screen.getByTestId("nav-backend-apis"));
    expect(await screen.findByTestId("login-page")).toBeInTheDocument();
    expect(getAuthState().token).toBeNull();
  });

  it("says on the password page how many personal MCP tokens a change revokes", async () => {
    // 쓸 수 있는 것과 준비 중인 것만 센다. 이미 회수된 것은 빼고 말한다 (P32 7절)
    tokenState.mine = [
      personalTokenFixture(),
      personalTokenFixture({ token_id: "pt-warm", status: "pending", ready_at: NOW }),
      personalTokenFixture({ token_id: "pt-gone", status: "revoked", revoked_at: NOW, revoke_reason: "user" }),
    ];
    const user = userEvent.setup();
    render(<TestApp initialPath="/account/password" />);
    await signInLocal(user);
    expect(await screen.findByTestId("pw-token-warning")).toHaveTextContent("비밀번호를 바꾸면 개인 MCP 토큰 2개가 모두 회수됩니다.");
  });

  it("lets an admin list and revoke one user's personal MCP tokens from the row menu", async () => {
    tokenState.byUser["u-alice"] = [personalTokenFixture({ token_id: "pt-alice", label: "alice laptop" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/users" />);
    await signInLocal(user);
    await user.click(await screen.findByTestId("user-menu-alice"));
    await user.click(await screen.findByTestId("user-tokens-alice"));
    const dialog = await screen.findByTestId("user-tokens-dialog");
    expect(await within(dialog).findByTestId("pat-row-pt-alice")).toHaveTextContent("alice laptop");
    await user.click(within(dialog).getByTestId("pat-revoke-pt-alice"));
    // 회수는 확인을 거친다. 확인 대화상자가 목록 대화상자 위에 뜬다
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/users/u-alice/tokens/pt-alice")).toBeDefined());
    await waitFor(() => expect(within(dialog).getByTestId("pat-status-pt-alice")).toHaveTextContent("회수됨"));
    expect(within(dialog).getByTestId("pat-status-pt-alice")).toHaveTextContent("관리자가 회수");
    expect(within(dialog).queryByTestId("pat-revoke-pt-alice")).not.toBeInTheDocument();
    // OAuth 가 꺼져 있으면 연결 목록이 없고 묻지도 않는다
    expect(within(dialog).queryByTestId("user-grants")).not.toBeInTheDocument();
    expect(lastRequest("GET", "/users/u-alice/oauth-grants")).toBeUndefined();
  });

  it("lets an admin list and disconnect one user's MCP client connections in the same dialog", async () => {
    server.use(oauthConfigHandler);
    grantState.byUser["u-alice"] = [oauthGrantFixture({ grant_id: "og-alice", client_name: "Cursor", client_kind: "dcr", client_host: null })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/admin/users" />);
    await signInLocal(user);
    await user.click(await screen.findByTestId("user-menu-alice"));
    expect(screen.getByTestId("user-tokens-alice")).toHaveTextContent("이 사람의 개인 MCP 토큰과 MCP 클라이언트 연결을 보고 끊습니다.");
    await user.click(screen.getByTestId("user-tokens-alice"));
    const dialog = await screen.findByTestId("user-tokens-dialog");
    expect(dialog).toHaveTextContent("개인 토큰");
    const grants = within(dialog).getByTestId("user-grants");
    expect(grants).toHaveTextContent("연결된 MCP 클라이언트");
    expect(await within(grants).findByTestId("grant-client-og-alice")).toHaveTextContent("Cursor클라이언트가 스스로 밝힌 이름");
    await user.click(within(grants).getByTestId("grant-revoke-og-alice"));
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/users/u-alice/oauth-grants/og-alice")).toBeDefined());
    await waitFor(() => expect(within(grants).getByTestId("grant-status-og-alice")).toHaveTextContent("끊김관리자가 끊음"));
    expect(within(grants).queryByTestId("grant-revoke-og-alice")).not.toBeInTheDocument();
  });
});

describe("Users outside local auth", () => {
  it("header mode says that Users exists only in local auth mode and draws no user table", async () => {
    render(<TestApp initialPath="/admin/users" />);
    expect(await screen.findByTestId("users-not-local")).toHaveTextContent(dictionary("ko").admin.users.notLocal("header"));
    expect(screen.queryByTestId("users-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("user-create-open")).not.toBeInTheDocument();
  });
});
