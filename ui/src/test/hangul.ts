/**
 * 소스에서 화면 글자가 될 수 있는 한글 문장을 뽑는다 (P49 8절). 문자열 리터럴, 템플릿 리터럴, JSX 요소의 글자와 한국어를
 * 맞춰 보는 정규식만 보고 주석은 보지 않는다. 템플릿의 `${…}`, JSX 의 `{…}` 와 안쪽 요소는 모두 `{}` 로 적어 한 문장으로
 * 잇는다. 그래서 JSX 문장을 템플릿 함수로 옮기면 같은 문장이 나오고, 자리 옆의 띄어쓰기가 바뀌면(`{} 부터` 와 `{}부터`)
 * 다른 문장이 된다.
 * JSX 글자는 React 처럼 줄을 다듬고, 문장의 공백은 하나로 줄이고 앞뒤를 다듬는다.
 *
 * ui/scripts/korean-moved.mjs 가 node 로 바로 읽으므로 이 파일은 지울 수 있는 타입 문법만 쓰고 `@/` 경로를 쓰지 않는다.
 */
import ts from "typescript";

/** 한글 음절과 자모. */
export const HANGUL = /[ㄱ-ㆎ가-힣]/;

/** 문장 안의 값 자리. */
const SLOT = "{}";

/** 화면 소스 파일인지. 테스트와 선언 파일은 화면에 나오지 않으므로 뺀다. 경로는 ui/src 에서 본 상대 경로다. */
export function isScreenSource(path: string): boolean {
  return /\.tsx?$/.test(path) && !path.endsWith(".d.ts") && !path.startsWith("test/") && !/\.test\.tsx?$/.test(path);
}

/** 한국어 사전 파일인지. 경로는 ui/src 에서 본 상대 경로다. */
export function isKoreanDictionary(path: string): boolean {
  return path.startsWith("i18n/ko/");
}

/** JSX 글자를 React 가 그리는 꼴로 다듬는다. 줄마다 안쪽 끝의 공백을 지우고, 빈 줄을 빼고, 줄 사이를 공백 하나로 잇는다. */
function jsxText(raw: string): string {
  const lines = raw.replace(/\t/g, " ").split(/\r\n|\n|\r/);
  const lastFilled = lines.reduce((last, line, i) => (/[^ ]/.test(line) ? i : last), -1);
  return lines
    .map((line, i) => {
      let text = line;
      if (i > 0) text = text.replace(/^ +/, "");
      if (i < lines.length - 1) text = text.replace(/ +$/, "");
      return text && i < lastFilled ? `${text} ` : text;
    })
    .join("");
}

/** JSX 요소의 자식들을 한 문장으로. 값과 안쪽 요소는 자리 하나다. 주석만 든 `{}` 는 자리가 아니다. */
function jsxRun(children: ts.NodeArray<ts.JsxChild>): string {
  return children
    .map((child) => {
      if (ts.isJsxText(child)) return jsxText(child.text);
      if (ts.isJsxExpression(child)) return child.expression ? SLOT : "";
      return SLOT;
    })
    .join("");
}

