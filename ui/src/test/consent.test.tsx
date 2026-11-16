import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { type MockInstance, vi } from "vitest";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { getAuthState } from "@/lib/auth";
import { leave } from "@/lib/leave";
import { signInLocal } from "./localSignIn";
import { APPROVED_REDIRECT, CONSENT_HANDLE, DENIED_REDIRECT, HttpResponse, authConfigState, consentState, consentViewFixture, http, lastRequest, meAdmin, server } from "./msw";

const CONSENT_PATH = `/oauth/consent?request=${CONSENT_HANDLE}`;
const CLAUDE = "https://claude.ai/oauth/claude-code-client-metadata";

/** 토큰 없이 동의 화면을 열면 로그인을 거쳐 돌아온다. 돌아온 화면의 제목을 기다린다. */
async function openSignedIn(user: UserEvent, path = CONSENT_PATH): Promise<HTMLElement> {
  render(<TestApp initialPath={path} />);
  expect(await screen.findByTestId("login-page")).toBeInTheDocument();
  await signInLocal(user);
  return screen.findByRole("heading", { name: /대감에 연결하려고 합니다$/ });
}

/**
 * MCP 클라이언트 연결 동의 (P33 16절). 클라이언트는 스스로 밝힌 것이라 CIMD 와 DCR 모두 "직접 시작하지 않았다면 거절"
 * 경고가 같은 크기로 보인다. 허용하면 서버가 준 주소로만 떠나고, 최근 로그인이 아니면 다시 로그인한 뒤 돌아온다.
 */
