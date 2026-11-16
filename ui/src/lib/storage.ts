/**
 * 브라우저에 남기는 것 한 곳. 이 파일 밖에서는 `window.localStorage` 를 직접 만지지 않는다. 탭 하나 안에서만 살면 되는 것(IdP
 * 로 다시 로그인하러 떠나는 토큰 폼의 입력)은 `window.sessionStorage` 이고 그것도 여기서만 만진다. local 모드의 로그인 토큰은
 * lib/auth.ts 가 따로 둔다.
 *
 * 사설 모드나 저장 차단에서는 접근 자체가 던지므로 읽기·쓰기를 try/catch 한 벌로 감싼다. 여기 남는 것은
 * 전부 편의 상태라 저장이 실패해도 화면은 기본값으로 뜨고 세션 동안은 메모리 값으로 동작한다.
 *
 * `useSyncExternalStore` 로 화면에 붙이는 스토어가 다섯(테넌트·테마·언어·비서 UI·사이드바)이고 리스너 배선이
 * 같아서 `createStore()` 하나로 모았다. 스냅숏 계산만 스토어마다 다르다.
 */

/** 지금 브라우저에 남기는 키 전부. 새 상태를 더하면 여기에 이름을 먼저 둔다. */
export const STORAGE_KEYS = {
  /** 지금 고른 테넌트. */
  tenant: "daegam.tenant",
  /** 최근 테넌트 목록(JSON 배열, 최대 20). */
  tenants: "daegam.tenants",
  /** 고른 테마("dark" | "light"). */
  theme: "daegam.theme",
  /** 고른 화면 언어("ko" | "en", P49). */
  language: "daegam.language",
  /** 사이드바 접힘("collapsed" | "expanded"). */
  sidebar: "daegam.nav.sidebar",
  /** 접힌 사이드바 그룹 라벨(JSON 배열). */
  navCollapsed: "daegam.nav.collapsed",
  /** 비서 패널 열림("true" | "false", D31). */
  assistantOpen: "daegam.assistant.open",
  /** 비서의 화면 따라가기("true" | "false", D31). */
  assistantFollow: "daegam.assistant.follow",
} as const;

/** 목록의 모양("card" | "table", D47). 목록마다 따로 기억하므로 `${prefix}${list}` 다. */
export const LIST_MODE_KEY_PREFIX = "daegam.list.mode.";

export function listModeKey(list: string): string {
  return `${LIST_MODE_KEY_PREFIX}${list}`;
}

/** 테넌트별 비서 세션 id 의 키 접두사. `${prefix}${tenant}` 다 (D31). */
export const ASSISTANT_SESSION_KEY_PREFIX = "daegam.assistant.session.";

export function assistantSessionKey(tenant: string): string {
  return `${ASSISTANT_SESSION_KEY_PREFIX}${tenant}`;
}

/** 홈 챗에서 마지막으로 고른 상대(청지기를 뜻하는 assistant 또는 인스턴스 id, D35). 테넌트마다 따로 기억한다. */
export const HOME_CHAT_AGENT_KEY_PREFIX = "daegam.home.agent.";

export function homeChatAgentKey(tenant: string): string {
  return `${HOME_CHAT_AGENT_KEY_PREFIX}${tenant}`;
}

/** 비서와 나눌 대화에 붙일 LLM provider (D45). 테넌트마다 따로 기억한다. 세션이 이미 provider 를
 * 갖고 있으면 그 대화에서는 세션의 것이 이긴다. 여기 있는 것은 다음 새 대화의 기본값이다. */
export const ASSISTANT_PROVIDER_KEY_PREFIX = "daegam.assistant.provider.";

export function assistantProviderKey(tenant: string): string {
  return `${ASSISTANT_PROVIDER_KEY_PREFIX}${tenant}`;
}

/**
 * IdP 로 다시 로그인하러 떠나는 토큰 폼이 남기는 입력의 키 접두사 (P53b 6.4절). 돌아오면 같은 폼이 되살리고 지운다. 탭 하나
 * 안에서만 살면 되므로 sessionStorage 다. `${prefix}${form}` 이다.
 */
export const TOKEN_DRAFT_KEY_PREFIX = "daegam.token.draft.";

export function tokenDraftKey(form: string): string {
  return `${TOKEN_DRAFT_KEY_PREFIX}${form}`;
}

/** 이 탭에 남긴 값. 없거나 저장소에 못 닿으면 null 이다. */
export function peekSessionItem(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

/** 이 탭에 남긴 값을 지운다. 저장소에 못 닿으면 지울 것도 없다. */
export function removeSessionItem(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    /* 저장소에 못 닿으면 남긴 것도 없다 */
  }
}

/** 이 탭에 값을 남긴다. 저장 실패는 무시한다(돌아온 폼이 빈 채로 뜬다). */
export function writeSessionItem(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    /* 저장 불가여도 다시 로그인은 된다. 입력만 다시 넣는다 */
  }
}

/** 저장된 값. 저장소에 못 닿으면 null 이다. */
export function readItem(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** 값을 남긴다. null 이면 지운다. 저장 실패는 무시한다(메모리 값으로 계속 동작한다). */
export function writeItem(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* 저장 불가(사설 모드 등)여도 세션 동안은 메모리 값으로 동작한다 */
  }
}

/** JSON 배열로 저장된 문자열 목록. 깨졌으면 빈 배열이다. */
export function readStringList(key: string): string[] {
  try {
    const parsed: unknown = JSON.parse(readItem(key) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

/** 조건에 맞는 키를 지운다. 테스트 초기화처럼 접두사로 여러 개를 지울 때 쓴다. */
export function removeMatching(match: (key: string) => boolean): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i);
      if (key && match(key)) keys.push(key);
    }
    for (const key of keys) window.localStorage.removeItem(key);
  } catch {
    /* 저장소에 못 닿으면 지울 것도 없다 */
  }
}

export interface Store {
  /** `useSyncExternalStore` 의 subscribe. */
  subscribe: (listener: () => void) => () => void;
  /** 값이 바뀌었음을 알린다. */
  emit: () => void;
}

/** 리스너 Set 과 구독·알림 배선. 스냅숏은 스토어가 자기 방식으로 만든다. */
export function createStore(): Store {
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    emit: () => {
      for (const listener of listeners) listener();
    },
  };
}
