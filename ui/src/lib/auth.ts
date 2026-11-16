/**
 * 인증 상태. GET /api/auth/config 의 auth_mode 로 갈린다.
 *  - header: 지금처럼 테넌트 헤더만 보낸다. 주체는 platform-admin (GET /api/me).
 *  - local:  플랫폼이 발급자다 (D40). POST /api/auth/login 으로 받은 토큰을 sessionStorage 에 두고 Authorization
 *            헤더로 보낸다. 갱신이 없으므로 메모리만 쓰면 새로고침마다 로그인하게 된다. 탭을 닫으면 사라진다.
 *            401 이 오면 토큰을 지우고 Layout 이 /login 으로 보낸다.
 *  - oidc:   oidc-client-ts 로 Authorization Code + PKCE. 접근 토큰은 메모리에만 두고 silent renew 로 갱신한다.
 *            로그인하면 Authorization 헤더를 넣고 GET /api/me 로 소속을 받는다. 갱신할 때마다 새 토큰으로 /api/me 를
 *            부른다. 플랫폼은 그 토큰의 iat 로 사람이 아직 있는지 알고(P53b 6.3절), 채팅의 위임이 그것에 기댄다.
 *            오래 가는 자격(개인·서비스 어카운트 토큰, 로그인 연결)을 만들기 전에는 IdP 로그인이 10분 안이어야 한다.
 *            아니면 max_age 로, 그래도 안 되면 prompt=login 과 max_age 로 다시 로그인하고, 그것도 안 되면 멈춘다(6.4절).
 * oidc-client-ts 는 oidc 모드일 때만 동적으로 들여온다 (테스트는 스텁으로 바꾼다).
 */
import { useSyncExternalStore } from "react";

import type { AuthConfig, MeOut, TokenOut } from "@/api/types";
import { createStore } from "@/lib/storage";

export type AuthMode = "unknown" | "header" | "local" | "oidc";

export interface AuthState {
  mode: AuthMode;
  config: AuthConfig | null;
  me: MeOut | null;
  token: string | null;
  loading: boolean;
  error: string | null;
  /** local: 첫 로그인이거나 관리자가 재설정해 비밀번호를 바꿔야 한다. 바꾸기 전에는 다른 화면을 열지 않는다. */
  mustChangePassword: boolean;
  /** oidc: 다시 로그인을 두 번 해도 IdP 가 새 로그인을 알려 주지 않았다. 폼과 동의 화면이 멈추고 이것을 알린다. */
  reauthFailed: boolean;
}

const INITIAL: AuthState = {
  mode: "unknown",
  config: null,
  me: null,
  token: null,
  loading: false,
  error: null,
  mustChangePassword: false,
  reauthFailed: false,
};
let state: AuthState = INITIAL;
const store = createStore();

function set(patch: Partial<AuthState>) {
  state = { ...state, ...patch };
  store.emit();
}

export function getAuthState(): AuthState {
  return state;
}

export function getAccessToken(): string | null {
  return state.token;
}

export function useAuthState(): AuthState {
  return useSyncExternalStore(store.subscribe, () => state, () => state);
}

/**
 * GET /me 가 끝나 권한을 판단할 수 있는가. 그 전에는 scope 를 모르므로 누구든 "권한 없음" 으로 읽힌다.
 * /admin·클러스터·테넌트 설정 화면이 이 판단 하나를 같이 쓴다.
 */
export function isAuthReady(auth: AuthState): boolean {
  return auth.me !== null || (auth.mode !== "unknown" && !auth.loading);
}

/** `/auth/config` 에 `idp_confirm_seconds` 가 없을 때의 긴 한도(초). 플랫폼의 기본값(7일)과 같다. */
const DEFAULT_IDP_CONFIRM_SECONDS = 7 * 86_400;

/** oidc 모드의 긴 한도를 날 수로 (P53b 6.3절). 토큰 화면과 실행의 멈춘 까닭이 말한다. */
export function idpConfirmDays(config: AuthConfig | null): number {
  return Math.round((config?.idp_confirm_seconds ?? DEFAULT_IDP_CONFIRM_SECONDS) / 86_400);
}

