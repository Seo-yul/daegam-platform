/**
 * Database 의 verify 가 남긴 `capabilities` 를 칩으로 읽는다 (D32 pgvector, D33 checkpointer).
 * 값의 정본은 platform/daegam/services/data_services.py 다. 칩의 색은 여기서 정하고 글자는 사전(dataServices.capability)에 있다.
 */
import type { BadgeProps } from "@/components/ui/badge";
import type { DataService, DataServiceVerifyResult } from "@/api/types";
import type { Messages } from "@/i18n";

export interface CapabilityBadge {
  variant: BadgeProps["variant"];
  text: string;
}

/**
 * redis 를 체크포인터로 쓸 수 있는가. verify 가 `ready` 를 남겼고 db 가 0 이어야 한다.
 *
 * db 를 함께 보는 것은 모듈이 다 있어도 db 가 0 이 아니면 RediSearch 가 인덱스를 못 만들어
 * 체크포인터로 못 쓰기 때문이다(D33 의 구멍, P17 F5). verify 는 접속만 보므로 그 조합에도
 * `ready` 를 남기는데, 그대로 초록 칩을 보이면 memory 노드의 `redis_db_not_zero` 오류와
 * 같은 화면에서 반대 말을 한다(실측).
 */
export function checkpointerUsable(value: string | undefined, db?: number | null): boolean {
  return value === "ready" && (db ?? 0) === 0;
}

/** redis 의 `capabilities.checkpointer` 칩. memory 노드 셀렉트 아래와 Database 상세가 쓴다. 없으면 verify 전이다. */
export function checkpointerBadge(t: Messages, capability: string | undefined, db?: number | null): CapabilityBadge {
  const text = t.dataServices.capability;
  switch (capability) {
    case "ready":
      // 쓸 수 없는 ready 는 db 가 0 이 아닌 경우뿐이다
      return checkpointerUsable(capability, db) ? { variant: "ok", text: text.checkpointer.ready } : { variant: "warn", text: text.checkpointer.dbNotZero(db ?? 0) };
    case "modules_missing":
    case "unknown":
      return { variant: "warn", text: text.checkpointer[capability] };
    default:
      return { variant: "muted", text: text.unverified };
  }
}

/** 이 Database 종류가 남기는 capability 이름. 상세가 이 순서로 칩을 놓는다. */
export function capabilityNames(kind: DataService["kind"]): ("pgvector" | "checkpointer")[] {
  return kind === "redis" ? ["checkpointer"] : ["pgvector"];
}

export interface RedisVerify {
  version: string | null;
  modules: string[] | null;
  checkpointer: string;
}

/** verify 응답의 `redis` 를 좁힌다. 타입이 `{[key]: unknown}` 이라 여기서 검사한다. redis 가 아니면 null 이다. */
export function redisVerify(result: DataServiceVerifyResult): RedisVerify | null {
  const r = result.redis;
  if (!r) return null;
  const modules = Array.isArray(r.modules) ? r.modules.filter((m): m is string => typeof m === "string") : null;
  return {
    version: typeof r.version === "string" ? r.version : null,
    modules,
    checkpointer: typeof r.checkpointer === "string" ? r.checkpointer : "unknown",
  };
}

/** "Redis 8.2.1 · 모듈 search, ReJSON · checkpointer ready". 버전 권한이 없으면 그렇게 말하고 모듈 권한이 없으면 그 부분을 뺀다. */
export function redisVerifyText(t: Messages, r: RedisVerify): string {
  const text = t.dataServices.verify;
  const parts = [r.version ? `Redis ${r.version}` : text.noVersion];
  if (r.modules) parts.push(text.modules(r.modules.join(", ")));
  parts.push(`checkpointer ${r.checkpointer}`);
  return parts.join(" · ");
}
