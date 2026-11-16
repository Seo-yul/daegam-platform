/**
 * 오퍼레이터(ToolHive)가 올린 CR condition 을 화면 색으로 옮긴다.
 *
 * 쿠버네티스 condition 에는 극성이 있다. `Ready` 처럼 True 가 좋은 것이 있고,
 * `StdioReplicaCapped`·`SessionStorageWarning` 처럼 **True 가 문제**인 것이 있다. 그래서 False 를
 * 일률적으로 빨갛게 칠하면 멀쩡한 인스턴스가 "제한 없음"·"경고 없음" 을 빨간 실패로 보이게 된다.
 * 실제로 Ready 인 MCP Server 가 빨간 False 두 개를 달고 있었다.
 *
 * 우리가 뜻을 확실히 아는 것은 `Ready` 뿐이다. 그것만 극성을 적용하고 나머지는 중립으로 둔다.
 * 전체 판정은 이미 phase 배지와 message 가 말한다.
 */
export type ConditionTone = "ok" | "error" | "muted";

/** True 가 좋은 뜻인 condition. 여기 없는 것은 뜻을 모르므로 중립으로 그린다. */
const POSITIVE_TYPES = new Set(["Ready"]);

export function conditionTone(type: string | null | undefined, status: string | null | undefined): ConditionTone {
  if (!type || !POSITIVE_TYPES.has(type)) return status === "True" ? "ok" : "muted";
  if (status === "True") return "ok";
  if (status === "False") return "error";
  return "muted";
}
