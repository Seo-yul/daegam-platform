/**
 * 연결 카드 (P32 10절). Gateway 는 응답의 `auth` 로, MCP Server 는 `useAuthState().mode` 로 local·oidc·anonymous 세
 * 갈래가 된다. 조각에는 토큰 값 대신 env `DAEGAM_PAT` 이 들고, local 은 그 자리에서 리소스 테넌트의 토큰을 받는다.
 * OAuth 가 켜진 local 에서는 "로그인으로 연결" 탭이 맨 앞이고 토큰 갈래는 "토큰으로 연결" 로 묶인다 (P33 16절).
 * 데이터 플레인이 플랫폼을 믿는 oidc 는 local 처럼 그 자리에서 토큰을 받고, IdP 를 믿는 oidc 만 IdP 토큰을 말한다 (P53b
 * 6.1절).
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TestApp } from "@/App";
import { dictionary } from "@/i18n";
import { resetAuthState } from "@/lib/auth";
import { CONNECT_CLIENTS, LOGIN_CLIENTS, connectClients, connectSnippets } from "@/lib/connectSnippets";
import { signInLocal } from "./localSignIn";
import { oidcConfig, signedInUser } from "./oidcSignIn";
import { oidcStub } from "./stubs/oidc-client-ts";
import {
  HttpResponse,
  LOCAL_ADMIN_PASSWORD,
  PAT_VALUE,
  authConfigState,
  exposedGatewayFixture,
  exposedMcpServerFixture,
  gatewayFixture,
  gatewayState,
  http,
  lastRequest,
  localGatewayFixture,
  mcpServerState,
  meTokenHolder,
  oauthConfigHandler,
  server,
} from "./msw";

const GW_URL = "http://gw-support.e2e.local/mcp";
const ko = dictionary("ko");

/** 30초 전에 IdP 에서 로그인한 oidc 사람과 그 모드의 화면. 소속은 default 테넌트다. */
function oidcSignedIn(issuer: "idp" | "platform", oauth = false): void {
  authConfigState.mode = "oidc";
  oidcStub.user = signedInUser(30);
  server.use(
    http.get("/auth/config", () => HttpResponse.json(oidcConfig(issuer, { oauth }))),
    http.get("/me", () => HttpResponse.json(meTokenHolder)),
  );
}

function snippetText(): string {
  return screen.getByTestId("connect-snippets").textContent ?? "";
}

/** 탭마다 조각을 모두 읽는다. 토큰 값이 어디에도 없는지 보는 데 쓴다. */
async function allSnippets(user: ReturnType<typeof userEvent.setup>): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const client of CONNECT_CLIENTS) {
    await user.click(within(screen.getByTestId("connect-card")).getByTestId(`tab-${client}`));
    out[client] = snippetText();
  }
  return out;
}

