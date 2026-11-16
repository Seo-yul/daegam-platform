/**
 * OpenAPI 3.x 문서를 화면에 그릴 수 있는 모양으로 푸는 순수 함수들. 외부 뷰어 없이 자체 렌더한다.
 * 문서는 서버가 검증한 것이므로 형태를 믿되, 어긋난 값은 조용히 건너뛴다.
 *
 * FastAPI 가 내는 OpenAPI 3.1 의 꼴도 읽는다. `Optional[X]` 는 `anyOf: [X, {type: null}]` 이라 X 에 " | null" 을
 * 붙인 형으로 보이고 X 의 속성을 그대로 편다. 갈래가 둘 넘는 anyOf·oneOf 는 갈래 형을 " | " 로 잇는다. `const` 는
 * 값 하나인 enum 이고, 형식이 없으면 `contentMediaType`(파일 업로드)을 형식 자리에 둔다.
 *
 *   listOperations / groupByTag : paths 를 오퍼레이션 목록·태그 그룹으로
 *   resolveRef / deref          : `$ref` 를 문서 안에서 푼다 (JSON pointer, 순환은 이름만 남긴다)
 *   flattenSchema               : 스키마를 들여쓰기 행 목록으로 편다 (속성 표)
 *   operationParameters / requestBodyOf / responsesOf / soapContractOf
 */
import { isRecord } from "./json";

export type JsonObject = Record<string, unknown>;
export type OpenApiDoc = JsonObject;

export const METHODS = ["get", "post", "put", "patch", "delete", "head", "options", "trace"] as const;
export type HttpMethod = (typeof METHODS)[number];

export interface OperationEntry {
  /** operationId. 없으면 `method path` 다. 목록 키·선택 값으로 쓴다. */
  id: string;
  operationId: string | null;
  method: HttpMethod;
  path: string;
  summary: string | null;
  description: string | null;
  deprecated: boolean;
  tags: string[];
  operation: JsonObject;
  pathItem: JsonObject;
}

export interface TagGroup {
  tag: string;
  description: string | null;
  operations: OperationEntry[];
}

export interface Resolved {
  value: JsonObject | undefined;
  /** 따라간 `$ref`. 없으면 null. */
  ref: string | null;
  /** `$ref` 의 마지막 조각 (components.schemas.Pet → Pet). */
  name: string | null;
}

export interface SchemaRow {
  name: string;
  depth: number;
  type: string;
  required: boolean;
  description: string | null;
  /** 이 행이 `$ref` 로 온 스키마면 그 이름. */
  ref: string | null;
  /** 조상에서 이미 펼친 `$ref` 가 다시 나온 경우. 자식은 펼치지 않는다. */
  circular: boolean;
  deprecated: boolean;
  enumValues: string[] | null;
}

export interface ParameterRow {
  name: string;
  in: string;
  required: boolean;
  type: string;
  description: string | null;
  deprecated: boolean;
  enumValues: string[] | null;
}

export interface ContentView {
  contentType: string;
  /** 루트 스키마의 형 라벨 (예: Pet, array<Pet>, string). */
  type: string;
  ref: string | null;
  rows: SchemaRow[];
}

export interface RequestBodyView {
  required: boolean;
  description: string | null;
  contents: ContentView[];
}

export interface ResponseView {
  status: string;
  description: string | null;
  contents: ContentView[];
}

export interface SoapContract {
  operation: string | null;
  soapAction: string | null;
  soapVersion: string | null;
  style: string | null;
  endpoint: string | null;
  input: { element: string | null; namespace: string | null } | null;
  output: { element: string | null; namespace: string | null } | null;
}

export interface ServerView {
  url: string;
  description: string | null;
}

export interface InfoView {
  title: string;
  version: string | null;
  description: string | null;
}

const UNTAGGED = "default";
const NULL_SUFFIX = " | null";
const MAX_DEPTH = 8;
const MAX_REF_HOPS = 16;

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

// -- $ref ------------------------------------------------------------------------------------

