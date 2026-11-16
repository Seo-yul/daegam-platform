/**
 * 연결 카드의 설정 조각 (P32 10절, P33 16절). 밖의 MCP 클라이언트 다섯이 Gateway 나 노출한 MCP Server 를 부르는 설정이다.
 *
 * 인증 갈래(`ConnectVia`)가 셋이다. token 은 개인 토큰 헤더를 얹는다. 조각에는 토큰 값을 넣지 않고 env `DAEGAM_PAT` 을
 * 가리킨다. 그래야 화면과 설정 파일에 토큰이 남지 않는다. `DAEGAM_MCP_TOKEN` 은 에이전트 파드가 D41 서비스 토큰에 이미
 * 쓰는 이름이라 피한다. login 은 주소만 넣고 클라이언트가 401 에서 스스로 브라우저 로그인을 연다(OAuth). curl 은 로그인을
 * 열지 못하므로 login 에는 없다. open 은 인증 경계가 anonymous 라 헤더 없는 조각이다.
 *
 * 조각의 제목과 안내는 사전의 `detail.connectCard.snippets` 다. 파일 경로 제목(~/.cursor/mcp.json 등)은 말이 아니라 여기 둔다.
 * VS Code 조각의 토큰 설명은 사람이 설정 파일에 붙여 넣어 지니는 글이라 조각을 만드는 순간의 화면 언어를 따른다(P49 5절).
 *
 * Claude Code 와 Codex 의 설정 파일 꼴(`claudeConfig`, `codexConfig`)은 허브의 클라이언트 설정 내보내기(`lib/clientExport`)와
 * 함께 쓴다 (P55a 2절). 서버 하나를 적는 규칙이 두 곳에 따로 있으면 한쪽만 바뀐다.
 */
import type { Messages } from "@/i18n";

/** 개인 MCP 토큰을 담는 env 이름. Agent 외부 호출 카드의 curl 예시도 이 이름을 쓴다. */
export const PAT_ENV = "DAEGAM_PAT";

/** 토큰을 env 로 두는 명령. `export DAEGAM_PAT=<토큰>` 은 셸 기록에 토큰을 남긴다. */
export function readEnvCommand(env: string): string {
  return `read -rs ${env} && export ${env}`;
}

export const READ_PAT_COMMAND = readEnvCommand(PAT_ENV);

export const CONNECT_CLIENTS = ["claude", "cursor", "vscode", "codex", "curl"] as const;
export type ConnectClient = (typeof CONNECT_CLIENTS)[number];

/** 스스로 브라우저 로그인을 여는 클라이언트. curl 은 빠진다. */
export const LOGIN_CLIENTS = CONNECT_CLIENTS.filter((c) => c !== "curl");

/** 클라이언트가 어떻게 인증하는가. 개인 토큰 헤더, 스스로 여는 브라우저 로그인(P33), 인증 없음(anonymous) 셋이다. */
export type ConnectVia = "token" | "login" | "open";

/** OAuth 인가 서버가 켜졌을 때 연결 카드와 내보내기 맨 위의 갈래. 로그인이 먼저다. */
export const CONNECT_MODES = ["login", "token"] as const;
export type ConnectMode = (typeof CONNECT_MODES)[number];

export const CLIENT_LABEL: Record<ConnectClient, string> = {
  claude: "Claude Code",
  cursor: "Cursor",
  vscode: "VS Code",
  codex: "Codex",
  curl: "curl",
};

export interface Snippet {
  id: string;
  title: string;
  code: string;
  note?: string;
}

/** `name` 은 리소스의 K8s 이름이고 `url` 은 `status.external_url` 이다. */
export interface ConnectTarget {
  name: string;
  url: string;
  via: ConnectVia;
}

/** 그 갈래가 고를 수 있는 클라이언트 탭. */
export function connectClients(via: ConnectVia): readonly ConnectClient[] {
  return via === "login" ? LOGIN_CLIENTS : CONNECT_CLIENTS;
}

/** 클라이언트가 실행할 때 env 를 펼치는 꼴. Claude Code 설정과 셸이 같은 문법을 쓴다. */
function bearerEnv(env: string): string {
  return `Bearer \${${env}}`;
}

const BEARER_ENV = bearerEnv(PAT_ENV);
const INITIALIZE = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}';

type SnippetText = Messages["detail"]["connectCard"]["snippets"];

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function withToken(target: ConnectTarget): boolean {
  return target.via === "token";
}

/** 토큰이 있으면 Authorization 헤더를 얹은 서버 항목이다. */
function server(base: Record<string, unknown>, target: ConnectTarget, authorization: string): Record<string, unknown> {
  return withToken(target) ? { ...base, headers: { Authorization: authorization } } : base;
}

/**
 * 설정 파일에 적는 서버 하나. `url` 은 클라이언트가 그대로 받는 주소이고, `tokenEnv` 는 토큰을 담은 env 이름이다. 없으면 주소만
 * 적는다(로그인 갈래, 인증 없음, 직접 연결 항목). 토큰 값은 어디에도 적지 않는다.
 */
export interface ClientServer {
  name: string;
  url: string;
  tokenEnv?: string;
  /** SSE 만 말하는 서버(허브 항목의 `source.transport` 가 sse). Codex 는 streamable HTTP 만 읽어 이 서버를 적지 않는다. */
  sse?: boolean;
}