/** 발급자가 있는 토큰 인증인가. local 과 oidc 다. 노출·게이트웨이 인증 경계·에이전트 규칙이 이것을 본다. */
export function tokenAuth(mode: string | undefined | null): boolean {
  return mode === "oidc" || mode === "local";
}

/**
 * 데이터 플레인이 플랫폼 토큰을 받는 oidc 모드인가 (DAEGAM_DATA_PLANE_ISSUER=platform, P53b 6.1절). `/auth/config` 의
 * `data_plane_issuer` 가 platform 일 때다. 기본값이다.
 */
export function platformDataPlane(auth: Pick<AuthState, "mode" | "config">): boolean {
  return auth.mode === "oidc" && auth.config?.data_plane_issuer === "platform";
}

/**
 * 데이터 플레인이 IdP 토큰만 받는 oidc 모드인가 (DAEGAM_DATA_PLANE_ISSUER=idp, P53b 6.1절). oidc 모드에서 `platformDataPlane`
 * 이 아니면 그렇다. 그동안 Gateway 규칙은 Agent 를 받지 않고 Agent 는 Gateway 를 Tenant Settings 의 agent_oauth 로 부른다.
 */
export function idpDataPlane(auth: Pick<AuthState, "mode" | "config">): boolean {
  return auth.mode === "oidc" && !platformDataPlane(auth);
}

/** 데이터 플레인의 인증 경계. Gateway 응답의 `auth` 와 같은 세 값이다. */
export type AuthBoundary = "oidc" | "local" | "anonymous";

/**
 * 인증 모드(또는 Gateway 의 `auth`)를 인증 경계로. local 은 플랫폼 발급자를 믿고, 그 밖(header)은 anonymous 다. oidc 경계가
 * 누구의 토큰을 받는지는 데이터 플레인 발급자가 정한다(`platformDataPlane`, `idpDataPlane`, `takesPlatformTokens`). Gateway
 * 폼의 읽기 전용 줄과 연결 카드가 이것으로 갈린다.
 */
export function authBoundary(mode: string): AuthBoundary {
  return mode === "oidc" || mode === "local" ? mode : "anonymous";
}

/**
 * 경계가 플랫폼이 낸 토큰을 받는가. local 과, 데이터 플레인이 플랫폼을 믿는 oidc 다(`platformDataPlane`, P53b 6.1절). 그런
 * 경계의 연결 카드는 그 자리에서 개인 토큰을 내고 OAuth 가 켜졌으면 로그인 탭을 앞에 둔다. oidc 경계는 화면이 oidc 로 로그인해
 * `/auth/config` 의 발급자를 읽었을 때만 플랫폼으로 본다.
 */
export function takesPlatformTokens(boundary: AuthBoundary, auth: Pick<AuthState, "mode" | "config">): boolean {
  return boundary === "local" || (boundary === "oidc" && platformDataPlane(auth));
}

/**
 * MCP 클라이언트의 OAuth 인가 서버가 일하는가 (P33 1절). 플랫폼이 사람 토큰을 내는 모드(local, oidc)이고 `/auth/config` 의
 * `oauth.enabled` 가 참일 때다. 연결 카드의 "로그인으로 연결" 탭이 이것을 본다.
 */
export function oauthEnabled(auth: Pick<AuthState, "mode" | "config">): boolean {
  return tokenAuth(auth.mode) && auth.config?.oauth?.enabled === true;
}

/**
 * 연결된 MCP 클라이언트 목록이 있는가. 인가 서버가 일하거나 잠들었을 때다(P53b 6.5절). 잠든 동안에도 맺어 둔 연결을 보고
 * 끊을 수 있다. 꺼져 있으면 연결 API 가 404 다.
 */
export function oauthConnections(auth: Pick<AuthState, "mode" | "config">): boolean {
  const oauth = auth.config?.oauth;
  return tokenAuth(auth.mode) && (oauth?.enabled === true || oauth?.dormant === true);
}

/** 테스트용 초기화. */
export function resetAuthState(): void {
  userManager = null;
  initPromise = null;
  callbackPromise = null;
  writeStoredToken(null);
  state = INITIAL;
  store.emit();
}