/** JSON pointer 한 조각을 되돌린다 (~1 → /, ~0 → ~). */
function unescapePointer(token: string): string {
  return decodeURIComponent(token).replace(/~1/g, "/").replace(/~0/g, "~");
}

/** 문서 안 `$ref`("#/components/schemas/Pet") 를 푼다. 바깥 문서·없는 경로는 undefined 다. */
export function resolveRef(doc: OpenApiDoc, ref: string): unknown {
  if (!ref.startsWith("#")) return undefined;
  const pointer = ref.slice(1);
  if (pointer === "") return doc;
  if (!pointer.startsWith("/")) return undefined;
  let cur: unknown = doc;
  for (const token of pointer.slice(1).split("/")) {
    const key = unescapePointer(token);
    if (Array.isArray(cur)) {
      const i = Number(key);
      if (!Number.isInteger(i) || i < 0 || i >= cur.length) return undefined;
      cur = cur[i];
    } else if (isRecord(cur)) {
      if (!(key in cur)) return undefined;
      cur = cur[key];
    } else {
      return undefined;
    }
  }
  return cur;
}

export function refName(ref: string): string {
  const last = ref.split("/").pop() ?? ref;
  return unescapePointer(last);
}

/** 노드가 `$ref` 면 (연쇄해서) 푼 객체를, 아니면 노드 자체를 돌려준다. */
export function deref(doc: OpenApiDoc, node: unknown): Resolved {
  let cur = node;
  let ref: string | null = null;
  for (let hops = 0; hops < MAX_REF_HOPS; hops += 1) {
    if (!isRecord(cur)) return { value: undefined, ref, name: ref ? refName(ref) : null };
    const r = cur.$ref;
    if (typeof r !== "string") return { value: cur, ref, name: ref ? refName(ref) : null };
    ref = r;
    cur = resolveRef(doc, r);
  }
  return { value: undefined, ref, name: ref ? refName(ref) : null };
}

// -- operations ----------------------------------------------------------------------------

export function listOperations(doc: OpenApiDoc): OperationEntry[] {
  const out: OperationEntry[] = [];
  const paths = isRecord(doc.paths) ? doc.paths : {};
  for (const [path, rawItem] of Object.entries(paths)) {
    if (path.startsWith("x-")) continue;
    const pathItem = deref(doc, rawItem).value;
    if (!pathItem) continue;
    for (const method of METHODS) {
      const op = pathItem[method];
      if (!isRecord(op)) continue;
      const operationId = str(op.operationId);
      out.push({
        id: operationId ?? `${method} ${path}`,
        operationId,
        method,
        path,
        summary: str(op.summary),
        description: str(op.description),
        deprecated: op.deprecated === true,
        tags: strList(op.tags),
        operation: op,
        pathItem,
      });
    }
  }
  return out;
}

/** 태그별로 묶는다. 순서는 문서의 tags[] 선언 순, 그다음 등장 순이다. 태그가 없으면 default 그룹이다. */
export function groupByTag(doc: OpenApiDoc): TagGroup[] {
  const declared = new Map<string, string | null>();
  if (Array.isArray(doc.tags)) {
    for (const t of doc.tags) {
      if (isRecord(t) && typeof t.name === "string") declared.set(t.name, str(t.description));
    }
  }
  const groups = new Map<string, TagGroup>();
  for (const [tag, description] of declared) groups.set(tag, { tag, description, operations: [] });
  for (const entry of listOperations(doc)) {
    const tags = entry.tags.length ? entry.tags : [UNTAGGED];
    for (const tag of tags) {
      let g = groups.get(tag);
      if (!g) {
        g = { tag, description: null, operations: [] };
        groups.set(tag, g);
      }
      g.operations.push(entry);
    }
  }
  return [...groups.values()].filter((g) => g.operations.length);
}

export function findOperation(doc: OpenApiDoc, id: string): OperationEntry | undefined {
  return listOperations(doc).find((e) => e.id === id || e.operationId === id);
}

