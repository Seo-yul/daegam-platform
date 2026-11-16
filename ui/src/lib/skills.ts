/**
 * Skill 화면의 규칙 (D64). 형식 검사는 서버 한 곳(`services/skill_format.py`)이 하고 화면은 그 결과를 그린다.
 *
 * 그래서 여기에는 경로·머리말 규칙을 다시 적지 않는다. 편집기가 쓰는 SKILL.md 틀과 머리말 `name` 한 줄 다루기, 검사 결과와
 * 리비전 차이를 화면 모양으로 옮기는 순수 함수만 둔다. 숫자 상한은 문구에 쓰는 것만 서버 값을 옮겨 두고(`SKILL_LIMITS`, 사전도
 * 읽으므로 `lib/limits.ts` 에 있다), 서버 값과 같은지는 `platform/tests/test_skill_ui_parity.py` 가 본다. 화면 문장은 사전의
 * `skills` 다.
 */
import { ApiError } from "@/api/client";
import type { HubAction, SkillFileMeta, SkillIssue } from "@/api/types";
import type { Messages } from "@/i18n";
import { errorText } from "@/lib/errorText";
import { isMarkdownPath } from "@/lib/files";
import { SKILL_LIMITS } from "@/lib/limits";
import { MIB, ownValue } from "@/lib/utils";

export { SKILL_LIMITS };

export const SKILL_MD = "SKILL.md";

/** 가져오기 본문 상한. 넘는 파일은 올린 뒤 413 을 받는 대신 고른 자리에서 되돌린다. */
export const SKILL_IMPORT_MAX_BYTES = SKILL_LIMITS.importMib * MIB;
/** 가져오기가 받는 파일. zip 이나 SKILL.md 하나다. */
export const SKILL_IMPORT_ACCEPT = ".zip,.md";
/** 입력을 멈추고 검사를 부르기까지 기다리는 시간이다. */
export const SKILL_VALIDATE_DELAY_MS = 500;
/** 리비전 탭이 보이는 digest 앞자리 수다. */
const DIGEST_SHOWN = 12;

