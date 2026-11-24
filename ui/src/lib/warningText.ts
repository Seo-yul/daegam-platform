/**
 * 리소스가 내는 상태를 화면 문구로 바꾼다.
 *
 * 서버의 `status.warnings` 는 `코드: 영어 설명` 이고 배포·재배포 응답의 `warnings` 도 같은 꼴이다.
 * 세 상세 화면이 그것을 그대로 찍고 있어서 한국어 화면에 영어 세 줄과 `(D16)` 같은 내부 결정 번호가
 * 그대로 섰다. 사전의 표(warnings.codes)는 코드마다 무엇이 걸렸고 무엇을 하면 되는지를 한 문장으로 적어 둔 것이다.
 *
 * 표에 없는 코드는 서버 문장을 그대로 보인다. 옮기지 못한 것을 감추는 것보다 영어로라도 보이는 편이 낫다.
 *
 * phase 와 없는 리소스 판정도 같이 둔다. 셋 다 "서버가 준 상태를 화면 문구로 바꾼다" 는 같은 일이고,
 * 상세 세 화면이 글자까지 같은 문장을 각자 적으면 문구를 손대는 사람이 하나를 빠뜨린다. phase 문장은
 * 사전의 resources.phase 에 있다.
 *
 * 문장은 사전(P49)에 있고 여기에는 코드, 서버 문장의 조각, 판정만 남는다.
 */
import { ApiError } from "@/api/client";
import type { Messages } from "@/i18n";
import type { CodeModeCause, HiddenCause, PausedSources } from "@/i18n/ko/warnings";
import { GATE_REASONS } from "@/lib/codeExecution";
import { isCodeShape } from "@/lib/errorText";
import { ownValue } from "@/lib/utils";

/** 경고 하나를 화면에 쓸 꼴로 푼 것. */
export interface DescribedWarning {
  /** `코드: 설명` 의 코드. 코드 없이 문장만 오는 경고도 있어서 그때는 null 이다. */
  code: string | null;
  /** 표 셀에 들어갈 짧은 이름. 표에 없으면 원문이다. */
  label: string;
  /** 한 문장 설명. 표에 없으면 서버 원문 그대로다. */
  text: string;
  /** 표에 있는 경고인가. 목록·상세가 원문을 따로 붙일지 정할 때 쓴다. */
  known: boolean;
  /** 사전 문장이 대신한 서버 원문에서 이름만 뽑은 것. 어떤 것이 걸렸는지를 사전 문장 아래에 그대로 보인다. 없으면 null 이다. */
  detail: string | null;
}

/** 원문 끝의 괄호 안. 규칙 경고와 게시된 백엔드의 경고가 걸린 이름을 거기 싣는다 (platform/daegam/services/tool_rules.py
 * rule_warning, platform/daegam/services/published_backends.py published_warning). */
const trailingNames = (raw: string): string | null => /\(([^)]+)\)$/.exec(raw)?.[1] ?? null;

/**
 * 사전 문장으로 바꿔도 이름은 서버 원문에만 있는 코드. 값은 원문에서 이름 부분을 뽑는다.
 * builtin_tool_name 은 `(main: write_file from calc; subagents[a]: read_file from fs)` 처럼 괄호 안에 Agent·Tool·소스를 싣는다
 * (agent-runtime/runtime/compile.py `builtin_name_warnings`).
 */