/** 검색: operationId·경로·summary·태그에 부분 일치 (대소문자 무시). 빈 검색은 전부다. */
export function filterGroups(groups: TagGroup[], query: string): TagGroup[] {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  return groups
    .map((g) => ({
      ...g,
      operations: g.operations.filter((op) =>
        [op.id, op.path, op.summary ?? "", op.method, g.tag].some((s) => s.toLowerCase().includes(q)),
      ),
    }))
    .filter((g) => g.operations.length);
}

// -- schema ----------------------------------------------------------------------------------

/**
 * anyOf·oneOf 의 갈래를 null 갈래와 나머지로 나눈다. 둘 다 없으면 null 이다. FastAPI 는 `Optional[X]` 를
 * `anyOf: [X, {type: null}]` 로 적는다.
 */
function unionOf(schema: JsonObject): { branches: unknown[]; nullable: boolean } | null {
  const raw = Array.isArray(schema.anyOf) ? schema.anyOf : Array.isArray(schema.oneOf) ? schema.oneOf : null;
  if (!raw) return null;
  const branches = raw.filter((b) => !(isRecord(b) && b.type === "null"));
  return { branches, nullable: branches.length < raw.length };
}

/**
 * 갈래가 하나뿐인 union(`Optional[X]`)이면 X 를 푼 것이다. 속성을 함께 가진 스키마나 갈래가 여럿인 union 은
 * null 이다. 형 라벨·행 펼치기·enum 이 X 를 본다.
 */
function optionalInner(doc: OpenApiDoc, schema: JsonObject): Resolved | null {
  if (isRecord(schema.properties)) return null;
  const union = unionOf(schema);
  if (!union || union.branches.length !== 1) return null;
  return deref(doc, union.branches[0]);
}

function typeOf(schema: JsonObject): { type: string | null; nullable: boolean } {
  let nullable = schema.nullable === true;
  let type: string | null = null;
  if (typeof schema.type === "string") type = schema.type;
  else if (Array.isArray(schema.type)) {
    // 3.1: type: [string, "null"]
    const types = strList(schema.type);
    nullable = nullable || types.includes("null");
    type = types.filter((t) => t !== "null").join(" | ") || null;
  }
  return { type, nullable };
}

/** allOf 를 한 객체로 합친다 (properties·required 를 모은다). 다른 키는 첫 부분이 이긴다. */
function mergeAllOf(doc: OpenApiDoc, schema: JsonObject, stack: ReadonlySet<string>): JsonObject {
  if (!Array.isArray(schema.allOf)) return schema;
  const properties: JsonObject = {};
  const required = new Set<string>(strList(schema.required));
  const merged: JsonObject = { ...schema };
  delete merged.allOf;
  for (const part of schema.allOf) {
    const r = deref(doc, part);
    if (!r.value || (r.ref && stack.has(r.ref))) continue;
    const inner = mergeAllOf(doc, r.value, r.ref ? new Set([...stack, r.ref]) : stack);
    if (isRecord(inner.properties)) Object.assign(properties, inner.properties);
    for (const name of strList(inner.required)) required.add(name);
    for (const [k, v] of Object.entries(inner)) {
      if (k === "properties" || k === "required" || k in merged) continue;
      merged[k] = v;
    }
  }
  if (isRecord(schema.properties)) Object.assign(properties, schema.properties);
  if (Object.keys(properties).length) merged.properties = properties;
  if (required.size) merged.required = [...required];
  if (!merged.type && merged.properties) merged.type = "object";
  return merged;
}

