/**
 * 화면 문구 사전 (P49 9절). 두 언어 사전의 모양과 함수, 영어 값에 남은 한글, 아직 사전으로 옮기지 않은 화면 파일을 본다.
 * 키가 한쪽에만 있거나 값의 종류가 다르면 tsc 가 먼저 막는다. 여기서는 tsc 가 못 보는 것(매개변수 수, 빠뜨린 인자,
 * 영어 값의 한글, 옮기지 않은 파일)을 본다.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { Fragment, createElement, isValidElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { dictionary } from "@/i18n";
import { deleteTitle } from "@/lib/deletion";

import { HANGUL, hangulFragments, isKoreanDictionary, isLatinData, isScreenSource, latinFragments } from "./hangul";
import type { SampleFile } from "./i18n-samples/types";

/** 영역별 견본 파일(i18n-samples/*.ts)을 하나로 모은다. 단위마다 자기 영역 파일만 고친다. */
const SAMPLE_MODULES = Object.values(import.meta.glob<Partial<SampleFile>>("./i18n-samples/*.ts", { eager: true }));
const SAMPLES: SampleFile["samples"] = Object.assign({}, ...SAMPLE_MODULES.map((module) => module.samples ?? {}));
const SELECTORS: NonNullable<SampleFile["selectors"]> = Object.assign({}, ...SAMPLE_MODULES.map((module) => module.selectors ?? {}));

/**
 * 사전 밖에서 한글이 남아도 되는 화면 파일(i18n-pending/*.ts). P49 가 모든 화면 문구를 옮긴 뒤에는 늘 남는 목록
 * (permanent.ts)만 있다. 화면 문구를 사전 밖에 새로 적으면 아래 검사가 실패한다.
 */
const PENDING: readonly string[] = Object.values(import.meta.glob<readonly string[] | undefined>("./i18n-pending/*.ts", { eager: true, import: "pending" }))
  .flat()
  .filter((path): path is string => typeof path === "string");

interface Leaf {
  path: string;
  ko: unknown;
  en: unknown;
}

function isBranch(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !isValidElement(value);
}

/** 두 사전을 나란히 걸어 끝 값(문자열, 함수)을 경로와 함께 모은다. */
function leaves(ko: unknown, en: unknown, path = ""): Leaf[] {
  if (!isBranch(ko) || !isBranch(en)) return [{ path, ko, en }];
  const keys = new Set([...Object.keys(ko), ...Object.keys(en)]);
  return [...keys].flatMap((key) => leaves(ko[key], en[key], path ? `${path}.${key}` : key));
}

function markup(value: unknown): string {
  return typeof value === "string" ? value : renderToStaticMarkup(createElement(Fragment, null, value as never));
}

type Fn = (...args: unknown[]) => unknown;

/** 견본 묶음마다 부른 출력. 인자가 없는 함수는 한 번 부른다. */
function outputs(fn: Fn, path: string): string[] {
  return (SAMPLES[path] ?? [[]]).map((args) => markup(fn(...args)));
}

const LEAVES = leaves(dictionary("ko"), dictionary("en"));
const FUNCTIONS = LEAVES.filter((leaf) => typeof leaf.ko === "function");