export function hasScope(me: MeOut | null, tenant: string, scope: string): boolean {
  if (!me) return false;
  if (me.is_platform_admin) return true;
  return (me.scopes_by_tenant[tenant] ?? []).includes(scope);
}

// -- local 토큰 보관 --------------------------------------------------------------------

const LOCAL_TOKEN_KEY = "daegam.local.token";

function readStoredToken(): string | null {
  try {
    return window.sessionStorage.getItem(LOCAL_TOKEN_KEY);
  } catch {
    return null;
  }
}

function writeStoredToken(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(LOCAL_TOKEN_KEY, token);
    else window.sessionStorage.removeItem(LOCAL_TOKEN_KEY);
  } catch {
    // 저장소가 막힌 창(시크릿 모드 등)에서는 메모리만 쓴다. 새로고침하면 다시 로그인한다
  }
}

// -- oidc-client-ts ------------------------------------------------------------------

/**
 * oidc-client-ts 의 User 가운데 쓰는 것. 마지막 IdP 로그인 시각은 `profile` 이 아니라 `id_token` 에서 읽는다. 라이브러리가
 * `auth_time` 을 프로토콜 클레임으로 보고 profile 에서 지우고(filterProtocolClaims), 플랫폼이 보는 것도 이 ID 토큰이다.
 */
type SignedInUser = { access_token: string; id_token?: string; expired?: boolean; profile?: Record<string, unknown> };

type UserManagerLike = {
  getUser(): Promise<SignedInUser | null>;
  signoutRedirect(): Promise<void>;
  signinRedirect(args?: Record<string, unknown>): Promise<void>;
  signinCallback(): Promise<unknown>;
  removeUser(): Promise<void>;
  events: {
    addUserLoaded(cb: (user: { access_token: string }) => void): void;
    addAccessTokenExpired(cb: () => void): void;
    addSilentRenewError(cb: (e: Error) => void): void;
  };
};

let userManager: UserManagerLike | null = null;
let initPromise: Promise<void> | null = null;
// /callback 의 처리. 한 페이지 로드에 한 번만 한다. React StrictMode 가 개발 서버에서 effect 를 두 번 돌리면 같은 코드를 두 번
// 바꾸게 되고, Keycloak 은 두 번째 교환을 거절하며 이 클라이언트를 IdP 세션에서 떼어 첫 갱신부터 실패한다(F0 기록)
let callbackPromise: Promise<string | null> | null = null;

// UI 는 루트에서 서빙된다. Okta 앱에 등록한 redirect URI 도 이 경로여야 한다.
export const CALLBACK_PATH = "/callback";

async function makeUserManager(cfg: AuthConfig): Promise<UserManagerLike> {
  const oidc = await import("oidc-client-ts");
  const um = new oidc.UserManager({
    authority: cfg.issuer ?? "",
    client_id: cfg.client_id ?? "",
    redirect_uri: window.location.origin + CALLBACK_PATH,
    silent_redirect_uri: window.location.origin + CALLBACK_PATH,
    post_logout_redirect_uri: window.location.origin + "/",
    response_type: "code",
    scope: (cfg.scopes ?? ["openid", "profile", "email"]).join(" "),
    automaticSilentRenew: true,
    // 토큰은 메모리에만 둔다 (sessionStorage 금지). 새로고침은 silent renew 가 맡는다.
    userStore: new oidc.WebStorageStateStore({ store: new oidc.InMemoryWebStorage() }),
    // Okta: audience 는 authorization server 의 설정이라 요청에 넣지 않는다. 필요하면 extraQueryParams.
  });
  // 조용한 갱신마다 새 토큰으로 /api/me 를 부른다. 플랫폼이 사람 기록의 마지막 IdP 확인(iat)을 이 요청에서 배운다(P53b 6.3절)
  um.events.addUserLoaded((u) => {
    set({ token: u.access_token });
    void loadMe();
  });
  um.events.addAccessTokenExpired(() => set({ token: null }));
  um.events.addSilentRenewError((e) => set({ error: `silent renew failed: ${e.message}` }));
  return um as unknown as UserManagerLike;
}

