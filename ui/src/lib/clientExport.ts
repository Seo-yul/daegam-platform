/**
 * 클라이언트 설정 내보내기 (P55a 2절, D99). 허브 목록의 `GET /api/hub` 응답에서 사람의 MCP 클라이언트(Claude Code, Codex)가 읽을
 * 설정 파일을 브라우저가 만든다. 라우트는 없다. 서버 하나를 적는 꼴은 연결 카드와 같은 `lib/connectSnippets` 의 것이다.
 *
 * - 주소는 `status.external_url` 만 읽는다. remote·entry 의 주소를 가르는 것은 서버다(`status.address_class`, P55a 1절). 화면은 URL 을
 *   가르지 않는다.
 * - 한 파일은 한 테넌트다. 개인 토큰이 테넌트 하나를 열기 때문이다(D58). 플랫폼을 거치는 항목(internal, published)은 테넌트마다의
 *   env `DAEGAM_PAT_<T>` 를 가리키고, 직접 연결 항목(remote, entry)은 주소만 담는다. 토큰 값은 어디에도 적지 않는다.
 * - 플랫폼 관리자의 허용 목록은 Claude Code 관리 설정의 `allowedMcpServers` 한 키다. `serverUrl` 만 쓰고 `serverName` 은 쓰지 않는다.
 *   이름은 사용자가 아무렇게나 붙일 수 있기 때문이다.
 */
import type { Cluster, HubItem } from "@/api/types";
import { PAT_ENV, claudeConfig, codexConfig, type ClientServer, type ConnectVia } from "@/lib/connectSnippets";
import { displayName } from "@/lib/names";

/** 내보낼 수 있는 클라이언트. 회사가 쓰는 둘이다 (P55a F0 1항). */
export const EXPORT_CLIENTS = ["claude", "codex"] as const;
export type ExportClient = (typeof EXPORT_CLIENTS)[number];

/**
 * 클라이언트마다 내려받는 파일. Claude Code 는 프로젝트의 `.mcp.json`(또는 `~/.claude.json` 의 `mcpServers`), Codex 는
 * `~/.codex/config.toml` 에 합칠 표들이다. 브라우저는 점으로 시작하는 이름을 저장하지 못하고 앞의 점을 떼므로(Chromium 실측)
 * 처음부터 `mcp.json` 으로 내려받고, 대화상자의 미리보기 제목이 놓을 자리를 말한다.
 */
export const EXPORT_FILE: Record<ExportClient, { name: string; type: string }> = {
  claude: { name: "mcp.json", type: "application/json" },
  codex: { name: "config.toml", type: "application/toml" },
};

/** 플랫폼 관리자가 내려받는 두 파일. 허용 목록은 관리 설정에 합칠 한 키이고 고정 목록은 Claude Code 의 독점 관리 파일이다. */
export const ADMIN_FILE = {
  allowlist: { name: "allowed-mcp-servers.json", type: "application/json" },
  exclusive: { name: "managed-mcp.json", type: "application/json" },
} as const;

/** Claude Code 관리 설정의 허용 목록 키 (P55a F0 1항). 내려받는 파일은 이 한 키다. */
export const ALLOWLIST_KEY = "allowedMcpServers";

/** 개인 토큰 env 의 머리. 연결 카드의 `DAEGAM_PAT` 에 테넌트를 붙인다. */
export const TENANT_PAT_PREFIX = `${PAT_ENV}_`;

/**
 * 목록에서 항목이 서는 자리. ready 는 기본으로 고르고, private 은 흐리게 두되 고를 수 있다. noAddress 와 credentials 는 고를 수
 * 없다. local 로 갈린 주소는 노출하지 않은 항목처럼 noAddress 다.
 */
export type ExportState = "ready" | "private" | "noAddress" | "credentials";

