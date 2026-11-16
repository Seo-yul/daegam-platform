/**
 * 개인 MCP 토큰(P32)의 화면 규칙. 만료 선택, 상태 배지의 색, 토큰을 받을 수 있는 테넌트다. 말은 사전의
 * `account` 에 있다(P49).
 *
 * 서버 규칙은 `platform/daegam/services/personal_tokens.py` 다. 만료는 지금부터 1일 이상 `max_days` 이하이고,
 * 테넌트는 역할과 무관하게 소속 줄이 있어야 한다. platform-admin 의 `*` 는 소속이 아니다.
 */
import { ApiError } from "@/api/client";
import type { MeOut, PersonalToken, PersonalTokenStatus } from "@/api/types";
import type { BadgeProps } from "@/components/ui/badge";
import type { Messages } from "@/i18n";
import { lockedMessage } from "@/lib/password";
import { peekSessionItem, removeSessionItem, tokenDraftKey, writeSessionItem } from "@/lib/storage";
import { memberTenantNames } from "@/lib/tenant";
import { DAY_MS } from "@/lib/utils";

const DAYS_PER_YEAR = 365;

/** `/auth/config` 에 `personal_tokens` 가 없을 때의 상한. 서버 `DAEGAM_PAT_MAX_DAYS` 의 기본값과 같다. */
export const DEFAULT_MAX_DAYS = DAYS_PER_YEAR;

/** 미리 정한 만료(일). 마지막 것은 "1년" 으로 읽는다. */
export const EXPIRY_PRESETS = [7, 30, 90, 180, DAYS_PER_YEAR] as const;

const DEFAULT_EXPIRY_DAYS = 30;

/** 만료 선택에서 날짜를 직접 고르는 항목의 값. */
export const CUSTOM_EXPIRY = "date";

export function expiryLabel(t: Messages, days: number): string {
  return days === DAYS_PER_YEAR ? t.account.issue.expiryYear : t.account.issue.expiryDays(days);
}

/** 상한을 넘는 항목은 숨긴다. */
export function expiryPresets(maxDays: number): number[] {
  return EXPIRY_PRESETS.filter((d) => d <= maxDays);
}

/**
 * 처음 선택. preferred(기본 30일, 서비스 어카운트는 90일)가 상한을 넘거나 미리 정한 값이 아니면 남은 것 중 가장 긴 것이고, 그것도
 * 없으면 날짜 선택이다.
 */
export function defaultExpiry(maxDays: number, preferred: number = DEFAULT_EXPIRY_DAYS): string {
  const presets = expiryPresets(maxDays);
  if (presets.includes(preferred)) return String(preferred);
  const longest = presets.at(-1);
  return longest === undefined ? CUSTOM_EXPIRY : String(longest);
}

/** 날짜 칸의 값(로컬 날짜 YYYY-MM-DD). */
export function dateInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 날짜 칸이 받는 범위. 내일부터 상한 날까지다. */
export function dateRange(maxDays: number, now = new Date()): { min: string; max: string } {
  return { min: dateInputValue(new Date(now.getTime() + DAY_MS)), max: dateInputValue(new Date(now.getTime() + maxDays * DAY_MS)) };
}

/** 오늘에서 그 날짜까지 며칠인가. 두 날짜를 UTC 자정으로 읽어 날 수만 세므로 일광 절약 시간에 흔들리지 않는다. */
function daysUntil(date: string, now: Date): number {
  const picked = Date.parse(`${date}T00:00:00Z`);
  const today = Date.parse(`${dateInputValue(now)}T00:00:00Z`);
  return Math.round((picked - today) / DAY_MS);
}

/**
 * 선택을 만료 시각(ISO)으로. 날짜를 고르면 그날의 지금 시각이라 내일은 정확히 1일, 상한 날은 정확히 상한이다.
 * 범위를 벗어나거나 날짜를 아직 고르지 않았으면 null 이다.
 */
export function expiresAtFor(choice: string, date: string, maxDays: number, now = new Date()): string | null {
  const days = choice === CUSTOM_EXPIRY ? daysUntil(date, now) : Number(choice);
  if (!Number.isFinite(days) || days < 1 || days > maxDays) return null;
  return new Date(now.getTime() + days * DAY_MS).toISOString();
}

/** 목록의 상태 배지 하나. 개인 토큰과 OAuth 연결 목록이 같은 모양을 쓴다. */
export interface StatusBadge {
  label: string;
  variant: BadgeProps["variant"];
}

/** 목록의 상태 배지 색. 라벨은 사전의 `account.tokenTable.status` 다. */
export const STATUS_VARIANT: Record<PersonalTokenStatus, BadgeProps["variant"]> = {
  active: "ok",
  pending: "info",
  revoking: "warn",
  revoked: "muted",
  expired: "muted",
};

/** 지금 쓸 수 있거나 곧 쓸 수 있는 토큰. 비밀번호를 바꾸면 이것들이 회수되고, 목록에서 회수할 수 있는 것도 이것이다. */
export function isLive(token: Pick<PersonalToken, "status">): boolean {
  return token.status === "active" || token.status === "pending";
}

/** 토큰을 받을 수 있는 테넌트. `/me` 의 소속 줄에서 `*` 를 빼고 이름순으로 둔다. */
export function memberTenants(me: MeOut | null): string[] {
  return [...new Set(memberTenantNames(me?.memberships ?? []))].sort();
}

/** IdP 로 다시 로그인하러 떠날 때 남기는 발급 폼의 입력 (P53b 6.4절). 비밀번호는 없다(oidc 모드의 폼이다). */
export interface IssueDraft {
  tenant: string;
  label: string;
  expiry: string;
  date: string;
}

export function saveIssueDraft(form: string, draft: IssueDraft): void {
  writeSessionItem(tokenDraftKey(form), JSON.stringify(draft));
}

/** 남긴 입력이 있는가. 다시 로그인하고 돌아온 화면이 그 폼을 열어 둘지 정한다. */
export function hasIssueDraft(form: string): boolean {
  return peekSessionItem(tokenDraftKey(form)) !== null;
}

/** 남긴 입력을 지운다. 돌아온 폼이 되살린 뒤에 부른다. */
export function clearIssueDraft(form: string): void {
  removeSessionItem(tokenDraftKey(form));
}

/** 남긴 입력. 없거나 모양이 다르면 null 이다. 지우지는 않는다. */
export function peekIssueDraft(form: string): IssueDraft | null {
  try {
    const parsed: unknown = JSON.parse(peekSessionItem(tokenDraftKey(form)) ?? "null");
    if (!parsed || typeof parsed !== "object") return null;
    const d = parsed as Record<string, unknown>;
    const fields = [d.tenant, d.label, d.expiry, d.date];
    if (!fields.every((v) => typeof v === "string")) return null;
    return { tenant: d.tenant as string, label: d.label as string, expiry: d.expiry as string, date: d.date as string };
  } catch {
    return null;
  }
}

/** 발급 폼의 비밀번호 칸 오류. 틀렸거나 잠긴 것만 칸에 붙이고 나머지는 폼 아래 오류 상자가 말한다. */
export function passwordFieldError(t: Messages, err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  if (err.code === "invalid_credentials") return t.account.issue.wrongPassword;
  if (err.code === "login_locked") return lockedMessage(t, err.retryAfter);
  return null;
}