// 클라이언트(@/api/client)를 쓰면 initAuth 로 되돌아오므로 여기서는 직접 fetch 한다.
// 경로는 실제 URL 그대로다 (플랫폼 API 는 /api 아래).
async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<{ status: number; data: T | null }> {
  const headers = new Headers(init.headers);
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const res = await fetch(window.location.origin + path, { ...init, headers });
  if (!res.ok) return { status: res.status, data: null };
  return { status: res.status, data: (await res.json()) as T };
}

async function loadMe(): Promise<void> {
  const r = await fetchJson<MeOut>("/api/me");
  if (r.status === 401 && state.mode === "local") {
    // 저장해 둔 토큰이 죽었다(만료·비밀번호 변경·계정 막힘). 로그인 화면으로 간다
    handleUnauthorized();
    return;
  }
  set({ me: r.data });
}

/** 앱 기동 시 한 번. */
export function initAuth(): Promise<void> {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    set({ loading: true, error: null });
    try {
      const cfg = (await fetchJson<AuthConfig>("/api/auth/config")).data;
      if (!cfg) {
        set({ mode: "header", config: null });
        await loadMe();
        return;
      }
      if (cfg.auth_mode === "local") {
        set({ mode: "local", config: cfg, token: readStoredToken() });
        if (state.token) await loadMe();
        return;
      }
      if (cfg.auth_mode !== "oidc") {
        set({ mode: "header", config: cfg });
        await loadMe();
        return;
      }
      set({ mode: "oidc", config: cfg });
      userManager = await makeUserManager(cfg);
      const user = await userManager.getUser();
      if (user && !user.expired) {
        set({ token: user.access_token });
        await loadMe();
      }
    } catch (e) {
      set({ error: (e as Error).message });
    } finally {
      set({ loading: false });
    }
  })();
  return initPromise;
}

// -- local 모드 -----------------------------------------------------------------------

