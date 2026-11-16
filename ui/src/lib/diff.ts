import { isRecord } from "./json";

/** 두 JSON 값의 차이를 경로 목록으로. 리비전 diff 처럼 작은 문서에 쓴다. */
export interface DiffEntry {
  path: string;
  from: unknown;
  to: unknown;
}

export function diffJson(a: unknown, b: unknown, path = ""): DiffEntry[] {
  if (isRecord(a) && isRecord(b)) {
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
    return keys.flatMap((k) => diffJson(a[k], b[k], path ? `${path}.${k}` : k));
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const n = Math.max(a.length, b.length);
    const out: DiffEntry[] = [];
    for (let i = 0; i < n; i++) out.push(...diffJson(a[i], b[i], `${path}[${i}]`));
    return out;
  }
  if (JSON.stringify(a) === JSON.stringify(b)) return [];
  return [{ path: path || "$", from: a, to: b }];
}

export function showValue(v: unknown): string {
  if (v === undefined) return "(none)";
  return typeof v === "string" ? v : JSON.stringify(v);
}