describe("connect snippets", () => {
  const target = { name: "gw-support", url: GW_URL };

  it("point at the DAEGAM_PAT env, never at a token value", () => {
    const code = (client: (typeof CONNECT_CLIENTS)[number]) => connectSnippets(ko, client, { ...target, via: "token" }).map((s) => s.code).join("\n");
    expect(code("claude")).toContain(`claude mcp add --transport http gw-support ${GW_URL} --header "Authorization: Bearer \${DAEGAM_PAT}"`);
    expect(JSON.parse(connectSnippets(ko, "claude", { ...target, via: "token" })[1].code)).toEqual({
      mcpServers: { "gw-support": { type: "http", url: GW_URL, headers: { Authorization: "Bearer ${DAEGAM_PAT}" } } },
    });
    expect(JSON.parse(code("cursor"))).toEqual({ mcpServers: { "gw-support": { url: GW_URL, headers: { Authorization: "Bearer ${env:DAEGAM_PAT}" } } } });
    // VS Code 는 env 대신 처음 쓸 때 토큰을 묻는다
    expect(JSON.parse(code("vscode"))).toEqual({
      inputs: [{ type: "promptString", id: "daegam-token", description: "대감 MCP 토큰", password: true }],
      servers: { "gw-support": { type: "http", url: GW_URL, headers: { Authorization: "Bearer ${input:daegam-token}" } } },
    });
    // 표 이름은 늘 따옴표로 감싼다. 이름의 . 가 중첩 표를 만들지 못한다 (P55a 2절, 내보내기와 같은 꼴)
    expect(code("codex")).toBe(`[mcp_servers."gw-support"]\nurl = "${GW_URL}"\nbearer_token_env_var = "DAEGAM_PAT"`);
    expect(code("curl")).toContain(`curl -sS ${GW_URL} -H "Authorization: Bearer $DAEGAM_PAT"`);
    expect(code("curl")).toContain('"method":"initialize"');
  });

  it("write the VS Code token prompt in the screen language at the moment they are made", () => {
    const description = (language: "ko" | "en") => JSON.parse(connectSnippets(dictionary(language), "vscode", { ...target, via: "token" })[0].code).inputs[0].description;
    expect(description("ko")).toBe("대감 MCP 토큰");
    expect(description("en")).toBe("Daegam MCP token");
  });

  it("drop the Authorization header when the boundary is anonymous", () => {
    for (const client of CONNECT_CLIENTS) {
      const text = connectSnippets(ko, client, { ...target, via: "open" }).map((s) => s.code).join("\n");
      expect(text, client).toContain(GW_URL);
      expect(text, client).not.toMatch(/Authorization|DAEGAM_PAT|bearer_token|inputs/);
    }
  });

  it("carry only the address when the client signs in by itself, and leave curl out", () => {
    expect(connectClients("login")).toEqual(["claude", "cursor", "vscode", "codex"]);
    expect(connectClients("token")).toEqual(CONNECT_CLIENTS);
    const snippets = (client: (typeof LOGIN_CLIENTS)[number]) => connectSnippets(ko, client, { ...target, via: "login" });
    expect(snippets("claude")[0]).toMatchObject({ code: `claude mcp add --transport http gw-support ${GW_URL}`, note: "추가한 뒤 Claude Code 세션에서 /mcp 로 로그인합니다." });
    expect(JSON.parse(snippets("claude")[1].code)).toEqual({ mcpServers: { "gw-support": { type: "http", url: GW_URL } } });
    expect(JSON.parse(snippets("vscode")[0].code)).toEqual({ servers: { "gw-support": { type: "http", url: GW_URL } } });
    expect(snippets("vscode")[0].note).toBe("처음 쓸 때 VS Code 가 브라우저 로그인을 엽니다.");
    expect(JSON.parse(snippets("cursor")[0].code)).toEqual({ mcpServers: { "gw-support": { url: GW_URL } } });
    expect(snippets("codex")[0].code).toBe(`codex mcp add gw-support --url ${GW_URL}\ncodex mcp login gw-support`);
    for (const client of LOGIN_CLIENTS) {
      expect(snippets(client).map((s) => s.code).join("\n"), client).not.toMatch(/Authorization|DAEGAM_PAT|bearer_token|inputs/);
    }
  });
});