describe("MCP client consent", () => {
  let assign: MockInstance<(url: string) => void>;

  beforeEach(() => {
    authConfigState.mode = "local";
    assign = vi.spyOn(leave, "assign").mockImplementation(() => undefined);
  });

  it("shows a DCR client as self-declared, the target, the scope and the loopback warning", async () => {
    server.use(http.get("/me", () => HttpResponse.json({ ...meAdmin, name: "alice" })));
    const user = userEvent.setup();
    expect(await openSignedIn(user)).toHaveTextContent("Script Client가 대감에 연결하려고 합니다");
    // 탭 제목은 화면 언어의 제품 이름이다 (P80 7절)
    expect(document.title).toBe("대감");
    expect(lastRequest("POST", "/oauth/consent/inspect")?.body).toEqual({ request: CONSENT_HANDLE });
    expect(screen.getByTestId("consent-client")).toHaveTextContent("클라이언트가 스스로 밝힌 이름입니다.");
    expect(screen.queryByTestId("consent-name-collides")).not.toBeInTheDocument();
    const target = screen.getByTestId("consent-target");
    expect(target).toHaveTextContent("계산 Gateway");
    expect(target).toHaveTextContent("https://gw.acme.example/mcp");
    expect(screen.getByTestId("consent-scope")).toHaveTextContent(
      "acme의 Gateway, 노출한 MCP Server, 노출한 Agent 를 alice의 이름으로 부를 수 있습니다. 액세스 토큰은 15분마다 새로 받고 연결은 최대 30일 유지됩니다.",
    );
    expect(screen.getByTestId("consent-loopback")).toHaveTextContent("로그인 결과를 이 컴퓨터(localhost:53112)로 보냅니다.");
    expect(screen.getByTestId("consent-loopback")).toHaveTextContent("이 연결을 직접 시작하지 않았다면 거절해 주세요.");
    expect(screen.getByTestId("consent-approve")).toBeEnabled();
  });

  it("names only the host that published a CIMD document and still warns", async () => {
    consentState.view = consentViewFixture({ client: { name: "Claude Code", ref: CLAUDE, kind: "cimd", host: "claude.ai", name_collides: false } });
    const user = userEvent.setup();
    expect(await openSignedIn(user)).toHaveTextContent("Claude Code가 대감에 연결하려고 합니다");
    expect(screen.getByTestId("consent-client")).toHaveTextContent("claude.ai가 공개한 클라이언트 정보입니다.");
    // 신원을 확인했다는 말을 쓰지 않는다. 직접 시작 경고는 DCR 과 같은 크기로 보인다
    expect(screen.queryByText(/확인된|확인했|인증된|verified/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("consent-loopback")).toHaveTextContent("이 컴퓨터의 어떤 프로그램이든 이 클라이언트를 내세울 수 있습니다.");
  });

  it("warns when a self-declared name mimics a known client", async () => {
    consentState.view = consentViewFixture({ client: { name: "Claude Code helper", ref: "dcr:1", kind: "dcr", host: null, name_collides: true } });
    const user = userEvent.setup();
    await openSignedIn(user);
    expect(screen.getByTestId("consent-name-collides")).toHaveTextContent("잘 알려진 클라이언트의 이름을 쓰고 있지만 스스로 밝힌 이름입니다.");
  });

  it("gives an outsider only the public address and no approve button", async () => {
    const hidden = { kind: null, name: null, display_name: null, tenant: null };
    consentState.view = consentViewFixture({ member: false, resource: { url: "https://gw.acme.example/mcp", ...hidden } });
    const user = userEvent.setup();
    await openSignedIn(user);
    expect(screen.getByTestId("consent-target")).toHaveTextContent("https://gw.acme.example/mcp");
    expect(screen.getByTestId("consent-not-member")).toHaveTextContent("이 테넌트의 멤버만 연결할 수 있습니다.");
    expect(screen.queryByTestId("consent-approve")).not.toBeInTheDocument();
    expect(screen.queryByText("계산 Gateway")).not.toBeInTheDocument();
    expect(screen.queryByTestId("consent-scope")).not.toBeInTheDocument();
    expect(screen.getByTestId("consent-deny")).toBeInTheDocument();
  });

  it.each([
    ["request_expired", "요청이 만료됐습니다. 클라이언트에서 연결을 다시 시작해 주세요."],
    ["request_used", "이미 답한 요청입니다. 클라이언트에서 연결을 다시 시작해 주세요."],
  ])("says when the request is %s", async (code, sentence) => {
    consentState.inspectError = code;
    const user = userEvent.setup();
    render(<TestApp initialPath={CONSENT_PATH} />);
    await signInLocal(user);
    expect(await screen.findByTestId("consent-error")).toHaveTextContent(sentence);
  });

  it("maps a known ?error= code to its sentence without asking to sign in", async () => {
    render(<TestApp initialPath="/oauth/consent?error=client_metadata_unavailable" />);
    expect(await screen.findByTestId("consent-error")).toHaveTextContent("클라이언트 정보를 받지 못했습니다.");
    // 확인하지 않은 요청의 오류라 로그인할 것이 없다
    expect(screen.queryByTestId("login-page")).not.toBeInTheDocument();
  });

  it("never shows the text of an unknown ?error= value", async () => {
    render(<TestApp initialPath={`/oauth/consent?error=${encodeURIComponent("<b>injected</b> text")}`} />);
    expect(await screen.findByTestId("consent-error")).toHaveTextContent(dictionary("ko").auth.consent.generic);
    expect(document.body.textContent).not.toContain("injected");
  });

  it("leaves for the address the server gave on approve", async () => {
    const user = userEvent.setup();
    await openSignedIn(user);
    await user.click(screen.getByTestId("consent-approve"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED_REDIRECT));
    expect(await screen.findByTestId("consent-notice")).toHaveTextContent("클라이언트로 돌아갑니다. 이 탭은 닫아도 됩니다.");
    expect(lastRequest("POST", "/oauth/consent/approve")?.body).toEqual({ request: CONSENT_HANDLE });
  });

  it("leaves with access_denied on deny", async () => {
    const user = userEvent.setup();
    await openSignedIn(user);
    await user.click(screen.getByTestId("consent-deny"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(DENIED_REDIRECT));
  });

  it("says why an approve failed without leaving", async () => {
    consentState.approveErrors = ["consent_limit"];
    const user = userEvent.setup();
    await openSignedIn(user);
    await user.click(screen.getByTestId("consent-approve"));
    expect(await screen.findByTestId("consent-failure")).toHaveTextContent("짧은 시간에 연결을 너무 많이 허용했습니다.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("says that MCP sign-in waits for platform tokens when the OAuth server is dormant", async () => {
    consentState.approveErrors = ["temporarily_unavailable"];
    const user = userEvent.setup();
    await openSignedIn(user);
    await user.click(screen.getByTestId("consent-approve"));
    expect(await screen.findByTestId("consent-failure")).toHaveTextContent("지금은 MCP 로그인 연결을 허용할 수 없습니다.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("signs in again when approve needs a recent sign-in and comes back to the same request", async () => {
    consentState.approveErrors = ["reauth_required"];
    const user = userEvent.setup();
    await openSignedIn(user);
    await user.click(screen.getByTestId("consent-approve"));
    // 저장된 토큰이 있어도 로그인 폼이 보인다
    expect(await screen.findByTestId("login-reauth")).toHaveTextContent("보안을 위해 다시 로그인해 주세요.");
    expect(getAuthState().token).not.toBeNull();
    await signInLocal(user);
    await screen.findByRole("heading", { name: /대감에 연결하려고 합니다$/ });
    await user.click(screen.getByTestId("consent-approve"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(APPROVED_REDIRECT));
  });

  it("tells a deployment without local sign-in that MCP login is not used", async () => {
    authConfigState.mode = "header";
    render(<TestApp initialPath={CONSENT_PATH} />);
    expect(await screen.findByTestId("consent-notice")).toHaveTextContent("이 배포본은 MCP 로그인 연결을 쓰지 않습니다.");
  });
});
