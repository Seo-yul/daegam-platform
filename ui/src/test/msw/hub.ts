/**
 * MCP Hub 픽스처와 핸들러 (P7 단위 5, P8 F6, D23): /hub, access, refresh-tools, manifest.
 * `hubState` 는 게시·삭제 핸들러가 바꾸는 mutable 상태다. mcp-servers.ts 와 서로 import 하지만
 * 모듈 평가 시점에는 상대 값을 읽지 않는다.
 *
 * 출처(`source`)의 필드 이름은 서버가 그대로 두었다 (D42). 가리키는 것이 배포된 워크로드 하나라
 * 리소스가 합쳐져도 계약이 바뀌지 않았고, 목도 서버를 따른다.
 */
import { HttpResponse, type HttpHandler } from "msw";

import type { HubAccess, HubAccessInput, HubCatalog, HubCatalogServer, HubItem } from "@/api/types";

import { HUB_ACTIONS } from "@/api/types";
import { emptyAccess, normalizeAccess } from "@/lib/hubAccess";

import { NOW, apiError, docStore, http, record } from "./core";
import { mcpServerState } from "./mcp-servers";

/** 응답의 scope 는 access 에서 파생한다. 다른 테넌트에 view 나 consume 이 열려 있으면 shared 다. */
export function deriveScope(access: HubAccess, owner: string): HubItem["scope"] {
  const opened = (["view", "consume"] as const).some((a) => access[a].tenants.some((t) => t === "*" || t !== owner));
  return opened ? "shared" : "tenant";
}

/** 세 동작이 모두 참인 주체(platform-admin, header 모드)의 판정. */
export const ALL_PERMISSIONS = { view: true, consume: true, manage: true } as const;

export function hubItemFixture(over: Partial<HubItem> = {}): HubItem {
  return {
    id: "hub-calc",
    tenant: "default",
    name: "calc",
    display_name: "calc",
    kind: "internal",
    source: {
      url: null,
      transport: null,
      image: null,
      port: null,
      instance_id: "ms-calc",
      mcp_profile_id: "ms-calc",
      tools_filter: null,
      gateway_instance_id: null,
    },
    cluster_id: "cl-home",
    k8s: { kind: "MCPServer", name: "mcp-calc", namespace: "daegam-default", group: "daegam-default", tool_config: null },
    tool_names: ["Add", "Divide"],
    tool_names_at: NOW,
    status: { phase: "Ready", ready: true, url: "http://mcp-calc-proxy.daegam-default.svc:8080/mcp", external_url: "https://calc.example.com/mcp", message: null, observed_at: NOW, error: null },
    scope: "tenant",
    access: emptyAccess(),
    permissions: { ...ALL_PERMISSIONS, replace_header: false, replace_secret_env: false },
    auth: null,
    access_updated_by: null,
    access_updated_at: null,
    overrides: {},
    state: "created",
    last_error: null,
    created_at: NOW,
    updated_at: NOW,
    ...over,
  };
}

/** 다른 테넌트(acme)가 platform-admin 의 정책으로 연 remote 항목. 요청 주체는 보되 고르지 못한다. */
export const hubSharedReadOnly: HubItem = {
  ...hubItemFixture(),
  id: "hub-shared-docs",
  tenant: "acme",
  name: "docs",
  display_name: "docs",
  kind: "remote",
  source: { url: "https://docs.example.com/mcp", transport: "streamable-http", image: null, port: null, instance_id: null, mcp_profile_id: null, tools_filter: null, gateway_instance_id: null },
  k8s: { kind: "MCPRemoteProxy", name: "hub-shared-docs", namespace: "daegam-acme", group: null, tool_config: null },
  tool_names: ["Search"],
  scope: "shared",
  access: { view: { tenants: ["*"], roles: [], groups: [] }, consume: { tenants: ["billing"], roles: [], groups: [] }, manage: { tenants: [], roles: ["platform-admin"], groups: [] } },
  permissions: { view: true, consume: false, manage: false, replace_header: false, replace_secret_env: false },
};

