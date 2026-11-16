/**
 * 허브 목록의 클라이언트 설정 내보내기 대화상자 (P55a 2절·8절). 파일은 요청 테넌트 하나의 것이고 토큰 값이 없다. 갈래는 연결
 * 카드와 같다. header 모드는 주소만 담고 경고를 보이고, local 은 테넌트의 토큰 env 를 가리키며, OAuth 가 켜졌으면 로그인이 먼저다.
 * 항목의 주소 갈래는 서버가 준 `status.address_class` 대로 흐려지거나 막힌다. 플랫폼 관리자는 회사 전체 허용 목록도 받는다.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

import { TestApp } from "@/App";
import { ko } from "@/i18n/ko";

import { stubDownloads } from "./downloads";
import { signInLocal } from "./localSignIn";
import { HttpResponse, authConfigState, hubDirectFixture as direct, hubItemFixture, hubSharedReadOnly, hubState, http, meTokenHolder, oauthConfigHandler, server } from "./msw";

const text = ko.hub.export;
const card = ko.detail.connectCard;
const EVERYONE = { tenants: ["*"], roles: [], groups: [] };

/** 요청 테넌트의 internal(calc), 사설망·자격 증명·outside 직접 연결 항목, acme 가 연 docs, 주소 없는 image. */
function seed(): void {
  const docs = { ...hubSharedReadOnly, status: { ...hubSharedReadOnly.status!, external_url: "https://docs.example.com/mcp", address_class: "outside" as const } };
  hubState.items = [
    hubItemFixture(),
    direct("vpn", "http://10.0.0.5:8080/mcp", "private"),
    direct("keyed", "https://k.example.com/mcp?key=s3cret", "credentials"),
    direct("search", "https://search.example.com/mcp", "outside", { access: { ...hubItemFixture().access, view: EVERYONE } }),
    docs,
    { ...hubItemFixture(), id: "hub-img", name: "img", display_name: "img", kind: "image" },
  ];
}

async function openDialog(user: UserEvent): Promise<HTMLElement> {
  const button = await screen.findByTestId("hub-export-clients");
  await screen.findByTestId("hub-row-calc");
  await user.click(button);
  return screen.findByTestId("client-export-dialog");
}

function preview(dialog: HTMLElement): string {
  return within(dialog).getByTestId("client-export-file").textContent ?? "";
}