export function hangulFragments(source: string, fileName: string): string[] {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, false, kind);
  const found: string[] = [];
  const add = (text: string) => {
    const sentence = text.replace(/\s+/g, " ").trim();
    if (HANGUL.test(sentence)) found.push(sentence);
  };
  const visit = (node: ts.Node): void => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isRegularExpressionLiteral(node)) {
      add(node.text);
    } else if (ts.isTemplateExpression(node)) {
      add(node.head.text + node.templateSpans.map((span) => SLOT + span.literal.text).join(""));
    } else if (ts.isJsxElement(node) || ts.isJsxFragment(node)) {
      add(jsxRun(node.children));
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

/** 라틴 글자 두 자 이상. 화면 글자가 영어로 적혔는지 본다 (P73 2절). */
export const LATIN = /[A-Za-z]{2}/;

/**
 * 화면에 글자로 나오는 JSX 속성. 다른 속성(className, data-testid, href 따위)은 글자가 아니다. `resource` 는 상세의
 * 없는 리소스 화면과 삭제 대화상자가 종류 이름으로 그린다.
 */
const TEXT_ATTRIBUTES: ReadonlySet<string> = new Set(["title", "aria-label", "placeholder", "alt", "label", "resource"]);

/** 식에서 화면에 그대로 나올 문자열. 문자열 리터럴과 조건식·`??`·`||` 의 문자열 갈래다. 나머지 식은 보지 않는다. */
function literalBranches(node: ts.Expression): string[] {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [node.text];
  if (ts.isParenthesizedExpression(node)) return literalBranches(node.expression);
  if (ts.isConditionalExpression(node)) return [...literalBranches(node.whenTrue), ...literalBranches(node.whenFalse)];
  if (ts.isBinaryExpression(node) && (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken || node.operatorToken.kind === ts.SyntaxKind.BarBarToken)) {
    return literalBranches(node.right);
  }
  return [];
}

/**
 * 소스에서 화면 글자가 될 수 있는 영어 문장을 뽑는다 (P73 2절). JSX 요소의 글자, 글자 속성(TEXT_ATTRIBUTES)의 값,
 * 자식 자리 `{…}` 의 문자열(literalBranches)을 본다. 일반 문자열 리터럴은 키·코드·경로가 섞여 있어 보지 않으므로
 * 변수나 함수를 거쳐 그려지는 글자는 잡지 못한다. 문장은 hangulFragments 와 같은 꼴(`{}` 자리)이다.
 * `resource` 속성의 값이 kinds 에 든 리소스 종류 키면(삭제 대화상자, 참조 안내) 글자가 아니므로 뺀다.
 */
export function latinFragments(source: string, fileName: string, kinds: ReadonlySet<string> = new Set()): string[] {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
  const found: string[] = [];
  const add = (text: string) => {
    const sentence = text.replace(/\s+/g, " ").trim();
    if (LATIN.test(sentence.replaceAll(SLOT, ""))) found.push(sentence);
  };
  const visit = (node: ts.Node): void => {
    if (ts.isJsxElement(node) || ts.isJsxFragment(node)) {
      add(jsxRun(node.children));
    } else if (ts.isJsxAttribute(node) && TEXT_ATTRIBUTES.has(node.name.getText(file)) && node.initializer) {
      const init = node.initializer;
      if (ts.isStringLiteral(init)) {
        if (!(node.name.getText(file) === "resource" && kinds.has(init.text))) add(init.text);
      }
      else if (ts.isJsxExpression(init) && init.expression) literalBranches(init.expression).forEach(add);
    } else if (ts.isJsxExpression(node) && node.expression && node.parent && (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))) {
      literalBranches(node.expression).forEach(add);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

/** 낱말이 아니라 데이터라 사전에 두지 않는 글자. 명령 이름, 단위, 역할 이름이다. */
export const latinData: readonly string[] = ["curl", "ms", "platform-admin"];

/** 키나 경로 꼴의 낱말: 밑줄 낀 이름(top_k), 대입(a=b), 주소(https://), 점 낀 이름(mcp.example.com), 경로(/x), 와일드카드(*.x). */
const DATA_WORD = /_\w|=\S|:\/\/|\w\.\w|^\/\w|\*\./;

/**
 * 영어 문장이 데이터뿐인지 (P73 2절). 영문자가 든 낱말마다 데이터 목록(latinData)에 있거나 키·경로 꼴(DATA_WORD)이어야
 * 한다. 낱말 안의 값 자리는 글자 하나로 보고(metadata.{}), 영문자 없는 낱말(·, /)은 보지 않는다.
 */
export function isLatinData(text: string): boolean {
  return text
    .split(/\s+/)
    .filter((word) => /[A-Za-z]/.test(word.replaceAll(SLOT, "")))
    .every((word) => latinData.includes(word) || DATA_WORD.test(word.replaceAll(SLOT, "x")));
}
