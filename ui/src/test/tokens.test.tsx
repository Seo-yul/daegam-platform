/**
 * 내 MCP 토큰 (P32 10절). 목록의 상태와 회수 이유, 발급 폼(한 번 보이는 토큰, 준비 시각, 만료 상한, 틀린 비밀번호),
 * 확인을 거치는 회수, local 이 아닐 때의 안내다. OAuth 가 켜졌을 때의 "연결된 MCP 클라이언트" 절도 본다 (P33 16절).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { getAuthState } from "@/lib/auth";
import { dateInputValue, expiresAtFor, expiryPresets } from "@/lib/personalTokens";
import { submitConfirm } from "./confirm";
import { signInLocal } from "./localSignIn";
import {
  HttpResponse,
  LOCAL_ADMIN_PASSWORD,
  LOCAL_TOKEN,
  NOW,
  PAT_VALUE,
  authConfigState,
  grantState,
  http,
  lastRequest,
  localAuthConfig,
  localLoginState,
  meTokenHolder,
  oauthConfigHandler,
  oauthGrantFixture,
  personalTokenFixture,
  server,
  tokenState,
} from "./msw";
import { selectValue } from "./select";

const DAY_MS = 86_400_000;

/** local 모드로 로그인해 /account/tokens 를 연다. 기본은 default·acme 의 멤버다. */
async function openTokens(user: ReturnType<typeof userEvent.setup>) {
  render(<TestApp initialPath="/account/tokens" />);
  await signInLocal(user);
  return screen.findByTestId("tokens-page");
}

describe("personal token rules", () => {
  it("hides presets over the cap and keeps the date range from tomorrow to the cap", () => {
    expect(expiryPresets(365)).toEqual([7, 30, 90, 180, 365]);
    expect(expiryPresets(60)).toEqual([7, 30]);
    const now = new Date(2026, 8, 27, 10, 0);
    // 날짜를 고르면 그날의 지금 시각이다. 내일은 정확히 1일, 상한 날은 정확히 상한이다
    expect(expiresAtFor("date", "2026-09-28", 60, now)).toBe(new Date(now.getTime() + DAY_MS).toISOString());
    expect(expiresAtFor("date", "2026-11-26", 60, now)).toBe(new Date(now.getTime() + 60 * DAY_MS).toISOString());
    expect(expiresAtFor("date", "2026-09-27", 60, now)).toBeNull();
    expect(expiresAtFor("date", "2026-11-27", 60, now)).toBeNull();
    expect(expiresAtFor("date", "", 60, now)).toBeNull();
    expect(expiresAtFor("90", "", 60, now)).toBeNull();
  });
});