const DETAIL_OF: Record<string, (raw: string) => string | null> = {
  builtin_tool_name: (raw) => /\(([^)]+)\)/.exec(raw)?.[1] ?? null,
  // `agent_tool_hidden: … (cause: item; tools: a, b)` 의 Tool 이름이다(platform/daegam/services/agent_status.py hidden_tool_warnings)
  agent_tool_hidden: (raw) => /tools: ([^)]+)\)/.exec(raw)?.[1] ?? null,
  rules_tool_missing: trailingNames,
  rules_digest_behind: trailingNames,
  rules_gateways_behind: trailingNames,
  published_backend_closed: trailingNames,
  published_consumer_pending: trailingNames,
  published_names_unknown: trailingNames,
  // `tools_source_newer: … (tools-billing: agent)` 의 노드와 종류다(platform/daegam/services/agent_status.py newer_source_warning)
  tools_source_newer: trailingNames,
  // 게시한 Agent 를 Tool 로 쓰는 tools 노드다(platform/daegam/services/agent_calls.py status_warnings)
  agent_source_unpublished: trailingNames,
  agent_source_not_serving: trailingNames,
  agent_call_timeout_short: trailingNames,
  // `code_mode_closed: … (cause: tools; tools: a, b)` 의 Tool 이름이나 `(cause: unknown_names; backends: x)` 의 허브 항목 이름이다
  // (platform/daegam/services/gateway_code_mode.py closed_warning)
  code_mode_closed: (raw) => /(?:tools|backends): ([^)]+)\)/.exec(raw)?.[1] ?? null,
  code_mode_tools_hidden: trailingNames,
};

/**
 * 경고 표가 아니라 오류 표(errors.codes)에 문장이 있는 상태 경고다. 게시를 거둔 Agent 소스와 꺼진 Agent 호출은 저장할 때는 오류나
 * 경고이고 배포 뒤에는 상태 경고로 선다(platform/daegam/services/agent_calls.py status_warnings).
 */
const ERROR_TEXT_WARNINGS: ReadonlySet<string> = new Set(["agent_source_unpublished", "agent_calls_disabled"]);

/** 서버 문장의 `<key>: <값>` 가운데 values 에 있는 것. 모르는 값이면 null 이다. */
/**
 * 원문 괄호의 `<이름>: <값>` 의 값. 값은 `)` 나 `;` 로 끝나야 하고 영문자와 밑줄만 받으므로, 서버 원문의 다른 글이 화면 문장에
 * 들지 않는다(platform/daegam/services/tool_rules.py `coded_warning`).
 */
function rawValue(raw: string, key: string): string | null {
  return new RegExp(`${key}: ([A-Za-z_]+)(?=[);])`).exec(raw)?.[1] ?? null;
}

function valueIn<C extends string>(raw: string, key: string, values: readonly C[]): C | null {
  const value = rawValue(raw, key);
  return values.find((c) => c === value) ?? null;
}

/** 규칙이 Agent 의 Tool 을 숨기는 원인 (platform/daegam/services/agent_status.py HIDDEN_CAUSES). */
const HIDDEN_CAUSES: readonly HiddenCause[] = ["not_named", "item", "oidc", "reissue"];
/** Code mode 를 끈 까닭 (platform/daegam/services/gateway_code_mode.py CLOSED_CAUSES, CAUSE_TOOLHIVE). */
const CODE_MODE_CAUSES: readonly CodeModeCause[] = ["tools", "unknown_names", "reserved_name", "exposed", "owner_cedar", "toolhive"];

/**
 * 서버 문장의 `(<이름>: <까닭>)` 으로 사전 문장 하나를 고르는 코드. 값은 읽을 이름과 아는 까닭이다. 코드 실행 게이트는 `reason:`
 * (platform/daegam/services/sandboxes.py `gate_warnings`), Code mode 는 `cause:` 다(platform/daegam/services/gateway_code_mode.py
 * `closed_warning`).
 */
const CHOSEN_BY: Record<string, { key: string; values: readonly string[] }> = {
  sandbox_gate_closed: { key: "reason", values: GATE_REASONS },
  code_mode_closed: { key: "cause", values: CODE_MODE_CAUSES },
};

/**
 * 사전 문장에 수가 들어가는 코드. 값은 서버 원문에서 수를 뽑는다. runs_paused_by_deploy 는 `this deploy paused 2 schedule(s) and
 * 1 trigger(s) that run as someone else, and 0 whose settings no longer hold` 꼴이다(platform/daegam/services/runs/service.py
 * `RunService.on_deploy`).
 */
const COUNTS_OF: Record<string, (raw: string) => PausedSources | null> = {
  runs_paused_by_deploy: (raw) => {
    const m = /paused (\d+) schedule\(s\) and (\d+) trigger\(s\).*, and (\d+) whose/.exec(raw);
    return m ? { schedules: Number(m[1]), triggers: Number(m[2]), broken: Number(m[3]) } : null;
  },
};

