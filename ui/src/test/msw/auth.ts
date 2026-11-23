/** 인증·설정·멤버 (P7 단위 7): /auth/config, /me, /tenants, /settings, /group-mappings, 테넌트 설정·멤버. */
import { HttpResponse, type HttpHandler } from "msw";

import type { DataPlaneAuth, GroupMapping, HubImagesStatus, MeOut, Membership, OtlpSettings, PlatformSettings, TenantSettings, UserOut } from "@/api/types";

import { NOW, http, record } from "./core";

export const meAdmin: MeOut = {
  sub: "header-admin",
  // header 모드의 실제 주체에는 이름이 없다. 홈 인사가 sub 를 쓰지 않는 근거다 (D45)
  name: null,
  email: "admin@example.com",
  groups: [],
  client_id: null,
  subject_type: "user",
  memberships: [{ tenant: "*", role: "platform-admin", source: "manual" }],
  is_platform_admin: true,
  scopes_by_tenant: { "*": ["settings:write", "members:write", "audit:read"] },
};

export const meDeveloper: MeOut = {
  sub: "dev@example.com",
  name: "김개발",
  email: "dev@example.com",
  groups: ["daegam-default-dev"],
  client_id: null,
  subject_type: "user",
  memberships: [{ tenant: "default", role: "developer", source: "okta_group" }],
  is_platform_admin: false,
  scopes_by_tenant: {
    default: ["backend:read", "backend:write", "mcp:read", "mcp:write", "mcp:deploy", "hub:read", "hub:write", "gateway:read", "gateway:write", "gateway:deploy", "skill:read", "skill:write", "audit:read", "settings:read"],
  },
};

/** viewer 는 read 만 있다. 게시·정책 변경 버튼이 없어야 한다. */
export const meViewer: MeOut = {
  sub: "viewer@example.com",
  name: "viewer",
  email: "viewer@example.com",
  groups: [],
  client_id: null,
  subject_type: "user",
  memberships: [{ tenant: "default", role: "viewer", source: "manual" }],
  is_platform_admin: false,
  scopes_by_tenant: { default: ["backend:read", "mcp:read", "hub:read", "gateway:read", "audit:read", "settings:read"] },
};

/** GET /settings/data-plane-auth 의 기본 답. 다시 배포할 소비자가 없는 상태다. 수를 보는 시험은 server.use 로 바꾼다. */
export const dataPlaneAuth: DataPlaneAuth = {
  install_id: "install-test",
  summary: null,
  clusters: [],
  render_epochs: {},
  kinds: {},
  fallbacks: {},
  handovers: {},
  entry_conversions: {},
  redis_plain_consumers: {},
  shipped_redis_cert_expires_at: null,
  shipped_redis_cert_expiring: false,
  blocked: [],
  exposed_agents_stale_runtime: 0,
  mcp_servers_stale_ca: 0,
  turn_tokens: { clusters: [], authz_forbid_gateways: [], agents_without_turn_tokens: [] },
  data_plane_issuer: null,
};

export const platformSettings: PlatformSettings = {
  default_runtime_image: "daegam-mcp-runtime:0.1.0",
  default_agent_image: "daegam-agent-runtime:0.1.0",
  default_model: "claude-opus-5",
  default_cluster_id: null,
  namespace_template: "daegam-{tenant}",
  oidc: null,
  llm_providers: [{ name: "openai-main", kind: "openai", model: null, api_key_secret: "openai-key", base_url: null, stream_usage: true }],
  defaults: { spec_generator: "openai-main" , assistant: null },
  assistant: { enabled: false, confirm_deploy: true, effort: "low", max_tool_calls_per_turn: 24, turn_timeout_seconds: 300 },
  skills: { max_per_tenant: 200, tenant_quota_mb: 64 },
  web_tools: {
    fetch: {
      enabled: false, per_minute: 60, per_user_per_minute: 10, per_user_per_day: 200, max_bytes: 2097152, max_chars: 20000,
      timeout_seconds: 15, max_redirects: 5, allowed_ports: [80, 443], blocked_host_suffixes: [], respect_robots: true,
      blocked_cidrs: [], user_agent_contact: null,
    },
    search: {
      enabled: false, provider: "brave", endpoint: null, api_key_secret: null, per_minute: 30, per_day: 2000,
      per_user_per_minute: 10, per_user_per_day: 200, timeout_seconds: 10, max_count: 10,
    },
    external_clients: false,
  },
  network: { ca_bundle: null },
  llm_gateway: { limits: { tenant_tokens_per_minute: null, tenants: {}, providers: {} } },
  default_web_tools_image: "daegam-web-tools:0.1.0",
  channels: { teams: { enabled: false, app_id: null, entra_tenant_id: null, secret_set: false } },
  hub_images: { prefix: null, public_keys: [], revoked_keys: [], refused_digests: [], credential_set: false },
  updated_at: NOW,
  updated_by: null,
};

