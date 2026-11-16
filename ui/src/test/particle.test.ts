import { dictionary } from "@/i18n";
import { type ParticlePair, hasFinalConsonant, particle } from "@/lib/particle";

/** 받침에 따라 조사를 고른다. 화면에 "을(를)" 이 찍히지 않게 한다. */

const CASES: [string, ParticlePair, string][] = [
  // 한글은 끝 음절의 받침으로 정한다
  ["공휴일 도구", "을/를", "를"],
  ["업무 도우미 게이트웨이", "이/가", "가"],
  ["사내 규정", "을/를", "을"],
  // 숫자와 영문은 읽는 소리로 정한다
  ["팀 Redis", "은/는", "는"],
  ["r1", "이/가", "이"],
  ["r2", "이/가", "가"],
  ["r10", "이/가", "이"],
  ["r11", "을/를", "을"],
  ["r100", "을/를", "을"],
  ["holidays-mcp", "을/를", "를"],
  ["HTML", "을/를", "을"],
  ["Agent", "과/와", "와"],
  ["Database", "과/와", "와"],
  // 끝의 영문 낱말이 소문자 er·or 로 끝나면 단어로 읽는다(서버, 클러스터, 에디터). 대문자 약어는 글자 이름이다
  ["MCP Server", "이/가", "가"],
  ["Cluster", "은/는", "는"],
  ["Monaco Editor", "을/를", "를"],
  ["MCP Server", "으로/로", "로"],
  ["ECR", "이/가", "이"],
];

test.each(CASES)("%s + %s -> %s", (word, pair, expected) => {
  expect(particle(word, pair)).toBe(expected);
});

test.each([
  // 끝의 따옴표와 괄호는 건너뛴다
  ["'사내 규정'", true],
  ["(도구)", false],
  // 판단할 글자가 없으면 받침이 없다
  ["", false],
])("hasFinalConsonant(%j) is %s", (word, expected) => {
  expect(hasFinalConsonant(word)).toBe(expected);
});

describe("리비전 번호 뒤 조사", () => {
  const ko = dictionary("ko");
  it("번호의 끝소리를 따라 붙고 앞을 띄우지 않는다", () => {
    expect([1, 2, 3].map((n) => ko.agents.canvasPage.loadRevision(n).split(" ")[4])).toEqual(["r1을", "r2를", "r3을"]);
    expect([1, 2, 3].map((n) => ko.gateways.form.revisionNotice(n))).toEqual([
      "저장하면 리비전 r1이 만들어집니다.",
      "저장하면 리비전 r2가 만들어집니다.",
      "저장하면 리비전 r3이 만들어집니다.",
    ]);
    expect(ko.detail.cloneDialog.body("이 Gateway 의", 2, "Gateway")).toContain("r2를 복사하여 새 Gateway");
  });
});

describe("으로/로", () => {
  it("ㄹ 받침과 받침 없는 끝소리는 로, 다른 받침은 으로다", () => {
    expect(["r1", "r2", "r3", "r7", "r10"].map((w) => `${w}${particle(w, "으로/로")}`)).toEqual(["r1로", "r2로", "r3으로", "r7로", "r10으로"]);
    expect(["서울", "학교", "집"].map((w) => particle(w, "으로/로"))).toEqual(["로", "로", "으로"]);
  });

  it("리비전 번호 뒤 로 를 번호에 맞추고 띄우지 않는다", () => {
    const ko = dictionary("ko");
    expect([1, 2, 3].map((n) => ko.gateways.deployment.emptyBody(n))).toEqual([
      "클러스터와 네임스페이스, 외부 노출을 정하면 정의 r1로 Gateway 를 올립니다.",
      "클러스터와 네임스페이스, 외부 노출을 정하면 정의 r2로 Gateway 를 올립니다.",
      "클러스터와 네임스페이스, 외부 노출을 정하면 정의 r3으로 Gateway 를 올립니다.",
    ]);
  });
});