/**
 * 표의 한 줄. 까닭으로 문장을 고르는 줄(코드 실행 게이트, Code mode)은 text 대신 까닭마다의 문장을 들고, 까닭을 문장 안에 넣는
 * 줄(노출 앞단의 넘겨받기)은 그 문장을 만드는 함수를 든다.
 */
type WarningEntry =
  | { text: string | ((counts: PausedSources) => string); causes?: Record<HiddenCause, string> }
  | { choices: Record<string, string> }
  | { withReason: (reason: string) => string };

/**
 * 표의 한 줄을 문장으로. 수를 받는 문장은 원문에서 수를 읽고, 읽지 못하면 null 이라 부르는 쪽이 원문을 보인다. 원인을 받는
 * 문장은 원문의 원인 문장을 잇고, 원인을 읽지 못하면 앞 문장만이다. 까닭을 받는 줄은 CHOSEN_BY 가 그 코드에 정한 이름
 * (`reason:`·`cause:`)으로 원문의 까닭을 읽어 그 문장을 고르고, 까닭을 읽지 못하면 null 이다. 까닭을 문장 안에 넣는 줄은 원문의
 * `(reason: <까닭>)` 을 그대로 넣고(platform/daegam/services/exposure_front.py `handover_warning`), 까닭이 없으면 null 이다.
 */
function entryText(entry: WarningEntry, code: string, raw: string | null): string | null {
  if ("choices" in entry) {
    const by = ownValue(CHOSEN_BY, code);
    const chosen = raw === null || by === undefined ? null : valueIn(raw, by.key, by.values);
    return chosen ? (ownValue(entry.choices, chosen) ?? null) : null;
  }
  if ("withReason" in entry) {
    const reason = raw === null ? null : rawValue(raw, "reason");
    return reason ? entry.withReason(reason) : null;
  }
  if (typeof entry.text === "string") {
    const cause = entry.causes && raw !== null ? valueIn(raw, "cause", HIDDEN_CAUSES) : null;
    return cause && entry.causes ? `${entry.text} ${entry.causes[cause]}` : entry.text;
  }
  const counts = raw === null ? null : (ownValue(COUNTS_OF, code)?.(raw) ?? null);
  return counts ? entry.text(counts) : null;
}

/**
 * 코드 없이 오는 경고 하나. match 는 서버 영어 문장의 고정된 조각이고, key 는 사전(warnings.prose)의 문장이다.
 * 사전의 text 가 함수면 문장 끝에 서버가 고른 고치는 법(`fixHint`)을 받는다.
 */
interface ProseWarning {
  match: string;
  key: keyof Messages["warnings"]["prose"];
}

/**
 * 도구 이름을 모를 때 무엇을 하면 되는지. 서버가 백엔드 종류를 보고 이미 갈라 적어 주므로
 * (`services/gateways.unknown_names_hint`) 화면은 그 판단을 다시 하지 않고 문장에서 읽어 낸다.
 * 파생 항목(internal·published)에는 도구 새로고침이 없어 422 다 (D49).
 */
function fixHint(t: Messages, raw: string): string {
  const refresh = raw.includes("run refresh-tools");
  const redeploy = raw.includes("redeploy the source");
  if (refresh && redeploy) return t.warnings.fixHint.both;
  if (redeploy) return t.warnings.fixHint.redeploy;
  return t.warnings.fixHint.refresh;
}

/**
 * 코드 없이 영어 문장만 오는 경고. Gateway 의 도구 충돌 계획과 검사 안내 넷이 그렇다
 * (`services/gateway_plan.py` 의 prefix 폴백·priority 미상, `services/gateway_validation.py` 의 필터 미검증·백엔드 과다).
 * 앞에 코드가 없으니 문장 안의 고정된 조각으로 알아본다.
 */
export const PROSE_WARNINGS: readonly ProseWarning[] = [
  { match: "no tool_names; falling back to prefix mode", key: "prefixFallback" },
  { match: "priority mode with unknown tool_names", key: "priorityConflict" },
  { match: "tools filter not validated", key: "filterUnchecked" },
  { match: "every session fans out tools/list", key: "tooManyBackends" },
];

