import { stringify } from "yaml";

/** CR 미리보기·manifest 표시용. 키 순서를 유지하고 긴 문자열은 접지 않는다. */
export function toYaml(value: unknown): string {
  if (value === null || value === undefined) return "";
  return stringify(value, { lineWidth: 0, sortMapEntries: false });
}