/** 스키마의 형을 한 단어로. `$ref` 객체는 이름(Pet), 배열은 array<Pet>, 형식은 string(date-time). */
export function schemaTypeLabel(doc: OpenApiDoc, node: unknown, depth = 0): string {
  const r = deref(doc, node);
  const schema = r.value;
  if (!schema) return r.name ?? "unknown";
  const { type, nullable } = typeOf(schema);
  const suffix = nullable ? NULL_SUFFIX : "";
  if (r.name && (type === "object" || type === null) && (isRecord(schema.properties) || Array.isArray(schema.allOf))) {
    return r.name + suffix;
  }
  if (Array.isArray(schema.allOf)) return (r.name ?? "object") + suffix;
  const union = unionOf(schema);
  if (union) {
    // 이름이 있는 union 스키마는 이름으로 보인다. 갈래는 속성 표의 행이 보인다
    if (r.name) return r.name + (union.nullable ? NULL_SUFFIX : suffix);
    // 갈래가 이미 " | null" 이면(자기를 가리키는 Optional 등) null 을 한 번만 붙인다
    let anyNull = union.nullable || nullable;
    const labels = (depth < 3 ? union.branches.map((b) => schemaTypeLabel(doc, b, depth + 1)) : ["…"]).map((label) => {
      if (!label.endsWith(NULL_SUFFIX)) return label;
      anyNull = true;
      return label.slice(0, -NULL_SUFFIX.length);
    });
    const unique = [...new Set(labels)];
    return (unique.length ? unique.join(" | ") : "any") + (anyNull ? NULL_SUFFIX : "");
  }
  if (type === "array") {
    const items = depth < 3 ? schemaTypeLabel(doc, schema.items, depth + 1) : "…";
    return `array<${items}>` + suffix;
  }
  if (type === null) return (isRecord(schema.properties) ? "object" : r.name ?? "any") + suffix;
  // OpenAPI 3.1 은 파일 업로드를 format: binary 대신 contentMediaType 으로 적는다
  const format = str(schema.format) ?? str(schema.contentMediaType);
  const base = format ? `${type}(${format})` : type;
  if (r.name && type !== "object") return `${base} (${r.name})` + suffix;
  return base + suffix;
}

function enumValue(v: unknown): string {
  return v === null ? "null" : typeof v === "string" ? v : JSON.stringify(v);
}

/**
 * enum 의 값들. `const` 는 값 하나인 enum 이다. `Optional[Literal[...]]` 이면 X 의 값이다. X 가 다시 자기를 가리키는
 * `Optional` 이면 끝나지 않으므로 `$ref` 를 따라가는 것과 같은 수(MAX_REF_HOPS)에서 멈춘다.
 */
function enumOf(doc: OpenApiDoc, schema: JsonObject, hops = 0): string[] | null {
  if (Array.isArray(schema.enum)) return schema.enum.map(enumValue);
  if ("const" in schema) return [enumValue(schema.const)];
  if (hops >= MAX_REF_HOPS) return null;
  const inner = optionalInner(doc, schema)?.value;
  return inner ? enumOf(doc, inner, hops + 1) : null;
}

/**
 * 스키마를 속성 행으로 편다. 루트 자체는 행이 아니고(라벨은 schemaTypeLabel 로), 루트가 객체면
 * 그 속성들이 depth 0 이다. 배열이면 items 의 속성을 바로 편다. `$ref` 는 풀고, 조상에 있던
 * `$ref` 가 다시 나오면 circular 행으로 남긴다. `Optional[X]` 는 X 의 속성을 같은 깊이에 펴고, 갈래가 여럿인
 * oneOf/anyOf 는 null 이 아닌 갈래마다 한 행씩이다.
 */