/** 코드 없이 온 경고의 문장. 사전의 문장이 함수면 서버가 고른 고치는 법을 이어 받는다. */
function proseText(t: Messages, prose: ProseWarning, raw: string): string {
  const text = t.warnings.prose[prose.key].text;
  return typeof text === "function" ? text(fixHint(t, raw)) : text;
}

/** 코드에 맞는 한 문장. 표에 없으면 null 이다. */
export function warningText(t: Messages, code: string): string | null {
  const entry = ownValue(t.warnings.codes, code);
  return entry ? entryText(entry, code, null) : null;
}

/**
 * `코드: 설명` 에서 코드만. 코드 꼴(소문자·숫자·밑줄)이 아니면 코드가 없는 것이다.
 *
 * 코드 꼴만 보므로 Gateway 가 허브 항목 이름을 앞에 다는 경고(`weather: tool_names unknown …`)는
 * 이름을 코드로 읽는다. 그것은 describeWarning 이 문장 조각으로 걸러 낸다.
 */
export function warningCode(raw: string): string | null {
  const head = raw.split(":", 1)[0].trim();
  return isCodeShape(head) ? head : null;
}

/**
 * 같은 경고인지 가르는 열쇠. 코드가 있으면 코드이고 없으면 문장 원문이다.
 *
 * 상세 화면은 배포 응답이 실어 준 경고와 폴링으로 받은 `status.warnings` 를 따로 그린다. 방금 배포한
 * 직후에는 같은 조건이 양쪽에 다 들어와 같은 문장이 두 번 선다. 서버가 붙이는 영어 설명이 두 경로에서
 * 조금씩 다를 수 있으므로 문장이 아니라 코드로 가른다.
 */
export function warningKey(raw: string): string {
  return warningCode(raw) ?? raw;
}

/** warnings 에서 이미 보인 것(shown)과 같은 경고를 뺀 나머지. 같은지는 warningKey 로 가른다. */
export function unshownWarnings(warnings: readonly string[], shown: readonly string[]): string[] {
  const keys = new Set(shown.map(warningKey));
  return warnings.filter((w) => !keys.has(warningKey(w)));
}

/**
 * 경고 하나를 화면에 쓸 꼴로. 표에도 조각 목록에도 없으면 서버 원문을 그대로 돌려준다. resource 가 "hub" 면 허브 항목 상세의
 * 문장(warnings.hubCodes)을 먼저 본다. 같은 코드를 Gateway 도 내지만 고치는 법이 다르다(P52 7절).
 */
export function describeWarning(t: Messages, raw: string, resource?: "hub"): DescribedWarning {
  const head = warningCode(raw);
  const byCode = head ? ((resource === "hub" ? ownValue(t.warnings.hubCodes, head) : undefined) ?? ownValue(t.warnings.codes, head)) : undefined;
  const byCodeText = head && byCode ? entryText(byCode, head, raw) : null;
  if (head && byCode && byCodeText !== null) {
    return { code: head, label: byCode.label, text: byCodeText, known: true, detail: ownValue(DETAIL_OF, head)?.(raw) ?? null };
  }
  // 저장을 막는 오류와 같은 일이 배포 뒤에 경고로 서는 코드는 오류 표의 문장을 쓴다. 짧은 이름은 없어 코드가 선다
  const asError = head && ERROR_TEXT_WARNINGS.has(head) ? ownValue(t.errors.codes, head) : undefined;
  if (head && asError) return { code: head, label: head, text: asError, known: true, detail: ownValue(DETAIL_OF, head)?.(raw) ?? null };
  // 문장으로 알아본 것은 코드로 안 것이 아니므로 code 를 비운다. 앞머리가 코드 꼴이어도 그것은 이름이다
  const prose = PROSE_WARNINGS.find((p) => raw.includes(p.match));
  if (prose) return { code: null, label: t.warnings.prose[prose.key].label, text: proseText(t, prose, raw), known: true, detail: null };
  return { code: head, label: head ?? raw, text: raw, known: false, detail: null };
}

