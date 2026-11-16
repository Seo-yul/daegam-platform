import { describe, expect, it } from "vitest";

import { conditionArguments, draftOf, methodReadOnly, opening, parseValue, ruleOf, ruledCount } from "@/lib/toolRules";

describe("Tool 규칙의 꼴 (P52 1·4절)", () => {
  it("조건의 값을 인자의 형식으로 바꾸고 읽지 못한 숫자는 글 그대로 둔다", () => {
    expect(parseValue("10", "equals", "integer")).toBe(10);
    expect(parseValue("ten", "max", "integer")).toBe("ten");
    expect(parseValue("true", "equals", "boolean")).toBe(true);
    expect(parseValue("KRW, USD ,", "one_of", "string")).toEqual(["KRW", "USD"]);
    expect(parseValue("1, 2", "one_of", "integer")).toEqual([1, 2]);
    expect(parseValue("32", "max_length", "string")).toBe(32);
    expect(parseValue("ord-", "prefix", "string")).toBe("ord-");
  });

  it("고치지 않은 조건의 값은 저장된 그대로 보낸다", () => {
    const stored = { conditions: [{ path: "n", op: "max" as const, value: 100, optional: false }] };
    // 인자 목록이 없어 형식을 몰라도 저장된 숫자가 글로 바뀌지 않는다
    expect(ruleOf("server", draftOf(stored), null, false)?.conditions).toEqual(stored.conditions);
    const edited = draftOf(stored);
    edited.conditions[0].text = "50";
    expect(ruleOf("server", edited, { n: "integer" }, false)?.conditions?.[0].value).toBe(50);
  });

  it("출처가 받지 않는 칸은 넣지 않고 아무것도 정하지 않은 규칙은 null 이다", () => {
    const draft = { ...draftOf(undefined), roles: ["tenant-admin" as const, "viewer" as const], groups: "ops, ops", agents: ["b", "a"], readOnly: true };
    expect(ruleOf("item", draft, null, false)).toEqual({ callers: { roles: ["viewer", "tenant-admin"] }, read_only: true });
    expect(ruleOf("gateway", draft, null, false)).toEqual({ callers: { roles: ["viewer", "tenant-admin"], groups: ["ops"], agents: ["a", "b"] } });
    // 기본값과 같은 표시는 저장하지 않는다
    expect(ruleOf("server", { ...draftOf(undefined), readOnly: true }, null, true)).toBeNull();
  });

  it("대상을 모두 지운 규칙은 열린다고 말하고 표시만 둔 규칙은 세지 않는다", () => {
    const stored = { callers: { roles: ["viewer" as const] } };
    expect(opening(stored, draftOf(undefined))).toBe(true);
    expect(opening(stored, draftOf(stored))).toBe(false);
    expect(ruledCount({ a: stored, b: { read_only: true }, c: { conditions: [{ path: "x", op: "min", value: 1, optional: false }] } })).toBe(2);
  });

  it("조건은 형식을 아는 스칼라 인자에만 두고 GET·HEAD 는 읽기다", () => {
    expect(conditionArguments({ a: "string", b: "array", c: "object", d: "unknown", e: "number" })).toEqual([
      ["a", "string"],
      ["e", "number"],
    ]);
    expect(methodReadOnly("get") && methodReadOnly("HEAD") && !methodReadOnly("POST") && !methodReadOnly(null)).toBe(true);
  });
});
