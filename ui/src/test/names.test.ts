import { dictionary } from "@/i18n";
import { LOADING_LABEL, displayName, k8sResourceName, nameSearchText, optionLabel, resolveName, secondaryName, slugify, validateDisplayName, validateName } from "@/lib/names";

const ko = dictionary("ko");

describe("names (D24)", () => {
  it.each([
    // 표시 이름이 null 이거나 이름과 같으면 이름 하나만
    [{ name: "calc", display_name: null }, "calc", null, "calc"],
    [{ name: "calc", display_name: "calc" }, "calc", null, "calc"],
    // 다르면 표시 이름이 앞이고, K8s 이름은 보조로 서고 고르기 목록에서는 괄호로 붙는다
    [{ name: "calc", display_name: "계산기" }, "계산기", "calc", "계산기 (calc)"],
  ])("%j shows %s with secondary %s and option label %s", (named, display, secondary, option) => {
    expect(displayName(named)).toBe(display);
    expect(secondaryName(named)).toBe(secondary);
    expect(optionLabel(named)).toBe(option);
  });

  it("nameSearchText searches both names", () => {
    expect(nameSearchText({ name: "calc", display_name: "계산기" })).toBe("계산기 calc");
    expect(nameSearchText({ name: "calc", display_name: null })).toBe("calc");
  });

  it.each([
    // 공백·기호 묶음은 하이픈 하나로 접고 양끝을 정리한다
    ["a  b", "a-b"],
    ["a--b", "a-b"],
    ["  Hello, World! ", "hello-world"],
    ["My_Calc.v2", "my-calc-v2"],
    // NFKD 로 푼 뒤 비 ASCII 를 지우므로 한글만이면 빈 이름이다
    ["계산기 SOAP", "soap"],
    ["계산기", ""],
    ["Café Menü", "cafe-menu"],
    // 32자에서 자르고, 32번째 글자가 하이픈이면 그것도 지운다
    ["abcdefghij-abcdefghij-abcdefghij-abcdefghij", "abcdefghij-abcdefghij-abcdefghij"],
    ["abcdefghij-abcdefghij-abcdefghi-x", "abcdefghij-abcdefghij-abcdefghi"],
  ])("slugify(%j) is %j", (display, name) => {
    expect(slugify(display)).toBe(name);
  });

  it.each(["calculator", "calc-1", "a".repeat(32)])("validateName accepts %s", (name) => {
    expect(validateName(ko, name)).toBeNull();
  });

  // 어긴 규칙마다 사용자가 할 일을 ~해 주세요 로 적는다
  it.each([
    ["", "입력해 주세요"],
    ["a".repeat(33), "32자"],
    ["Calc", "소문자 영문"],
    ["-calc", "양끝"],
    ["a--b", "연속"],
    ["my_calc", "하이픈(-)"],
  ])("validateName rejects %j and names the rule: %s", (name, rule) => {
    expect(validateName(ko, name)).toContain(rule);
  });

  it.each(["", "계산기 SOAP", "x".repeat(80)])("validateDisplayName accepts %j", (display) => {
    expect(validateDisplayName(ko, display)).toBeNull();
  });

  it.each([
    ["   ", "공백"],
    ["x".repeat(81), "80자"],
  ])("validateDisplayName rejects %j and names the rule: %s", (display, rule) => {
    expect(validateDisplayName(ko, display)).toContain(rule);
  });

  it("k8sResourceName joins the prefix and name", () => {
    expect(k8sResourceName("mcp", "calculator")).toBe("mcp-calculator");
  });

  it("resolveName never shows the id: name, deleted, or still loading", () => {
    const list = [{ id: "3f2a9c1e-uuid", name: "calc", display_name: "계산기" }];
    expect(resolveName(ko, list, "3f2a9c1e-uuid")).toBe("계산기");
    // 목록을 받았는데 없으면 지워진 것이고, 아직 못 받았으면 모르는 것이다. 둘을 섞으면 멀쩡한 참조가 지워진 것처럼 보인다
    expect(resolveName(ko, list, "gone-uuid")).toBe(ko.ui.names.deleted);
    expect(resolveName(ko, undefined, "3f2a9c1e-uuid")).toBe(LOADING_LABEL);
  });
});
