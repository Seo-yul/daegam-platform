/**
 * RFC 6901 JSON pointer 와 D25 변경 목록(changes[{id, pointer, kind, before, after}]) 을 다루는 순수 함수.
 *
 *   parsePointer / formatPointer / parentPointer : "/paths/~1pets/get" ↔ ["paths", "/pets", "get"] (~0·~1 언이스케이프)
 *   applyChanges                                 : 원본 deep copy 위에 채택한 변경만 심는다 (changed·added 는 set, removed 는 delete)
 *   groupChanges / summarizeChanges              : /paths/{p}/{m} 접두로 operation 별 묶음, components.schemas 는 참조 operation 수
 *
 * 화면의 오른쪽 YAML 미리보기용이다. 저장은 서버가 같은 pointer 를 원본에 심어 표면 보호·verify 를 거친 뒤 한다.
 */
import type { SpecChange } from "@/api/types";
import type { Messages } from "@/i18n";

import { isRecord } from "./json";
import { listOperations, METHODS, refName, type HttpMethod, type OpenApiDoc } from "./openapi";

export function parsePointer(pointer: string): string[] {
  if (pointer === "") return [];
  if (!pointer.startsWith("/")) throw new Error(`invalid JSON pointer: ${pointer}`);
  return pointer
    .slice(1)
    .split("/")
    .map((token) => token.replace(/~1/g, "/").replace(/~0/g, "~"));
}