describe("클라이언트 설정 내보내기", () => {
  it("header 모드: 주소만 담고 인증 없음 경고를 보이며, 고를 수 없는 항목은 까닭을 단다", async () => {
    seed();
    const saved = stubDownloads();
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    const dialog = await openDialog(user);
    expect(within(dialog).getByRole("heading", { name: text.title })).toBeInTheDocument();
    expect(within(dialog).getByTestId("client-export-anonymous")).toHaveTextContent(card.anonymous);
    // image 는 주소가 없어 목록에 없다
    expect(within(dialog).queryByTestId("export-item-img")).toBeNull();
    expect(within(dialog).getByTestId("export-item-calc")).toBeChecked();
    expect(within(dialog).getByTestId("export-item-search")).toBeChecked();
    expect(within(dialog).getByTestId("export-item-search-direct")).toHaveTextContent(text.direct);
    expect(within(dialog).getByTestId("client-export-direct-hint")).toHaveTextContent(text.directHint);
    // 사설망 주소는 흐리지만 고를 수 있고 처음에는 고르지 않는다
    expect(within(dialog).getByTestId("export-item-vpn")).not.toBeChecked();
    expect(within(dialog).getByTestId("export-item-vpn")).toBeEnabled();
    expect(within(dialog).getByTestId("export-item-vpn-private")).toHaveTextContent(text.private);
    expect(within(dialog).getByTestId("client-export-private-hint")).toHaveTextContent(text.privateHint);
    // 자격 증명이 들 수 있는 주소는 고를 수 없다
    expect(within(dialog).getByTestId("export-item-keyed")).toBeDisabled();
    expect(within(dialog).getByTestId("export-item-keyed-reason")).toHaveTextContent(text.urlCredentials);

    expect(JSON.parse(preview(dialog))).toEqual({
      mcpServers: {
        calc: { type: "http", url: "https://calc.example.com/mcp" },
        search: { type: "http", url: "https://search.example.com/mcp" },
        "docs-ACME": { type: "http", url: "https://docs.example.com/mcp" },
      },
    });
    expect(preview(dialog)).not.toContain("s3cret");
    await user.click(within(dialog).getByTestId("client-export-download"));
    expect(saved).toEqual(["mcp.json"]);

    await user.click(within(dialog).getByTestId("export-item-vpn"));
    await user.click(within(dialog).getByTestId("tab-codex"));
    expect(preview(dialog)).toContain('[mcp_servers."vpn"]\nurl = "http://10.0.0.5:8080/mcp"');
    expect(preview(dialog)).not.toMatch(/bearer_token|DAEGAM_PAT/);
    await user.click(within(dialog).getByTestId("client-export-download"));
    expect(saved).toEqual(["mcp.json", "config.toml"]);
  });

  it("SSE 항목은 Claude Code 에 type sse 로 들고, Codex 를 고르면 까닭을 달고 막혔다가 돌아가면 다시 든다", async () => {
    const legacy = direct("legacy", "https://legacy.example.com/sse", "outside");
    hubState.items = [hubItemFixture(), { ...legacy, source: { ...legacy.source, transport: "sse" } }];
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    const dialog = await openDialog(user);
    expect(JSON.parse(preview(dialog)).mcpServers.legacy).toEqual({ type: "sse", url: "https://legacy.example.com/sse" });
    await user.click(within(dialog).getByTestId("tab-codex"));
    expect(within(dialog).getByTestId("export-item-legacy")).toBeDisabled();
    expect(within(dialog).getByTestId("export-item-legacy")).not.toBeChecked();
    expect(within(dialog).getByTestId("export-item-legacy-reason")).toHaveTextContent(text.sseCodex);
    expect(preview(dialog)).not.toContain("legacy");
    await user.click(within(dialog).getByTestId("tab-claude"));
    expect(within(dialog).getByTestId("export-item-legacy")).toBeChecked();
    expect(within(dialog).queryByTestId("export-item-legacy-reason")).toBeNull();
  });

  it("플랫폼 관리자는 허용 목록을 받고, 고정 목록은 펼쳐야 보인다", async () => {
    seed();
    const saved = stubDownloads();
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    const dialog = await openDialog(user);
    const company = within(dialog).getByTestId("client-export-company");
    expect(company).toHaveTextContent(text.allowlist);
    expect(company).toHaveTextContent(text.allowlistHint);
    const download = within(company).getByTestId("client-export-allowlist-download");
    // 클러스터를 다 읽어야 노출 도메인이 든다
    await vi.waitFor(() => expect(download).toBeEnabled());
    expect(JSON.parse(within(company).getByTestId("client-export-allowlist").textContent ?? "")).toEqual({
      // acme 의 docs 도 view 가 * 이고 role 과 그룹이 비어 모든 멤버에게 열려 있다. 사설망·자격 증명·calc 는 없다
      allowedMcpServers: [
        { serverUrl: "https://*.apps.local/mcp" },
        { serverUrl: "https://*.*.apps.local/mcp" },
        { serverUrl: "http://*.apps.local/mcp" },
        { serverUrl: "http://*.*.apps.local/mcp" },
        { serverUrl: "https://search.example.com/mcp" },
        { serverUrl: "https://docs.example.com/mcp" },
      ],
    });
    await user.click(download);
    expect(saved).toEqual(["allowed-mcp-servers.json"]);

    expect(within(company).getByTestId("client-export-exclusive-file")).not.toBeVisible();
    await user.click(within(company).getByText(text.exclusive));
    expect(within(company).getByTestId("client-export-exclusive")).toHaveTextContent(text.exclusiveHint);
    expect(JSON.parse(within(company).getByTestId("client-export-exclusive-file").textContent ?? "")).toEqual({
      mcpServers: { search: { type: "http", url: "https://search.example.com/mcp" }, "docs-ACME": { type: "http", url: "https://docs.example.com/mcp" } },
    });
    await user.click(within(company).getByTestId("client-export-exclusive-download"));
    expect(saved).toEqual(["allowed-mcp-servers.json", "managed-mcp.json"]);
    expect(within(company).queryByTestId("client-export-allowlist-skipped")).toBeNull();
  });

  it("허용 목록에 패턴으로 적지 못한 항목은 이름으로 알린다", async () => {
    hubState.items = [hubItemFixture(), direct("glob", "https://glob.example.com/*", "outside", { access: { ...hubItemFixture().access, view: EVERYONE } })];
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    const dialog = await openDialog(user);
    expect(await within(dialog).findByTestId("client-export-allowlist-skipped")).toHaveTextContent(text.allowlistSkipped("glob"));
    expect(within(dialog).getByTestId("client-export-allowlist")).not.toHaveTextContent("glob.example.com");
  });

  it("local: 플랫폼을 거치는 항목만 테넌트의 토큰 env 를 가리키고, 관리자가 아니면 허용 목록이 없다", async () => {
    seed();
    authConfigState.mode = "local";
    server.use(http.get("/me", () => HttpResponse.json(meTokenHolder)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await signInLocal(user);
    const dialog = await openDialog(user);
    expect(within(dialog).getByTestId("client-export-token-hint")).toHaveTextContent(text.tokenHint("default"));
    expect(within(dialog).getByTestId("client-export-tokens-link")).toHaveAttribute("href", "/account/tokens");
    expect(within(dialog).getByTestId("client-export-token-steps")).toHaveTextContent("그다음 클라이언트를 시작할 셸에서 아래 명령으로 DAEGAM_PAT_DEFAULT에 토큰을 넣어 주세요.");
    expect(within(dialog).getByTestId("client-export-read-pat")).toHaveTextContent("read -rs DAEGAM_PAT_DEFAULT && export DAEGAM_PAT_DEFAULT");
    expect(within(dialog).queryByTestId("tab-login")).toBeNull();
    expect(within(dialog).queryByTestId("client-export-company")).toBeNull();
    expect(JSON.parse(preview(dialog)).mcpServers).toEqual({
      calc: { type: "http", url: "https://calc.example.com/mcp", headers: { Authorization: "Bearer ${DAEGAM_PAT_DEFAULT}" } },
      search: { type: "http", url: "https://search.example.com/mcp" },
      "docs-ACME": { type: "http", url: "https://docs.example.com/mcp" },
    });
    await user.click(within(dialog).getByTestId("tab-codex"));
    expect(preview(dialog)).toContain('[mcp_servers."calc"]\nurl = "https://calc.example.com/mcp"\nbearer_token_env_var = "DAEGAM_PAT_DEFAULT"');
    expect(preview(dialog)).toContain('[mcp_servers."search"]\nurl = "https://search.example.com/mcp"\n\n');
  });

  it("local: 요청 테넌트의 멤버가 아니면 발급을 권하지 않고 멤버 계정을 말한다", async () => {
    // 목의 기본 /me 는 * 하나만 가진 platform-admin 이다. * 는 소속이 아니다
    seed();
    authConfigState.mode = "local";
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await signInLocal(user);
    const dialog = await openDialog(user);
    expect(within(dialog).getByTestId("client-export-not-member")).toHaveTextContent(card.notMember);
    expect(within(dialog).queryByTestId("client-export-token-steps")).toBeNull();
    expect(within(dialog).getByTestId("client-export-token-hint")).toHaveTextContent(text.tokenHint("default"));
  });

  it("local 과 OAuth: 로그인 갈래가 먼저이고 주소만 담으며 플랫폼을 거치는 항목의 로그인 명령을 보인다", async () => {
    seed();
    authConfigState.mode = "local";
    server.use(oauthConfigHandler, http.get("/me", () => HttpResponse.json(meTokenHolder)));
    const user = userEvent.setup();
    render(<TestApp initialPath="/hub" />);
    await signInLocal(user);
    const dialog = await openDialog(user);
    expect(within(dialog).getByTestId("tab-login")).toHaveAttribute("aria-selected", "true");
    expect(within(dialog).getByTestId("client-export-login")).toHaveTextContent(card.login);
    expect(within(dialog).getByTestId("client-export-login-commands")).toHaveTextContent("claude mcp login calc");
    expect(within(dialog).getByTestId("client-export-login-commands")).not.toHaveTextContent("search");
    expect(preview(dialog)).not.toMatch(/Authorization|DAEGAM_PAT/);
    await user.click(within(dialog).getByTestId("tab-codex"));
    expect(within(dialog).getByTestId("client-export-login-commands")).toHaveTextContent("codex mcp login calc");
    await user.click(within(dialog).getByTestId("tab-token"));
    expect(preview(dialog)).toContain('bearer_token_env_var = "DAEGAM_PAT_DEFAULT"');
  });
});