/** Claude Code `mcpServers` 의 한 항목. `type` 이 없고 `url` 만 있으면 Claude Code 가 stdio 로 읽고 건너뛴다. */
function claudeServer({ url, tokenEnv, sse }: ClientServer): Record<string, unknown> {
  const base = { type: sse ? "sse" : "http", url };
  return tokenEnv ? { ...base, headers: { Authorization: bearerEnv(tokenEnv) } } : base;
}

/** Claude Code 의 `.mcp.json` (또는 `~/.claude.json` 에 합칠 `mcpServers`). env 는 Claude Code 가 실행할 때 펼친다. */
export function claudeConfig(servers: readonly ClientServer[]): string {
  return json({ mcpServers: Object.fromEntries(servers.map((s) => [s.name, claudeServer(s)])) });
}

/**
 * TOML 기본 문자열. `"` 와 `\` 와 제어 글자를 이스케이프한다. 표 이름도 늘 이것으로 적어, 이름의 `.` 가 중첩 표를 만들지
 * 못하게 한다.
 */
export function tomlString(value: string): string {
  const escaped = value.replace(/[\\"\u0000-\u001f\u007f]/g, (c) => (c === "\\" || c === '"' ? `\\${c}` : `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`));
  return `"${escaped}"`;
}

/**
 * Codex 의 `config.toml` 에 둘 `[mcp_servers."<name>"]` 표들. 토큰은 `bearer_token_env_var` 로 env 이름만 적는다. Codex 는
 * streamable HTTP 만 읽으므로 SSE 서버는 적지 않는다.
 */
export function codexConfig(servers: readonly ClientServer[]): string {
  return servers
    .filter((s) => !s.sse)
    .map(({ name, url, tokenEnv }) => {
      const lines = [`[mcp_servers.${tomlString(name)}]`, `url = ${tomlString(url)}`];
      if (tokenEnv) lines.push(`bearer_token_env_var = ${tomlString(tokenEnv)}`);
      return lines.join("\n");
    })
    .join("\n\n");
}

/** Codex 사용자 설정 파일. 연결 카드 조각의 제목이자 내보내기 미리보기의 제목이다. 말이 아니라 경로라 사전에 두지 않는다. */
export const CODEX_CONFIG_PATH = "~/.codex/config.toml";

/** 클라이언트가 그 서버의 브라우저 로그인을 여는 명령. */
export function loginCommand(client: "claude" | "codex", name: string): string {
  return `${client} mcp login ${name}`;
}

function cardServer(target: ConnectTarget): ClientServer {
  return { name: target.name, url: target.url, tokenEnv: withToken(target) ? PAT_ENV : undefined };
}

function claude(text: SnippetText, target: ConnectTarget): Snippet[] {
  const header = withToken(target) ? ` --header "Authorization: ${BEARER_ENV}"` : "";
  // anonymous(open)는 안내가 없다
  const notes: { cli?: string; json?: string } = target.via === "open" ? {} : text.claude[target.via];
  return [
    { id: "claude-cli", title: text.commandLine, code: `claude mcp add --transport http ${target.name} ${target.url}${header}`, note: notes.cli },
    { id: "claude-json", title: text.projectJson, code: claudeConfig([cardServer(target)]), note: notes.json },
  ];
}

function cursor(text: SnippetText, target: ConnectTarget): Snippet[] {
  const note = target.via === "login" ? text.cursorLogin : undefined;
  return [{ id: "cursor-json", title: "~/.cursor/mcp.json", code: json({ mcpServers: { [target.name]: server({ url: target.url }, target, `Bearer \${env:${PAT_ENV}}`) } }), note }];
}

function vscode(text: SnippetText, target: ConnectTarget): Snippet[] {
  const input = { type: "promptString", id: "daegam-token", description: text.vscodeInput, password: true };
  const servers = { [target.name]: server({ type: "http", url: target.url }, target, "Bearer ${input:daegam-token}") };
  const note = target.via === "open" ? undefined : text.vscode[target.via];
  return [{ id: "vscode-json", title: ".vscode/mcp.json", code: json(withToken(target) ? { inputs: [input], servers } : { servers }), note }];
}

function codex(text: SnippetText, target: ConnectTarget): Snippet[] {
  if (target.via === "login") {
    const code = [`codex mcp add ${target.name} --url ${target.url}`, loginCommand("codex", target.name)].join("\n");
    return [{ id: "codex-cli", title: text.commandLine, code, note: text.codexLogin }];
  }
  return [{ id: "codex-toml", title: CODEX_CONFIG_PATH, code: codexConfig([cardServer(target)]) }];
}

function curl(text: SnippetText, target: ConnectTarget): Snippet[] {
  const auth = withToken(target) ? ` -H "Authorization: Bearer $${PAT_ENV}"` : "";
  const code = [`curl -sS ${target.url}${auth} \\`, `  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\`, `  -d '${INITIALIZE}'`].join("\n");
  return [{ id: "curl", title: text.initialize, code }];
}

const BUILDERS: Record<ConnectClient, (text: SnippetText, target: ConnectTarget) => Snippet[]> = { claude, cursor, vscode, codex, curl };

/** 한 클라이언트의 설정 조각. 제목과 안내, VS Code 의 토큰 설명은 지금 화면 언어다. */
export function connectSnippets(t: Messages, client: ConnectClient, target: ConnectTarget): Snippet[] {
  return BUILDERS[client](t.detail.connectCard.snippets, target);
}
