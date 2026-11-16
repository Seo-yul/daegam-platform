/**
 * 화면이 사전을 읽는 길 (D67, docs/guide/ui.md 3절 "Where text lives"). 글자를 그리는 컴포넌트는 `useT()`, 컴포넌트 밖의
 * 도우미는 인자로 받은 사전, 핸들러 안의 한순간은 `messages()` 로 지금 언어의 사전을 읽는다. 사전 밖에서 한 언어의 사전 모듈을
 * 값으로 가져오거나 `dictionary()` 로 언어를 정해 읽으면, 또는 모듈 맨 위에서 `messages()` 를 불러 남기면 언어를 바꿔도 그
 * 글자가 따라 바뀌지 않는다. 화면마다 영어로 그려 보는 대신 이 검사가 그 길을 막는다.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, posix } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { isScreenSource } from "./hangul";

/** 한 언어의 사전 모듈. 경로는 ui/src 에서 본 상대 경로다. */
const LANGUAGE_MODULE = /^i18n\/(ko|en)(\/|$)/;

/** `useT`, `messages`, `dictionary` 를 내보내는 모듈. */
const I18N_INDEX = /^i18n(\/index)?$/;

/** 가져오는 경로를 ui/src 에서 본 상대 경로로. 패키지 이름이면 null 이다. */
function modulePath(specifier: string, file: string): string | null {
  if (specifier.startsWith("@/")) return specifier.slice(2);
  if (specifier.startsWith(".")) return posix.join(posix.dirname(file), specifier);
  return null;
}

/** 타입만 가져오는지. 타입은 실행 중에 값을 읽지 않으므로 사전 모듈에서 가져와도 된다. */
function typeOnly(node: ts.ImportDeclaration | ts.ExportDeclaration): boolean {
  if (ts.isExportDeclaration(node)) {
    const clause = node.exportClause;
    return node.isTypeOnly || (clause !== undefined && ts.isNamedExports(clause) && clause.elements.length > 0 && clause.elements.every((e) => e.isTypeOnly));
  }
  const clause = node.importClause;
  if (!clause) return false;
  if (clause.isTypeOnly) return true;
  const bindings = clause.namedBindings;
  return !clause.name && bindings !== undefined && ts.isNamedImports(bindings) && bindings.elements.length > 0 && bindings.elements.every((e) => e.isTypeOnly);
}

/** i18n 의 색인에서 가져온 이름 가운데 원래 이름이 `name` 인 것들의 이 파일 안 이름. */
function localNames(node: ts.ImportDeclaration, name: string): string[] {
  const bindings = node.importClause?.namedBindings;
  if (!bindings || !ts.isNamedImports(bindings)) return [];
  return bindings.elements.filter((e) => !e.isTypeOnly && (e.propertyName ?? e.name).text === name).map((e) => e.name.text);
}

/**
 * 파일이 사전을 읽는 길 가운데 규칙을 어기는 것. 사전 밖(i18n/ 밖)의 파일이 한 언어의 사전 모듈을 값으로 가져오거나 다시
 * 내보내는 것, `dictionary` 를 가져오는 것, 그리고 어느 파일이든 함수 밖(모듈 맨 위)에서 `messages()` 를 부르는 것이다.
 * 경로는 ui/src 에서 본 상대 경로다.
 */
function dictionaryAccessProblems(source: string, file: string): string[] {
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, kind);
  const insideI18n = file.startsWith("i18n/");
  const problems: string[] = [];
  const where = (node: ts.Node) => `${file}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`;
  const messageNames = new Set(insideI18n ? ["messages"] : []);

  for (const statement of sf.statements) {
    if (!(ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement)) || !statement.moduleSpecifier) continue;
    if (!ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const path = modulePath(statement.moduleSpecifier.text, file);
    if (path === null) continue;
    if (!insideI18n && LANGUAGE_MODULE.test(path) && !typeOnly(statement)) problems.push(`${where(statement)} 한 언어의 사전을 값으로 가져온다`);
    if (ts.isImportDeclaration(statement) && I18N_INDEX.test(path)) {
      if (!insideI18n && localNames(statement, "dictionary").length) problems.push(`${where(statement)} dictionary 를 가져온다`);
      localNames(statement, "messages").forEach((name) => messageNames.add(name));
    }
  }

  const visit = (node: ts.Node, inFunction: boolean): void => {
    if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (callee.kind === ts.SyntaxKind.ImportKeyword && !insideI18n) {
        const [arg] = node.arguments;
        const path = arg && ts.isStringLiteralLike(arg) ? modulePath(arg.text, file) : null;
        if (path !== null && LANGUAGE_MODULE.test(path)) problems.push(`${where(node)} 한 언어의 사전을 값으로 가져온다`);
      }
      if (!inFunction && ts.isIdentifier(callee) && messageNames.has(callee.text)) problems.push(`${where(node)} 모듈 맨 위에서 messages() 를 부른다`);
    }
    ts.forEachChild(node, (child) => visit(child, inFunction || ts.isFunctionLike(node)));
  };
  visit(sf, false);
  return problems;
}

describe("화면이 사전을 읽는 길", () => {
  it("사전 밖의 화면 파일은 한 언어의 사전을 값으로 가져오지 않고, 모듈 맨 위에서 messages() 를 부르지 않는다", () => {
    const src = join(import.meta.dirname, "..");
    const problems = readdirSync(src, { recursive: true })
      .map(String)
      .filter(isScreenSource)
      .flatMap((path) => dictionaryAccessProblems(readFileSync(join(src, path), "utf8"), path));
    expect(problems).toEqual([]);
  });

  it("값 가져오기, dictionary, 모듈 맨 위의 messages() 를 잡고 타입 가져오기와 함수 안의 messages() 는 둔다", () => {
    const bad = [
      'import { ko } from "@/i18n/ko";',
      'import { en as english } from "../../i18n/en/index";',
      'export { ko } from "@/i18n/ko";',
      'import { dictionary } from "@/i18n";',
      'import { messages as m } from "@/i18n";',
      "const LABEL = m().ui.close;",
      'export const later = () => import("@/i18n/en");',
    ].join("\n");
    expect(dictionaryAccessProblems(bad, "pages/x/Page.tsx")).toEqual([
      "pages/x/Page.tsx:1 한 언어의 사전을 값으로 가져온다",
      "pages/x/Page.tsx:2 한 언어의 사전을 값으로 가져온다",
      "pages/x/Page.tsx:3 한 언어의 사전을 값으로 가져온다",
      "pages/x/Page.tsx:4 dictionary 를 가져온다",
      "pages/x/Page.tsx:6 모듈 맨 위에서 messages() 를 부른다",
      "pages/x/Page.tsx:7 한 언어의 사전을 값으로 가져온다",
    ]);
    const good = [
      'import type { HiddenCause } from "@/i18n/ko/warnings";',
      'import { type PausedSources } from "@/i18n/ko/warnings";',
      'import { messages, useT } from "@/i18n";',
      "export function confirmText(): string {",
      "  return messages().ui.unsavedGuard.confirm;",
      "}",
      "export const onClick = () => window.confirm(messages().ui.close);",
    ].join("\n");
    expect(dictionaryAccessProblems(good, "lib/x.ts")).toEqual([]);
    // 사전 안에서는 언어별 모듈을 서로 가져온다
    expect(dictionaryAccessProblems('import { ko } from "./ko";', "i18n/index.ts")).toEqual([]);
  });
});
