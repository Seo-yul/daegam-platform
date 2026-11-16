/**
 * Database 연결 확인(verify)이 낸 코드를 "다음에 무엇을 하면 되는지" 로 옮긴다. 사용자가 할 일은 `~해 주세요` 다.
 *
 * verify 는 실패도 오류가 아니라 결과 객체({ok: false, error, detail})로 오므로 ErrorMessage 가 코드를 풀어
 * 주지 않는다. 그래서 여기서 코드를 문장으로 바꾼다. 코드 갈래는 postgres 가 connect_failed·auth_failed·
 * unknown_database·query_failed(services/knowledge/pg.py), redis 가 auth_failed·connect_failed·
 * select_failed·ping_failed·io_error 와 TLS 끝점의 tls_failed·ca_missing(services/data_services.py) 다.
 *
 * 사전의 dataServices.verify 에는 오류 사전(i18n/{ko,en}/errors.ts)에 없거나, 이 화면에서 할 일을 말해야 해서 문장이 달라야
 * 하는 코드만 둔다. auth_failed 는 다시 넣고 저장하라고, query_failed 는 verify 계정의 권한을 보라고 말한다. 나머지
 * (unknown_database 등)는 오류 사전의 문장을 그대로 쓴다.
 */
import type { DataService } from "@/api/types";
import type { BadgeProps } from "@/components/ui/badge";
import type { Messages } from "@/i18n";
import { checkpointerBadge, checkpointerUsable, type CapabilityBadge } from "@/lib/dataServices";
import { errorText } from "@/lib/errorText";
import { ownValue } from "@/lib/utils";

/** 오류 사전의 다른 코드와 같은 일이라 그 문장을 쓰는 갈래. connect_failed 는 다른 화면이 쓰는 db_unreachable 이다. */
const SAME_AS_ERROR_CODE = { connect_failed: "db_unreachable" } as const satisfies Record<string, keyof Messages["errors"]["codes"]>;

/** verify 결과의 error 코드에 붙일 한 문장. 모르는 코드면 null 이라 코드 줄만 남는다. */
export function verifyErrorText(t: Messages, kind: DataService["kind"], code: string | null | undefined): string | null {
  if (!code) return null;
  const text = t.dataServices.verify;
  return ownValue<string>(text[kind], code) ?? ownValue(text.common, code) ?? errorText(t, ownValue(SAME_AS_ERROR_CODE, code) ?? code);
}

/** postgres 의 `capabilities.pgvector` 값이 뜻하는 칩의 색. 글자는 사전의 dataServices.capability.pgvector 다. */
const PGVECTOR_VARIANT = { installed: "ok", available: "info", missing: "warn" } as const satisfies Record<string, BadgeProps["variant"]>;

/**
 * capability 칩에 쓸 사람 문구와 색. checkpointer 는 이미 있는 표를 그대로 쓰고 db 도 함께 본다.
 * pgvector 는 verify 전(undefined)이면 회색이고, 표에 없는 값이 오면 노란색으로 다시 확인하라고 한다.
 */
export function capabilityBadge(t: Messages, name: "pgvector" | "checkpointer", value: string | undefined, db?: number | null): CapabilityBadge {
  if (name === "checkpointer") return checkpointerBadge(t, value, db);
  const text = t.dataServices.capability;
  if (value === undefined) return { variant: "muted", text: text.unverified };
  const variant = ownValue<BadgeProps["variant"]>(PGVECTOR_VARIANT, value);
  const label = ownValue(text.pgvector, value);
  return variant && label ? { variant, text: label } : { variant: "warn", text: text.unverified };
}

/**
 * capability 값이 뜻하는 것과 다음 행동. 칩만으로는 "그래서 무엇을 하라는 것인지" 가 안 보인다.
 * 특히 redis 의 modules_missing 은 접속이 성공한 초록 화면에 함께 뜨므로 문제로 읽히지 않는다.
 */
export function capabilityNote(t: Messages, name: "pgvector" | "checkpointer", value: string | undefined, db?: number | null): string | null {
  const text = t.dataServices.capability.note;
  if (name === "checkpointer") {
    // 모듈이 다 있어도 db 가 0 이 아니면 RediSearch 가 인덱스를 못 만든다 (P17 F5)
    if (value === "ready" && !checkpointerUsable(value, db)) return text.checkpointer.dbNotZero(db ?? 0);
    switch (value) {
      case "modules_missing":
      case "unknown":
        return text.checkpointer[value];
      case "ready":
        return null;
      default:
        return text.checkpointer.unverified;
    }
  }
  switch (value) {
    case "missing":
    case "available":
      return text.pgvector[value];
    case "installed":
      return null;
    default:
      return text.pgvector.unverified;
  }
}
