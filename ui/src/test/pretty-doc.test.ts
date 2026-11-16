import { describe, expect, it } from "vitest";

import { prettyDoc } from "@/lib/prettyDoc";

describe("prettyDoc", () => {
  const already = '{\n  "a": 1\n}';

  it.each([
    ["한 줄짜리 JSON 을 들여쓴다", '{"openapi":"3.0.0","paths":{"/pets":{"get":{}}}}', '{\n  "openapi": "3.0.0",\n  "paths": {\n    "/pets": {\n      "get": {}\n    }\n  }\n}'],
    [
      "줄바꿈 없는 WSDL 을 태그 깊이로 들여쓴다",
      '<definitions name="Calc"><portType name="p"><operation name="Add"/></portType></definitions>',
      '<definitions name="Calc">\n  <portType name="p">\n    <operation name="Add"/>\n  </portType>\n</definitions>',
    ],
    ["YAML 은 다시 담아 고르게 맞춘다", "openapi: 3.0.0\ninfo:\n      title: 펫스토어\n", "openapi: 3.0.0\ninfo:\n  title: 펫스토어\n"],
    // 호출부가 이 값을 원문과 견주어 체크박스를 세울지 정한다
    ["이미 고르게 들여쓴 문서는 그대로 돌려준다", already, already],
  ])("%s", (_case, text, pretty) => {
    expect(prettyDoc(text)).toBe(pretty);
  });

  // 억지로 고쳐 보이면 사람이 저장된 것이 그런 줄 안다
  it.each([
    "",
    "   ",
    "{ 망가진 json",
    // YAML 파서는 평범한 낱말 하나도 "문자열" 로 받아 준다. 그것까지 통과시키면 아무 글이나 지난다
    "그냥 글입니다",
    "123",
  ])("못 알아보는 글(%j)은 null 이라 호출부가 원문을 그대로 보인다", (text) => {
    expect(prettyDoc(text)).toBeNull();
  });
});