/** deepagents·플랫폼과 같은 머리말 정규식이다(`^---\s*\n(.*?)\n---\s*\n`, DOTALL). 여는 줄·본문·닫는 줄을 따로 잡는다. */
const FRONTMATTER_RE = /^(---\s*\n)([\s\S]*?)(\n---\s*\n)/;
/** 머리말의 name 줄. 서버 `_NAME_LINE` 처럼 콜론 앞 공백을 받는다. */
const NAME_LINE_RE = /^name[ \t]*:[^\n]*$/m;
const QUOTED_RE = /^(["'])(.*)\1$/;
/**
 * 이름 규칙(D24)에 맞아도 YAML 이 글이 아닌 값으로 읽는 이름. 정수(`2024`, `0x1f`, `0b101`), 날짜(`2024-01-01`), YAML 1.1 의
 * 불리언과 null 이다. 숫자와 하이픈만으로 된 이름은 넉넉히 모두 따옴표를 친다. 서버 `renamed()` 처럼 필요할 때만 감싼다.
 */
const YAML_NOT_TEXT_RE = /^(?:[0-9-]+|0b[01]+|0x[0-9a-f]+|yes|no|true|false|on|off|null)$/;

/** 접근 정책 세 동작이 Skill 에서 뜻하는 것. 편집기와 미리 보기가 허브 문구 대신 이것을 쓴다. view 는 허브 항목과 같은 뜻이다. */
export function skillActionLabels(t: Messages): Record<HubAction, { title: string; hint: string }> {
  return { view: t.hub.access.actions.view, ...t.skills.access.actions };
}

/** 다른 테넌트를 넣을 수 있는 동작. manage 는 소유 테넌트 밖으로 열지 못한다(422 cross_tenant_manage_unsupported). */
export const SKILL_TENANT_ACTIONS: readonly HubAction[] = ["view", "consume"];

/** 머리말의 name 줄. YAML 이 글로 읽지 않는 이름은 따옴표로 감싼다. */
function nameLine(name: string): string {
  return `name: ${YAML_NOT_TEXT_RE.test(name) ? JSON.stringify(name) : name}`;
}

/** "직접 작성" 이 여는 SKILL.md 틀. 머리말 name 은 이름 칸이 채운다. 글은 만드는 순간의 화면 언어를 따른다(P49 5절). */
export function skillTemplate(t: Messages, name: string): string {
  const text = t.skills.template;
  return ["---", nameLine(name), `description: ${text.description}`, "---", `# ${text.heading}`, "", `1. ${text.step}`, ""].join("\n");
}

function frontmatter(content: string): RegExpExecArray | null {
  return FRONTMATTER_RE.exec(content);
}

/** 머리말의 name. 머리말이나 name 줄이 없으면 빈 문자열이다. 따옴표로 감싼 값은 벗긴다. */
export function frontmatterName(content: string): string {
  const block = frontmatter(content)?.[2] ?? "";
  const line = NAME_LINE_RE.exec(block)?.[0];
  if (!line) return "";
  const value = line.slice(line.indexOf(":") + 1).trim();
  return QUOTED_RE.exec(value)?.[2] ?? value;
}

/** 머리말의 name 줄을 바꾼다. name 줄이 없으면 머리말 첫 줄로 넣고, 머리말이 없으면 그대로 둔다(검사가 알린다). */
export function withFrontmatterName(content: string, name: string): string {
  const match = frontmatter(content);
  if (!match) return content;
  const [whole, open, block, close] = match;
  const line = nameLine(name);
  // 바꿀 글을 함수로 넘겨 이름 안의 `$&` 같은 글자가 치환 패턴으로 읽히지 않게 한다
  const next = NAME_LINE_RE.test(block) ? block.replace(NAME_LINE_RE, () => line) : `${line}\n${block}`;
  return `${open}${next}${close}${content.slice(whole.length)}`;
}

/** 미리 보기에 그릴 본문. 머리말은 개요 탭이 따로 보이므로 뗀다. */
export function skillBody(content: string): string {
  const match = frontmatter(content);
  return match ? content.slice(match[0].length) : content;
}

/** 편집기·차이 보기의 문법. 마크다운 밖은 문법 없는 글이다. */
export function fileLanguage(path: string): "markdown" | "plaintext" {
  return isMarkdownPath(path) ? "markdown" : "plaintext";
}

/** SKILL.md 를 맨 앞에 두고 나머지는 경로 순이다. 편집기·트리·차이 목록이 같은 차례를 쓴다. */
export function byPath(a: string, b: string): number {
  if (a === SKILL_MD || b === SKILL_MD) return a === SKILL_MD ? (b === SKILL_MD ? 0 : -1) : 1;
  return a.localeCompare(b);
}

/** 파일 트리 한 줄. 폴더 줄은 그 아래 파일보다 먼저 온다. */
export interface FileTreeRow {
  path: string;
  name: string;
  depth: number;
  dir: boolean;
}

export function fileTree(paths: readonly string[]): FileTreeRow[] {
  const rows: FileTreeRow[] = [];
  const seen = new Set<string>();
  for (const path of [...paths].sort(byPath)) {
    const parts = path.split("/");
    parts.slice(0, -1).forEach((name, depth) => {
      const dir = parts.slice(0, depth + 1).join("/");
      if (seen.has(dir)) return;
      seen.add(dir);
      rows.push({ path: dir, name, depth, dir: true });
    });
    rows.push({ path, name: parts[parts.length - 1], depth: parts.length - 1, dir: false });
  }
  return rows;
}

export type FileChange = "added" | "removed" | "changed";

/** 두 리비전 사이에 바뀐 파일. 같은 경로의 sha256 이 같으면 빠진다. */
export function revisionChanges(before: readonly SkillFileMeta[], after: readonly SkillFileMeta[]): { path: string; change: FileChange }[] {
  const old = new Map(before.map((f) => [f.path, f.sha256]));
  const now = new Map(after.map((f) => [f.path, f.sha256]));
  const paths = [...new Set([...old.keys(), ...now.keys()])].sort(byPath);
  return paths.flatMap((path) => {
    const a = old.get(path);
    const b = now.get(path);
    if (a === b) return [];
    return [{ path, change: a === undefined ? "added" : b === undefined ? "removed" : "changed" }];
  });
}

/** `sha256:<hex>` 의 앞 12자. */
export function shortDigest(digest: string): string {
  return digest.replace(/^sha256:/, "").slice(0, DIGEST_SHOWN);
}

// -- 검사 결과 ------------------------------------------------------------------------

/**
 * issue 한 줄의 화면 문장. 사전의 검사 표(`skills.issues`)에 없으면 공통 오류 표, 거기도 없으면 서버 원문이다. 서버가 내는 code 를
 * 표가 다 덮는지는 `src/test/error-text.test.ts` 가 본다.
 */
export function issueText(t: Messages, issue: SkillIssue): string {
  return ownValue(t.skills.issues, issue.code) ?? errorText(t, issue.code) ?? issue.message;
}

/**
 * 서버 message 를 문장 아래에 함께 보일 code. 이 둘의 message 만 "U+202E at line 3" 처럼 고칠 글자의 자리를 담는다. 나머지의
 * message 는 화면 문장과 같은 말을 영어로 되풀이할 뿐이라 보이지 않는다.
 */
const LOCATED_CODES: ReadonlySet<string> = new Set(["invisible_chars", "control_chars"]);

/** 화면 문장 아래에 보일 서버 원문. 고칠 자리를 담은 code 만 있고 나머지는 null 이다. */
export function issueLocation(issue: SkillIssue): string | null {
  return LOCATED_CODES.has(issue.code) ? issue.message : null;
}

/** 422 `invalid_skill` 이 실어 온 issue. 다른 오류면 null 이다. */
export function invalidSkillIssues(error: unknown): SkillIssue[] | null {
  if (!(error instanceof ApiError) || error.code !== "invalid_skill") return null;
  return error.extra<SkillIssue[]>("issues") ?? [];
}

export function errorsIn(issues: readonly SkillIssue[]): SkillIssue[] {
  return issues.filter((i) => i.severity === "error");
}
