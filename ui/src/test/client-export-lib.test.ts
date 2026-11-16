/**
 * 클라이언트 설정 내보내기의 파일 규칙 (P55a 2절). 토큰 값은 어디에도 없고 테넌트마다의 env 만 있다. 테넌트 이름의 `<T>` 는
 * 대소문자와 `.`·`_` 를 가르고, TOML 표 이름은 늘 따옴표 안이다. 플랫폼 관리자의 허용 목록은 모든 멤버에게 열린 직접 연결 항목과
 * 노출 도메인 패턴만 담는다.
 */
import type { Cluster, HubItem } from "@/api/types";
import {
  allowlist,
  companyItems,
  domainPatterns,
  exclusiveFile,
  exportFile,
  exportRows,
  exportServers,
  initialSelection,
  selectable,
  serverNames,
  serverUrlPattern,
  tenantTag,
  tenantTokenEnv,
} from "@/lib/clientExport";
import { codexConfig, tomlString } from "@/lib/connectSnippets";

import { clusterHome, clusterLocked, hubDirectFixture as direct, hubItemFixture } from "./msw";

const EVERYONE = { tenants: ["*"], roles: [], groups: [] };

/** 플랫폼을 거치는 항목(internal). 노출 주소가 있다. */
function served(name: string, url: string | null = `https://${name}.default.apps.local/mcp`): HubItem {
  const base = hubItemFixture();
  return { ...base, id: `hub-${name}`, name, display_name: name, status: { ...base.status!, external_url: url } };
}

/** SSE 만 말하는 직접 연결 항목. */
function sse(name: string, url: string): HubItem {
  const item = direct(name, url, "outside");
  return { ...item, source: { ...item.source, transport: "sse" } };
}

describe("테넌트의 토큰 env", () => {
  it("[a-z0-9-] 만 든 이름은 대문자와 _ 로 바꾸고, 그 밖의 글자가 있으면 해시를 붙여 가른다", () => {
    expect(tenantTag("dev")).toBe("DEV");
    expect(tenantTag("team-a")).toBe("TEAM_A");
    const tags = ["dev", "Dev", "DEV", "team-a", "team_a", "team.a"].map(tenantTag);
    expect(new Set(tags).size).toBe(tags.length);
    expect(tenantTag("Dev")).toMatch(/^DEV_[0-9A-F]{6}$/);
    expect(tenantTag("team.a")).toMatch(/^TEAM_A_[0-9A-F]{6}$/);
    // 같은 이름은 늘 같은 env 다. 두 번 내보낸 파일이 같은 변수를 가리킨다
    expect(tenantTag("team.a")).toBe(tenantTag("team.a"));
    for (const tenant of ["dev", "Dev", "team.a", "team_a", "9lives", "a".repeat(64)]) expect(tenantTokenEnv(tenant)).toMatch(/^[A-Za-z_][A-Za-z0-9_]*$/);
    expect(tenantTokenEnv("dev")).toBe("DAEGAM_PAT_DEV");
  });
});