/** GET /settings/hub-images/status 의 기본 답 (P55 6절). 설정하지 않은 상태다. 수를 보는 시험은 server.use 로 바꾼다. */
export const hubImagesStatus: HubImagesStatus = { configured: false, keys: [], overlaps: [], refused: [] };

/** 저장한 적이 없는 OTLP 설정. 서버의 기본값이다. */
export const otlpSettings: OtlpSettings = { enabled: false, endpoint: null, headers_secret: null, sample_ratio: 1, always_errors: true, capture_content: false };

export const tenantSettings: TenantSettings = {
  tenant: "default",
  default_cluster_id: null,
  allowed_cluster_ids: null,
  namespace: null,
  quota: null,
  default_bindings: null,
  mcp_group: null,
  agent_client_ids: [],
  agent_oauth: null,
  allowed_namespaces: [],
  updated_at: NOW,
  updated_by: null,
};

export const members: Membership[] = [
  { sub: "dev@example.com", tenant: "default", role: "developer", source: "okta_group", updated_at: NOW },
  { sub: "bot-1", tenant: "default", role: "viewer", source: "manual", updated_at: NOW },
];

export const groupMappings: GroupMapping[] = [
  { group: "daegam-admins", tenant: "*", role: "platform-admin" },
  { group: "daegam-default-dev", tenant: "default", role: "developer" },
];

/**
 * oidc 모드의 GET /auth/config. 플랫폼이 oidc 모드에서 늘 싣는 필드를 모두 싣고, 데이터 플레인 발급자는 플랫폼의 기본값인
 * platform 이다(P53b 6.1절, D104). IdP 를 믿는 길을 보는 시험은 `oidcIdpAuthConfig` 로 덮는다. 데이터 플레인
 * audience(mcp_audience)는 Agent 외부 호출 카드가 보인다 (D27). 공개 주소가 https 가 아니라 OAuth 인가 서버는 꺼져 있다.
 */
export const oidcAuthConfig = {
  auth_mode: "oidc",
  issuer: "https://okta.example.com/oauth2/default",
  client_id: "0oa123",
  audience: "api://daegam",
  scopes: ["openid", "profile", "email", "groups"],
  groups_claim: "groups",
  mcp_audience: "daegam-mcp",
  personal_tokens: { max_days: 365 },
  oauth: { enabled: false, dormant: false },
  data_plane_issuer: "platform",
  idp_confirm_seconds: 7 * 86_400,
  recent_sign_in_seconds: 600,
};

/** 데이터 플레인이 IdP 를 믿는 oidc 모드(DAEGAM_DATA_PLANE_ISSUER=idp)의 GET /auth/config. 인가 서버는 잠든다 (P53b 6.5절). */
export const oidcIdpAuthConfig = { ...oidcAuthConfig, oauth: { enabled: false, dormant: true }, data_plane_issuer: "idp" };

/**
 * GET /auth/config 의 인증 모드. 기본은 header 다. oidc 모드 화면(Agent 노출 등)을 보는 테스트가 바꾼다. 스모크 목 서버는
 * MOCK_AUTH_MODE 로 정한다.
 */
export const authConfigState: { mode: "header" | "oidc" | "local" } = { mode: "header" };

export function resetAuthConfigState(): void {
  authConfigState.mode = "header";
}

/**
 * local 모드의 GET /auth/config (D40). 브라우저는 issuer 를 쓰지 않는다. 개인 토큰 발급 폼은 max_days 를 본다 (P32).
 * 공개 주소가 없는 배포본이라(issuer 가 서비스 발급자다) OAuth 인가 서버는 꺼져 있다 (P33 1절).
 */
export const localAuthConfig = {
  auth_mode: "local",
  issuer: "http://daegam-platform.daegam-system.svc:9000",
  mcp_audience: "daegam-mcp",
  token_ttl: 43200,
  personal_tokens: { max_days: 365 },
  oauth: { enabled: false },
};

/** OAuth 인가 서버가 켜진 local 모드의 GET /auth/config (P33 1절). https 공개 주소가 있는 배포본이라 issuer 가 그 주소다. */
export const localOAuthConfig = { ...localAuthConfig, issuer: "https://daegam.example.com", oauth: { enabled: true } };

/** 테스트가 `server.use(oauthConfigHandler)` 로 OAuth 를 켠다. 테스트가 끝나면 resetHandlers 가 되돌린다. */
export const oauthConfigHandler = http.get("/auth/config", async ({ request }) => {
  await record(request);
  return HttpResponse.json(localOAuthConfig);
});

