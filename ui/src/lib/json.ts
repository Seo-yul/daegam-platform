/** JSON 값을 다루는 작은 공용 함수. 여러 lib·화면이 같은 규칙을 제각기 적지 않게 여기 한 벌만 둔다. */

/** 배열이 아닌 객체(JSON 의 mapping)인가. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 유한한 수면 그 수, 아니면 null. 서버가 자유 객체로 건네는 칸을 좁힐 때 쓴다. */
export function finiteNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** 키 순서와 상관없이 같은 값이면 같은 문자열. 배열 안 객체의 키도 정렬하고, 배열 순서는 그대로 둔다. */
export function canonicalJson(value: unknown): string {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (!isRecord(v)) return v;
    return Object.fromEntries(
      Object.keys(v)
        .sort()
        .map((k) => [k, sort(v[k])]),
    );
  };
  return JSON.stringify(sort(value ?? null));
}

/** JSON textarea 값을 파싱한다. 비어 있으면 null. */
export function parseJsonObject(text: string): { value: Record<string, unknown> | null; error: string | null } {
  if (!text.trim()) return { value: null, error: null };
  try {
    const v: unknown = JSON.parse(text);
    if (!isRecord(v)) return { value: null, error: "must be a JSON object" };
    return { value: v, error: null };
  } catch (e) {
    return { value: null, error: `invalid JSON: ${(e as Error).message}` };
  }
}
