/**
 * 허브 접근 정책 (docs/guide/hub-gateway.md 7절, D23) 의 폼 쪽 규칙.
 * 서버가 판정을 맡고 UI 는 응답의 permissions 로 버튼을 그리므로 여기에는 모양을 다루는 순수 함수만 둔다.
 */
import { HUB_ACTIONS, type AccessSubjects, type HubAccess, type HubAccessInput, type HubAction, type MeOut } from "@/api/types";
import type { Messages } from "@/i18n";
import { hasScope } from "@/lib/auth";

/** 동작을 품는 동작. 서버의 `auth/hub_access.py` `IMPLIED_BY` 와 같다. */
const IMPLIED_BY: Record<HubAction, HubAction[]> = { view: ["view", "consume", "manage"], consume: ["consume", "manage"], manage: ["manage"] };

/**
 * action 의 경계에서 빠지는 다른 테넌트가 있는가. 서버의 `auth/hub_access.py` `narrowed` 와 같은 규칙이다. after 에 `*` 가 있으면
 * 아무도 빠지지 않고, before 에만 `*` 가 있으면 빠진다. `[a]` 를 `[*]` 로 넓히는 것은 빼는 것이 아니다. owner 는 항목을 가진 테넌트다.
 */
export function narrowed(before: HubAccess, after: HubAccess, owner: string, action: HubAction): boolean {
  const boundary = (access: HubAccess) => new Set(IMPLIED_BY[action].flatMap((a) => access[a].tenants).filter((t) => t !== owner));
  const old = boundary(before);
  const next = boundary(after);
  if (next.has("*")) return false;
  return old.has("*") || [...old].some((t) => !next.has(t));
}

/** role 최소값 셀렉트의 선택지. 순서가 ROLE_RANK 다. */
export const ACCESS_ROLES = ["viewer", "developer", "tenant-admin", "platform-admin"] as const;

/** 동작별 기본 최소 role. roles·groups 를 둘 다 비우면 이 role 이상이다. */
export const DEFAULT_MIN_ROLE: Record<HubAction, string> = { view: "viewer", consume: "developer", manage: "developer" };

export function emptySubjects(): AccessSubjects {
  return { tenants: [], roles: [], groups: [] };
}

export function emptyAccess(): HubAccess {
  return { view: emptySubjects(), consume: emptySubjects(), manage: emptySubjects() };
}

/** 응답이나 부분 입력을 세 동작 × 세 목록이 모두 있는 모양으로 채운다. */
export function normalizeAccess(input: HubAccessInput | HubAccess | null | undefined): HubAccess {
  const out = emptyAccess();
  if (!input) return out;
  for (const action of HUB_ACTIONS) {
    const s = input[action];
    if (!s) continue;
    out[action] = { tenants: [...(s.tenants ?? [])], roles: [...(s.roles ?? [])], groups: [...(s.groups ?? [])] };
  }
  return out;
}

export function isEmptyAccess(access: HubAccess): boolean {
  return HUB_ACTIONS.every((a) => !access[a].tenants.length && !access[a].roles.length && !access[a].groups.length);
}

/** 게시 본문의 access. 비어 있으면 생략한다 (서버 기본값이 빈 정책이다). */
export function accessForPublish(access: HubAccess): HubAccessInput | undefined {
  return isEmptyAccess(access) ? undefined : access;
}

/**
 * scope 는 서버 값 그대로 나오면 뜻이 안 보인다. 요약과 정책 카드가 같은 한 줄을 쓴다. owner 는 항목을 가진 테넌트를 부르는
 * 말이고 비우면 허브 항목의 것이다. Skill 은 "소유 테넌트" 를 넘긴다(`lib/skills.ts`).
 */
export function scopeHint(t: Messages, scope: "tenant" | "shared", owner: string = t.hub.access.owner): string {
  return scope === "shared" ? t.hub.access.scope.shared : t.hub.access.scope.tenant(owner);
}

/**
 * 경계를 넓히는 저장을 할 수 있는가. 허브 항목은 hub:share, Skill 은 skill:share 이고 지금은 둘 다 platform-admin 만 가진다.
 * 두 리소스가 같은 방식(요청 테넌트의 scope)으로 판정해야 서버 규칙이 바뀔 때 한쪽만 어긋나지 않는다.
 */
export function canShare(me: MeOut | null, tenant: string, scope: "hub:share" | "skill:share"): boolean {
  return hasScope(me, tenant, scope);
}

/** 한 동작의 주체를 한 줄로. 읽기 전용 표시에 쓴다. owner 는 scopeHint 와 같다. */
export function describeSubjects(t: Messages, action: HubAction, s: AccessSubjects, owner: string = t.hub.access.owner): string {
  const text = t.hub.access.subjects;
  const parts: string[] = [];
  parts.push(s.tenants.length ? (s.tenants.includes("*") ? t.shared.accessPolicyEditor.allTenants : text.tenants(s.tenants.join(", "))) : text.owner(owner));
  if (!s.roles.length && !s.groups.length) parts.push(text.minRole(DEFAULT_MIN_ROLE[action]));
  if (s.roles.length) parts.push(text.roles(s.roles.join(", ")));
  if (s.groups.length) parts.push(text.groups(s.groups.join(", ")));
  return parts.join(" · ");
}