export const authSettingsHandlers: HttpHandler[] = [
  http.get("/auth/config", async ({ request }) => {
    await record(request);
    if (authConfigState.mode === "oidc") return HttpResponse.json(oidcAuthConfig);
    if (authConfigState.mode === "local") return HttpResponse.json(localAuthConfig);
    return HttpResponse.json({ auth_mode: "header" });
  }),
  http.get("/me", async ({ request }) => {
    await record(request);
    return HttpResponse.json(meAdmin);
  }),
  http.get("/tenants", async ({ request }) => {
    await record(request);
    return HttpResponse.json([
      { tenant: "*", role: "platform-admin" },
      { tenant: "default", role: "platform-admin" },
      { tenant: "acme", role: "platform-admin" },
    ]);
  }),
  http.get("/settings", async ({ request }) => {
    await record(request);
    return HttpResponse.json(platformSettings);
  }),
  http.patch("/settings", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json({ ...platformSettings, ...(r.body as object), updated_by: "header-admin" });
  }),
  http.get("/settings/hub-images/status", async ({ request }) => {
    await record(request);
    return HttpResponse.json(hubImagesStatus);
  }),
  http.put("/settings/hub-images/credential", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ credential_set: true });
  }),
  http.get("/settings/data-plane-auth", async ({ request }) => {
    await record(request);
    return HttpResponse.json(dataPlaneAuth);
  }),
  /** OTLP 내보내기 설정 (P37 15절). Platform Settings 와 따로인 문서다. */
  http.get("/settings/observability/otlp", async ({ request }) => {
    await record(request);
    return HttpResponse.json(otlpSettings);
  }),
  http.put("/settings/observability/otlp", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json(r.body as OtlpSettings);
  }),
  http.get("/settings/observability/otlp/status", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ last_success: NOW, failures: 2, consecutive_failures: 0, dropped: 1, queued: 0 });
  }),
  /** provider 키 넣기 (D56). 플랫폼이 관리 Secret 을 만들고 그 이름만 돌려준다. 키는 돌려주지 않는다. */
  http.put("/settings/llm-providers/:name/api-key", async ({ request, params }) => {
    await record(request);
    const name = String(params.name);
    return HttpResponse.json({ name, api_key_secret: `daegam-llm-key-${name.toLowerCase()}` });
  }),
  /** LLM provider 연결 확인 (D25). 픽스처의 provider 는 ok, 모르는 이름은 404. 다른 코드는 테스트가 덮어쓴다. */
  http.post("/settings/llm-providers/:name/check", async ({ request, params }) => {
    await record(request);
    if (!platformSettings.llm_providers.some((p) => p.name === params.name)) {
      return HttpResponse.json({ detail: { code: "not_found", detail: `llm provider '${String(params.name)}' is not configured` } }, { status: 404 });
    }
    return HttpResponse.json({ ok: true, code: "ok" });
  }),
  http.get("/group-mappings", async ({ request }) => {
    await record(request);
    return HttpResponse.json(groupMappings);
  }),
  http.put("/group-mappings", async ({ request }) => {
    const r = await record(request);
    return HttpResponse.json(r.body as GroupMapping[]);
  }),
  http.get("/tenants/:tenant/settings", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ ...tenantSettings, tenant: params.tenant });
  }),
  http.patch("/tenants/:tenant/settings", async ({ request, params }) => {
    const r = await record(request);
    return HttpResponse.json({ ...tenantSettings, tenant: params.tenant, ...(r.body as object), updated_by: "header-admin" });
  }),
  http.get("/tenants/:tenant/members", async ({ request }) => {
    await record(request);
    return HttpResponse.json(members);
  }),
  http.put("/tenants/:tenant/members", async ({ request, params }) => {
    const r = await record(request);
    const manual = (r.body as { sub: string; role: string }[]).map((m) => ({ ...m, tenant: String(params.tenant), source: "manual", updated_at: NOW }));
    return HttpResponse.json([...members.filter((m) => m.source !== "manual"), ...manual]);
  }),
];

// -- local 인증 모드 (D40): 로그인·비밀번호·사용자 관리 ---------------------------------------------

export const LOCAL_TOKEN = "local-token-1";
export const LOCAL_ADMIN_PASSWORD = "bootstrap-password-1234";

export const localUsers: UserOut[] = [
  {
    id: "u-admin",
    username: "admin",
    email: null,
    display_name: null,
    disabled: false,
    must_change_password: false,
    platform_admin: true,
    created_at: NOW,
    updated_at: NOW,
    last_login_at: NOW,
  },
  {
    id: "u-alice",
    username: "alice",
    email: "alice@example.com",
    display_name: "Alice",
    disabled: false,
    must_change_password: true,
    platform_admin: false,
    created_at: NOW,
    updated_at: NOW,
    last_login_at: null,
  },
];

