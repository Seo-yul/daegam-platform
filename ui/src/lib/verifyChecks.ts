/**
 * verify 리포트 체크 id 의 묶음과 심각도. 정본은 platform/daegam/pipeline/verdict.py 의 CHECK_LEVELS 다.
 * id 집합과 심각도(error/warn)는 그 표와 같아야 하고, 테스트가 파일을 읽어 대조한다.
 * 이름표와 풀이는 사전의 backendApis.checks 에 있다.
 */
import type { CheckResult, CheckStatus, VerdictItem } from "@/api/types";
import type { Messages } from "@/i18n";
import { ownValue } from "@/lib/utils";

import { isRecord } from "./json";

export type CheckGroup = "document" | "tool-name" | "tool-description" | "x-soap" | "deep" | "other";

/** 이 체크가 실패했을 때 플랫폼이 매기는 심각도. error 면 등록 거부, warn 이면 경고다. */
export type CheckSeverity = "error" | "warn";

interface CheckRule {
  group: CheckGroup;
  severity: CheckSeverity;
}

/** 표에 놓이는 순서. other 는 사전에 없는 id 를 받는 자리다. */
export const CHECK_GROUPS: readonly CheckGroup[] = ["document", "tool-name", "tool-description", "x-soap", "deep", "other"];

/** 체크 id 마다 표의 묶음과 실패했을 때의 심각도. */
export const CHECK_INFO = {
  // document
  "document.mapping": { group: "document", severity: "error" },
  "document.openapi3": { group: "document", severity: "error" },
  "document.has-paths": { group: "document", severity: "error" },
  "document.has-operations": { group: "document", severity: "error" },
  // tool-name
  "tool-name.present": { group: "tool-name", severity: "error" },
  "tool-name.safe": { group: "tool-name", severity: "error" },
  "tool-name.normalized": { group: "tool-name", severity: "warn" },
  "tool-name.unique": { group: "tool-name", severity: "error" },
  "tool-name.normalization-collision": { group: "tool-name", severity: "error" },
  // tool-description
  "tool-description.present": { group: "tool-description", severity: "warn" },
  // x-soap
  "x-soap.version": { group: "x-soap", severity: "warn" },
  "x-soap.endpoint": { group: "x-soap", severity: "warn" },
  "x-soap.input-element": { group: "x-soap", severity: "error" },
  "x-soap.output-element": { group: "x-soap", severity: "warn" },
  "x-soap.refs": { group: "x-soap", severity: "error" },
  "x-soap.choice": { group: "x-soap", severity: "error" },
  "x-soap.substitution": { group: "x-soap", severity: "error" },
  "x-soap.mixed-rest": { group: "x-soap", severity: "error" },
  // deep
  "openapi.schema-valid": { group: "deep", severity: "error" },
  "fastmcp.roundtrip": { group: "deep", severity: "error" },
  "fastmcp.tool-materialized": { group: "deep", severity: "error" },
} as const satisfies Readonly<Record<string, CheckRule>>;

/** 사전에 있는 체크 id. 사전의 이름표 표가 이 id 를 모두 덮는다. */
export type CheckId = keyof typeof CHECK_INFO;

/** 사전에 있는 id 목록. CHECK_LEVELS 와 같아야 한다. */
export const CHECK_IDS: readonly string[] = Object.keys(CHECK_INFO);

/** 체크 하나를 화면에 쓸 꼴로. 사전에 없는 id 는 other 그룹, 제목은 id 그대로다. 심각도는 알 수 없어 null 이다. */
export function describeCheck(t: Messages, id: string): { group: CheckGroup; title: string; description: string; severity: CheckSeverity | null } {
  const rule: CheckRule | undefined = ownValue(CHECK_INFO, id);
  const text = ownValue(t.backendApis.checks.check, id);
  return rule && text ? { ...rule, ...text } : { group: "other", title: id, description: "", severity: null };
}