export function escapeToken(token: string): string {
  return token.replace(/~/g, "~0").replace(/\//g, "~1");
}

export function formatPointer(tokens: readonly string[]): string {
  return tokens.map((token) => "/" + escapeToken(token)).join("");
}

/** 부모 pointer. 루트("") 의 부모는 null 이다. */
export function parentPointer(pointer: string): string | null {
  const tokens = parsePointer(pointer);
  if (!tokens.length) return null;
  return formatPointer(tokens.slice(0, -1));
}

function child(cur: unknown, token: string): unknown {
  if (Array.isArray(cur)) return cur[Number(token)];
  if (isRecord(cur)) return cur[token];
  return undefined;
}

function assign(cur: unknown, token: string, value: unknown): void {
  if (Array.isArray(cur)) cur[token === "-" ? cur.length : Number(token)] = value;
  else if (isRecord(cur)) cur[token] = value;
}

function setAt(root: unknown, tokens: string[], value: unknown): void {
  let cur: unknown = root;
  for (let i = 0; i < tokens.length - 1; i += 1) {
    const next = child(cur, tokens[i]);
    if (next !== null && typeof next === "object") {
      cur = next;
      continue;
    }
    // 중간 컨테이너가 없으면 만든다 (예: added /x-s2o/minify 의 /x-s2o). 다음 토큰이 숫자면 배열이다.
    const made: unknown = /^\d+$/.test(tokens[i + 1]) ? [] : {};
    assign(cur, tokens[i], made);
    cur = made;
  }
  assign(cur, tokens[tokens.length - 1], value);
}

function removeAt(root: unknown, tokens: string[]): void {
  let cur: unknown = root;
  for (const token of tokens.slice(0, -1)) {
    cur = child(cur, token);
    if (cur === undefined) return;
  }
  const last = tokens[tokens.length - 1];
  if (Array.isArray(cur)) {
    const i = Number(last);
    if (Number.isInteger(i) && i >= 0 && i < cur.length) cur.splice(i, 1);
  } else if (isRecord(cur)) {
    delete cur[last];
  }
}

/** 같은 배열의 삭제는 큰 인덱스부터 해야 앞 항목의 인덱스가 밀리지 않는다. 토큰 단위로 숫자는 숫자로 비교한다. */
function comparePointerDesc(a: string, b: string): number {
  const ta = parsePointer(a);
  const tb = parsePointer(b);
  for (let i = 0; i < Math.min(ta.length, tb.length); i += 1) {
    if (ta[i] === tb[i]) continue;
    const na = /^\d+$/.test(ta[i]) ? Number(ta[i]) : null;
    const nb = /^\d+$/.test(tb[i]) ? Number(tb[i]) : null;
    if (na !== null && nb !== null) return nb - na;
    return ta[i] < tb[i] ? 1 : -1;
  }
  return tb.length - ta.length;
}

/** 원본은 건드리지 않는다. changed·added 는 after 를 심고, removed 는 지운다. 루트("") 교체는 없다. */
export function applyChanges(doc: OpenApiDoc, changes: readonly SpecChange[]): OpenApiDoc {
  const out = JSON.parse(JSON.stringify(doc)) as OpenApiDoc;
  for (const c of changes) {
    if (c.kind === "removed") continue;
    const tokens = parsePointer(c.pointer);
    if (tokens.length) setAt(out, tokens, c.after);
  }
  const removed = changes.filter((c) => c.kind === "removed").sort((a, b) => comparePointerDesc(a.pointer, b.pointer));
  for (const c of removed) {
    const tokens = parsePointer(c.pointer);
    if (tokens.length) removeAt(out, tokens);
  }
  return out;
}

// -- 묶음 ----------------------------------------------------------------------------------

export type ChangeGroupKind = "operation" | "schema" | "info" | "other";

export interface ChangeGroup {
  /** testid 에 쓰는 안전한 키. operation 은 `op:<method>:<path>`, 스키마는 `schema:<name>`. */
  key: string;
  kind: ChangeGroupKind;
  /** 화면 제목. operation 은 "GET /pets", 스키마는 이름이다. */
  title: string;
  /** operationId, 또는 스키마의 "operation N개가 참조". */
  subtitle: string | null;
  /** 이 묶음의 pointer 접두 토큰 수. 항목 라벨은 그 뒤 토큰이다. */
  prefixLength: number;
  changes: SpecChange[];
}

const SCHEMA_REF_PREFIX = "#/components/schemas/";

function collectSchemaRefs(node: unknown, out: Set<string>): Set<string> {
  if (Array.isArray(node)) {
    for (const item of node) collectSchemaRefs(item, out);
  } else if (isRecord(node)) {
    if (typeof node.$ref === "string" && node.$ref.startsWith(SCHEMA_REF_PREFIX)) out.add(refName(node.$ref));
    for (const v of Object.values(node)) collectSchemaRefs(v, out);
  }
  return out;
}

/**
 * components.schemas 각각을 참조하는 operation 수. 스키마 사이의 `$ref`(allOf·items·properties) 를 따라 닫힌 집합으로 센다.
 * Pet 이 NewPet 을 allOf 로 포함하면 Pet 을 쓰는 operation 은 NewPet 도 참조하는 것이다.
 */
export function schemaOperationRefs(doc: OpenApiDoc): Map<string, number> {
  const schemas = isRecord(doc.components) && isRecord(doc.components.schemas) ? doc.components.schemas : {};
  const direct = new Map<string, Set<string>>();
  for (const [name, schema] of Object.entries(schemas)) direct.set(name, collectSchemaRefs(schema, new Set()));
  const closure = (start: Set<string>): Set<string> => {
    const seen = new Set(start);
    const stack = [...start];
    while (stack.length) {
      const n = stack.pop() as string;
      for (const m of direct.get(n) ?? []) {
        if (!seen.has(m)) {
          seen.add(m);
          stack.push(m);
        }
      }
    }
    return seen;
  };
  const counts = new Map<string, number>();
  for (const name of Object.keys(schemas)) counts.set(name, 0);
  for (const op of listOperations(doc)) {
    const refs = collectSchemaRefs(op.operation, new Set());
    collectSchemaRefs(op.pathItem.parameters, refs);
    for (const s of closure(refs)) counts.set(s, (counts.get(s) ?? 0) + 1);
  }
  return counts;
}

function isMethod(token: string): token is HttpMethod {
  return (METHODS as readonly string[]).includes(token);
}

/** operation 묶음은 문서의 operation 순서, 그다음 스키마·info·기타 순이다. 묶음 안 항목은 받은 순서다. */
export function groupChanges(t: Messages, doc: OpenApiDoc, changes: readonly SpecChange[]): ChangeGroup[] {
  const groups = new Map<string, ChangeGroup>();
  const refs = schemaOperationRefs(doc);
  const ops = listOperations(doc);
  const opIdOf = new Map<string, string | null>(ops.map((o) => [`${o.method}:${o.path}`, o.operationId]));

  for (const c of changes) {
    const tokens = parsePointer(c.pointer);
    let key: string;
    let make: () => ChangeGroup;
    if (tokens[0] === "paths" && tokens.length >= 3 && isMethod(tokens[2])) {
      key = `op:${tokens[2]}:${tokens[1]}`;
      make = () => ({
        key,
        kind: "operation",
        title: `${tokens[2].toUpperCase()} ${tokens[1]}`,
        subtitle: opIdOf.get(`${tokens[2]}:${tokens[1]}`) ?? null,
        prefixLength: 3,
        changes: [],
      });
    } else if (tokens[0] === "components" && tokens[1] === "schemas" && tokens.length >= 3) {
      key = `schema:${tokens[2]}`;
      const n = refs.get(tokens[2]) ?? 0;
      make = () => ({ key, kind: "schema", title: tokens[2], subtitle: t.ui.pointer.schemaRefs(n), prefixLength: 3, changes: [] });
    } else if (tokens[0] === "info") {
      key = "info";
      make = () => ({ key, kind: "info", title: "info", subtitle: null, prefixLength: 1, changes: [] });
    } else {
      key = `other:${tokens[0] ?? ""}`;
      make = () => ({ key, kind: "other", title: tokens[0] ? `/${escapeToken(tokens[0])}` : "/", subtitle: null, prefixLength: tokens.length ? 1 : 0, changes: [] });
    }
    let g = groups.get(key);
    if (!g) {
      g = make();
      groups.set(key, g);
    }
    g.changes.push(c);
  }

  const order: Record<ChangeGroupKind, number> = { operation: 0, schema: 1, info: 2, other: 3 };
  const opOrder = new Map<string, number>(ops.map((o, i) => [`op:${o.method}:${o.path}`, i]));
  return [...groups.values()].sort((a, b) => {
    if (a.kind !== b.kind) return order[a.kind] - order[b.kind];
    if (a.kind === "operation") return (opOrder.get(a.key) ?? 1e9) - (opOrder.get(b.key) ?? 1e9);
    return a.title.localeCompare(b.title);
  });
}

/** 묶음 안에서 보일 항목 라벨: 접두 뒤 토큰을 "/" 로 잇는다 ("description", "parameters/0/description"). */
export function changeLabel(group: ChangeGroup, change: SpecChange): string {
  const rest = parsePointer(change.pointer).slice(group.prefixLength);
  return rest.length ? rest.join("/") : change.pointer || "/";
}

export interface ChangeSummary {
  totalOperations: number;
  changedOperations: number;
}

export function summarizeChanges(doc: OpenApiDoc, groups: readonly ChangeGroup[]): ChangeSummary {
  return {
    totalOperations: listOperations(doc).length,
    changedOperations: groups.filter((g) => g.kind === "operation").length,
  };
}
