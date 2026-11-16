/**
 * 스펙 문서를 읽기 좋게 들여쓴다. Backend API 편집 화면의 "등록된 문서" 가 쓴다.
 *
 * 올린 그대로를 보관하므로 한 줄로 밀어 넣은 JSON 이나 줄바꿈 없는 WSDL 이 그대로 저장돼 있다.
 * 그것을 편집기에 그대로 띄우면 가로로 끝없이 흐르는 한 줄이라 읽을 수가 없다.
 *
 * **보기만 바꾼다.** 저장된 본문은 건드리지 않는다. 스펙 본문은 sha256 으로 같은지를 가리므로
 * (`source_sha256`) 들여쓰기만 달라져도 다른 문서가 된다. 그래서 "편집기로 가져오기" 는 이 결과가
 * 아니라 원본을 넣는다.
 *
 * 못 알아보는 것은 `null` 이다. 호출부는 그때 원문을 그대로 보인다. 억지로 고쳐 보이면 사람이
 * 저장된 것이 그런 줄 안다.
 */
import { parse as parseYaml } from "yaml";

import { toYaml } from "@/lib/yaml";

/** XML 을 태그 깊이로 들여쓴다. 이미 줄이 나뉘어 있어도 다시 고르게 맞춘다. */
function prettyXml(text: string): string | null {
  const flat = text.replace(/>\s+</g, "><").trim();
  if (!flat.startsWith("<")) return null;
  const parts = flat.replace(/></g, ">\n<").split("\n");
  const out: string[] = [];
  let depth = 0;
  for (const raw of parts) {
    const line = raw.trim();
    if (!line) continue;
    // 닫는 태그는 제 줄의 깊이를 먼저 한 칸 줄인다
    if (/^<\//.test(line)) depth = Math.max(0, depth - 1);
    out.push("  ".repeat(depth) + line);
    const opens = /^<[^!?/]/.test(line);
    const selfCloses = /\/>$/.test(line) || /^<[^>]*>.*<\/[^>]+>$/.test(line);
    if (opens && !selfCloses) depth += 1;
  }
  return out.join("\n");
}

/**
 * 읽기 좋게 들여쓴 판. 못 알아보면 null 이다.
 *
 * 차례는 JSON, XML, YAML 이다. YAML 파서는 평범한 글자 하나도 "문자열 한 개" 로 받아 주므로
 * 맨 뒤에 두고, 결과가 객체나 배열일 때만 쓴다. 그러지 않으면 아무 글이나 통과한다.
 */
export function prettyDoc(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;

  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return null;
    }
  }

  if (trimmed.startsWith("<")) return prettyXml(trimmed);

  try {
    const parsed: unknown = parseYaml(trimmed);
    if (parsed === null || typeof parsed !== "object") return null;
    return toYaml(parsed);
  } catch {
    return null;
  }
}