export interface ExportRow {
  item: HubItem;
  state: ExportState;
  /** 클라이언트가 바깥 서버를 바로 부르는 항목(remote, entry). 플랫폼의 감사가 없고 로그인은 그 서버를 따른다. */
  direct: boolean;
  /** SSE 만 말하는 직접 연결 항목. Claude Code 는 `type: "sse"` 로 적고 Codex 는 읽지 못해 고를 수 없다. */
  sse: boolean;
}

/** 플랫폼을 거치지 않는 종류. 주소가 등록한 그대로다. */
export function isDirectItem(item: HubItem): boolean {
  return item.kind === "remote" || item.kind === "entry";
}

/**
 * SSE 만 말하는 항목인가. 직접 연결 항목의 전송은 등록한 `source.transport` 다. 플랫폼을 거치는 항목의 주소는 노출 앞단의
 * `/mcp` 라 늘 streamable HTTP 다.
 */
function isSse(item: HubItem): boolean {
  return isDirectItem(item) && item.source.transport === "sse";
}

/** 클라이언트에 줄 주소가 있을 수 있는 종류. image 와 platform 은 주소가 없어 목록에 없다. */
function listed(item: HubItem): boolean {
  return isDirectItem(item) || item.kind === "internal" || item.kind === "published";
}

function stateOf(item: HubItem): ExportState {
  const st = item.status;
  if (st?.address_class === "credentials") return "credentials";
  if (!st?.external_url) return "noAddress";
  return st.address_class === "private" ? "private" : "ready";
}

/** 내보내기 목록의 행. 응답의 차례를 지킨다. */
export function exportRows(items: readonly HubItem[]): ExportRow[] {
  return items.filter(listed).map((item) => ({ item, state: stateOf(item), direct: isDirectItem(item), sse: isSse(item) }));
}

/** 그 클라이언트의 파일에 넣을 수 있는 행인가. Codex 는 SSE 서버를 읽지 못한다. */
export function selectable(row: ExportRow, client: ExportClient): boolean {
  return (row.state === "ready" || row.state === "private") && !(client === "codex" && row.sse);
}

/** 처음에 골라 두는 항목. 사설망 주소는 사람이 고를 때만 내보낸다 (P55a). */
export function initialSelection(rows: readonly ExportRow[]): Set<string> {
  return new Set(rows.filter((r) => r.state === "ready").map((r) => r.item.id));
}

/** 32비트 FNV-1a 를 16진 여섯 자리로. 이름을 가르는 짧은 표시일 뿐 비밀이 아니다. */
function shortHash(value: string): string {
  let h = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(value)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0").slice(0, 6).toUpperCase();
}

/**
 * 테넌트 이름의 `<T>`. 테넌트 이름(`TENANT_RE`)에서 `[A-Za-z0-9]` 밖의 글자를 `_` 로 바꾸고 대문자로 올린다. 이름에 `[a-z0-9-]`
 * 밖의 글자가 있으면 이름 그대로의 짧은 해시를 붙여 `Dev` 와 `dev`, `team-a` 와 `team_a` 를 가른다. 결과는 셸 변수 이름에 쓸 수 있다.
 */
export function tenantTag(tenant: string): string {
  const mapped = tenant.replace(/[^A-Za-z0-9]/g, "_").toUpperCase();
  return /^[a-z0-9-]+$/.test(tenant) ? mapped : `${mapped}_${shortHash(tenant)}`;
}

/** 테넌트의 개인 토큰을 담는 env. 두 테넌트의 파일을 한 사용자 설정에 합쳐도 테넌트마다 제 토큰을 보낸다. */
export function tenantTokenEnv(tenant: string): string {
  return `${TENANT_PAT_PREFIX}${tenantTag(tenant)}`;
}

/** candidates 가운데 아직 쓰지 않은 첫 이름. 모두 쓰였으면 마지막 것에 수를 붙인다. */
function unused(taken: ReadonlySet<string>, candidates: readonly string[]): string {
  const free = candidates.find((c) => !taken.has(c));
  if (free !== undefined) return free;
  const last = candidates[candidates.length - 1];
  let n = 2;
  while (taken.has(`${last}-${n}`)) n++;
  return `${last}-${n}`;
}