describe("화면 문구 사전", () => {
  it("두 사전은 같은 자리에 같은 종류의 값을 둔다", () => {
    const mismatched = LEAVES.filter((leaf) => typeof leaf.ko !== typeof leaf.en).map((leaf) => leaf.path);
    expect(mismatched).toEqual([]);
  });

  it("인자를 받는 함수에는 견본이 있고, 견본과 선택 인자 표는 사전의 함수만 가리킨다", () => {
    const missing = FUNCTIONS.filter(({ path, ko }) => (ko as Fn).length > 0 && !SAMPLES[path]).map((leaf) => leaf.path);
    expect(missing).toEqual([]);
    const functionPaths = new Set(FUNCTIONS.map((leaf) => leaf.path));
    const stale = [...Object.keys(SAMPLES), ...Object.keys(SELECTORS)].filter((path) => !functionPaths.has(path));
    expect(stale).toEqual([]);
  });

  it("영어 값에는 한글이 없다", () => {
    const korean = LEAVES.filter(({ path, en }) => (typeof en === "function" ? outputs(en as Fn, path) : [markup(en)]).some((out) => HANGUL.test(out))).map(
      (leaf) => leaf.path,
    );
    expect(korean).toEqual([]);
  });

  it("두 언어의 함수는 매개변수 수가 같고 견본 인자를 모두 적는다", () => {
    const problems: string[] = [];
    for (const { path, ko, en } of FUNCTIONS) {
      const koFn = ko as Fn;
      const enFn = en as Fn;
      if (koFn.length !== enFn.length) problems.push(`${path}: 매개변수 ${koFn.length}개와 ${enFn.length}개`);
      const first = [outputs(koFn, path)[0], outputs(enFn, path)[0]];
      (SAMPLES[path]?.[0] ?? []).forEach((arg, i) => {
        if (SELECTORS[path]?.includes(i)) return;
        const shown = isValidElement(arg) ? markup(arg) : typeof arg === "string" || typeof arg === "number" ? String(arg) : null;
        if (shown !== null && first.some((out) => !out.includes(shown))) problems.push(`${path}: ${i}번째 인자를 적지 않는다`);
      });
    }
    expect(problems).toEqual([]);
  });

  it("영어 삭제 제목은 동사 뒤의 hub item 을 소문자로 적고 다른 종류 이름은 그대로다 (7절)", () => {
    expect(deleteTitle(dictionary("en"), "hub")).toBe("Delete hub item");
    expect(deleteTitle(dictionary("en"), "backend-api", "Unpublish")).toBe("Unpublish Backend API");
    expect(deleteTitle(dictionary("ko"), "hub")).toBe("Hub item 삭제");
  });

  it("사전 밖에서 한글을 가진 화면 파일은 늘 남는 목록뿐이다", () => {
    const src = join(import.meta.dirname, "..");
    const withHangul = readdirSync(src, { recursive: true })
      .map(String)
      .filter((path) => isScreenSource(path) && !isKoreanDictionary(path))
      .filter((path) => hangulFragments(readFileSync(join(src, path), "utf8"), path).length > 0)
      .sort();
    expect(withHangul).toEqual([...PENDING].sort());
  });

  it("사전 밖에서 영어 화면 글자를 가진 파일은 없다 (P73 2절)", () => {
    const src = join(import.meta.dirname, "..");
    // 삭제 대화상자와 참조 안내의 resource 는 글자가 아니라 리소스 종류 키다
    const kinds = new Set(Object.keys(dictionary("en").resources.kind));
    const withLatin = readdirSync(src, { recursive: true })
      .map(String)
      .filter((path) => isScreenSource(path) && !path.startsWith("i18n/"))
      .filter((path) => latinFragments(readFileSync(join(src, path), "utf8"), path, kinds).some((text) => !isLatinData(text)))
      .sort();
    expect(withLatin).toEqual([]);
  });

  it("데이터 규칙은 키·경로 꼴과 데이터 목록만 통과시킨다", () => {
    for (const text of ["Loading...", "Yes/No", "agent", "replicas", "{} failed"]) expect(isLatinData(text), text).toBe(false);
    for (const text of ["top_k", "https://x", "metadata.{}", "platform-admin"]) expect(isLatinData(text), text).toBe(true);
    const kinds = new Set(["agent"]);
    expect(latinFragments('export const A = () => <D resource="agent" />;', "a.tsx", kinds)).toEqual([]);
    expect(latinFragments('export const A = () => <p title="agent">agent</p>;', "a.tsx", kinds)).toEqual(["agent", "agent"]);
  });

  it("영어 글자 검사는 JSX 글자와 글자 속성만 본다", () => {
    const source = 'const a = "code_name";\nexport const X = () => <p title="Open" className="p-2" data-testid="x">replicas {n}<b>2</b></p>;';
    expect(latinFragments(source, "x.tsx")).toEqual(["replicas {}{}", "Open"]);
    const branches = 'export const Y = () => <R resource="MCP Server" title={on ? "Shown" : x ?? "Hidden"} label={name}>{"Raw text"}{ok ? "Yes" : <i />}</R>;';
    expect(latinFragments(branches, "y.tsx").sort()).toEqual(["Hidden", "MCP Server", "Raw text", "Shown", "Yes"].sort());
  });
});