describe("connect card on the Gateway overview", () => {
  it("local: issues a token for the gateway's tenant right there and shows the snippets", async () => {
    authConfigState.mode = "local";
    gatewayState.items = [localGatewayFixture()];
    server.use(http.get("/me", () => HttpResponse.json(meTokenHolder)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    await signInLocal(user);
    const card = await screen.findByTestId("connect-card");
    // 주소 카드 바로 뒤, 참조 카드 앞이다
    const address = screen.getByTestId("address-card");
    expect(address.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.compareDocumentPosition(screen.getByTestId("references-card")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // 테넌트는 그 Gateway 의 것으로 고정이고 이름은 "<표시 이름> 연결" 이 처음 값이다
    expect(within(card).queryByTestId("it-tenant")).not.toBeInTheDocument();
    expect(within(card).getByTestId("it-label")).toHaveValue("support 연결");
    // 토큰을 셸 기록에 남기지 않는 입력법을 보인다
    expect(within(card).getByTestId("connect-read-pat")).toHaveTextContent("read -rs DAEGAM_PAT && export DAEGAM_PAT");
    expect(card).toHaveTextContent("토큰을 명령줄에 쓰면 셸 기록에 남습니다.");
    expect(within(card).getByTestId("connect-scope")).toHaveTextContent("이 토큰은 default의 Gateway, 노출한 MCP Server, 노출한 Agent 를 부를 수 있습니다.");
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent(`claude mcp add --transport http gw-support ${GW_URL}`);
    expect(card).toHaveTextContent("셸이 토큰 값을 펼쳐 Claude Code 설정에 그대로 적습니다.");
    // OAuth 가 꺼져 있으면 로그인 갈래가 없다
    expect(within(card).queryByTestId("tab-login")).not.toBeInTheDocument();

    await user.type(within(card).getByTestId("it-password"), LOCAL_ADMIN_PASSWORD);
    await user.click(within(card).getByTestId("it-submit"));
    expect(await within(card).findByTestId("issued-token-value")).toHaveTextContent(PAT_VALUE);
    expect(lastRequest("POST", "/me/tokens")?.body).toMatchObject({ tenant: "default", label: "support 연결" });
    // 조각은 토큰을 받은 뒤에도 env 를 가리킨다
    const snippets = await allSnippets(user);
    for (const [client, text] of Object.entries(snippets)) {
      expect(text, client).not.toContain(PAT_VALUE);
      expect(text, client).toMatch(client === "vscode" ? /\$\{input:daegam-token\}/ : /DAEGAM_PAT/);
    }
  });

  it("local with OAuth: signing in comes first and the token way is one tab behind", async () => {
    authConfigState.mode = "local";
    gatewayState.items = [localGatewayFixture()];
    server.use(oauthConfigHandler, http.get("/me", () => HttpResponse.json(meTokenHolder)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    await signInLocal(user);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("tab-login")).toHaveTextContent("로그인으로 연결");
    expect(within(card).getByTestId("tab-login")).toHaveAttribute("aria-selected", "true");
    expect(within(card).getByTestId("tab-token")).toHaveTextContent("토큰으로 연결");
    expect(within(card).getByTestId("connect-login")).toHaveTextContent("설정에는 주소만 넣습니다.");
    expect(within(card).getByTestId("connect-login-fallback")).toHaveTextContent("클라이언트가 로그인을 시작하지 못하면 토큰으로 연결해 주세요.");
    // 로그인 조각에는 주소만 있고 발급 폼과 curl 이 없다
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent(`claude mcp add --transport http gw-support ${GW_URL}`);
    expect(within(card).getByTestId("snippet-claude-cli")).not.toHaveTextContent("Authorization");
    expect(within(card).queryByTestId("tab-curl")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("issue-token-form")).not.toBeInTheDocument();
    await user.click(within(card).getByTestId("tab-codex"));
    expect(within(card).getByTestId("snippet-codex-cli")).toHaveTextContent("codex mcp login gw-support");
    // 토큰 갈래는 P32 의 발급 폼과 헤더 조각 그대로다
    await user.click(within(card).getByTestId("tab-token"));
    expect(within(card).getByTestId("issue-token-form")).toBeInTheDocument();
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent("Authorization: Bearer ${DAEGAM_PAT}");
    expect(within(card).getByTestId("tab-curl")).toBeInTheDocument();
    expect(within(card).queryByTestId("connect-login")).not.toBeInTheDocument();
  });

  it("local with OAuth but no external address has no sign-in way either", async () => {
    authConfigState.mode = "local";
    gatewayState.items = [gatewayFixture({ auth: "local" })];
    server.use(oauthConfigHandler);
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    await signInLocal(user);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("connect-no-address")).toBeInTheDocument();
    expect(within(card).queryByTestId("tab-login")).not.toBeInTheDocument();
  });

  it("local: someone outside the tenant gets the snippets but no issue form", async () => {
    // 목의 기본 /me 는 * 하나만 가진 platform-admin 이다. * 는 소속이 아니다
    authConfigState.mode = "local";
    gatewayState.items = [localGatewayFixture()];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    await signInLocal(user);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("connect-not-member")).toHaveTextContent("이 테넌트의 멤버만 토큰을 받을 수 있습니다. 멤버 계정으로 로그인해 주세요.");
    expect(within(card).queryByTestId("issue-token-form")).not.toBeInTheDocument();
    expect(within(card).getByTestId("snippet-claude-cli")).toBeInTheDocument();
  });

  it("without an external address only says so and points at the deployment tab", async () => {
    gatewayState.items = [gatewayFixture({ auth: "local" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("connect-no-address")).toHaveTextContent("외부 주소가 없어 클러스터 밖의 MCP 클라이언트가 호출할 수 없습니다.");
    expect(within(card).queryByTestId("connect-snippets")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("issue-token-form")).not.toBeInTheDocument();
    await user.click(within(card).getByTestId("connect-deploy"));
    await waitFor(() => expect(screen.getByTestId("tab-deployment")).toHaveAttribute("aria-selected", "true"));
  });

  it("oidc trusting the platform: issues a token after a recent IdP sign-in, as local mode does", async () => {
    oidcSignedIn("platform");
    gatewayState.items = [exposedGatewayFixture({ auth: "oidc" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    const card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("issue-token-form")).toBeInTheDocument());
    // IdP 토큰을 말하지 않고, 비밀번호 칸 대신 최근 IdP 로그인을 본다
    expect(within(card).queryByTestId("connect-oidc")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("it-password")).not.toBeInTheDocument();
    expect(within(card).getByTestId("it-label")).toHaveValue("support 연결");
    expect(within(card).getByTestId("connect-scope")).toHaveTextContent("이 토큰은 default의 Gateway, 노출한 MCP Server, 노출한 Agent 를 부를 수 있습니다.");
    // 인가 서버가 꺼져 있으면 로그인 갈래가 없다
    expect(within(card).queryByTestId("tab-login")).not.toBeInTheDocument();
    // 목의 /me/tokens 는 비밀번호 없는 발급을 받지 않으므로 보낸 요청만 본다. 최근 로그인이라 IdP 로 떠나지 않는다
    await user.click(within(card).getByTestId("it-submit"));
    await waitFor(() => expect(lastRequest("POST", "/me/tokens")).toBeDefined());
    const sent = lastRequest("POST", "/me/tokens")!;
    expect(sent.body).toMatchObject({ tenant: "default", label: "support 연결" });
    expect(sent.body).not.toHaveProperty("password");
    expect(sent.headers["x-daegam-id-token"]).toBe(oidcStub.user?.id_token);
    expect(oidcStub.signinRedirectCalls).toHaveLength(0);
  });

  it("oidc trusting the platform with OAuth awake: signing in comes first", async () => {
    oidcSignedIn("platform", true);
    gatewayState.items = [exposedGatewayFixture({ auth: "oidc" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    const card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("tab-login")).toHaveAttribute("aria-selected", "true"));
    expect(within(card).getByTestId("connect-login")).toBeInTheDocument();
    expect(within(card).queryByTestId("connect-oidc")).not.toBeInTheDocument();
    await user.click(within(card).getByTestId("tab-token"));
    expect(within(card).getByTestId("issue-token-form")).toBeInTheDocument();
  });

  it("oidc trusting the platform: a form that left for a fresh sign-in comes back on the token tab with its input", async () => {
    oidcSignedIn("platform", true);
    oidcStub.user = signedInUser(3600);
    gatewayState.items = [exposedGatewayFixture({ auth: "oidc" })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    let card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("tab-login")).toHaveAttribute("aria-selected", "true"));
    await user.click(within(card).getByTestId("tab-token"));
    await user.clear(within(card).getByTestId("it-label"));
    await user.type(within(card).getByTestId("it-label"), "ci runner");
    // 한 시간 전의 로그인이라 입력을 남기고 IdP 로 떠난다
    await user.click(within(card).getByTestId("it-submit"));
    await waitFor(() => expect(oidcStub.signinRedirectCalls).toHaveLength(1));
    expect(lastRequest("POST", "/me/tokens")).toBeUndefined();

    // 새로 로그인하고 같은 주소로 돌아온 페이지. 로그인 탭이 아니라 입력이 남은 토큰 탭에서 연다
    cleanup();
    resetAuthState();
    oidcStub.user = signedInUser(30);
    render(<TestApp initialPath="/gateways/gw-support" />);
    card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("tab-token")).toHaveAttribute("aria-selected", "true"));
    expect(within(card).getByTestId("it-label")).toHaveValue("ci runner");
  });

  it("oidc trusting the IdP: tells to use the IdP token and keeps the env snippets", async () => {
    oidcSignedIn("idp");
    gatewayState.items = [exposedGatewayFixture({ auth: "oidc" })];
    render(<TestApp initialPath="/gateways/gw-support" />);
    const card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("connect-oidc")).toHaveTextContent("플랫폼이 발급한 토큰은 받지 않습니다."));
    expect(within(card).getByTestId("connect-oidc")).toHaveTextContent("IdP 가 MCP 클라이언트 등록을 받으면 클라이언트가 스스로 IdP 로그인을 열 수도 있습니다.");
    expect(within(card).queryByTestId("tab-login")).not.toBeInTheDocument();
    expect(within(card).queryByTestId("issue-token-form")).not.toBeInTheDocument();
    expect(within(card).getByTestId("connect-read-pat")).toBeInTheDocument();
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent("Authorization: Bearer ${DAEGAM_PAT}");
    expect(within(card).queryByTestId("connect-scope")).not.toBeInTheDocument();
  });

  it("anonymous: warns that anyone who reaches the address can call it and drops the token", async () => {
    gatewayState.items = [exposedGatewayFixture()];
    const user = userEvent.setup();
    render(<TestApp initialPath="/gateways/gw-support" />);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("connect-anonymous")).toHaveTextContent("인증 없이 열려 있어 이 주소에 접속할 수 있으면 누구나 Tool 을 호출할 수 있습니다.");
    expect(within(card).queryByTestId("connect-read-pat")).not.toBeInTheDocument();
    for (const [client, text] of Object.entries(await allSnippets(user))) {
      expect(text, client).toContain(GW_URL);
      expect(text, client).not.toContain("Authorization");
    }
  });
});

describe("connect card on the MCP Server overview", () => {
  it("is not there while the server is not exposed", async () => {
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    expect(await screen.findByTestId("address-card")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-card")).not.toBeInTheDocument();
  });

  it("follows the platform auth mode: header is anonymous", async () => {
    mcpServerState.items = [exposedMcpServerFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("connect-anonymous")).toBeInTheDocument();
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent("claude mcp add --transport http mcp-calc https://calc.default.apps.local/mcp");
  });

  it("follows the platform auth mode: local issues a token for the server's tenant", async () => {
    authConfigState.mode = "local";
    mcpServerState.items = [exposedMcpServerFixture()];
    server.use(http.get("/me", () => HttpResponse.json(meTokenHolder)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    await signInLocal(user);
    const card = await screen.findByTestId("connect-card");
    expect(within(card).getByTestId("it-label")).toHaveValue("calc 연결");
    expect(within(card).getByTestId("snippet-claude-cli")).toHaveTextContent("Authorization: Bearer ${DAEGAM_PAT}");
  });

  it("follows the platform auth mode: oidc issues a token while the data plane trusts the platform", async () => {
    oidcSignedIn("platform");
    mcpServerState.items = [exposedMcpServerFixture()];
    render(<TestApp initialPath="/mcp-servers/ms-calc" />);
    const card = await screen.findByTestId("connect-card");
    await waitFor(() => expect(within(card).getByTestId("issue-token-form")).toBeInTheDocument());
    expect(within(card).queryByTestId("connect-oidc")).not.toBeInTheDocument();
    expect(within(card).getByTestId("it-label")).toHaveValue("calc 연결");
  });
});