export class LoginError extends Error {
  readonly status: number;
  readonly code: string;
  /** 429 의 Retry-After(남은 초). */
  readonly retryAfter: string | null;
  constructor(status: number, code: string, detail: string, retryAfter: string | null = null) {
    super(detail);
    this.name = "LoginError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

async function postLocal(path: string, body: unknown): Promise<TokenOut> {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  const res = await fetch(window.location.origin + path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) {
    const parsed = (await res.json().catch(() => null)) as { detail?: { code?: string; detail?: string } } | null;
    const d = parsed?.detail ?? {};
    throw new LoginError(res.status, d.code ?? `http_${res.status}`, d.detail ?? "request failed", res.headers.get("Retry-After"));
  }
  return (await res.json()) as TokenOut;
}

function adoptToken(out: TokenOut): void {
  writeStoredToken(out.access_token);
  set({ token: out.access_token, mustChangePassword: out.must_change_password, error: null });
}

/** local: 아이디·비밀번호로 로그인한다. 실패는 LoginError(code: invalid_credentials·account_disabled·login_locked). */
export async function loginLocal(username: string, password: string): Promise<{ mustChangePassword: boolean }> {
  const out = await postLocal("/api/auth/login", { username, password });
  adoptToken(out);
  await loadMe();
  return { mustChangePassword: out.must_change_password };
}

/** local: 본인 비밀번호를 바꾼다. 다른 세션은 끝나고 이 세션은 새 토큰으로 이어진다. */
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const out = await postLocal("/api/auth/password", { current_password: currentPassword, new_password: newPassword });
  adoptToken(out);
}

/** local: 401 을 받았다. 토큰을 지우면 Layout 이 로그인으로 보낸다. */
export function handleUnauthorized(): void {
  if (state.mode !== "local") return;
  writeStoredToken(null);
  set({ token: null, me: null, mustChangePassword: false });
}

/** local: 서버가 403 password_change_required 를 냈다. Layout 이 비밀번호 화면으로 보낸다. */
export function markPasswordChangeRequired(): void {
  if (state.mode === "local" && !state.mustChangePassword) set({ mustChangePassword: true });
}

// -- oidc 모드 -------------------------------------------------------------------------

/** IdP 로그인으로 떠난다. 돌아올 곳은 주지 않으면 지금 경로다. */
export async function login(returnTo: string = window.location.pathname): Promise<void> {
  if (!userManager) throw new Error("oidc not configured");
  await userManager.signinRedirect({ state: { returnTo } });
}

// -- oidc 의 최근 로그인 (P53b 6.4절) ------------------------------------------------------

/** 오래 가는 자격을 만드는 요청이 액세스 토큰 곁에 싣는 ID 토큰의 헤더. 플랫폼 `api/deps.py` 의 `ID_TOKEN_HEADER` 다. */
export const ID_TOKEN_HEADER = "X-Daegam-Id-Token";
/** `/auth/config` 에 `recent_sign_in_seconds` 가 없을 때의 창(초). 플랫폼의 `RECENT_AUTH` 와 같다. */
const DEFAULT_RECENT_SECONDS = 600;
/** 창에서 뺀 여유(초). 누르고 요청이 닿기까지와 IdP·플랫폼의 시계 차이를 덮는다. ID 토큰의 남은 수명도 이만큼은 있어야 한다. */
const RECENT_MARGIN_SECONDS = 60;

/** 다시 로그인한 시도. 1 은 max_age, 2 는 prompt=login 과 max_age 다. IdP 를 지나는 `state` 에 실려 콜백이 읽는다. */
type ReauthAttempt = 1 | 2;

/** 이 시간(초)보다 최근에 IdP 에서 로그인했어야 한다. max_age 로도 쓴다. */
function recentWindow(): number {
  return (state.config?.recent_sign_in_seconds ?? DEFAULT_RECENT_SECONDS) - RECENT_MARGIN_SECONDS;
}

/** ID 토큰(JWT)의 payload. 서명은 보지 않는다. 플랫폼이 본다. 꼴이 다르면 null 이다. */
function idTokenClaims(idToken: string | undefined): Record<string, unknown> | null {
  const payload = idToken?.split(".")[1];
  if (!payload) return null;
  try {
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payload.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return parsed !== null && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** 지금 ID 토큰이 최근 로그인을 증명하는가. `auth_time` 이 창 안이고 토큰이 여유를 두고 아직 살아 있어야 한다. */
function signedInRecently(user: SignedInUser | null, now: number = Date.now()): boolean {
  const claims = idTokenClaims(user?.id_token);
  const at = claims?.auth_time;
  const exp = claims?.exp;
  const seconds = now / 1000;
  return typeof at === "number" && typeof exp === "number" && exp - seconds > RECENT_MARGIN_SECONDS && seconds - at < recentWindow();
}

async function reauthRedirect(returnTo: string, attempt: ReauthAttempt): Promise<void> {
  if (!userManager) throw new Error("oidc not configured");
  const prompt = attempt === 2 ? { prompt: "login" } : {};
  await userManager.signinRedirect({ state: { returnTo, reauth: attempt }, max_age: recentWindow(), ...prompt });
}

/** oidc 에서 오래 가는 자격을 만들기 전에 IdP 에서 다시 로그인해야 하는가. local 은 비밀번호를 받고 header 는 묻지 않는다. */
export async function needsFreshSignIn(): Promise<boolean> {
  if (state.mode !== "oidc" || !userManager) return false;
  return !signedInRecently(await userManager.getUser());
}

/**
 * 다시 로그인하러 IdP 로 떠난다. 먼저 max_age 로 묻고, 돌아온 ID 토큰의 auth_time 이 여전히 오래면 콜백이 prompt=login 과
 * max_age 로 한 번 더 묻는다. 그래도 안 되면 `reauthFailed` 를 세우고 returnTo 로 돌아온다. 폼의 입력은 부르는 쪽이 떠나기
 * 전에 둔다.
 */
export async function startFreshSignIn(returnTo: string): Promise<void> {
  set({ reauthFailed: false });
  await reauthRedirect(safeReturnTo(returnTo), 1);
}

/**
 * 플랫폼이 403 reauth_required 를 냈는데 UI 의 ID 토큰은 최근 로그인을 증명한다. 다시 로그인해도 풀리지 않는 까닭(IdP 와
 * 플랫폼의 시계 차이, iat 없는 액세스 토큰)이라 떠나지 않고 멈춰 알린다. 그래야 누를 때마다 IdP 를 도는 일이 없다.
 */
export function markReauthFailed(): void {
  set({ reauthFailed: true });
}

/** 오래 가는 자격을 만드는 요청이 싣는 헤더. oidc 면 지금 ID 토큰이고, 그 밖에는 없다. */
export async function recentSignInHeaders(): Promise<Record<string, string>> {
  if (state.mode !== "oidc" || !userManager) return {};
  const user = await userManager.getUser();
  return user?.id_token ? { [ID_TOKEN_HEADER]: user.id_token } : {};
}

/**
 * 로그인 뒤 돌아갈 경로. 같은 origin 의 절대 경로만 받는다.
 * `state` 는 IdP 를 한 바퀴 돌아오므로 우리가 만든 값이라도 밖을 지난 값으로 다룬다. `//evil` 은
 * 스킴 상대 URL 이라 다른 origin 으로 가고, `javascript:`·`https:` 는 경로가 아니다. 그런 것은 `/` 로 돌린다.
 */
export function safeReturnTo(path: unknown): string {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return "/";
  return path;
}

/**
 * local: 로그인 화면의 주소. 로그인하면 returnTo 로 돌아간다. reauth 면 저장된 토큰이 있어도 폼을 보인다.
 * MCP 연결의 허용이 최근 10분 안의 로그인을 요구해 동의 화면이 쓴다 (P33 7절).
 */
export function loginPath(returnTo: string, { reauth = false }: { reauth?: boolean } = {}): string {
  return `/login?returnTo=${encodeURIComponent(returnTo)}${reauth ? "&reauth=1" : ""}`;
}

/**
 * local: 비밀번호 바꾸기 화면의 주소. 바꾼 뒤 returnTo 로 돌아간다. 처음 로그인하는 사람이 MCP 클라이언트에서 온
 * 인가 요청을 잃지 않게 강제 변경도 돌아갈 곳을 싣는다 (P33 16절).
 */
export function passwordPath(returnTo: string): string {
  return returnTo === "/" ? "/account/password" : `/account/password?returnTo=${encodeURIComponent(returnTo)}`;
}

/**
 * /callback 에서. 돌아갈 경로를 돌려준다. 한 페이지 로드에 한 번만 처리하고 다시 부르면 같은 결과다. 다시 로그인한 첫
 * 시도가 새 로그인을 받지 못했으면 둘째 시도로 떠나고 null 이다.
 */
export function handleCallback(): Promise<string | null> {
  callbackPromise ??= completeCallback();
  return callbackPromise;
}

async function completeCallback(): Promise<string | null> {
  await initAuth();
  if (!userManager) throw new Error("oidc not configured");
  const result = (await userManager.signinCallback()) as { state?: { returnTo?: string; reauth?: ReauthAttempt } } | undefined;
  const user = await userManager.getUser();
  if (user) set({ token: user.access_token });
  await loadMe();
  const returnTo = safeReturnTo(result?.state?.returnTo);
  const attempt = result?.state?.reauth;
  if (attempt !== undefined && !signedInRecently(user)) {
    if (attempt === 1) {
      await reauthRedirect(returnTo, 2);
      return null;
    }
    set({ reauthFailed: true });
  }
  return returnTo;
}

/**
 * 로그아웃. oidc 는 IdP 세션까지 끊는다. 우리 토큰만 지우면 다음 로그인이 IdP 의 SSO 세션으로 소리 없이
 * 다시 붙어, 공용 PC 에서 나갔다고 믿은 사람이 실제로는 나가지 못한다. IdP 가 end_session 을
 * 안 내면(discovery 에 없으면) 우리 쪽만 지운다. local 은 토큰에 상태가 없어 지우는 것이 곧 로그아웃이다.
 */
export async function logout(): Promise<void> {
  if (state.mode === "local") {
    const token = state.token;
    writeStoredToken(null);
    set({ token: null, me: null, mustChangePassword: false });
    if (token) {
      // 감사 기록용이다. 실패해도 이미 나갔다
      await fetch(window.location.origin + "/api/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token}` } }).catch(() => undefined);
    }
    return;
  }
  set({ token: null, me: null });
  if (!userManager) return;
  try {
    await userManager.signoutRedirect();
  } catch {
    await userManager.removeUser();
  }
}