describe("my MCP tokens", () => {
  beforeEach(() => {
    authConfigState.mode = "local";
    server.use(http.get("/me", () => HttpResponse.json(meTokenHolder)));
  });

  it("lists tokens with a status badge and the revoke reason, and offers revoke only for live ones", async () => {
    tokenState.mine = [
      personalTokenFixture({ token_id: "pt-a", label: "laptop" }),
      personalTokenFixture({ token_id: "pt-p", label: "warming", status: "pending", ready_at: "2026-09-02T10:20:00+00:00" }),
      personalTokenFixture({ token_id: "pt-r", label: "rotating", status: "revoking", revoke_reason: "password" }),
      personalTokenFixture({ token_id: "pt-x", label: "gone", status: "revoked", revoked_at: NOW, revoke_reason: "membership" }),
      personalTokenFixture({ token_id: "pt-e", label: "old", status: "expired" }),
    ];
    const user = userEvent.setup();
    await openTokens(user);
    expect(await screen.findByTestId("pat-status-pt-a")).toHaveTextContent("사용할 수 있음");
    expect(screen.getByTestId("pat-status-pt-p")).toHaveTextContent(/^준비 중\d\d:\d\d부터$/);
    // 회수 중은 슬롯 삭제를 기다리는 상태다. 이유를 함께 보인다
    expect(screen.getByTestId("pat-status-pt-r")).toHaveTextContent("회수 중비밀번호 변경");
    expect(screen.getByTestId("pat-status-pt-x")).toHaveTextContent("회수됨테넌트 소속 해제");
    expect(screen.getByTestId("pat-status-pt-e")).toHaveTextContent("만료됨");
    expect(screen.getByTestId("pat-row-pt-a")).toHaveTextContent("default");
    expect(screen.getByTestId("pat-revoke-pt-a")).toBeInTheDocument();
    expect(screen.getByTestId("pat-revoke-pt-p")).toBeInTheDocument();
    for (const id of ["pt-r", "pt-x", "pt-e"]) expect(screen.queryByTestId(`pat-revoke-${id}`)).not.toBeInTheDocument();
  });

  it("issues a token, shows it once with a copy button and the ready time, and refreshes the list", async () => {
    tokenState.readyAt = "2026-09-02T10:20:00+00:00";
    const user = userEvent.setup();
    await openTokens(user);
    const form = await screen.findByTestId("issue-token-form");
    // 테넌트는 /me 의 소속이고 platform-admin 의 * 는 없다. 만료는 30일이 처음 값이다
    expect(within(form).getByTestId("it-tenant")).toHaveValue("default");
    expect(within(form).getByTestId("it-expiry")).toHaveValue("30");
    await selectValue(user, "it-tenant", "acme");
    await user.type(within(form).getByTestId("it-label"), "  work laptop  ");
    await selectValue(user, "it-expiry", "90");
    expect(within(form).getByTestId("it-submit")).toBeDisabled();
    await user.type(within(form).getByTestId("it-password"), LOCAL_ADMIN_PASSWORD);
    const before = Date.now();
    await user.click(within(form).getByTestId("it-submit"));

    const issued = await screen.findByTestId("issued-token");
    expect(within(issued).getByTestId("issued-token-value")).toHaveTextContent(PAT_VALUE);
    expect(issued).toHaveTextContent("이 토큰은 지금 한 번만 보입니다. 복사해 주세요.");
    expect(within(issued).getByTestId("issued-token-copy")).toHaveAccessibleName("토큰 복사");
    expect(within(issued).getByTestId("issued-token-note")).toHaveTextContent(/^\d\d:\d\d부터 사용할 수 있습니다\.$/);
    const body = lastRequest("POST", "/me/tokens")?.body as { tenant: string; label: string; expires_at: string; password: string };
    expect(body).toMatchObject({ tenant: "acme", label: "work laptop", password: LOCAL_ADMIN_PASSWORD });
    const days = (Date.parse(body.expires_at) - before) / DAY_MS;
    expect(days).toBeGreaterThan(89.99);
    expect(days).toBeLessThan(90.01);
    // 비밀번호 칸은 비우고 목록을 다시 읽는다. 목록에는 토큰 값이 없다
    expect(within(form).getByTestId("it-password")).toHaveValue("");
    const row = await screen.findByTestId("pat-row-pt-3");
    expect(row).toHaveTextContent("work laptop");
    expect(within(row).getByTestId("pat-status-pt-3")).toHaveTextContent("준비 중");
    expect(screen.getByTestId("pat-table")).not.toHaveTextContent(PAT_VALUE);
  });

  it("hides expiry choices over max_days and takes a date between tomorrow and the cap", async () => {
    server.use(http.get("/auth/config", () => HttpResponse.json({ ...localAuthConfig, personal_tokens: { max_days: 60 } })));
    const user = userEvent.setup();
    await openTokens(user);
    await screen.findByTestId("issue-token-form");
    await user.click(screen.getByTestId("it-expiry"));
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getAllByRole("option").map((o) => o.textContent)).toEqual(["7일", "30일", "날짜 선택"]);
    await user.keyboard("{Escape}");
    await selectValue(user, "it-expiry", "date");
    const date = screen.getByTestId("it-date");
    const now = Date.now();
    expect(date).toHaveAttribute("min", dateInputValue(new Date(now + DAY_MS)));
    expect(date).toHaveAttribute("max", dateInputValue(new Date(now + 60 * DAY_MS)));
    await user.type(screen.getByTestId("it-label"), "ci");
    await user.type(screen.getByTestId("it-password"), LOCAL_ADMIN_PASSWORD);
    // 날짜를 고르기 전에는 보낼 수 없다
    expect(screen.getByTestId("it-submit")).toBeDisabled();
    fireEvent.change(date, { target: { value: dateInputValue(new Date(now + 10 * DAY_MS)) } });
    await user.click(screen.getByTestId("it-submit"));
    await waitFor(() => expect(lastRequest("POST", "/me/tokens")).toBeDefined());
    const body = lastRequest("POST", "/me/tokens")?.body as { expires_at: string };
    expect(Math.round((Date.parse(body.expires_at) - now) / DAY_MS)).toBe(10);
  });

  it("keeps a wrong or locked password inside the form without dropping the session", async () => {
    const user = userEvent.setup();
    await openTokens(user);
    await user.type(await screen.findByTestId("it-label"), "laptop");
    await user.type(screen.getByTestId("it-password"), "not the password 1");
    await user.click(screen.getByTestId("it-submit"));
    // 서버는 403 이다. 401 이면 화면의 401 훅이 세션을 지운다
    expect(await screen.findByText("비밀번호가 맞지 않습니다.")).toBeInTheDocument();
    expect(screen.getByTestId("it-password")).toHaveAttribute("aria-invalid", "true");
    expect(getAuthState().token).toBe(LOCAL_TOKEN);
    expect(screen.queryByTestId("issued-token")).not.toBeInTheDocument();
    // 거듭 틀려 잠기면 로그인 화면과 같은 문장이다
    localLoginState.locked = true;
    await user.click(screen.getByTestId("it-submit"));
    expect(await screen.findByText(/^로그인 시도가 너무 많아 계정이 잠겼습니다\. \d+분 뒤 다시 시도해 주세요\.$/)).toBeInTheDocument();
    // 다시 쓰기 시작하면 칸의 오류를 걷는다
    await user.type(screen.getByTestId("it-password"), "x");
    expect(screen.queryByText(/잠겼습니다/)).not.toBeInTheDocument();
  });

  it("says why a tenant is refused and keeps the form", async () => {
    tokenState.tenants = ["acme"];
    const user = userEvent.setup();
    await openTokens(user);
    await user.type(await screen.findByTestId("it-label"), "laptop");
    await user.type(screen.getByTestId("it-password"), LOCAL_ADMIN_PASSWORD);
    await user.click(screen.getByTestId("it-submit"));
    expect(await screen.findByTestId("it-error")).toHaveTextContent("이 테넌트의 멤버가 아니라 토큰을 받을 수 없습니다.");
  });

  it("revokes through a confirmation dialog and shows the reason", async () => {
    const user = userEvent.setup();
    await openTokens(user);
    await user.click(await screen.findByTestId("pat-revoke-pt-laptop"));
    expect(screen.getByTestId("confirm-dialog")).toHaveTextContent("16분 안에 거절됩니다");
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/me/tokens/pt-laptop")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("pat-status-pt-laptop")).toHaveTextContent("회수됨본인이 회수"));
    expect(screen.queryByTestId("pat-revoke-pt-laptop")).not.toBeInTheDocument();
  });

  it("tells a platform-admin without a tenant membership that no token can be issued", async () => {
    // 목의 기본 /me 는 * 하나만 가진 platform-admin 이다. * 는 소속이 아니다
    server.use(http.get("/me", () => HttpResponse.json({ ...meTokenHolder, memberships: [{ tenant: "*", role: "platform-admin", source: "manual" }] })));
    const user = userEvent.setup();
    await openTokens(user);
    expect(await screen.findByTestId("tokens-no-tenant")).toHaveTextContent("소속된 테넌트가 없어 토큰을 받을 수 없습니다.");
    expect(screen.queryByTestId("issue-token-form")).not.toBeInTheDocument();
  });
});

