#!/usr/bin/env node
// 한 단위가 한국어 문구를 사전으로 옮기기만 했는지 본다 (P49 8절). 커밋 전에 돌리고, 검토할 때 다시 돌린다.
//
//   node scripts/korean-moved.mjs <base> [head]    head 를 빼면 작업 트리와 견준다
//
// 화면 소스(ui/src 에서 테스트와 선언 파일을 뺀 것)의 문자열·템플릿·JSX 텍스트에서 한글 조각을 뽑아(src/test/hangul.ts)
// 사전 밖에서 사라진 조각을 한국어 사전(ui/src/i18n/ko/)에 새로 들어온 조각과 짝짓는다. 사전 밖에서 사라졌다가 다시
// 들어온 조각은 서로 지운다. 여러 번 나오던 같은 문구를 사전의 키 하나로 합친 조각은 따로 알리기만 한다. 사전 안에서
// 여러 키에 있던 같은 문구를 키 하나로 합친 조각(사전에서 사라졌지만 새 사전 어딘가에 남은 조각)도 그렇다. 짝이 없는
// 조각을 넷으로 나눠 적고, 하나라도 있으면 1 로 끝난다. 적힌 조각은 커밋 본문에 까닭을 적거나(같은 출력의 함수로 나눈
// 문장 등) 문구 검토의 대상이 된다.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";

import { hangulFragments, isKoreanDictionary, isScreenSource } from "../src/test/hangul.ts";

const [base, head] = process.argv.slice(2);
if (!base) {
  console.error("사용법: node scripts/korean-moved.mjs <base> [head]");
  process.exit(2);
}

const root = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
// 경로 인자는 실행 위치가 아니라 저장소 뿌리에서 본다
const git = (...args) => execFileSync("git", ["-C", root, ...args], { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
const SRC = "ui/src/";

function show(rev, path) {
  try {
    return execFileSync("git", ["-C", root, "show", `${rev}:${path}`], { encoding: "utf8", maxBuffer: 256 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] });
  } catch {
    return ""; // 그 판에 없는 파일
  }
}

function working(path) {
  const file = `${root}/${path}`;
  return existsSync(file) ? readFileSync(file, "utf8") : "";
}

/** 바뀐 화면 소스 경로(ui/src 에서 본 상대 경로). 작업 트리와 견줄 때는 추적하지 않는 새 파일도 넣는다. */
function changedSources() {
  // 이름 바꾸기를 찾으면 새 경로만 나와 옛 파일에서 사라진 한국어를 놓친다
  const paths = git("diff", "--name-only", "--no-renames", base, ...(head ? [head] : []), "--", SRC).split("\n");
  if (!head) paths.push(...git("ls-files", "--others", "--exclude-standard", "--", SRC).split("\n"));
  return [...new Set(paths.filter((p) => p.startsWith(SRC)).map((p) => p.slice(SRC.length)))].filter(isScreenSource);
}

const bump = (counts, key, by) => counts.set(key, (counts.get(key) ?? 0) + by);
const dictionary = new Map(); // 조각 -> 새 판 개수 - 옛 판 개수
const outside = new Map();
for (const path of changedSources()) {
  const counts = isKoreanDictionary(path) ? dictionary : outside;
  const full = SRC + path;
  for (const fragment of hangulFragments(show(base, full), path)) bump(counts, fragment, -1);
  for (const fragment of hangulFragments(head ? show(head, full) : working(full), path)) bump(counts, fragment, 1);
}

/** 새 판의 한국어 사전 전체에 있는 조각. 같은 문구를 키 하나로 합쳤는지 가린다. */
function dictionaryFragments() {
  const dir = "ui/src/i18n/ko/";
  const paths = head
    ? git("ls-tree", "-r", "--name-only", head, "--", dir).split("\n").filter(Boolean)
    : readdirSync(`${root}/${dir}`, { recursive: true }).map((p) => dir + String(p));
  return new Set(paths.filter((p) => /\.tsx?$/.test(p)).flatMap((p) => hangulFragments(head ? show(head, p) : working(p), p)));
}

const lost = []; // 사전 밖에서 사라졌는데 사전에 새로 들어오지 않았다
const addedOutside = []; // 사전 밖에 새로 생긴 한국어
for (const [fragment, n] of outside) {
  if (n > 0) {
    addedOutside.push([fragment, n]);
    continue;
  }
  const moved = Math.min(-n, Math.max(0, dictionary.get(fragment) ?? 0));
  if (moved) bump(dictionary, fragment, -moved);
  if (-n > moved) lost.push([fragment, -n - moved]);
}
const addedToDictionary = [...dictionary].filter(([, n]) => n > 0); // 옮긴 것이 아닌 새 한국어
const dropped = [...dictionary].filter(([, n]) => n < 0).map(([f, n]) => [f, -n]);
// 사전 어딘가에 이미 있으면 여러 번 나오던 문구를 키 하나로 합친 것이고, 없으면 바뀌었거나 사라진 한국어다. 사전에서
// 사라진 조각도 같다. 두 영역에 있던 같은 문구를 공용 키 하나로 옮기면 사전 안에서 개수만 준다
const inDictionary = lost.length || dropped.length ? dictionaryFragments() : new Set();
const merged = lost.filter(([fragment]) => inDictionary.has(fragment));
const changed = lost.filter(([fragment]) => !inDictionary.has(fragment));
const mergedInDictionary = dropped.filter(([fragment]) => inDictionary.has(fragment));
const droppedFromDictionary = dropped.filter(([fragment]) => !inDictionary.has(fragment));

const sections = [
  ["사전 밖에서 사라졌지만 사전에 없는 조각", changed],
  ["사전에 새로 들어왔지만 사전 밖에서 오지 않은 조각", addedToDictionary],
  ["사전 밖에 새로 생긴 조각", addedOutside],
  ["사전에서 사라진 조각", droppedFromDictionary],
];
let unmatched = 0;
for (const [title, rows] of sections) {
  if (!rows.length) continue;
  console.log(`== ${title} (${rows.length})`);
  for (const [fragment, n] of rows.sort(([a], [b]) => a.localeCompare(b))) console.log(`  ${n > 1 ? `${n}× ` : ""}${fragment}`);
  unmatched += rows.length;
}
for (const [title, rows] of [
  ["같은 문구를 사전의 키 하나로 합친 조각", merged],
  ["사전 안에서 같은 문구를 키 하나로 합친 조각", mergedInDictionary],
]) {
  if (!rows.length) continue;
  console.log(`-- ${title} (${rows.length}, 실패가 아니다)`);
  for (const [fragment, n] of rows.sort(([a], [b]) => a.localeCompare(b))) console.log(`  ${n > 1 ? `${n}× ` : ""}${fragment}`);
}
if (unmatched) process.exit(1);
console.log("한국어 조각이 모두 사전으로 옮겨졌다.");