/**
 * 설정 파일의 서버 이름. 항목 이름이고, 다른 테넌트의 항목은 그 테넌트의 `<T>` 를 붙인다. 그래도 겹치면 id 의 앞머리를 붙인다.
 * 요청 테넌트의 항목을 먼저 정해 제 이름을 그대로 갖게 한다.
 */
export function serverNames(items: readonly HubItem[], tenant: string): Map<string, string> {
  const ordered = [...items.filter((i) => i.tenant === tenant), ...items.filter((i) => i.tenant !== tenant)];
  const taken = new Set<string>();
  const names = new Map<string, string>();
  for (const item of ordered) {
    const base = item.tenant === tenant ? item.name : `${item.name}-${tenantTag(item.tenant)}`;
    const name = unused(taken, [base, `${base}-${item.id.slice(0, 6)}`, `${base}-${item.id}`]);
    taken.add(name);
    names.set(item.id, name);
  }
  return names;
}

/** 내보내는 서버 하나. 직접 연결 항목은 토큰을 가리키지 않고, 로그인 명령도 없다. */
export interface ExportServer extends ClientServer {
  direct: boolean;
}

/**
 * 고른 항목의 서버 목록. token 갈래에서 플랫폼을 거치는 항목만 테넌트 토큰 env 를 가리킨다. 직접 연결 항목은 그 서버의 로그인을
 * 따르므로 주소만 담는다. 주소가 없는 항목은 빠진다.
 */
export function exportServers(items: readonly HubItem[], tenant: string, via: ConnectVia): ExportServer[] {
  const withUrl = items.filter((i) => i.status?.external_url);
  const names = serverNames(withUrl, tenant);
  const tokenEnv = via === "token" ? tenantTokenEnv(tenant) : undefined;
  return withUrl.map((item) => {
    const direct = isDirectItem(item);
    return { name: names.get(item.id)!, url: item.status!.external_url!, tokenEnv: direct ? undefined : tokenEnv, direct, sse: isSse(item) };
  });
}

/** 클라이언트가 읽을 파일의 글. 끝에 줄바꿈을 둔다. Codex 파일에는 SSE 서버가 들지 않는다(`codexConfig`). */
export function exportFile(client: ExportClient, servers: readonly ClientServer[]): string {
  return `${client === "claude" ? claudeConfig(servers) : codexConfig(servers)}\n`;
}

/** view 가 모든 테넌트(`*`)를 담고 role 과 그룹을 비워, 모든 테넌트의 모든 멤버에게 열린 항목인가. */
export function openToEveryMember(item: HubItem): boolean {
  const view = item.access.view;
  return view.tenants.includes("*") && !view.roles.length && !view.groups.length;
}

/** 허용 목록과 고정 목록에 드는 항목. 모든 멤버에게 열렸고 서버가 outside 로 가른 직접 연결 항목만이다. */
export function companyItems(items: readonly HubItem[]): HubItem[] {
  return items.filter((i) => isDirectItem(i) && openToEveryMember(i) && i.status?.address_class === "outside" && i.status.external_url);
}

/**
 * 주소 하나의 `serverUrl` 패턴. origin 과 경로이고 사용자 정보와 쿼리는 넣지 않는다(서버가 outside 로 가른 주소에는 처음부터 없다).
 * 호스트는 클라이언트(Node 의 WHATWG URL)가 읽는 대로 적는다. 소문자 A-label 이고, 기본 포트가 아니면 포트를 적는다. 포트 없는 호스트는
 * 443(https)과 80(http)에만 맞으므로 넓히지 않는다. `:*` 은 붙이지 않는다. 경로에 `*` 가 있으면 다른 경로까지 열리므로 패턴을 만들지 않는다.
 */