/** 플랫폼이 스펙에서 직접 찾는 항목의 code (platform/daegam/pipeline/verdict.py CASE_COLLISION_CODE). */
const CASE_COLLISION_CODE = "tool-args.case-collision";

/**
 * verdict 항목의 문장 (P49 6절). 화면이 아는 code 이고 항목이 그 인자를 가지면 화면 언어로 만든다. 그 밖에는 서버가 쓴
 * message 다. 라이브러리 항목의 영어와 args 가 생기기 전에 저장된 판정이 그렇다.
 */
export function verdictItemText(t: Messages, item: Pick<VerdictItem, "code" | "message"> & Partial<Pick<VerdictItem, "args">>): string {
  const { tool, names } = item.args ?? {};
  if (item.code === CASE_COLLISION_CODE && typeof tool === "string" && Array.isArray(names)) return t.backendApis.verdict.caseCollision(tool, names.join(" · "));
  return item.message;
}

export interface GroupedChecks {
  group: CheckGroup;
  rows: CheckResult[];
}

/** 결과를 그룹 순서로 묶는다. 그룹 안에서는 서버가 준 순서를 지킨다. 빈 그룹은 뺀다. */
export function groupCheckResults(results: readonly CheckResult[]): GroupedChecks[] {
  const byGroup = new Map<CheckGroup, CheckResult[]>();
  for (const r of results) {
    const g = ownValue<CheckRule>(CHECK_INFO, r.id)?.group ?? "other";
    const list = byGroup.get(g);
    if (list) list.push(r);
    else byGroup.set(g, [r]);
  }
  return CHECK_GROUPS.flatMap((group) => {
    const rows = byGroup.get(group);
    return rows?.length ? [{ group, rows }] : [];
  });
}

export type CheckCounts = Record<CheckStatus, number> & { total: number };

export function countCheckResults(results: readonly CheckResult[]): CheckCounts {
  const c: CheckCounts = { pass: 0, warn: 0, fail: 0, skip: 0, total: results.length };
  for (const r of results) {
    if (r.status in c) c[r.status] += 1;
  }
  return c;
}

export interface RoundtripTool {
  name: string;
  params: { name: string; type?: string; required?: boolean }[];
}

function paramsOf(tool: Record<string, unknown>): RoundtripTool["params"] {
  const raw = tool.parameters ?? tool.params ?? tool.inputSchema ?? tool.input_schema;
  if (Array.isArray(raw)) {
    return raw.flatMap((p) => {
      if (typeof p === "string") return [{ name: p }];
      if (isRecord(p) && typeof p.name === "string") {
        return [{ name: p.name, type: typeof p.type === "string" ? p.type : undefined, required: p.required === true }];
      }
      return [];
    });
  }
  if (isRecord(raw)) {
    const props = isRecord(raw.properties) ? raw.properties : raw;
    const required = new Set(Array.isArray(raw.required) ? raw.required.map(String) : []);
    return Object.entries(props).map(([name, schema]) => ({
      name,
      type: isRecord(schema) && typeof schema.type === "string" ? schema.type : undefined,
      required: required.has(name),
    }));
  }
  return [];
}

/** fastmcp.roundtrip 의 data.tools 를 표로 그릴 수 있는 모양으로 편다. 모양이 다르면 null 이다. */
export function roundtripTools(result: CheckResult | undefined): RoundtripTool[] | null {
  if (!result || !isRecord(result.data) || !Array.isArray(result.data.tools)) return null;
  return result.data.tools.flatMap((tool) => {
    if (typeof tool === "string") return [{ name: tool, params: [] }];
    if (isRecord(tool) && typeof tool.name === "string") return [{ name: tool.name, params: paramsOf(tool) }];
    return [];
  });
}

export interface CheckRef {
  id: string;
  url: string;
}

export function checkRefs(result: CheckResult): CheckRef[] {
  if (!Array.isArray(result.refs)) return [];
  return result.refs.flatMap((r) =>
    isRecord(r) && typeof r.url === "string" ? [{ id: typeof r.id === "string" ? r.id : r.url, url: r.url }] : [],
  );
}
