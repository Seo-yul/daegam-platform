/**
 * ``` 코드 펜스 나누기. 마크다운 지연 청크(components/markdown/MarkdownBody)가 오기 전의 대체 화면이 쓴다(D61). 줄바꿈과 코드
 * 블록만 다루므로 청크가 와서 마크다운으로 바뀌어도 글의 자리가 크게 흔들리지 않는다.
 */

export interface TextSegment {
  kind: "text" | "code";
  text: string;
  lang?: string;
}

/**
 * ``` 코드 펜스만 코드 블록으로 나눈다. 닫히지 않은 코드 펜스(스트리밍 중)는 CommonMark 처럼 끝까지 코드다. 빈 글 조각은 버리고,
 * 아무것도 없으면 빈 text 조각 하나다.
 */
export function splitCodeFences(text: string): TextSegment[] {
  const out: TextSegment[] = [];
  let code: string[] | null = null;
  let lang: string | undefined;
  let buf: string[] = [];
  const flushText = () => {
    const t = buf.join("\n");
    if (t) out.push({ kind: "text", text: t });
    buf = [];
  };
  for (const line of text.split("\n")) {
    const fence = /^\s*```([\w+-]*)\s*$/.exec(line);
    if (fence) {
      if (code === null) {
        flushText();
        code = [];
        lang = fence[1] || undefined;
      } else {
        out.push({ kind: "code", text: code.join("\n"), lang });
        code = null;
        lang = undefined;
      }
      continue;
    }
    if (code !== null) code.push(line);
    else buf.push(line);
  }
  if (code !== null) out.push({ kind: "code", text: code.join("\n"), lang });
  else flushText();
  if (!out.length) out.push({ kind: "text", text: "" });
  return out;
}
