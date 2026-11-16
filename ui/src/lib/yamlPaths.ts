/**
 * 플랫폼 경로 문자열(platform/daegam/k8s/merge.py format_path) ↔ YAML 줄 번호.
 * 예: "spec.podTemplateSpec.spec.containers[name=mcp].resources.limits.cpu",
 *     "metadata.labels[daegam.devops.ai.kr/*]", "spec.env[name=SPEC2OPENAPI_*]"
 * 세그먼트는 fnmatch 식(`*`)이고 `key=value` 는 키 리스트 셀렉터, `*` 는 모든 항목이다.
 * JSON pointer(RFC 6901, linesForPointers) 는 토큰을 글자 그대로 맞추는 `exact` 세그먼트로 같은 해석기를 탄다.
 * OpenAPI 경로 키("/pets/{petId}", "application/vnd.api+json") 에는 점이 들어갈 수 있어 점 경로로는 못 적는다.
 * 최선 노력이다. 못 찾으면 빈 배열을 돌려주고 호출자는 목록만 보여 준다.
 */
import { isMap, isPair, isScalar, isSeq, LineCounter, parseDocument, type Node, type Pair } from "yaml";

import { parsePointer } from "./pointer";

export type Segment =
  | { kind: "key"; pattern: string }
  | { kind: "selector"; key: string; pattern: string }
  | { kind: "index"; index: number }
  | { kind: "any" }
  | { kind: "exact"; token: string };

export function parsePath(path: string): Segment[] {
  const segs: Segment[] = [];
  let buf = "";
  let i = 0;
  const flush = () => {
    if (buf) segs.push(toSegment(buf));
    buf = "";
  };
  while (i < path.length) {
    const ch = path[i];
    if (ch === ".") {
      flush();
      i++;
    } else if (ch === "[") {
      flush();
      const end = path.indexOf("]", i);
      const inner = end < 0 ? path.slice(i + 1) : path.slice(i + 1, end);
      segs.push(toSegment(inner));
      i = end < 0 ? path.length : end + 1;
    } else {
      buf += ch;
      i++;
    }
  }
  flush();
  return segs;
}

function toSegment(raw: string): Segment {
  if (raw === "*") return { kind: "any" };
  if (/^\d+$/.test(raw)) return { kind: "index", index: Number(raw) };
  const eq = raw.indexOf("=");
  if (eq > 0) return { kind: "selector", key: raw.slice(0, eq), pattern: raw.slice(eq + 1) };
  return { kind: "key", pattern: raw };
}

function globToRegex(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\?]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${esc}$`);
}

function matches(pattern: string, value: unknown): boolean {
  if (value === null || value === undefined) return false;
  return globToRegex(pattern).test(String(value));
}

function scalarValue(node: unknown): unknown {
  return isScalar(node) ? node.value : undefined;
}

/** 각 세그먼트를 따라 내려가며 마지막에 닿은 노드(또는 pair)의 시작 오프셋을 모은다. */
function resolve(node: Node | null | undefined, segs: Segment[], depth: number, out: number[]): void {
  if (!node) return;
  if (depth === segs.length) {
    if (node.range) out.push(node.range[0]);
    return;
  }
  const seg = segs[depth];
  if (isMap(node)) {
    if (seg.kind !== "key" && seg.kind !== "exact") return;
    for (const pair of node.items as Pair[]) {
      const key = scalarValue(pair.key);
      const hit = seg.kind === "exact" ? key !== null && key !== undefined && String(key) === seg.token : matches(seg.pattern, key);
      if (!hit) continue;
      if (depth === segs.length - 1) {
        const keyNode = pair.key as Node;
        if (keyNode?.range) out.push(keyNode.range[0]);
      } else {
        resolve(pair.value as Node, segs, depth + 1, out);
      }
    }
    return;
  }
  if (isSeq(node)) {
    const items = node.items as Node[];
    let selected: Node[] = [];
    if (seg.kind === "any") selected = items;
    else if (seg.kind === "index") selected = items[seg.index] ? [items[seg.index]] : [];
    else if (seg.kind === "exact") selected = /^\d+$/.test(seg.token) && items[Number(seg.token)] ? [items[Number(seg.token)]] : [];
    else if (seg.kind === "selector") {
      selected = items.filter((it) => isMap(it) && matches(seg.pattern, scalarValue(it.get(seg.key, true))));
    } else return;
    for (const it of selected) {
      if (depth === segs.length - 1) {
        if (it.range) out.push(it.range[0]);
      } else resolve(it, segs, depth + 1, out);
    }
    return;
  }
  if (isPair(node)) resolve(node.value as Node, segs, depth, out);
}

export interface PathLines {
  path: string;
  lines: number[]; // 1-based
}

function linesFor(yamlText: string, items: { path: string; segs: () => Segment[] }[]): PathLines[] {
  // 찾을 경로가 없으면 파싱할 까닭이 없다. 편집기는 오류가 없을 때도 키 입력마다 이것을 부른다
  if (!items.length) return [];
  let doc: ReturnType<typeof parseDocument> | null = null;
  const lc = new LineCounter();
  try {
    doc = parseDocument(yamlText, { lineCounter: lc, keepSourceTokens: true });
  } catch {
    return items.map(({ path }) => ({ path, lines: [] }));
  }
  const root = doc.contents as Node | null;
  return items.map(({ path, segs }) => {
    const offsets: number[] = [];
    try {
      resolve(root, segs(), 0, offsets);
    } catch {
      /* 최선 노력 */
    }
    const lines = [...new Set(offsets.map((o) => lc.linePos(o).line))].sort((a, b) => a - b);
    return { path, lines };
  });
}

/** YAML 본문에서 경로들이 가리키는 줄 번호. 파싱 실패나 미발견은 lines: []. */
export function linesForPaths(yamlText: string, paths: string[]): PathLines[] {
  return linesFor(
    yamlText,
    paths.map((path) => ({ path, segs: () => parsePath(path) })),
  );
}

/** JSON pointer(RFC 6901) 가 가리키는 줄 번호. 토큰은 글자 그대로 맞추고 시퀀스에서는 숫자 토큰이 인덱스다. */
export function linesForPointers(yamlText: string, pointers: string[]): PathLines[] {
  return linesFor(
    yamlText,
    pointers.map((path) => ({ path, segs: () => parsePointer(path).map((token) => ({ kind: "exact", token }) as Segment) })),
  );
}