export function flattenSchema(doc: OpenApiDoc, root: unknown, maxDepth = MAX_DEPTH): SchemaRow[] {
  const out: SchemaRow[] = [];

  function children(schema: JsonObject, depth: number, stack: ReadonlySet<string>): void {
    if (depth > maxDepth) return;
    const merged = mergeAllOf(doc, schema, stack);
    const inner = optionalInner(doc, merged);
    if (inner) {
      if (!inner.value || (inner.ref && stack.has(inner.ref))) return;
      children(inner.value, depth, inner.ref ? new Set([...stack, inner.ref]) : stack);
      return;
    }
    const { type } = typeOf(merged);
    if (type === "array" || (type === null && merged.items !== undefined && !merged.properties)) {
      const items = deref(doc, merged.items);
      if (!items.value || (items.ref && stack.has(items.ref))) return;
      children(items.value, depth, items.ref ? new Set([...stack, items.ref]) : stack);
      return;
    }
    const required = new Set(strList(merged.required));
    if (isRecord(merged.properties)) {
      for (const [name, prop] of Object.entries(merged.properties)) {
        node(name, prop, depth, required.has(name), stack);
      }
    }
    if (isRecord(merged.additionalProperties)) {
      // propertyNames 의 enum 은 쓸 수 있는 키다. 값에 enum 이 없을 때 {key} 행에 보인다
      const names = deref(doc, merged.propertyNames).value;
      const keys = names ? enumOf(doc, names) : null;
      node("{key}", merged.additionalProperties, depth, false, stack, keys);
    }
    unionOf(merged)?.branches.forEach((v, i) => {
      const r = deref(doc, v);
      node(r.name ?? `option ${i + 1}`, v, depth, false, stack);
    });
  }

  function node(
    name: string,
    raw: unknown,
    depth: number,
    required: boolean,
    stack: ReadonlySet<string>,
    keyEnum: string[] | null = null,
  ): void {
    const r = deref(doc, raw);
    if (!r.value) {
      out.push(row(name, depth, r.name ?? "unknown", required, null, r.name, false, false, null));
      return;
    }
    const schema = r.value;
    // `Optional[X]` 의 X, 배열이면 items 의 `$ref` 가 조상에 있어도 순환이다 (Owner.pets: array<Pet> 안의 Pet)
    const inner = optionalInner(doc, schema);
    const target = inner?.value ?? schema;
    const itemsRef = typeOf(target).type === "array" ? deref(doc, target.items).ref : null;
    const circular = [r.ref, inner?.ref ?? null, itemsRef].some((ref) => ref !== null && stack.has(ref));
    out.push(
      row(
        name,
        depth,
        schemaTypeLabel(doc, raw),
        required,
        str(schema.description) ?? str(target.description),
        r.name ?? inner?.name ?? null,
        circular,
        schema.deprecated === true,
        enumOf(doc, schema) ?? keyEnum,
      ),
    );
    if (circular) return;
    children(schema, depth + 1, r.ref ? new Set([...stack, r.ref]) : stack);
  }

  const rootResolved = deref(doc, root);
  if (!rootResolved.value) return out;
  children(rootResolved.value, 0, rootResolved.ref ? new Set([rootResolved.ref]) : new Set());
  return out;
}

function row(
  name: string,
  depth: number,
  type: string,
  required: boolean,
  description: string | null,
  ref: string | null,
  circular: boolean,
  deprecated: boolean,
  enumValues: string[] | null,
): SchemaRow {
  return { name, depth, type, required, description, ref, circular, deprecated, enumValues };
}

// -- parameters / body / responses ---------------------------------------------------------

function parameterSchema(p: JsonObject): unknown {
  if (p.schema !== undefined) return p.schema;
  if (isRecord(p.content)) {
    const first = Object.values(p.content)[0];
    if (isRecord(first)) return first.schema;
  }
  return undefined;
}

/** 경로 수준 파라미터에 오퍼레이션 파라미터를 덮는다 (같은 name+in 이면 오퍼레이션 쪽). */
export function operationParameters(doc: OpenApiDoc, entry: OperationEntry): ParameterRow[] {
  const byKey = new Map<string, ParameterRow>();
  for (const list of [entry.pathItem.parameters, entry.operation.parameters]) {
    if (!Array.isArray(list)) continue;
    for (const raw of list) {
      const p = deref(doc, raw).value;
      if (!p || typeof p.name !== "string" || typeof p.in !== "string") continue;
      const outer = deref(doc, parameterSchema(p)).value;
      const inner = outer ? optionalInner(doc, outer)?.value : undefined;
      byKey.set(`${p.in}:${p.name}`, {
        name: p.name,
        in: p.in,
        required: p.in === "path" || p.required === true,
        type: parameterSchema(p) === undefined ? "string" : schemaTypeLabel(doc, parameterSchema(p)),
        description: str(p.description) ?? str(outer?.description) ?? str(inner?.description),
        deprecated: p.deprecated === true,
        enumValues: outer ? enumOf(doc, outer) : null,
      });
    }
  }
  return [...byKey.values()];
}

