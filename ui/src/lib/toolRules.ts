/**
 * Tool 규칙(P52 1·4절)의 저장 꼴과 화면이 고치는 꼴 사이의 변환. 카드(components/ToolRules.tsx)는 이 꼴만 다룬다.
 *
 * - 저장 꼴은 세 출처의 합집합이다. MCP Server 는 callers·read_only·conditions, Gateway 는 callers, 허브 image 항목은
 *   역할만 든 callers 와 read_only 다. 서버가 출처마다 받는 칸을 다시 검사한다(422).
 * - 읽기 전용 표시가 기본값(서버는 GET·HEAD 가 읽기, 항목은 쓰기)과 같으면 저장하지 않는다(null). 아무것도 정하지 않은
 *   규칙은 서버가 버리므로 화면도 보내지 않는다.
 * - 조건의 값은 화면에서 글로 고치고 저장할 때 인자의 형식으로 바꾼다. 숫자로 읽지 못한 값은 글 그대로 보내 서버가
 *   형식 오류(rule_argument_type)로 말하게 한다.
 */
import type { Schemas } from "@/api/types";

export type RuleOperator = Schemas["ArgCondition"]["op"];
export type RuleRole = NonNullable<Schemas["ToolCallers"]["roles"]>[number];
export type RulesNotice = Schemas["RulesNotice"];
/** 규칙을 든 리소스. 출처마다 받는 칸이 다르다. */
export type RuleKind = "server" | "gateway" | "item";

/**
 * 규칙이 고정한 Tool 목록(Gateway 백엔드의 filter, image 항목의 toolsFilter)이 목록에 없는 이름의 호출까지 거절하는가. P52 F0b 9번이
 * 잴 일이고 아직 재지 않았다. 지금 아는 것은 그 이름이 tools/list 에서 빠진다는 것뿐이라 "그 Tool 만 제공합니다" 는 안내
 * (toolRules.gatewayPinHint·itemPinHint)를 감춘다. F0b 9번의 통과를 기록하는 PR 에서 true 로 바꾼다.
 */
export const PINNED_LIST_REFUSES_CALLS = false;

/** 역할은 최소 역할이다. 낮은 것부터다. */
export const RULE_ROLES: readonly RuleRole[] = ["viewer", "developer", "tenant-admin"];
export const RULE_OPERATORS: readonly RuleOperator[] = ["equals", "one_of", "min", "max", "max_length", "prefix"];

/** 저장 꼴의 규칙 하나. 세 출처의 합집합이다. */
export interface StoredRule {
  callers?: Schemas["ToolCallers"];
  read_only?: boolean | null;
  conditions?: Schemas["ArgCondition"][];
}

/** 화면이 고치는 조건 하나. 값은 글이고, stored 는 저장된 값이다. 글을 고치지 않았으면 저장된 값을 그대로 보낸다. */
export interface ConditionDraft {
  path: string;
  op: RuleOperator;
  text: string;
  optional: boolean;
  stored?: unknown;
}

/** 화면이 고치는 규칙 하나. groups 는 쉼표로 나눈 글이고 readOnly 가 null 이면 기본값이다. */
export interface RuleDraft {
  roles: RuleRole[];
  groups: string;
  agents: string[];
  readOnly: boolean | null;
  conditions: ConditionDraft[];
}

/** 서버가 읽기 전용으로 보는 메서드. 표시가 없을 때의 기본값이다(platform/daegam/services/tool_effects.py READ_METHODS). */
const READ_METHODS = new Set(["GET", "HEAD"]);

/** MCP Server 의 Tool 이 읽기 전용 표시의 기본값으로 읽기인가. 메서드를 모르면 쓰기다. */
export function methodReadOnly(method: string | null | undefined): boolean {
  return READ_METHODS.has((method ?? "").toUpperCase());
}

/** 조건의 값을 글로. one_of 의 목록은 쉼표로 잇는다. */
export function valueText(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  return value === undefined || value === null ? "" : String(value);
}

function scalar(text: string, type: string | undefined): unknown {
  const trimmed = text.trim();
  if (type === "integer" || type === "number") {
    const n = Number(trimmed);
    return trimmed !== "" && Number.isFinite(n) ? n : trimmed;
  }
  if (type === "boolean") return trimmed === "true" ? true : trimmed === "false" ? false : trimmed;
  return text;
}