/** phase 배지 옆에 덧붙이는 사람 말. 모르는 phase 면 null 이다. */
export function phaseText(t: Messages, phase: string | null | undefined): string | null {
  return phase ? (ownValue(t.resources.phase.label, phase) ?? null) : null;
}

/** 아직 뜨는 중인 phase. 진행 배너가 phase 마다 다른 말을 한다. Waking 은 잠든 Agent 를 다시 띄우는 중이다 (P44 10절). */
type ProgressingPhase = "Pending" | "Rolling" | "Waking";

/** 아직 뜨는 중인 phase 인가. 이때는 진행 배너를 세운다. */
export function isProgressing(phase: string | null | undefined): phase is ProgressingPhase {
  return phase === "Pending" || phase === "Rolling" || phase === "Waking";
}

/**
 * phase 별로 "이만큼 넘으면 이상하다" 고 보는 시간(초).
 *
 * 파드가 뜨는 데 보통 30초, 교체가 끝나는 데 보통 1분이다. 그 네 배쯤을 넘기면 기다림이 아니라
 * 무엇인가 걸린 것이다. Deployment 의 progressDeadline 600초를 기다리면 화면은 10분 동안
 * "곧 됩니다" 를 말한다.
 */
const PROGRESS_LIMIT_SECONDS: Record<ProgressingPhase, number> = { Pending: 120, Rolling: 180, Waking: 120 };

/** 진행 배너. 예상 시간을 넘기면 warn 으로 바뀐다. */
export interface ProgressBanner {
  variant: "info" | "warn";
  text: string;
}

/**
 * 진행 배너 문구. 폴링 주기와 흐른 시간을 초로 받는다.
 *
 * 처음부터 예상 시간을 말하고, 그것을 넘기면 기다리라는 말을 거두고 어디를 보라고 말한다. 흐른
 * 시간을 모르면(옛 문서라 `since` 가 없다) 늘 하던 말만 한다 (D43 둘째).
 */
export function progressBanner(t: Messages, phase: string | null | undefined, pollSeconds: number, elapsed: number | null = null): ProgressBanner | null {
  if (!isProgressing(phase)) return null;
  const limit = PROGRESS_LIMIT_SECONDS[phase];
  if (elapsed !== null && elapsed > limit) return { variant: "warn", text: t.resources.phase.slow[phase](Math.floor(limit / 60)) };
  return { variant: "info", text: t.resources.phase.progress[phase](pollSeconds) };
}

/** 배포가 주저앉은 phase 인가. */
export function isFailed(phase: string | null | undefined): phase is "Failed" | "Missing" {
  return phase === "Failed" || phase === "Missing";
}

/** 주저앉았을 때 다음에 무엇을 하면 되는지 한 줄. 멀쩡한 phase 면 null 이다. */
export function failureAdvice(t: Messages, phase: string | null | undefined): string | null {
  return isFailed(phase) ? t.resources.phase.failure[phase] : null;
}

/** 없는 리소스를 열었을 때. 410 이면 언제 지워졌는지도 온다. */
export interface MissingResource {
  /** 소프트 삭제된 것인가. 404 는 false 다. */
  gone: boolean;
  /** 410 이 함께 준 삭제 시각. 없으면 null 이다. */
  deletedAt: string | null;
}

/**
 * 조회 오류가 "그런 것이 없다" 인지 본다. 404 는 원래 없는 것이고 410 은 지워진 것이다
 * (`api/_common.py` 의 `gone_if_deleted`).
 *
 * 앞서 받아 둔 데이터가 있으면 null 이다. 폴링 도중 남이 지운 경우까지 화면을 통째로 갈아 끼우면
 * 보고 있던 것이 사라지므로, 그때는 지금처럼 마지막 상태와 끊김 배너를 그대로 둔다.
 */
export function missingResource(error: unknown, data: unknown): MissingResource | null {
  if (data !== undefined) return null;
  if (!(error instanceof ApiError)) return null;
  if (error.status === 410) return { gone: true, deletedAt: error.extra<string>("deleted_at") ?? null };
  if (error.status === 404) return { gone: false, deletedAt: null };
  return null;
}
