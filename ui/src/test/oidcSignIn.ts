import { oidcAuthConfig, oidcIdpAuthConfig } from "./msw";

/**
 * oidc 모드 시험의 공용 재료 (P53b 6.1·6.4절). 로그인 라이브러리는 test/stubs/oidc-client-ts.ts 의 대역이고, 시험은
 * `oidcStub.user` 에 `signedInUser` 를 넣고 GET /auth/config 를 `oidcConfig` 로 덮는다.
 */

/** JWT 모양의 ID 토큰. 서명은 보지 않으므로 아무 글자다. */
export function idToken(claims: Record<string, unknown>): string {
  const part = (value: unknown) => btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${part({ alg: "RS256" })}.${part(claims)}.sig`;
}

/**
 * age 초 전에 IdP 에서 로그인한 사람. 마지막 로그인은 ID 토큰의 auth_time 에만 있다. oidc-client-ts 는 auth_time 을 프로토콜
 * 클레임으로 보고 profile 에서 지우므로 대역도 profile 에 싣지 않는다. expiresIn 은 ID 토큰이 남은 초다.
 */
export function signedInUser(age: number, { expiresIn = 300 }: { expiresIn?: number } = {}) {
  const now = Math.floor(Date.now() / 1000);
  return { access_token: "tok-1", id_token: idToken({ sub: "u-admin", auth_time: now - age, iat: now, exp: now + expiresIn }), profile: { sub: "u-admin" } };
}

/**
 * oidc 모드의 GET /auth/config. issuer 는 데이터 플레인이 믿는 발급자다. 인가 서버는 idp 에서 잠들고, platform 에서는
 * oauth 가 참일 때 일한다.
 */
export function oidcConfig(issuer: "idp" | "platform", { oauth = false }: { oauth?: boolean } = {}) {
  if (issuer === "idp") return oidcIdpAuthConfig;
  return { ...oidcAuthConfig, oauth: { enabled: oauth, dormant: false } };
}