export function serverUrlPattern(url: string): string | null {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.username || u.password || u.search || !/^https?:$/.test(u.protocol)) return null;
  const pattern = `${u.protocol}//${u.host}${u.pathname}`;
  return pattern.includes("*") ? null : pattern;
}

/** 점이 하나 이상 있는 DNS 이름 꼴의 도메인. 패턴 문법(`*`)이나 다른 글자가 든 값, 라벨 하나짜리 값은 패턴으로 쓰지 않는다. */
const DOMAIN = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/;

/** 노출을 허용하고 도메인을 둔 클러스터의 도메인. 소문자이고 앞뒤의 점을 뗀다. 둘 중 하나라도 없으면 null 이다. */
function exposureDomain(c: Cluster): string | null {
  const domain = c.ingress.domain?.trim().toLowerCase().replace(/^\.+|\.+$/g, "");
  return c.ingress.enabled && domain ? domain : null;
}

/**
 * 클러스터의 노출 도메인 패턴. 노출 주소는 `<scheme>://<host>/mcp` 이고 호스트 기본값은 `<name>.<tenant>.<domain>` 이다. edge
 * 클러스터는 TLS 만 받고 그 밖의 클러스터는 TLS 없는 노출도 받으므로 http 도 연다. 호스트의 `*` 는 모든 포트에 맞는다.
 * `*` 가 점을 넘어 맞는지 Claude Code 문서가 말하지 않으므로(라벨 하나짜리 예만 있다) 기본 호스트의 두 라벨을 받는
 * `*.*.<domain>` 도 적는다. 패턴으로 적을 수 없는 도메인은 `skipped` 에 클러스터 이름으로 남긴다.
 */
export function domainPatterns(clusters: readonly Cluster[]): { patterns: string[]; skipped: string[] } {
  const patterns: string[] = [];
  const skipped: string[] = [];
  for (const c of clusters) {
    const domain = exposureDomain(c);
    if (!domain) continue;
    if (!DOMAIN.test(domain)) {
      skipped.push(displayName(c));
      continue;
    }
    for (const scheme of c.ingress.tls === "edge" ? ["https"] : ["https", "http"]) patterns.push(`${scheme}://*.${domain}/mcp`, `${scheme}://*.*.${domain}/mcp`);
  }
  return { patterns, skipped };
}

/** 관리 설정에 합칠 허용 목록. `file` 은 `allowedMcpServers` 한 키이고, `skipped` 는 패턴으로 적지 못해 빠진 것의 이름이다. */
export interface Allowlist {
  file: string;
  skipped: string[];
}

/**
 * 허용 목록. 노출 도메인 패턴이 먼저이고 겹치는 패턴은 하나만 남긴다. 패턴으로 적을 수 없는 도메인과 주소는 조용히 빼지 않고
 * `skipped` 로 알려, 관리자가 관리 설정에 직접 넣게 한다.
 */
export function allowlist(items: readonly HubItem[], clusters: readonly Cluster[]): Allowlist {
  const domains = domainPatterns(clusters);
  const patterns = [...domains.patterns];
  const skipped = [...domains.skipped];
  for (const item of companyItems(items)) {
    const pattern = serverUrlPattern(item.status!.external_url!);
    if (pattern) patterns.push(pattern);
    else skipped.push(displayName(item));
  }
  const entries = [...new Set(patterns)].map((serverUrl) => ({ serverUrl }));
  return { file: `${JSON.stringify({ [ALLOWLIST_KEY]: entries }, null, 2)}\n`, skipped };
}

/** Claude Code 의 독점 관리 파일 `managed-mcp.json`. `.mcp.json` 과 같은 꼴이고 허용 목록의 직접 연결 항목을 담는다. */
export function exclusiveFile(items: readonly HubItem[], tenant: string): string {
  return exportFile("claude", exportServers(companyItems(items), tenant, "open"));
}
