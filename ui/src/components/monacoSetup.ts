/**
 * Monaco 를 번들에서 띄우는 준비. lazy 로 들어오는 편집기 넷이 맨 위에서 한 번 부른다(MonacoJson 은 MonacoText 를 거친다).
 *
 * CDN 이 아니라 번들에서 로드한다(같은 origin, 오프라인 클러스터 고려). 워커도 우리가 만든 것을
 * 쓴다. 두 번 걸어도 되도록 이미 설정돼 있으면 넘어간다. 같은 네 줄이 편집기마다 적혀 있었다.
 */
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";

import { MONACO_LIGHT } from "@/lib/theme";

import { createWorker } from "./monacoWorker";

export function setupMonaco(): void {
  if (typeof window === "undefined" || window.MonacoEnvironment) return;
  window.MonacoEnvironment = { getWorker: () => createWorker() };
  loader.config({ monaco });
  // 기본 vs 테마는 커서 줄에 회색 테두리(#eeeeee)를 그려 빈 편집기에서 첫 줄이 덜 그린 상자처럼 보인다.
  // 라이트는 테두리를 없애고 옅은 먹 바탕으로만 커서 줄을 알린다. 값은 라이트 --ui-hover 와 같다.
  // Monaco 의 색은 CSS 변수를 읽지 못하므로 hex 로 옮겨 적는다 (P81 1절)
  monaco.editor.defineTheme(MONACO_LIGHT, {
    base: "vs",
    inherit: true,
    rules: [],
    colors: { "editor.lineHighlightBorder": "#00000000", "editor.lineHighlightBackground": "#1a19170b" },
  });
}