describe("connected MCP clients", () => {
  beforeEach(() => {
    authConfigState.mode = "local";
    server.use(http.get("/me", () => HttpResponse.json(meTokenHolder)));
  });

  it("is not there and not asked for while OAuth is off", async () => {
    const user = userEvent.setup();
    await openTokens(user);
    await screen.findByTestId("pat-table");
    expect(screen.queryByTestId("grants-card")).not.toBeInTheDocument();
    expect(lastRequest("GET", "/me/oauth-grants")).toBeUndefined();
  });

  it("lists connections with where the client name comes from, the target, and the status", async () => {
    server.use(oauthConfigHandler);
    grantState.mine = [
      oauthGrantFixture(),
      oauthGrantFixture({ grant_id: "og-dcr", client_name: "Cursor", client_ref: "dcr:0123456789abcdef", client_kind: "dcr", client_host: null, resource: "https://calc.apps.example/mcp", resource_kind: "mcp_server", tenant: "acme" }),
      oauthGrantFixture({ grant_id: "og-reuse", status: "revoked", revoked_at: NOW, revoked_reason: "refresh_reuse" }),
      oauthGrantFixture({ grant_id: "og-gone", status: "revoked", revoked_at: NOW, revoked_reason: "membership" }),
      oauthGrantFixture({ grant_id: "og-old", status: "expired" }),
    ];
    const user = userEvent.setup();
    await openTokens(user);
    const card = await screen.findByTestId("grants-card");
    expect(card).toHaveTextContent("연결된 MCP 클라이언트");
    // CIMD 는 문서를 공개한 호스트를, DCR 은 스스로 밝힌 이름이라고만 말한다
    expect(await within(card).findByTestId("grant-client-og-claude")).toHaveTextContent("Claude Codeclaude.ai가 공개한 클라이언트 정보");
    expect(within(card).getByTestId("grant-client-og-dcr")).toHaveTextContent("Cursor클라이언트가 스스로 밝힌 이름");
    expect(within(card).getByTestId("grant-row-og-claude")).toHaveTextContent("https://gw-support.apps.example/mcp");
    expect(within(card).getByTestId("grant-row-og-claude")).toHaveTextContent("Gateway");
    expect(within(card).getByTestId("grant-row-og-dcr")).toHaveTextContent("MCP Server");
    expect(within(card).getByTestId("grant-row-og-dcr")).toHaveTextContent("acme");
    expect(within(card).getByTestId("grant-status-og-claude")).toHaveTextContent("연결됨");
    expect(within(card).getByTestId("grant-status-og-reuse")).toHaveTextContent("끊김갱신 토큰 재사용");
    // 계정 사건은 개인 토큰과 같은 말이다
    expect(within(card).getByTestId("grant-status-og-gone")).toHaveTextContent("끊김테넌트 소속 해제");
    expect(within(card).getByTestId("grant-status-og-old")).toHaveTextContent("만료됨");
    expect(within(card).getByTestId("grant-revoke-og-claude")).toBeInTheDocument();
    for (const id of ["og-reuse", "og-gone", "og-old"]) expect(within(card).queryByTestId(`grant-revoke-${id}`)).not.toBeInTheDocument();
  });

  it("disconnects through a confirmation dialog", async () => {
    server.use(oauthConfigHandler);
    const user = userEvent.setup();
    await openTokens(user);
    await user.click(await screen.findByTestId("grant-revoke-og-claude"));
    expect(screen.getByTestId("confirm-dialog")).toHaveTextContent("Claude Code 연결 끊기");
    expect(screen.getByTestId("confirm-dialog")).toHaveTextContent("16분 안에 요청이 거절됩니다");
    await submitConfirm(user);
    await waitFor(() => expect(lastRequest("DELETE", "/me/oauth-grants/og-claude")).toBeDefined());
    await waitFor(() => expect(screen.getByTestId("grant-status-og-claude")).toHaveTextContent("끊김본인이 끊음"));
    expect(screen.queryByTestId("grant-revoke-og-claude")).not.toBeInTheDocument();
  });

  it("says so when nothing is connected", async () => {
    server.use(oauthConfigHandler);
    grantState.mine = [];
    const user = userEvent.setup();
    await openTokens(user);
    expect(await within(await screen.findByTestId("grants-card")).findByText("연결된 MCP 클라이언트가 없습니다.")).toBeInTheDocument();
  });
});

describe("my MCP tokens in header mode", () => {
  it("shows only a notice and does not ask for tokens", async () => {
    render(<TestApp initialPath="/account/tokens" />);
    expect(await screen.findByTestId("tokens-header-mode")).toHaveTextContent("개인 MCP 토큰은 header 인증 모드에서는 없습니다.");
    expect(screen.queryByTestId("pat-table")).not.toBeInTheDocument();
    expect(lastRequest("GET", "/me/tokens")).toBeUndefined();
  });
});