/** 글을 인자의 형식에 맞는 값으로. type 은 FastMCP 입력 스키마의 형식(string·integer·number·boolean)이다. */
export function parseValue(text: string, op: RuleOperator, type: string | undefined): unknown {
  if (op === "one_of") {
    return text
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => scalar(part, type));
  }
  if (op === "min" || op === "max" || op === "max_length") return scalar(text, "number");
  return scalar(text, type);
}

/** 그룹 글을 이름 목록으로. */
export function groupList(text: string): string[] {
  return [...new Set(text.split(",").map((g) => g.trim()).filter(Boolean))];
}

export function draftOf(rule: StoredRule | undefined): RuleDraft {
  return {
    roles: [...(rule?.callers?.roles ?? [])],
    groups: (rule?.callers?.groups ?? []).join(", "),
    agents: [...(rule?.callers?.agents ?? [])],
    readOnly: rule?.read_only ?? null,
    conditions: (rule?.conditions ?? []).map((c) => ({ path: c.path, op: c.op, text: valueText(c.value), optional: Boolean(c.optional), stored: c.value })),
  };
}

/** 규칙에 누가 부를지의 대상이 있는가. 서버의 `has_caller_rule` 과 같다. */
export function hasCallers(rule: StoredRule | undefined): boolean {
  const c = rule?.callers;
  return Boolean(c && ((c.roles?.length ?? 0) || (c.groups?.length ?? 0) || (c.agents?.length ?? 0)));
}

/** 누가 부를지의 대상이나 인자 조건이 있는 규칙의 수. 읽기 전용 표시만 있는 규칙은 세지 않는다(서버의 `holds_rules`). */
export function ruledCount(rules: Readonly<Record<string, StoredRule>>): number {
  return Object.values(rules).filter((r) => hasCallers(r) || (r.conditions?.length ?? 0) > 0).length;
}

/** 저장된 규칙에는 대상이 있었는데 고친 것에는 없다. 저장하면 그 Tool 이 모두에게 열린다(1절). */
export function opening(stored: StoredRule | undefined, draft: RuleDraft): boolean {
  return hasCallers(stored) && !draft.roles.length && !groupList(draft.groups).length && !draft.agents.length;
}

/**
 * 고친 꼴을 저장 꼴로. 출처가 받지 않는 칸은 넣지 않는다. arguments 는 그 Tool 의 {점 경로: 형식} 이고, readOnlyDefault 는
 * 읽기 전용 표시의 기본값이다. 아무것도 정하지 않았으면 null 이다.
 */
export function ruleOf(
  kind: RuleKind,
  draft: RuleDraft,
  arguments_: Readonly<Record<string, string>> | null | undefined,
  readOnlyDefault: boolean,
): StoredRule | null {
  const roles = RULE_ROLES.filter((r) => draft.roles.includes(r));
  const callers: Schemas["ToolCallers"] = { roles };
  if (kind !== "item") {
    callers.groups = groupList(draft.groups);
    callers.agents = [...new Set(draft.agents)].sort();
  }
  const out: StoredRule = { callers };
  if (kind !== "gateway" && draft.readOnly !== null && draft.readOnly !== readOnlyDefault) out.read_only = draft.readOnly;
  if (kind === "server" && draft.conditions.length) {
    out.conditions = draft.conditions.map((c) => ({
      path: c.path,
      op: c.op,
      value: c.stored !== undefined && c.text === valueText(c.stored) ? c.stored : parseValue(c.text, c.op, arguments_?.[c.path]),
      optional: c.optional,
    }));
  }
  const blank = !hasCallers(out) && out.read_only === undefined && !out.conditions;
  return blank ? null : out;
}

/** 저장된 규칙을 같은 변환에 한 번 태운 것. 고치기 전과 뒤를 같은 꼴로 견주려고 쓴다. */
export function normalizedRule(kind: RuleKind, rule: StoredRule | undefined, arguments_: Readonly<Record<string, string>> | null | undefined, readOnlyDefault: boolean): StoredRule | null {
  return ruleOf(kind, draftOf(rule), arguments_, readOnlyDefault);
}

/** 조건을 둘 수 있는 인자의 형식. 서버의 연산자 표가 받는 넷이다(`services/tool_rules.py` `_OP_TYPES`). */
const CONDITION_TYPES = new Set(["string", "integer", "number", "boolean"]);

/** 조건을 둘 수 있는 인자. 배열·객체와 형식을 모르는 인자는 뺀다. 인자 목록은 배열 안으로 내려가지 않는다(4절). */
export function conditionArguments(arguments_: Readonly<Record<string, string>> | null | undefined): [string, string][] {
  return Object.entries(arguments_ ?? {}).filter(([, type]) => CONDITION_TYPES.has(type));
}