/**
 * 로그인 결과를 테스트가 바꾼다. mustChange 면 첫 로그인 흐름, locked 면 로그인과 비밀번호 바꾸기가 429. 기본은 첫 로그인도
 * 잠금도 아니다.
 */
export const localLoginState: { mustChange: boolean; locked: boolean } = { mustChange: false, locked: false };

export function resetLocalLoginState(): void {
  localLoginState.mustChange = false;
  localLoginState.locked = false;
}

/** 로그인 잠금의 429. 로그인, 비밀번호 바꾸기, 개인 토큰 발급이 같은 잠금을 쓴다 (P32 6절). */
export function lockedResponse() {
  return HttpResponse.json(
    { detail: { code: "login_locked", detail: "too many failed attempts" } },
    { status: 429, headers: { "Retry-After": String(14 * 60) } },
  );
}

export const localAuthHandlers: HttpHandler[] = [
  http.post("/auth/login", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { username: string; password: string };
    if (localLoginState.locked) return lockedResponse();
    if (body.username !== "admin" || body.password !== LOCAL_ADMIN_PASSWORD) {
      return HttpResponse.json({ detail: { code: "invalid_credentials", detail: "username or password is wrong" } }, { status: 401 });
    }
    return HttpResponse.json({ access_token: LOCAL_TOKEN, token_type: "Bearer", expires_in: 43200, audience: "daegam-api", must_change_password: localLoginState.mustChange });
  }),
  http.post("/auth/password", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { current_password: string; new_password: string };
    // 서버는 로그인과 같은 잠금을 쓴다 (P32 6절)
    if (localLoginState.locked) return lockedResponse();
    if (body.current_password !== LOCAL_ADMIN_PASSWORD) {
      return HttpResponse.json({ detail: { code: "invalid_credentials", detail: "current password is wrong" } }, { status: 401 });
    }
    if (body.new_password.length < 12) {
      return HttpResponse.json({ detail: { code: "weak_password", detail: "password must be at least 12 characters" } }, { status: 422 });
    }
    return HttpResponse.json({ access_token: "local-token-2", token_type: "Bearer", expires_in: 43200, audience: "daegam-api", must_change_password: false });
  }),
  http.post("/auth/logout", async ({ request }) => {
    await record(request);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("/users", async ({ request }) => {
    await record(request);
    return HttpResponse.json(localUsers);
  }),
  http.get("/users/directory", async ({ request }) => {
    await record(request);
    return HttpResponse.json(localUsers.map((u) => ({ id: u.id, username: u.username, display_name: u.display_name })));
  }),
  http.post("/users", async ({ request }) => {
    const r = await record(request);
    const body = r.body as { username: string; password: string; email?: string | null; display_name?: string | null; platform_admin?: boolean };
    if (localUsers.some((u) => u.username === body.username)) {
      return HttpResponse.json({ detail: { code: "name_conflict", detail: `username '${body.username}' is taken` } }, { status: 409 });
    }
    const user: UserOut = {
      id: `u-${body.username}`,
      username: body.username,
      email: body.email ?? null,
      display_name: body.display_name ?? null,
      disabled: false,
      must_change_password: true,
      platform_admin: Boolean(body.platform_admin),
      created_at: NOW,
      updated_at: NOW,
      last_login_at: null,
    };
    return HttpResponse.json(user, { status: 201 });
  }),
  http.patch("/users/:id", async ({ request, params }) => {
    const r = await record(request);
    const user = localUsers.find((u) => u.id === params.id);
    if (!user) return HttpResponse.json({ detail: { code: "not_found", detail: "user not found" } }, { status: 404 });
    const patch = r.body as Partial<UserOut>;
    if (user.id === "u-admin" && (patch.platform_admin === false || patch.disabled === true)) {
      return HttpResponse.json({ detail: { code: "last_admin", detail: "cannot demote the last active platform-admin 'admin'" } }, { status: 422 });
    }
    return HttpResponse.json({ ...user, ...patch });
  }),
  http.post("/users/:id/password", async ({ request, params }) => {
    await record(request);
    const user = localUsers.find((u) => u.id === params.id);
    if (!user) return HttpResponse.json({ detail: { code: "not_found", detail: "user not found" } }, { status: 404 });
    return HttpResponse.json({ ...user, must_change_password: true });
  }),
  http.delete("/users/:id", async ({ request, params }) => {
    await record(request);
    return HttpResponse.json({ id: String(params.id), deleted: true });
  }),
];
