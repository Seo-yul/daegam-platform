/**
 * 테넌트 선택 상태. 인증이 header 모드인 동안 UI 는 선택한 테넌트를 X-Daegam-Tenant 헤더로 보낸다.
 * OIDC 로그인(P6 F1)이 붙으면 소속 테넌트 목록이 토큰에서 오고 이 저장소는 "현재 선택" 만 남는다.
 */
import { useSyncExternalStore } from "react";

import { STORAGE_KEYS, createStore, readItem, readStringList, writeItem } from "@/lib/storage";

export const TENANT_HEADER = "X-Daegam-Tenant";
export const DEFAULT_TENANT = "default";
/** platform-admin 소속 줄의 테넌트. 모든 테넌트라는 뜻이고 소속한 테넌트 하나가 아니다. */
export const ALL_TENANTS = "*";

const CURRENT_KEY = STORAGE_KEYS.tenant;
const RECENT_KEY = STORAGE_KEYS.tenants;
const TENANT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

const store = createStore();

let current: string | null = null;
let recent: string[] | null = null;

export function isValidTenant(name: string): boolean {
  return TENANT_RE.test(name);
}

/** 소속 줄들의 테넌트 이름. `*` 줄은 뺀다. 서버의 `rbac.holds_tenant` 와 같은 판정이다. */
export function memberTenantNames(rows: readonly { tenant: string }[]): string[] {
  return rows.map((m) => m.tenant).filter((t) => t !== ALL_TENANTS);
}

export function getTenant(): string {
  if (current === null) {
    const stored = readItem(CURRENT_KEY);
    current = stored && isValidTenant(stored) ? stored : DEFAULT_TENANT;
  }
  return current;
}

export function getRecentTenants(): string[] {
  if (recent === null) {
    recent = readStringList(RECENT_KEY).filter(isValidTenant);
    if (!recent.includes(getTenant())) recent = [getTenant(), ...recent];
  }
  return recent;
}

export function setTenant(name: string): void {
  const next = name.trim();
  if (!isValidTenant(next)) throw new Error(`invalid tenant name: ${name}`);
  // 최근 목록은 바꾸기 전의 현재 테넌트를 포함해야 하므로 current 를 바꾸기 전에 읽는다
  const previous = getRecentTenants();
  current = next;
  writeItem(CURRENT_KEY, next);
  const list = [next, ...previous.filter((t) => t !== next)].slice(0, 20);
  recent = list;
  writeItem(RECENT_KEY, JSON.stringify(list));
  store.emit();
}

/** 테스트용. 저장소·메모리 상태를 초기화한다. */
export function resetTenantStore(): void {
  current = null;
  recent = null;
  writeItem(CURRENT_KEY, null);
  writeItem(RECENT_KEY, null);
  store.emit();
}

export function useTenant(): string {
  return useSyncExternalStore(store.subscribe, getTenant, getTenant);
}

export function useRecentTenants(): string[] {
  return useSyncExternalStore(store.subscribe, getRecentTenants, getRecentTenants);
}