function contentViews(doc: OpenApiDoc, content: unknown): ContentView[] {
  if (!isRecord(content)) return [];
  return Object.entries(content).flatMap(([contentType, media]) => {
    if (!isRecord(media)) return [];
    const r = deref(doc, media.schema);
    return [
      {
        contentType,
        type: media.schema === undefined ? "-" : schemaTypeLabel(doc, media.schema),
        ref: r.name,
        rows: media.schema === undefined ? [] : flattenSchema(doc, media.schema),
      },
    ];
  });
}

export function requestBodyOf(doc: OpenApiDoc, entry: OperationEntry): RequestBodyView | null {
  const body = deref(doc, entry.operation.requestBody).value;
  if (!body) return null;
  return {
    required: body.required === true,
    description: str(body.description),
    contents: contentViews(doc, body.content),
  };
}

export function responsesOf(doc: OpenApiDoc, entry: OperationEntry): ResponseView[] {
  const responses = entry.operation.responses;
  if (!isRecord(responses)) return [];
  return Object.entries(responses).flatMap(([status, raw]) => {
    const r = deref(doc, raw).value;
    if (!r) return [];
    return [{ status, description: str(r.description), contents: contentViews(doc, r.content) }];
  });
}

function element(v: unknown): { element: string | null; namespace: string | null } | null {
  if (!isRecord(v)) return null;
  return { element: str(v.element), namespace: str(v.namespace) };
}

/** spec2openapi 가 WSDL 에서 만든 `x-soap` 계약. 오퍼레이션에 없으면 path item 것을 본다. */
export function soapContractOf(entry: OperationEntry): SoapContract | null {
  const raw = isRecord(entry.operation["x-soap"]) ? entry.operation["x-soap"] : entry.pathItem["x-soap"];
  if (!isRecord(raw)) return null;
  return {
    operation: str(raw.operation),
    soapAction: str(raw.soapAction),
    soapVersion: typeof raw.soapVersion === "number" ? String(raw.soapVersion) : str(raw.soapVersion),
    style: str(raw.style),
    endpoint: str(raw.endpoint),
    input: element(raw.input),
    output: element(raw.output),
  };
}

export function serversOf(doc: OpenApiDoc): ServerView[] {
  if (!Array.isArray(doc.servers)) return [];
  return doc.servers.flatMap((s) => (isRecord(s) && typeof s.url === "string" ? [{ url: s.url, description: str(s.description) }] : []));
}

export function infoOf(doc: OpenApiDoc): InfoView {
  const info = isRecord(doc.info) ? doc.info : {};
  return {
    title: str(info.title) ?? "Untitled API",
    version: typeof info.version === "number" ? String(info.version) : str(info.version),
    description: str(info.description),
  };
}

/**
 * 설명 텍스트를 아주 단순한 마크다운으로 토큰화한다: 빈 줄로 문단, 줄바꿈, `code`, [text](url), 맨 URL.
 * 렌더링은 컴포넌트가 한다. 이 이상의 마크다운(제목·표·목록)은 글자 그대로 보인다.
 */
export type TextToken =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "link"; text: string; href: string }
  | { kind: "break" };

export function tokenizeText(text: string): TextToken[][] {
  const paragraphs = text.replace(/\r\n/g, "\n").split(/\n{2,}/);
  return paragraphs.map((para) => {
    const tokens: TextToken[] = [];
    para.split("\n").forEach((line, i) => {
      if (i > 0) tokens.push({ kind: "break" });
      tokens.push(...inlineTokens(line));
    });
    return tokens;
  });
}

const INLINE = /(`[^`]+`)|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]+)/g;

function inlineTokens(line: string): TextToken[] {
  const out: TextToken[] = [];
  let last = 0;
  for (const m of line.matchAll(INLINE)) {
    const start = m.index ?? 0;
    if (start > last) out.push({ kind: "text", text: line.slice(last, start) });
    if (m[1]) out.push({ kind: "code", text: m[1].slice(1, -1) });
    else if (m[2] && m[3]) out.push({ kind: "link", text: m[2], href: m[3] });
    else if (m[4]) out.push({ kind: "link", text: m[4], href: m[4] });
    last = start + m[0].length;
  }
  if (last < line.length) out.push({ kind: "text", text: line.slice(last) });
  return out;
}
