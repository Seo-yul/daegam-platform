/**
 * Monaco 글 편집기 (Skill 파일, D64). SKILL.md 와 곁 마크다운은 markdown 문법으로, 나머지는 문법 없는 글로 연다.
 * MonacoYaml 처럼 코어 API 와 쓰는 문법만 번들하고 이 모듈은 lazy import 된다. MonacoJson 은 제 문법을 등록한 뒤 이 편집기를
 * 그대로 쓴다.
 */
import Editor from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";
import "monaco-editor/languages/definitions/markdown/register";

import { monacoTheme, useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

import { setupMonaco } from "./monacoSetup";

setupMonaco();

export interface MonacoTextProps {
  value: string;
  onChange?: (next: string) => void;
  /** 등록된 Monaco 언어 id. 이 모듈은 markdown 을 등록하고 plaintext 는 늘 있다. */
  language: string;
  readOnly?: boolean;
  height?: string;
  testId?: string;
}

export default function MonacoText({ value, onChange, language, readOnly, height = "24rem", testId }: MonacoTextProps) {
  // 편집기만 흰 바탕으로 남으면 다크 화면에서 그 칸만 도려낸 것처럼 보인다
  const theme = useTheme();
  return (
    <div data-testid={testId} className={cn("overflow-hidden rounded-md border", readOnly ? "border-line" : "border-field-line")}>
      <Editor
        height={height}
        theme={monacoTheme(theme)}
        // 붙여넣은 글의 줄바꿈을 CRLF 로 내보내지 않게 한다 (MonacoYaml 과 같은 까닭)
        onMount={(editor) => editor.getModel()?.setEOL(monaco.editor.EndOfLineSequence.LF)}
        language={language}
        value={value}
        onChange={(v) => onChange?.(v ?? "")}
        options={{ readOnly, minimap: { enabled: false }, fontSize: 12, scrollBeyondLastLine: false, tabSize: 2, wordWrap: "on", lineNumbersMinChars: 3 }}
      />
    </div>
  );
}
