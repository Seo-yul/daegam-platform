/**
 * 답의 인용 번호 (P36 7절, D61). 런타임은 검색 결과마다 세션 안에서 이어지는 번호(`cite`)를 주고, 모델은 답의 문장 뒤에
 * `[3]`·`[3, 5]` 로 단다. 화면은 전사 전체의 도구 호출 카드가 든 출처(`citations`)로 번호 표를 만들고, remark 플러그인이
 * 마크다운 글 노드의 번호를 칩 요소(`sup[data-cite]`)로 바꾼다. 묶음 안의 번호가 하나라도 표에 없으면 그 묶음은 글자 그대로이고,
 * 코드 안의 글은 글 노드가 아니라서 바뀌지 않는다.
 *
 * 출처에는 본문이 없다. 발췌는 같은 호출의 `tool_result` 본문에서 `cite` 로 찾는다. 그 본문은 도구 가드레일의 가림을 지난
 * 것이라 가려진 글이 칩에서 되살아나지 않는다.
 *
 * 말풍선 아래 "출처" 줄은 지연 청크의 파서를 기다리지 않고 글에서 바로 번호를 모은다(`citedNumbers`). ``` 코드 펜스와 인라인
 * 코드는 빼고 보지만 들여쓰기 코드 블록은 가르지 않는다. 모델이 들여쓰기 코드에 번호를 쓰는 일은 드물고, 그래도 출처 줄에
 * 칩이 하나 더 설 뿐이다.
 */
import type { ComponentType } from "react";
import type { Parent, PhrasingContent, Root, RootContent, Text } from "mdast";

import type { ChatItem } from "@/components/chat/items";
import type { CitationItem } from "@/lib/chatStream";
import { splitCodeFences } from "@/lib/codeFences";
import { isRecord, parseJsonObject } from "@/lib/json";
import { clipChars } from "@/lib/utils";

/** 작은 창의 발췌 길이(코드 포인트). */
export const EXCERPT_MAX_CHARS = 300;
/** 칩 요소가 번호를 싣는 hast 속성. React 에서는 `data-cite` 다. */
export const CITE_PROPERTY = "dataCite";

/** `[3]`·`[3, 5]` 꼴의 번호 묶음. 숫자 사이는 쉼표와 공백뿐이다. */
const GROUP = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
/** 인라인 코드. 백틱 수가 같은 짝까지다. */
const INLINE_CODE = /(`+)[\s\S]*?\1/g;
/** 칩으로 바꾸지 않는 노드. 링크 안에 단추를 넣으면 링크와 단추가 겹친다. */
const OPAQUE_NODES: ReadonlySet<string> = new Set(["link", "linkReference"]);

/** 칩 하나가 보이는 출처. 발췌가 없으면 null 이다. */
export interface CitedSource extends CitationItem {
  excerpt: string | null;
}

/** 번호 → 출처. 같은 번호는 뒤 카드의 것이 이긴다. */
export type CitationTable = ReadonlyMap<number, CitedSource>;

/** 마크다운에 번호 칩을 끼울 때 주는 것. 표에 있는 번호만 칩이 되고, 칩은 부르는 쪽이 그린다. */
export interface CitationMarkdown {
  known: ReadonlySet<number>;
  Chip: ComponentType<{ n: number }>;
}

/** 전사 항목 전체의 도구 호출 카드로 번호 표를 만든다. */
export function citationTable(items: readonly ChatItem[]): CitationTable {
  const table = new Map<number, CitedSource>();
  for (const it of items) {
    if (it.kind !== "tool_call" || !it.citations?.length) continue;
    const excerpts = resultExcerpts(it.result?.content);
    for (const c of it.citations) table.set(c.n, { ...c, excerpt: excerpts.get(c.n) ?? null });
  }
  return table;
}

/** 검색 도구 본문 `{"results": [{cite, content}]}` 에서 번호마다 발췌를 꺼낸다. 다른 모양이면 비어 있다. */
function resultExcerpts(content: string | undefined): Map<number, string> {
  const out = new Map<number, string>();
  const results = parseJsonObject(content ?? "").value?.results;
  if (!Array.isArray(results)) return out;
  for (const r of results) {
    if (isRecord(r) && typeof r.cite === "number" && typeof r.content === "string") out.set(r.cite, clipChars(r.content, EXCERPT_MAX_CHARS));
  }
  return out;
}

/** 묶음의 번호들이다. 하나라도 표에 없으면 null 이라 묶음은 글자로 남는다. */
function groupNumbers(inner: string, known: ReadonlySet<number>): number[] | null {
  const numbers = inner.split(",").map((part) => Number(part.trim()));
  return numbers.every((n) => known.has(n)) ? numbers : null;
}

/** 글의 번호 묶음을 표에 있는 번호만 처음 나온 차례로 모은다. 같은 번호는 한 번이다. 코드는 빼고 본다. */
export function citedNumbers(text: string, known: ReadonlySet<number>): number[] {
  const seen = new Set<number>();
  for (const seg of splitCodeFences(text)) {
    if (seg.kind === "code") continue;
    for (const match of seg.text.replace(INLINE_CODE, "").matchAll(GROUP)) {
      for (const n of groupNumbers(match[1], known) ?? []) seen.add(n);
    }
  }
  return [...seen];
}

/** hast 속성으로 실려 온 칩의 번호다. 칩이 아니면 null 이다. */
export function citeNumber(value: unknown): number | null {
  const n = Number(value);
  return value != null && Number.isInteger(n) && n > 0 ? n : null;
}

/** 번호 하나의 칩 노드다. remark-rehype 가 `data.hName` 을 보고 글을 `sup[data-cite]` 요소로 감싼다. */
function chipNode(n: number): Text {
  const value = String(n);
  return { type: "text", value, data: { hName: "sup", hProperties: { [CITE_PROPERTY]: value } } };
}

/** 글 노드 하나를 글과 칩으로 나눈다. 바꿀 것이 없으면 그대로다. */
function splitText(node: Text, known: ReadonlySet<number>): PhrasingContent[] {
  const out: PhrasingContent[] = [];
  let last = 0;
  for (const match of node.value.matchAll(GROUP)) {
    const numbers = groupNumbers(match[1], known);
    if (!numbers) continue;
    if (match.index > last) out.push({ type: "text", value: node.value.slice(last, match.index) });
    out.push(...numbers.map(chipNode));
    last = match.index + match[0].length;
  }
  if (!out.length) return [node];
  if (last < node.value.length) out.push({ type: "text", value: node.value.slice(last) });
  return out;
}

function visit(parent: Parent, known: ReadonlySet<number>): void {
  const next: RootContent[] = [];
  for (const child of parent.children) {
    if (child.type === "text") next.push(...splitText(child, known));
    else {
      if ("children" in child && !OPAQUE_NODES.has(child.type)) visit(child, known);
      next.push(child);
    }
  }
  parent.children = next as Parent["children"];
}

/** remark 플러그인. 옵션은 표에 있는 번호다. */
export function remarkCitations(known: ReadonlySet<number>) {
  return (tree: Root) => visit(tree, known);
}
