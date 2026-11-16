/**
 * 대화의 수명을 말하는 문장이다 (D46).
 *
 * 세션이 닫히는 것과 기록이 지워지는 것은 다른 일이다. 세션 TTL(Chat 입구 노드의
 * `session_ttl_seconds`)이 지나면 대화는 닫혀 이어 쓸 수 없고, 전사는 기록 보존 기간
 * (플랫폼 설정 `transcript_retention_days`, 상태 응답에 실려 온다)이 지나야 사라진다.
 *
 * 두 숫자는 배포마다 다르므로 문장에 박아 두지 않는다. 읽지 못한 값이 있으면 숫자를 빼고 성질만
 * 말한다. 틀린 숫자를 보이느니 없는 편이 낫다. 문장은 사전(chat.sessionLife)에 있다.
 */
import type { Messages } from "@/i18n";
import { formatDuration } from "@/lib/utils";

/** 대화 목록 아래에 붙는 안내다. 세션 TTL 과 기록 보존 기간을 둘 다 알아야 숫자를 쓴다. */
export function sessionLifeHint(t: Messages, ttlSeconds?: number | null, retentionDays?: number | null): string {
  const text = t.chat.sessionLife;
  if (!ttlSeconds || ttlSeconds <= 0 || retentionDays == null || retentionDays < 0) return text.plain;
  const ttl = formatDuration(t, ttlSeconds);
  return retentionDays === 0 ? text.deletedOnClose(ttl) : text.retained(ttl, retentionDays);
}

/** 닫힌 대화를 열었을 때 입력칸 자리에 오는 문장이다. */
export function closedSessionNotice(t: Messages, ttlSeconds?: number | null): string {
  const text = t.chat.sessionLife;
  if (!ttlSeconds || ttlSeconds <= 0) return text.closed;
  return text.closedAfter(formatDuration(t, ttlSeconds));
}