/** 표시 이름이 name 과 같은 원격 항목. 목록·상세가 이름을 하나만 보인다. */
export const hubRemote: HubItem = {
  ...hubItemFixture(),
  id: "hub-weather",
  name: "weather",
  display_name: "weather",
  kind: "remote",
  source: { url: "https://weather.example.com/mcp", transport: "streamable-http", image: null, port: null, instance_id: null, mcp_profile_id: null, tools_filter: null, gateway_instance_id: null },
  k8s: { kind: "MCPRemoteProxy", name: "hub-weather", namespace: "daegam-default", group: null, tool_config: null },
  tool_names: [],
  tool_names_at: null,
  status: { phase: "Pending", ready: false, url: null, message: "proxy starting", observed_at: NOW, error: null },
};

/**
 * 직접 연결 항목(remote) 하나. 주소의 갈래와 내보내는 주소는 서버 규칙대로다. outside·private 만 `external_url` 에 등록한 주소가
 * 실린다(P55a 1절). id 는 `hub-<name>` 이다.
 */
export function hubDirectFixture(name: string, url: string, cls: "outside" | "private" | "local" | "credentials", over: Partial<HubItem> = {}): HubItem {
  const base = hubItemFixture();
  return {
    ...base,
    id: `hub-${name}`,
    name,
    display_name: name,
    kind: "remote",
    source: { ...base.source, url, transport: "streamable-http", instance_id: null, mcp_profile_id: null },
    k8s: { kind: "MCPRemoteProxy", name: `hub-${name}`, namespace: "daegam-default", group: null, tool_config: null },
    status: { ...base.status!, url, external_url: cls === "outside" || cls === "private" ? url : null, address_class: cls },
    ...over,
  };
}

/** 카탈로그 스냅숏의 서버 하나 (P55 4절). 기본은 서버가 계산하는 표시가 모두 거짓인 원격이다. */
export function catalogServerFixture(over: Partial<HubCatalogServer>): HubCatalogServer {
  return {
    name: "server",
    title: "Server",
    description: "",
    kind: "remote",
    tier: "Official",
    status: "Active",
    tags: [],
    tools: [],
    repository_url: null,
    transport: "streamable-http",
    image: null,
    stdio: false,
    port: null,
    args: [],
    args_variables: false,
    env: [],
    platforms: [],
    provenance: null,
    url: null,
    url_template: false,
    url_secret_variables: false,
    headers: [],
    oauth_config: null,
    mirrored_image: null,
    needs_login: false,
    needs_credentials: false,
    import_header: null,
    ...over,
  };
}

/**
 * GET /hub/catalog 의 기본 답. 허브 이미지 설정이 켜져 이미지마다 허브 사본의 참조가 있다. 출처가 있는 HTTP 이미지(osv), 출처 없는
 * stdio 이미지(sqlite), 인증 없는 원격(docs), 비밀 헤더 하나의 원격(magic), 로그인이 필요한 원격(oauth-remote), 가져올 수 없는
 * 자격 증명 원격(multi-header)이다.
 */
export function hubCatalogFixture(): HubCatalog {
  return {
    catalog: "toolhive-catalog",
    release: "v0.20261026.0",
    last_updated: NOW,
    license: "Apache-2.0",
    admission: true,
    servers: [
      catalogServerFixture({
        name: "osv",
        title: "OSV",
        description: "Open source vulnerability lookups",
        kind: "image",
        tools: ["query_vulnerability", "query_vulnerabilities_batch"],
        image: { repository: "ghcr.io/stackloklabs/osv-mcp/server", tag: "0.1.4" },
        port: 8080,
        env: [
          { name: "OSV_LOG", description: "Log level", required: false, secret: false, default: "info" },
          { name: "OSV_TOKEN", description: "API token for the OSV mirror", required: true, secret: true, default: null },
        ],
        platforms: ["linux/amd64", "linux/arm64"],
        provenance: { repository_uri: "https://github.com/StacklokLabs/osv-mcp" },
        mirrored_image: "registry.example.com/hub/ghcr.io/stackloklabs/osv-mcp/server:0.1.4",
      }),
      catalogServerFixture({
        name: "sqlite",
        title: "SQLite",
        description: "Query a SQLite file",
        kind: "image",
        tier: "Community",
        image: { repository: "docker.io/mcp/sqlite", tag: "latest" },
        stdio: true,
        args: ["--db-path", "/tmp/db.sqlite"],
        platforms: ["linux/arm64"],
        mirrored_image: "registry.example.com/hub/docker.io/mcp/sqlite:latest",
      }),
      catalogServerFixture({ name: "docs", title: "Docs", description: "Public docs search", url: "https://docs.example.com/mcp" }),
      catalogServerFixture({
        name: "magic",
        title: "Magic",
        description: "UI components",
        url: "https://magic.example.com/mcp",
        headers: [{ name: "x-api-key", description: "API key", required: true, secret: true }],
        needs_credentials: true,
        import_header: "x-api-key",
      }),
      catalogServerFixture({ name: "oauth-remote", title: "OAuth remote", url: "https://oauth.example.com/mcp", oauth_config: { issuer: "https://login.example.com" }, needs_login: true }),
      catalogServerFixture({
        name: "multi-header",
        title: "Multi header",
        url: "https://multi.example.com/mcp",
        headers: [
          { name: "x-key", description: "", required: true, secret: true },
          { name: "x-account", description: "", required: true, secret: false },
        ],
        needs_credentials: true,
      }),
    ],
  };
}