describe("내보내는 파일", () => {
  const items = [served("calc"), direct("docs", "https://docs.example.com/mcp", "outside")];
  /** 요청 테넌트의 항목으로 둔다. 플랫폼을 거치는 항목은 요청 테넌트에만 있다. */
  const of = (tenant: string, list: HubItem[] = items) => list.map((i) => ({ ...i, tenant }));

  it("Claude Code: 플랫폼을 거치는 항목만 테넌트 env 를 가리키고 직접 연결 항목은 주소만 담는다", () => {
    const file = JSON.parse(exportFile("claude", exportServers(of("Dev"), "Dev", "token")));
    const env = tenantTokenEnv("Dev");
    expect(file).toEqual({
      mcpServers: {
        calc: { type: "http", url: "https://calc.default.apps.local/mcp", headers: { Authorization: `Bearer \${${env}}` } },
        docs: { type: "http", url: "https://docs.example.com/mcp" },
      },
    });
  });

  it("Codex: 표 이름은 따옴표 안이고 토큰은 bearer_token_env_var 로 env 이름만 적는다", () => {
    const file = exportFile("codex", exportServers(of("dev"), "dev", "token"));
    expect(file).toBe(
      [
        '[mcp_servers."calc"]',
        'url = "https://calc.default.apps.local/mcp"',
        'bearer_token_env_var = "DAEGAM_PAT_DEV"',
        "",
        '[mcp_servers."docs"]',
        'url = "https://docs.example.com/mcp"',
        "",
      ].join("\n"),
    );
  });

  it("로그인과 인증 없음 갈래는 주소만 담는다", () => {
    for (const via of ["login", "open"] as const) {
      for (const client of ["claude", "codex"] as const) {
        const file = exportFile(client, exportServers(of("dev"), "dev", via));
        expect(file, `${client} ${via}`).not.toMatch(/Authorization|DAEGAM_PAT|bearer_token|headers/);
        expect(file).toContain("https://calc.default.apps.local/mcp");
      }
    }
  });

  it("주소가 없는 항목은 빠지고, 직접 연결 항목에는 자격 증명이 들지 않는다", () => {
    const servers = exportServers(of("dev", [...items, served("hidden", null), direct("key", "https://k.example.com/mcp?key=s3cret", "credentials")]), "dev", "token");
    expect(servers.map((s) => s.name)).toEqual(["calc", "docs"]);
    expect(servers.find((s) => s.name === "docs")).toEqual({ name: "docs", url: "https://docs.example.com/mcp", tokenEnv: undefined, direct: true, sse: false });
    expect(exportFile("claude", servers)).not.toContain("s3cret");
  });

  it("다른 테넌트의 항목은 그 테넌트의 <T> 를 붙이고, 그래도 겹치면 id 앞머리를 붙인다", () => {
    const own = direct("docs-ACME", "https://a.example.com/mcp", "outside");
    const fromAcme = direct("docs", "https://b.example.com/mcp", "outside", { tenant: "acme", id: "abcdef123456" });
    const fromTeam = direct("docs", "https://c.example.com/mcp", "outside", { tenant: "team.a", id: "fedcba654321" });
    const names = serverNames([fromAcme, own, fromTeam], "default");
    expect(names.get(own.id)).toBe("docs-ACME");
    expect(names.get(fromAcme.id)).toBe("docs-ACME-abcdef");
    expect(names.get(fromTeam.id)).toBe(`docs-${tenantTag("team.a")}`);
    expect(new Set(names.values()).size).toBe(3);
  });

  it("SSE 항목은 Claude Code 에 type sse 로 들고 Codex 에는 들지 않으며, Codex 를 고르면 그 행을 고를 수 없다", () => {
    const list = of("dev", [served("calc"), sse("legacy", "https://legacy.example.com/sse")]);
    const servers = exportServers(list, "dev", "token");
    expect(JSON.parse(exportFile("claude", servers)).mcpServers.legacy).toEqual({ type: "sse", url: "https://legacy.example.com/sse" });
    expect(exportFile("codex", servers)).not.toContain("legacy");
    expect(exportFile("codex", servers)).toContain('[mcp_servers."calc"]');
    const row = exportRows(list).find((r) => r.item.name === "legacy")!;
    expect([row.sse, selectable(row, "claude"), selectable(row, "codex")]).toEqual([true, true, false]);
  });

  it("TOML 문자열은 따옴표·역슬래시·제어 글자를 이스케이프해 . 이 중첩 표를 만들지 못한다", () => {
    expect(tomlString('a"b\\c\nd\u0007')).toBe('"a\\"b\\\\c\\u000ad\\u0007"');
    expect(codexConfig([{ name: "team.a", url: 'https://x.example.com/p"q' }])).toBe('[mcp_servers."team.a"]\nurl = "https://x.example.com/p\\"q"');
  });
});

describe("내보내기 목록의 행", () => {
  it("image 와 platform 은 없고, 갈래마다 고를 수 있는지가 다르며 사설망 주소는 처음에 고르지 않는다", () => {
    const base = hubItemFixture();
    const rows = exportRows([
      served("calc"),
      served("hidden", null),
      direct("out", "https://o.example.com/mcp", "outside"),
      direct("vpn", "http://10.0.0.5/mcp", "private"),
      direct("loop", "http://127.0.0.1/mcp", "local"),
      direct("key", "https://k.example.com/mcp?k=1", "credentials"),
      { ...base, id: "hub-img", name: "img", kind: "image" },
      { ...base, id: "hub-web", name: "web", kind: "platform" },
    ]);
    expect(rows.map((r) => [r.item.name, r.state, r.direct])).toEqual([
      ["calc", "ready", false],
      ["hidden", "noAddress", false],
      ["out", "ready", true],
      ["vpn", "private", true],
      ["loop", "noAddress", true],
      ["key", "credentials", true],
    ]);
    expect([...initialSelection(rows)]).toEqual(["hub-calc", "hub-out"]);
  });
});

