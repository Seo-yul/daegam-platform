/**
 * 테스트용 oidc-client-ts 대체 (vite.config.ts test.alias). 호출 기록은 `oidcStub` 에 남긴다.
 *
 * `userLoaded` 는 화면이 단 조용한 갱신의 콜백이라 시험이 새 토큰으로 불러 갱신을 흉내 낸다. `signinCallback` 은 부른 수를
 * 세고, `callbackState`·`callbackUser` 를 돌려주거나 사람으로 둔다. 다시 로그인한 시도(P53b 6.4절)는 이 둘로 흉내 낸다.
 */
type StubUser = { access_token: string; id_token?: string; expired?: boolean; profile?: Record<string, unknown> };

export const oidcStub: {
  settings: Record<string, unknown> | null;
  signinRedirectCalls: Record<string, unknown>[];
  user: StubUser | null;
  userLoaded: ((user: StubUser) => void)[];
  callbackCalls: number;
  callbackState: Record<string, unknown>;
  callbackUser: StubUser;
} = {
  settings: null,
  signinRedirectCalls: [],
  user: null,
  userLoaded: [],
  callbackCalls: 0,
  callbackState: { returnTo: "/clusters" },
  callbackUser: { access_token: "tok-from-callback" },
};

export function resetOidcStub(): void {
  oidcStub.settings = null;
  oidcStub.signinRedirectCalls.length = 0;
  oidcStub.user = null;
  oidcStub.userLoaded.length = 0;
  oidcStub.callbackCalls = 0;
  oidcStub.callbackState = { returnTo: "/clusters" };
  oidcStub.callbackUser = { access_token: "tok-from-callback" };
}

export class InMemoryWebStorage {}
export class WebStorageStateStore {
  constructor(_opts: unknown) {}
}

export class UserManager {
  events = {
    addUserLoaded: (cb: (user: StubUser) => void) => {
      oidcStub.userLoaded.push(cb);
    },
    addAccessTokenExpired: (_cb: unknown) => undefined,
    addSilentRenewError: (_cb: unknown) => undefined,
  };
  constructor(settings: Record<string, unknown>) {
    oidcStub.settings = settings;
  }
  async getUser() {
    return oidcStub.user;
  }
  async signinRedirect(args: Record<string, unknown> = {}) {
    oidcStub.signinRedirectCalls.push(args);
  }
  async signinCallback() {
    oidcStub.callbackCalls += 1;
    oidcStub.user = oidcStub.callbackUser;
    return { state: oidcStub.callbackState };
  }
  async removeUser() {
    oidcStub.user = null;
  }
}