export const hubState: { items: HubItem[]; catalog: HubCatalog } = { items: [hubItemFixture(), hubRemote], catalog: hubCatalogFixture() };

export function resetHubState(): void {
  hubState.items = [hubItemFixture(), hubRemote];
  hubState.catalog = hubCatalogFixture();
}

/** mcp-servers·gateways 의 게시 핸들러도 이것으로 허브 항목을 찾는다. */
export const hubStore = docStore(hubState, "hub item");

export const hubHandlers: HttpHandler[] = [
  http.get("/hub", async ({ request }) => {
    await record(request);
    return HttpResponse.json(hubState.items);
  }),
  http.post("/hub", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { name: string; display_name?: string; kind: HubItem["kind"]; url?: string; transport?: string; image?: string; port?: number; tools_filter?: string[]; access?: HubAccessInput | null };
    const access = normalizeAccess(body.access);
    const item: HubItem = {
      ...hubItemFixture(),
      id: "hub-new",
      name: body.name,
      display_name: body.display_name ?? body.name,
      kind: body.kind,
      access,
      scope: deriveScope(access, "default"),
      source: {
        url: body.url ?? null,
        transport: (body.transport as HubItem["source"]["transport"]) ?? null,
        image: body.image ?? null,
        port: body.port ?? null,
        instance_id: null,
        mcp_profile_id: null,
        tools_filter: body.tools_filter ?? null,
        gateway_instance_id: null,
      },
      tool_names: body.tools_filter ?? [],
      tool_names_at: null,
      status: { phase: "Pending", ready: false, url: null, message: null, observed_at: NOW, error: null },
    };
    hubState.items = [...hubState.items, item];
    return HttpResponse.json(item, { status: 201 });
  }),
  // `/hub/:id` 보다 앞에 둔다. 그 핸들러가 catalog 를 id 로 읽는다
  http.get("/hub/catalog", async ({ request }) => {
    await record(request);
    return HttpResponse.json(hubState.catalog);
  }),
  http.get("/hub/:id", async ({ request, params }) => {
    await record(request);
    const item = hubStore.findOr404(params.id);
    return item instanceof Response ? item : HttpResponse.json(item);
  }),
  http.delete("/hub/:id", async ({ request, params }) => {
    await record(request);
    const item = hubStore.find(params.id);
    if (item && !item.permissions.manage) return apiError(403, "forbidden", "manage is required", { action: "manage" });
    hubStore.remove(params.id);
    // internal 항목 삭제는 게시 해제다. 그 MCP Server 의 hub_item 도 같이 비운다
    mcpServerState.items = mcpServerState.items.map((s) => (s.hub_item?.id === params.id ? { ...s, hub_item: null } : s));
    return HttpResponse.json({ id: params.id, deleted: true });
  }),
  // 접근 정책 저장 (5절). manage 가 없으면 403, internal·published 에 다른 테넌트는 422
  http.put("/hub/:id/access", async ({ request, params }) => {
    const r = await record(request);
    const item = hubStore.findOr404(params.id);
    if (item instanceof Response) return item;
    if (!item.permissions.manage) return apiError(403, "forbidden", "manage is required", { action: "manage" });
    const access = normalizeAccess(r.body as HubAccessInput);
    for (const a of HUB_ACTIONS) {
      const bad = access[a].roles.filter((role) => !["viewer", "developer", "tenant-admin", "platform-admin"].includes(role));
      if (bad.length) return apiError(422, "invalid_role", `unknown role in ${a}`, { action: a, roles: bad });
    }
    const crossed = HUB_ACTIONS.find((a) => access[a].tenants.some((t) => t !== item.tenant));
    if ((item.kind === "internal" || item.kind === "published") && crossed) {
      return apiError(422, "cross_tenant_unsupported", `${item.kind} items cannot be opened to other tenants`, { action: crossed, kind: item.kind });
    }
    return HttpResponse.json(hubStore.replace({ ...item, access, scope: deriveScope(access, item.tenant), access_updated_by: "header-admin", access_updated_at: NOW }));
  }),
  // Secret env 값 바꾸기 (P55 5절). 보낸 이름만 설정됨이 되고 값은 답에 없다
  http.put("/hub/:id/secret-env", async ({ request, params }) => {
    const r = await record(request);
    const item = hubStore.findOr404(params.id);
    if (item instanceof Response) return item;
    if (!item.permissions.replace_secret_env) return apiError(403, "forbidden", "only the creator or a tenant-admin replaces Secret values");
    const sent = Object.keys((r.body as { values: Record<string, string> }).values);
    const secretEnv = (item.source.secret_env ?? []).map((e) => (sent.includes(e.name) ? { ...e, set: true } : e));
    return HttpResponse.json(hubStore.replace({ ...item, source: { ...item.source, secret_env: secretEnv }, secret_env_updated_by: "header-admin", secret_env_updated_at: NOW }));
  }),
  http.post("/hub/:id/refresh-tools", async ({ request, params }) => {
    await record(request);
    const item = hubStore.findOr404(params.id);
    if (item instanceof Response) return item;
    return HttpResponse.json(hubStore.replace({ ...item, tool_names: ["GetWeather", "GetForecast"], tool_names_at: NOW }));
  }),
  // 도구 탭이 부르는 라이브 조회 (D48). `weather` 는 서버에 못 물어보는 쪽을 흉내 낸다
  http.get("/hub/:id/tools", async ({ request, params }) => {
    await record(request);
    const item = hubStore.findOr404(params.id);
    if (item instanceof Response) return item;
    // 이 조회는 플랫폼이 밖으로 나가는 호출이라 consume 이다 (D48)
    if (!item.permissions.consume) return apiError(403, "forbidden", "consume permission on this hub item required", { action: "consume" });
    if (!item.status?.ready) {
      return HttpResponse.json({ tools: [], listed: false, reason: `hub item has no URL yet (phase=${item.status?.phase ?? "unknown"})` });
    }
    // 서버가 내놓는 것은 저장된 사본과 무관하다. 그 사본은 게시·재배포 때 복사되는 것이라 어긋날 수
    // 있다. internal(calc)은 늘 Add·Divide 를 내놓으므로 저장된 쪽을 다르게 두면 어긋남을 흉내 낼 수
    // 있고, image 는 등록 때의 tools_filter 가 그대로 저장돼 서버에만 있는 도구가 생기는 쪽이다
    const live = item.kind === "internal" ? ["Add", "Divide"] : item.kind === "image" ? [...item.tool_names, "Extra"] : item.tool_names;
    return HttpResponse.json({
      tools: live.map((name, i) => ({
        name,
        title: i === 0 ? "두 수 더하기" : null,
        description: i === 0 ? "a 와 b 를 더한다.\n두 번째 줄은 접혀 있을 때 보이지 않는다." : null,
        input_schema: {
          type: "object",
          required: ["a"],
          properties: { a: { type: "integer", description: "왼쪽 값" }, b: { type: "number" }, tags: { type: "array", items: { type: "string" } } },
        },
        schema_omitted: false,
        output_schema: i === 0,
      })),
      listed: true,
      reason: null,
    });
  }),
  http.get("/hub/:id/manifest", async ({ request, params }) => {
    await record(request);
    const item = hubStore.findOr404(params.id);
    if (item instanceof Response) return item;
    if (item.k8s.kind === null) return HttpResponse.json({ documents: [] });
    return HttpResponse.json({
      documents: [
        {
          kind: item.k8s.kind,
          name: item.k8s.name,
          rendered: { apiVersion: "toolhive.stacklok.dev/v1alpha1", kind: item.k8s.kind, metadata: { name: item.k8s.name, namespace: item.k8s.namespace } },
          overrides: {},
        },
      ],
    });
  }),
];