describe("플랫폼 관리자의 허용 목록", () => {
  const open = direct("docs", "https://Docs.Example.com:8443/mcp#top", "outside", { access: { ...hubItemFixture().access, view: EVERYONE } });
  const roleOnly = direct("ops", "https://ops.example.com/mcp", "outside", { access: { ...hubItemFixture().access, view: { tenants: ["*"], roles: ["developer"], groups: [] } } });
  const tenantOnly = direct("mine", "https://mine.example.com/mcp", "outside");
  const vpn = direct("vpn", "http://10.0.0.5/mcp", "private", { access: { ...hubItemFixture().access, view: EVERYONE } });
  const servedOpen = { ...served("calc"), access: { ...hubItemFixture().access, view: EVERYONE } };

  it("모든 테넌트의 모든 멤버에게 열린 outside 직접 연결 항목만 담는다", () => {
    expect(companyItems([open, roleOnly, tenantOnly, vpn, servedOpen]).map((i) => i.name)).toEqual(["docs"]);
  });

  it("패턴은 origin 과 경로이고, 포트가 있으면 그 포트를 적으며 :* 를 붙이지 않는다", () => {
    expect(serverUrlPattern("https://Docs.Example.com:8443/mcp#top")).toBe("https://docs.example.com:8443/mcp");
    expect(serverUrlPattern("https://docs.example.com:443/Mcp")).toBe("https://docs.example.com/Mcp");
    expect(serverUrlPattern("http://docs.example.com:8080/mcp")).toBe("http://docs.example.com:8080/mcp");
    expect(serverUrlPattern("https://ｅｘａｍｐｌｅ.com/mcp")).toBe("https://example.com/mcp");
    // 사용자 정보, 쿼리, 경로의 * 는 패턴이 되지 않는다
    expect(serverUrlPattern("https://u:p@docs.example.com/mcp")).toBeNull();
    expect(serverUrlPattern("https://docs.example.com/mcp?key=1")).toBeNull();
    expect(serverUrlPattern("https://docs.example.com/*")).toBeNull();
  });

  it("노출 도메인은 클러스터마다 한 라벨과 두 라벨의 /mcp 경로 패턴이고, edge 클러스터는 https 만 연다", () => {
    const edge: Cluster = { ...clusterHome, id: "cl-e", ingress: { ...clusterHome.ingress, domain: ".Mcp.Example.com.", tls: "edge" } };
    const bad: Cluster = { ...clusterHome, id: "cl-b", display_name: "bad", ingress: { ...clusterHome.ingress, domain: "*.example.com" } };
    const single: Cluster = { ...clusterHome, id: "cl-s", display_name: "single", ingress: { ...clusterHome.ingress, domain: "corp" } };
    expect(domainPatterns([clusterHome, edge, clusterLocked, bad, single])).toEqual({
      patterns: [
        "https://*.apps.local/mcp",
        "https://*.*.apps.local/mcp",
        "http://*.apps.local/mcp",
        "http://*.*.apps.local/mcp",
        "https://*.mcp.example.com/mcp",
        "https://*.*.mcp.example.com/mcp",
      ],
      // 노출을 막은 클러스터는 빠질 것이 없고, 패턴으로 적을 수 없는 도메인은 이름으로 알린다
      skipped: ["bad", "single"],
    });
  });

  it("allowedMcpServers 한 키에 serverUrl 만 담고 사용자 정보와 쿼리가 없으며, 적지 못한 것은 이름으로 알린다", () => {
    const starPath = direct("glob", "https://glob.example.com/*", "outside", { access: { ...hubItemFixture().access, view: EVERYONE } });
    const result = allowlist([open, open, roleOnly, vpn, servedOpen, starPath], [clusterHome]);
    const file = JSON.parse(result.file);
    expect(Object.keys(file)).toEqual(["allowedMcpServers"]);
    expect(file.allowedMcpServers).toEqual([
      { serverUrl: "https://*.apps.local/mcp" },
      { serverUrl: "https://*.*.apps.local/mcp" },
      { serverUrl: "http://*.apps.local/mcp" },
      { serverUrl: "http://*.*.apps.local/mcp" },
      { serverUrl: "https://docs.example.com:8443/mcp" },
    ]);
    for (const entry of file.allowedMcpServers) {
      expect(Object.keys(entry)).toEqual(["serverUrl"]);
      expect(entry.serverUrl).not.toMatch(/@|\?|:\*/);
    }
    expect(result.skipped).toEqual(["glob"]);
  });

  it("고정 목록은 같은 항목을 .mcp.json 꼴로 담는다", () => {
    expect(JSON.parse(exclusiveFile([open, roleOnly, servedOpen], "default"))).toEqual({ mcpServers: { docs: { type: "http", url: "https://Docs.Example.com:8443/mcp#top" } } });
  });
});
