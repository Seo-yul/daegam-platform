import { conditionTone } from "@/lib/conditions";

/**
 * CR condition 의 색. Ready 인 멀쩡한 인스턴스가 빨간 False 두 개를 달면 안 된다.
 * StdioReplicaCapped=False 는 "제한 없음", SessionStorageWarning=False 는 "경고 없음" 이라는 좋은 뜻이다.
 */
describe("conditionTone", () => {
  it.each([
    // 경고 꼴의 condition 은 False 여도 중립이다
    ["StdioReplicaCapped", "False", "muted"],
    ["SessionStorageWarning", "False", "muted"],
    ["RateLimitConfigValid", "False", "muted"],
    // Ready 는 극성을 알므로 실패한 Ready 는 여전히 빨갛다
    ["Ready", "False", "error"],
    ["Ready", "True", "ok"],
    // True 는 어느 종류든 좋은 뜻이고 모르는 상태는 중립이다
    ["PodTemplateValid", "True", "ok"],
    ["PodTemplateValid", "Unknown", "muted"],
    [null, "True", "ok"],
  ])("%s=%s is %s", (type, status, tone) => {
    expect(conditionTone(type, status)).toBe(tone);
  });
});
