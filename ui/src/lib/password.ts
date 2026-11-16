/**
 * local 인증 모드(D40)의 비밀번호 길이 규칙. 서버 `platform/daegam/auth/password.py` 의 `MIN_LENGTH` 와 같다.
 * 서버는 파이썬 `len` 으로 코드 포인트를 세므로 여기서도 UTF-16 단위가 아니라 코드 포인트로 센다.
 */
import type { Messages } from "@/i18n";

export const MIN_PASSWORD_LENGTH = 12;

function length(value: string): number {
  return [...value].length;
}

export function passwordLongEnough(value: string): boolean {
  return length(value) >= MIN_PASSWORD_LENGTH;
}

/**
 * 비밀번호를 거듭 틀려 잠겼을 때(429 login_locked)의 문장. 로그인, 비밀번호 바꾸기, 개인 토큰 발급이 서버에서
 * 같은 잠금을 쓰므로(P32 6절) 화면도 같은 문장을 쓴다. retryAfter 는 서버의 Retry-After(남은 초)다.
 */
export function lockedMessage(t: Messages, retryAfter: string | null): string {
  const minutes = retryAfterMinutes(retryAfter);
  return minutes === null ? t.account.password.locked : t.account.password.lockedFor(minutes);
}

/** Retry-After(남은 초)를 올림한 분으로. 서버의 429 는 모두 초를 싣는다. 없거나 숫자가 아니면 null 이다. */
function retryAfterMinutes(seconds: string | null): number | null {
  const n = Number(seconds ?? "");
  if (!seconds || !Number.isFinite(n)) return null;
  return Math.max(1, Math.ceil(n / 60));
}

/** 입력 중인 비밀번호가 짧으면 몇 자 더 필요한지 말한다. 비었거나 충분하면 null 이다. */
export function passwordTooShort(t: Messages, value: string): string | null {
  const n = length(value);
  if (n === 0 || n >= MIN_PASSWORD_LENGTH) return null;
  return t.account.password.tooShort(n, MIN_PASSWORD_LENGTH - n);
}
